# vscode: Ubuntu 26.04 development image that runs VS Code in web mode
# (`code serve-web`) as an unprivileged user with password-less sudo.
#
# Package policy: Ubuntu 26.04 archive first. Vendor sources are only used
# where the archive has nothing usable:
#   - VS Code               Microsoft .deb (not in the archive)
#   - uv, ruff              PyPI via pipx (not in the archive)
#   - pnpm                  npm registry (not in the archive)
#   - Gradle               gradle.org (archive ships 4.4.1, which can't run on JDK 25)
#   - Claude Code CLI       claude.ai native installer (not in the archive)
#   - Codex CLI             GitHub release binary, sha256-verified (not in the archive)
#
# Each language is its own section and can be skipped with a build arg, e.g.
#   docker build --build-arg INSTALL_JAVA=false ...
# VS Code extensions aren't in the image: the VS Code container installs the
# profile's list in the background while it runs (hardcode-sync-extensions),
# so switching a language brings its extensions without a rebuild.

# Ubuntu 26.04 (resolute), one dated build of it, by digest: Dependabot moves
# it to the next dated build once that is a week old (docs/development.md,
# "Dependencies").
FROM ubuntu:resolute-20261006@sha256:c098726dd9b9733f3e885f8de0ef375206a311b0a317eadc40d347b1115fb944

ARG USERNAME=dev
# Keep 1000: rootless docker runs as uid 1000 too, so files in /shared line up.
ARG USER_UID=1000
ARG USER_GID=1000

ARG INSTALL_BUILD_ESSENTIALS=true
ARG INSTALL_PYTHON=true
ARG INSTALL_CPP=true
ARG INSTALL_RUST=true
ARG INSTALL_GO=true
ARG INSTALL_PERL=true
ARG INSTALL_RUBY=true
ARG INSTALL_NODE=true
ARG INSTALL_JAVA=true
ARG INSTALL_ANDROID=true
ARG INSTALL_DOTNET=true
ARG INSTALL_DOCKER=true
ARG INSTALL_CLAUDE_CLI=true
ARG INSTALL_CODEX_CLI=true

# "latest" or a release such as 1.140.0. Only the initial install: serve-web
# downloads and updates its own server inside ~/.vscode-server at runtime.
ARG VSCODE_VERSION=latest
# "latest", a release such as 9.8.0, or "none" to skip.
ARG GRADLE_VERSION=latest
# "latest" or a release such as 0.162.1. Updating Codex means rebuilding the
# image: its own update check is switched off (see the entrypoint).
ARG CODEX_VERSION=latest

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    TZ=Etc/UTC

# Keep downloaded .debs in the BuildKit cache mount so rebuilds are quick.
RUN rm -f /etc/apt/apt.conf.d/docker-clean \
 && echo 'Binary::apt::APT::Keep-Downloaded-Packages "true";' > /etc/apt/apt.conf.d/keep-cache \
 && printf '#!/bin/sh\nset -e\napt-get update\nexec apt-get install -y --no-install-recommends "$@"\n' > /usr/local/sbin/apt-install \
 && chmod 0755 /usr/local/sbin/apt-install

# ---------------------------------------------------------------------------
# Base system
# ---------------------------------------------------------------------------
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-install \
      ca-certificates curl wget gnupg sudo locales tzdata tini \
      git git-lfs openssh-client gh \
      bash-completion less vim nano tmux file tree jq ripgrep fd-find shellcheck \
      unzip zip xz-utils zstd bzip2 rsync \
      procps psmisc htop lsof iproute2 iputils-ping bind9-dnsutils netcat-openbsd man-db

# ---------------------------------------------------------------------------
# Unprivileged user with password-less sudo
# ---------------------------------------------------------------------------
RUN if id -u ubuntu >/dev/null 2>&1; then userdel --remove ubuntu; fi \
 && groupadd --gid "${USER_GID}" "${USERNAME}" \
 && useradd --uid "${USER_UID}" --gid "${USER_GID}" --create-home --shell /bin/bash "${USERNAME}" \
 && echo "${USERNAME} ALL=(ALL:ALL) NOPASSWD: ALL" > "/etc/sudoers.d/${USERNAME}" \
 && echo 'Defaults env_keep += "http_proxy https_proxy no_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY"' > /etc/sudoers.d/proxy-env \
 && chmod 0440 "/etc/sudoers.d/${USERNAME}" /etc/sudoers.d/proxy-env \
 && install -d -o "${USERNAME}" -g "${USERNAME}" -m 2775 /shared

# ---------------------------------------------------------------------------
# Build essentials (compilers + common -dev headers used by native packages)
# ---------------------------------------------------------------------------
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    if [ "${INSTALL_BUILD_ESSENTIALS}" = "true" ]; then \
      apt-install \
        build-essential pkg-config autoconf automake libtool make gettext \
        libssl-dev zlib1g-dev libffi-dev libbz2-dev libreadline-dev libsqlite3-dev \
        liblzma-dev libncurses-dev libxml2-dev libyaml-dev; \
    fi

# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    if [ "${INSTALL_PYTHON}" = "true" ]; then \
      apt-install python3 python3-dev python3-pip python3-venv python-is-python3 pipx black \
      # uv and ruff aren't packaged for 26.04: install from PyPI into /opt/pipx.
      && PIPX_GLOBAL_HOME=/opt/pipx PIPX_GLOBAL_BIN_DIR=/usr/local/bin pipx install --global uv ruff; \
    fi

# ---------------------------------------------------------------------------
# C / C++
# ---------------------------------------------------------------------------
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    if [ "${INSTALL_CPP}" = "true" ]; then \
      apt-install \
        g++ gdb clang clangd clang-format clang-tidy lldb lld llvm \
        cmake ninja-build ccache valgrind cppcheck bear; \
    fi

# ---------------------------------------------------------------------------
# Rust (cargo installs and the registry cache live in ~/.cargo)
# ---------------------------------------------------------------------------
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    if [ "${INSTALL_RUST}" = "true" ]; then \
      apt-install rustc cargo rustfmt rust-clippy rust-src rust-gdb rust-lldb; \
    fi

# ---------------------------------------------------------------------------
# Go (GOPATH defaults to ~/go)
# ---------------------------------------------------------------------------
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    if [ "${INSTALL_GO}" = "true" ]; then \
      apt-install golang-go gopls delve golang-golang-x-tools; \
    fi

# ---------------------------------------------------------------------------
# Perl (CPAN modules go to ~/perl5 via local::lib, see ENV below)
# ---------------------------------------------------------------------------
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    if [ "${INSTALL_PERL}" = "true" ]; then \
      apt-install perl perl-doc cpanminus liblocal-lib-perl perltidy libperl-critic-perl; \
    fi

# ---------------------------------------------------------------------------
# Ruby (gems go to ~/.gem, see ENV below)
# ---------------------------------------------------------------------------
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    if [ "${INSTALL_RUBY}" = "true" ]; then \
      apt-install ruby-full ruby-dev ruby-bundler; \
    fi

# ---------------------------------------------------------------------------
# JavaScript / TypeScript (global npm installs go to ~/.local, see ENV below)
# ---------------------------------------------------------------------------
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    if [ "${INSTALL_NODE}" = "true" ]; then \
      apt-install nodejs npm node-typescript yarnpkg \
      && ln -s /usr/bin/yarnpkg /usr/local/bin/yarn \
      # pnpm isn't packaged for 26.04 (and the archive's corepack is too old for it).
      && npm install --global --prefix /usr/local pnpm; \
    fi

# ---------------------------------------------------------------------------
# Java (Maven from the archive; Gradle from gradle.org, see header)
# ---------------------------------------------------------------------------
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    if [ "${INSTALL_JAVA}" = "true" ]; then \
      apt-install default-jdk maven \
      && if [ "${GRADLE_VERSION}" != "none" ]; then \
           version="${GRADLE_VERSION}"; \
           if [ "${version}" = "latest" ]; then \
             version="$(curl -fsSL https://services.gradle.org/versions/current | jq -r .version)"; \
           fi; \
           zip="gradle-${version}-bin.zip"; \
           curl -fsSLo "/tmp/${zip}" "https://services.gradle.org/distributions/${zip}" \
           && echo "$(curl -fsSL "https://services.gradle.org/distributions/${zip}.sha256")  /tmp/${zip}" | sha256sum -c - \
           && unzip -q "/tmp/${zip}" -d /opt \
           && ln -s "/opt/gradle-${version}/bin/gradle" /usr/local/bin/gradle \
           && rm "/tmp/${zip}"; \
         fi; \
    fi

# ---------------------------------------------------------------------------
# Android: JDK 21 and sdkmanager (F-Droid's, which checks each package
# against its public log of the SDK's checksums). The SDK itself goes into
# the home, ~/Android/Sdk (ANDROID_HOME, below): the user accepts Google's
# licences with `sdkmanager --licenses`, and Android's Gradle plugin then
# fetches what a project needs.
# ---------------------------------------------------------------------------
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    if [ "${INSTALL_ANDROID}" = "true" ]; then \
      apt-install openjdk-21-jdk-headless sdkmanager \
      # JDK 21 is the default java, beside Java's 25 too: the Gradle 8 that
      # most Android projects' wrappers name can't run on 25. Tool by tool,
      # as the headless JDK has no GUI tools for update-java-alternatives.
      && jdk="/usr/lib/jvm/java-21-openjdk-$(dpkg --print-architecture)" \
      && for tool in "${jdk}"/bin/*; do \
           name="$(basename "${tool}")"; \
           if update-alternatives --list "${name}" 2>/dev/null | grep -qx "${tool}"; then \
             update-alternatives --set "${name}" "${tool}"; \
           fi; \
         done; \
    fi

# ---------------------------------------------------------------------------
# C# / .NET
# ---------------------------------------------------------------------------
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    if [ "${INSTALL_DOTNET}" = "true" ]; then \
      apt-install dotnet-sdk-10.0; \
    fi

# ---------------------------------------------------------------------------
# Docker CLI, buildx and compose (talks to the rootless docker sidecar over TLS)
# ---------------------------------------------------------------------------
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    if [ "${INSTALL_DOCKER}" = "true" ]; then \
      apt-install docker.io docker-buildx docker-compose-v2; \
    fi

# ---------------------------------------------------------------------------
# Codex CLI: OpenAI's release binary (static, musl), checked against the
# sha256 digest GitHub publishes for it
# ---------------------------------------------------------------------------
RUN if [ "${INSTALL_CODEX_CLI}" = "true" ]; then \
      case "$(uname -m)" in \
        x86_64) target=x86_64-unknown-linux-musl ;; \
        aarch64) target=aarch64-unknown-linux-musl ;; \
        *) echo "no Codex release for $(uname -m)" >&2; exit 1 ;; \
      esac \
      && if [ "${CODEX_VERSION}" = "latest" ]; then \
           release=latest; \
         else \
           release="tags/rust-v${CODEX_VERSION}"; \
         fi \
      && meta="$(curl -fsSL "https://api.github.com/repos/openai/codex/releases/${release}")" \
      && asset="codex-${target}.tar.gz" \
      && url="$(jq -r --arg name "${asset}" '.assets[] | select(.name == $name) | .browser_download_url' <<<"${meta}")" \
      && digest="$(jq -r --arg name "${asset}" '.assets[] | select(.name == $name) | .digest // ""' <<<"${meta}")" \
      && case "${digest}" in sha256:*) ;; *) echo "no sha256 digest for ${asset}" >&2; exit 1 ;; esac \
      && curl -fsSLo /tmp/codex.tar.gz "${url}" \
      && echo "${digest#sha256:}  /tmp/codex.tar.gz" | sha256sum -c - \
      && tar -xzf /tmp/codex.tar.gz -C /tmp \
      && install -m 0755 "/tmp/codex-${target}" /usr/local/bin/codex \
      && rm -f /tmp/codex.tar.gz "/tmp/codex-${target}" \
      && codex --version; \
    fi

# ---------------------------------------------------------------------------
# VS Code (.deb from Microsoft, checksum-verified)
# ---------------------------------------------------------------------------
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    case "$(dpkg --print-architecture)" in \
      amd64) platform=linux-deb-x64 ;; \
      arm64) platform=linux-deb-arm64 ;; \
      *) echo "unsupported architecture" >&2; exit 1 ;; \
    esac \
 && if [ "${VSCODE_VERSION}" = "latest" ]; then \
      api="https://update.code.visualstudio.com/api/update/${platform}/stable/latest"; \
    else \
      api="https://update.code.visualstudio.com/api/versions/${VSCODE_VERSION}/${platform}/stable"; \
    fi \
 && meta="$(curl -fsSL "${api}")" \
 && curl -fsSLo /tmp/code.deb "$(jq -r .url <<<"${meta}")" \
 && echo "$(jq -r .sha256hash <<<"${meta}")  /tmp/code.deb" | sha256sum -c - \
 # Don't let the .deb add packages.microsoft.com: it isn't on the allowlist.
 && echo "code code/add-microsoft-repo boolean false" | debconf-set-selections \
 && apt-get update \
 && apt-get install -y --no-install-recommends /tmp/code.deb \
 && rm /tmp/code.deb

# ---------------------------------------------------------------------------
# VS Code extensions are installed at runtime, from the profile's list
# ---------------------------------------------------------------------------
COPY --chmod=0755 verify-extensions.sh /usr/local/bin/hardcode-verify-extensions
COPY --chmod=0755 sync-extensions.sh /usr/local/bin/hardcode-sync-extensions
COPY --chmod=0755 ssh-proxy-config.sh /usr/local/bin/hardcode-ssh-proxy-config
COPY --chmod=0755 ssh-key.sh /usr/local/bin/hardcode-ssh-key

# ---------------------------------------------------------------------------
# Everything below is installed into the user's home. On first start Docker
# copies it into the (empty) home volume.
# ---------------------------------------------------------------------------
USER ${USERNAME}
WORKDIR /home/${USERNAME}

RUN if [ "${INSTALL_CLAUDE_CLI}" = "true" ]; then \
      curl -fsSL https://claude.ai/install.sh | bash; \
    fi

RUN ln -s /shared ~/shared

# SSH goes through squid too (only to hosts with an "ssh" allowlist entry).
# ssh refuses a group-writable config file, hence the explicit mode.
COPY --chmod=0644 ssh_config /etc/ssh/ssh_config.d/10-hardcode-proxy.conf
# Last, so that editing it doesn't rebuild the layers above.
COPY --chmod=0755 entrypoint.sh /usr/local/bin/hardcode-entrypoint

ENV PATH=/home/${USERNAME}/.local/bin:/home/${USERNAME}/.cargo/bin:/home/${USERNAME}/go/bin:/home/${USERNAME}/.gem/bin:/home/${USERNAME}/perl5/bin:/home/${USERNAME}/Android/Sdk/cmdline-tools/latest/bin:/home/${USERNAME}/Android/Sdk/platform-tools:${PATH} \
    ANDROID_HOME=/home/${USERNAME}/Android/Sdk \
    NPM_CONFIG_PREFIX=/home/${USERNAME}/.local \
    GEM_HOME=/home/${USERNAME}/.gem \
    PERL5LIB=/home/${USERNAME}/perl5/lib/perl5 \
    PERL_LOCAL_LIB_ROOT=/home/${USERNAME}/perl5 \
    PERL_MB_OPT="--install_base /home/${USERNAME}/perl5" \
    PERL_MM_OPT="INSTALL_BASE=/home/${USERNAME}/perl5" \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1

EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/hardcode-entrypoint"]

# The languages and tools this image was built with: `hardcode show` compares
# them with the profile's, to say when a rebuild is due. Last, so that a
# change of language doesn't throw away the cache of every layer above.
LABEL io.github.hardcode-app.languages="python=${INSTALL_PYTHON} node=${INSTALL_NODE} java=${INSTALL_JAVA} android=${INSTALL_ANDROID} dotnet=${INSTALL_DOTNET} go=${INSTALL_GO} rust=${INSTALL_RUST} cpp=${INSTALL_CPP} ruby=${INSTALL_RUBY} perl=${INSTALL_PERL} build_essentials=${INSTALL_BUILD_ESSENTIALS} docker=${INSTALL_DOCKER} claude_cli=${INSTALL_CLAUDE_CLI} codex_cli=${INSTALL_CODEX_CLI}"
