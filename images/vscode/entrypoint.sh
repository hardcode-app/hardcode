#!/usr/bin/env bash
# The VS Code container's entrypoint: a little runtime setup, then VS Code in web mode.
#
# Any arguments replace VS Code: `docker compose run --rm vscode bash`.
set -euo pipefail

log() {
  printf '[vscode] %s\n' "$*"
}

# ---------------------------------------------------------------------------
# Volumes
# ---------------------------------------------------------------------------

# A volume created by another container first (/shared, by docker) is root's.
# docker runs as uid 1000 as well, so owning it as us works for both.
for dir in "$HOME" /shared; do
  if [ -d "$dir" ] && [ ! -w "$dir" ]; then
    log "taking ownership of $dir"
    sudo chown "$(id -u):$(id -g)" "$dir"
    sudo chmod 2775 "$dir"
  fi
done

# A home that wasn't seeded from the image (a fresh Kubernetes volume) still
# gets the link.
[ -L "$HOME/shared" ] || [ -e "$HOME/shared" ] || ln -s /shared "$HOME/shared"

# With no Docker daemon beside us (ENABLE_DOCKER=false), `docker` should say
# so at once, not wait on a host that isn't there.
if [ "${ENABLE_DOCKER:-true}" != true ]; then
  unset DOCKER_HOST DOCKER_TLS_VERIFY DOCKER_CERT_PATH TESTCONTAINERS_HOST_OVERRIDE
fi

# ---------------------------------------------------------------------------
# The home: the Android SDK's folder, and an SSH key
# ---------------------------------------------------------------------------

# The Android SDK's folder in the home: sdkmanager refuses an ANDROID_HOME
# that doesn't exist, and a home made before Android was switched on has
# none.
if command -v sdkmanager >/dev/null && [ -n "${ANDROID_HOME:-}" ]; then
  mkdir -p "$ANDROID_HOME"
fi

# The sandbox's own SSH key, made once when the home has none, so that no
# key of yours needs copying in: its public half goes to a git host, best as
# one repository's deploy key. A failure here mustn't keep VS Code from
# starting.
if made="$(hardcode-ssh-key "$(id -un)@hardcode")"; then
  if [ -n "$made" ]; then
    log "made this sandbox's SSH key; its public half is $made"
  fi
else
  log "couldn't make an SSH key in ~/.ssh"
fi

# ---------------------------------------------------------------------------
# Trust the egress proxy's CA: squid inspects HTTPS. update-ca-certificates
# also refreshes the Java keystore, through ca-certificates-java.
# ---------------------------------------------------------------------------

if [ -s /proxy-ca/ca.pem ]; then
  sudo install -m 0644 /proxy-ca/ca.pem /usr/local/share/ca-certificates/hardcode-proxy-ca.crt
  sudo update-ca-certificates >/dev/null 2>&1
fi

# ---------------------------------------------------------------------------
# Proxy settings for the tools that ignore HTTP(S)_PROXY. Each file is only
# written when it is missing, so your own edits win.
# ---------------------------------------------------------------------------

write_maven_settings() {
  mkdir -p ~/.m2
  cat > ~/.m2/settings.xml <<MAVEN
<settings>
  <proxies>
    <proxy><id>http</id><active>true</active><protocol>http</protocol><host>${proxy_host}</host><port>${proxy_port}</port><nonProxyHosts>${java_non_proxy}</nonProxyHosts></proxy>
    <proxy><id>https</id><active>true</active><protocol>https</protocol><host>${proxy_host}</host><port>${proxy_port}</port><nonProxyHosts>${java_non_proxy}</nonProxyHosts></proxy>
  </proxies>
</settings>
MAVEN
}

write_gradle_properties() {
  mkdir -p ~/.gradle
  cat > ~/.gradle/gradle.properties <<GRADLE
systemProp.http.proxyHost=${proxy_host}
systemProp.http.proxyPort=${proxy_port}
systemProp.http.nonProxyHosts=${java_non_proxy}
systemProp.https.proxyHost=${proxy_host}
systemProp.https.proxyPort=${proxy_port}
systemProp.https.nonProxyHosts=${java_non_proxy}
GRADLE
}

if [ -n "${HTTPS_PROXY:-${HTTP_PROXY:-}}" ]; then
  proxy="${HTTPS_PROXY:-$HTTP_PROXY}"

  # http://user@host:port/ -> host and port.
  host_and_port="${proxy#*://}"
  host_and_port="${host_and_port%%/*}"
  host_and_port="${host_and_port##*@}"
  proxy_host="${host_and_port%%:*}"
  proxy_port="${host_and_port##*:}"

  no_proxy_list="${NO_PROXY:-localhost,127.0.0.1}"
  java_non_proxy="${no_proxy_list//,/|}"

  # apt runs as root through sudo, without our environment: tell it directly.
  printf 'Acquire::http::Proxy "%s";\nAcquire::https::Proxy "%s";\n' "$proxy" "$proxy" \
    | sudo tee /etc/apt/apt.conf.d/90hardcode-proxy >/dev/null

  # SSH goes through the proxy too (to hosts with an "ssh" entry only). The
  # proxy's name differs between Compose (squid) and Kubernetes
  # (<profile>-squid.<namespace>.svc...), so the image's default stanza is
  # rewritten here.
  hardcode-ssh-proxy-config "$proxy_host" "$proxy_port" \
    | sudo tee /etc/ssh/ssh_config.d/10-hardcode-proxy.conf >/dev/null

  # The docker CLI passes these to every container and build it starts.
  if command -v docker >/dev/null && [ ! -e ~/.docker/config.json ]; then
    mkdir -p ~/.docker
    jq -n --arg proxy "$proxy" --arg no_proxy "$no_proxy_list" \
      '{proxies: {default: {httpProxy: $proxy, httpsProxy: $proxy, noProxy: $no_proxy}}}' \
      > ~/.docker/config.json
  fi

  if command -v mvn >/dev/null && [ ! -e ~/.m2/settings.xml ]; then
    write_maven_settings
  fi

  if command -v java >/dev/null && [ ! -e ~/.gradle/gradle.properties ]; then
    write_gradle_properties
  fi
fi

# ---------------------------------------------------------------------------
# The profile's own Ubuntu packages, on Kubernetes. The image is shared
# there, not built per profile, so the operator hands the list over
# (HARDCODE_PACKAGES, space-separated) and they are installed at every
# start, through the proxy apt was just given (the Ubuntu archive is in
# every profile's allowlist): it costs start-up time. On Compose the
# profile's image has them, and the variable isn't set. A failure is
# logged, and VS Code starts without them.
# ---------------------------------------------------------------------------

read -ra wanted_packages <<<"${HARDCODE_PACKAGES:-}"
if [ "${#wanted_packages[@]}" -gt 0 ]; then
  log "installing the profile's Ubuntu packages: ${wanted_packages[*]}"

  if hardcode-install-packages "${wanted_packages[@]}"; then
    log "installed the profile's Ubuntu packages"
  else
    log "WARNING: couldn't install the profile's Ubuntu packages (see above); VS Code starts without them"
  fi
fi

# ---------------------------------------------------------------------------
# Codex CLI: a first configuration, only written when there is none.
#
# - Its own sandbox is bubblewrap, which needs user namespaces; this
#   container doesn't allow them (nor should it), so every command Codex ran
#   would fail. The container, its proxy and its network are the sandbox
#   here, as they are for Claude Code. Codex still asks before acting, as
#   its approval policy says.
# - The binary comes with the image (a rebuild updates it), and the hosts its
#   update check, metrics and crash reports go to aren't on the allowlist,
#   so those are off rather than failing in the background.
# ---------------------------------------------------------------------------

if command -v codex >/dev/null && [ ! -e ~/.codex/config.toml ]; then
  mkdir -p ~/.codex
  cat > ~/.codex/config.toml <<'CODEX'
# Written by HardCode at the VS Code container's first start; yours to edit.
# Codex comes with the image: rebuild it to update (CODEX_VERSION in .env).
check_for_update_on_startup = false

# Codex's own sandbox (bubblewrap) can't run in this container: it needs user
# namespaces. The container, its proxy and its network are the sandbox.
sandbox_mode = "danger-full-access"

[analytics]
enabled = false

[feedback]
enabled = false

[otel]
metrics_exporter = "none"
CODEX
fi

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

# ---------------------------------------------------------------------------
# VS Code extensions: those every VS Code container has, those of the
# profile's languages and tools, and its own. They install in the
# background, so VS Code comes up at once and they arrive as they're ready;
# the watcher stays, and follows the profile's list whenever it changes
# (hardcode-sync-extensions says how).
# ---------------------------------------------------------------------------

hardcode-sync-extensions --watch &

# ---------------------------------------------------------------------------
# VS Code (web). serve-web downloads the matching server into
# ~/.vscode-server/cli on first use, and keeps it up to date by itself.
# ---------------------------------------------------------------------------

token_file="$HOME/.vscode-server/connection-token"
mkdir -p "$(dirname "$token_file")"

# A token set for the profile wins; otherwise the one made at the first start stays.
if [ -n "${VSCODE_CONNECTION_TOKEN:-}" ]; then
  printf '%s' "$VSCODE_CONNECTION_TOKEN" > "$token_file"
elif [ ! -s "$token_file" ]; then
  tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32 > "$token_file" || true
fi
chmod 0600 "$token_file"

# The token is a credential: it never goes to the log, which `hardcode
# logs`, the app's logs dialog and `docker logs` all show. `hardcode open`
# (and the app) give the URL with it.
log "VS Code: http://localhost:${VSCODE_PUBLISHED_PORT:-8000} (the URL with its token: hardcode open)"

# VS Code prints the token too ("Web UI available at ...?tkn=..."): its
# output goes through a filter that hides it, line by line as it comes.
exec code serve-web \
  --host 0.0.0.0 \
  --port 8000 \
  --connection-token-file "$token_file" \
  --accept-server-license-terms \
  --disable-telemetry \
  --cli-data-dir "$HOME/.vscode-server/cli" \
  --default-folder "${VSCODE_DEFAULT_FOLDER:-$HOME}" \
  > >(sed -u -E 's/(tkn=)[^&[:space:]]+/\1(hidden)/g') 2>&1
