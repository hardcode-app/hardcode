# The VS Code image

`images/vscode/Dockerfile` builds the container VS Code runs in: Ubuntu 26.04,
an unprivileged user `dev` with password-less sudo, VS Code in web mode, and
one section per language that can be switched off. Each profile builds its
own `hardcode-vscode:<profile>` from its `.env`.

## Package policy

Ubuntu 26.04 archive packages first. Vendor sources only where the archive
has nothing usable:

| Vendor source | Why |
|---|---|
| VS Code (Microsoft .deb, checksum-verified; the .deb is stopped from adding packages.microsoft.com) | not in the archive |
| uv, ruff (PyPI via pipx into `/opt/pipx`) | not packaged |
| pnpm (npm registry) | not packaged; the archive's corepack is too old |
| Gradle (gradle.org, sha256-verified; `GRADLE_VERSION`) | the archive's 4.4.1 can't run on JDK 25 |
| Claude Code CLI (claude.ai native installer) | not in the archive |
| Codex CLI (GitHub release binary, checked against the sha256 digest GitHub publishes; `CODEX_VERSION`) | not in the archive |

## Languages

| `.env` key / build arg | Installs |
|---|---|
| `INSTALL_BUILD_ESSENTIALS` | build-essential, autotools, pkg-config, gettext, common `-dev` libraries (OpenSSL, zlib, libffi, sqlite, readline, …) |
| `INSTALL_PYTHON` | Python 3.14, pip, venv, pipx, black; uv and ruff |
| `INSTALL_CPP` | GCC 15, Clang 21 with clangd/clang-format/clang-tidy, LLDB, GDB, lld, CMake, Ninja, ccache, Valgrind, cppcheck, bear |
| `INSTALL_RUST` | rustc/cargo 1.93, rustfmt, clippy, rust-src, rust-gdb, rust-lldb |
| `INSTALL_GO` | Go 1.26, gopls, delve, x/tools |
| `INSTALL_PERL` | Perl 5.40, cpanminus, local::lib, perltidy, Perl::Critic |
| `INSTALL_RUBY` | Ruby 3.3, bundler |
| `INSTALL_NODE` | Node.js 22, npm, TypeScript (archive: 5.2), yarn; pnpm |
| `INSTALL_JAVA` | OpenJDK 25 (default-jdk), Maven; Gradle |
| `INSTALL_DOTNET` | .NET SDK 10 |
| `INSTALL_DOCKER` | Docker CLI, buildx, compose v2 (talk to the rootless daemon) |
| `INSTALL_CLAUDE_CLI` | `claude` in the terminal: Anthropic's coding agent. With it on, the profile also gets its VS Code extension and its allowlist block |
| `INSTALL_CODEX_CLI` | `codex` in the terminal: OpenAI's coding agent, beside Claude Code. With it on, the profile also gets its VS Code extension and its allowlist block |

These are a profile's languages (and the four tools), chosen first: each
also opens the language's [allowlist block](allowlist.md#language-blocks) and
brings its VS Code extensions. `hardcode enable`/`disable <lang>...` (or the
app's *Languages / Tools*) set them, change the allowlist blocks and the
extensions at once, and rebuild for the toolchain; only the layers after the changed section are rebuilt, and apt
downloads are kept in a BuildKit cache mount. The image carries the
languages it was built with in its `io.github.hardcode-app.languages` label, which
`hardcode languages` and the app compare with the profile's to say when a
rebuild is due.

Per-user tool directories are on the `PATH` and persist in the home volume:
`~/.local/bin` (pipx, npm globals via `NPM_CONFIG_PREFIX`), `~/.cargo/bin`,
`~/go/bin`, `~/.gem/bin` (`GEM_HOME`), `~/perl5/bin` (local::lib).

## VS Code

`code serve-web` runs as `dev` on port 8000, with the connection token from
`~/.vscode-server/connection-token` (generated on first start, or
`VSCODE_CONNECTION_TOKEN` from `.env`), telemetry off, and
`VSCODE_DEFAULT_FOLDER` (or the home directory) as the default folder. The
image provides the initial `code` binary (`VSCODE_VERSION`, `latest` by
default); **the server itself is downloaded into `~/.vscode-server` at first
use and updates itself there**, often mid-session, exactly as upstream
intends. Don't pin or relocate it; it lives in the home volume and survives
rebuilds. The Marketplace and update hosts are on the default allowlist.

### Extensions

The image has none. A profile's VS Code container installs them while it
runs, from the profile's **extension list**: the one every container has
(Red Hat YAML), those of the profile's languages and tools (the catalogue in
`cli/hardcode_cli/langs.py`; the coding agents' extensions come with their
tools: Claude Code with the Claude Code CLI, Codex with the Codex CLI), and the profile's own
(`VSCODE_EXTENSIONS` in its `.env`: `hardcode extension add|remove`, or the
app's *Extensions*). The CLI writes the list to
`vscode-extensions.txt` in the profile's folder whenever it changes, and Compose
mounts it read-only at `/etc/hardcode/extensions/wanted`; on Kubernetes the
operator puts it in a ConfigMap mounted at the same place.

- python: Python, Pylance, debugpy, Black, Ruff, Jupyter
- node: ESLint, Prettier · java: Extension Pack for Java
- dotnet: C# Dev Kit, C# · go: Go · rust: rust-analyzer, CodeLLDB
- cpp: C/C++, CMake Tools, Makefile Tools · ruby: Ruby LSP
- docker: Containers · claude_cli: Claude Code · codex_cli: Codex

The entrypoint starts `hardcode-sync-extensions --watch` in the background
and starts VS Code straight away: a first start is quick, and the extensions
arrive as they're installed. The watcher then follows the list: switch a
language on, or add an extension, and it starts installing within seconds,
with no restart (VS Code picks a new extension up; an open window may ask to
be reloaded). What it installed before and the list no longer has is
removed, the members of a pack and the dependencies with it; extensions you
installed or uninstalled yourself in VS Code are left alone. It reads a list
only once it ends with `# end`, so a list caught half-written is never taken
for a shorter one.

**Only publishers with a verified domain on the Marketplace are allowed.**
Each extension on the list is checked before it is installed, with
everything it pulls in (its dependencies and a pack's members, as the
Marketplace lists them for its latest release, and theirs in turn), by
asking the Marketplace (`hardcode-verify-extensions --with-dependencies`).
VS Code is running and picks up whatever lands in its extensions folder,
so if any publisher in that set has no verified domain, nothing of it gets
there: the extension is refused, and remembered so it isn't asked about
again. What arrives is checked once more, in case a package pulls in more
than the Marketplace listed; then the whole set is removed, the extension
included. One that couldn't be installed or checked (the Marketplace didn't
answer within 30 seconds) is removed and tried again a minute later. The reviewed exceptions are the
catalogue's `APPROVED_UNVERIFIED`, marked `unverified-ok` on the list: the
only one is CodeLLDB, the Rust debugger VS Code's own Rust documentation
recommends.

How far it has got is in `~/.vscode-server/.hardcode-extensions-status.json`
(`installing` or `done`, how many of how many are in place, and what is
pending, refused or to be tried again), which `hardcode extensions` and the
app show, and in the VS Code container's log, one line per step, which the
Kubernetes admin shows.

## The Codex CLI

On by default, beside Claude Code (`INSTALL_CODEX_CLI`; `hardcode disable
codex` leaves it out). Its allowlist block opens what it needs, all `rw`
since each is an API: `auth.openai.com` (sign-in and token refresh),
`chatgpt.com/backend-api` (the model, on a ChatGPT plan) and `api.openai.com`
(the model, with an API key). Its update check (`api.github.com`), metrics
(`ab.chatgpt.com`) and crash reports stay closed.

- **Signing in.** The browser sign-in calls back to `localhost:1455` inside
  the container, which your browser can't reach. Use `codex login
  --device-auth` instead (switch device code login on in your ChatGPT
  security settings first), or an API key: `printenv OPENAI_API_KEY | codex
  login --with-api-key`. Credentials stay in `~/.codex`, in the home volume.
- **The proxy's CA.** `CODEX_CA_CERTIFICATE` points at the system bundle,
  which has the proxy's CA: Codex reads it for its WebSocket connections
  too, which `SSL_CERT_FILE` doesn't cover. Squid doesn't pass WebSocket
  upgrades, so Codex falls back to HTTPS, as it is built to.
- **Its first configuration.** The entrypoint writes `~/.codex/config.toml`
  when there is none: the update check, analytics, feedback uploads and
  metrics off (the binary comes with the image: rebuild to update it, with
  `CODEX_VERSION` in `.env` to pin a release), and `sandbox_mode =
  "danger-full-access"`. Codex's own sandbox is bubblewrap, which needs user
  namespaces, and this container doesn't allow them: every command Codex
  ran would fail. The container, its proxy and its network are the sandbox
  here, as they are for Claude Code; Codex still asks before acting, as its
  approval policy says. The file is yours to edit afterwards.

## What the entrypoint does at start

1. Takes ownership of `$HOME` and `/shared` if a volume was created
   root-owned.
2. Installs the proxy CA from `/proxy-ca/ca.pem` into the system store
   (`update-ca-certificates`, which also refreshes the Java keystore).
   `SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS`, `REQUESTS_CA_BUNDLE` and `PIP_CERT`
   are set for tools that keep their own bundle.
3. Configures the proxy for tools that ignore `HTTP(S)_PROXY`: apt
   (`/etc/apt/apt.conf.d/90hardcode-proxy`), the Docker CLI
   (`~/.docker/config.json`, which also passes the proxy into every container
   and build it starts), Maven (`~/.m2/settings.xml`) and Gradle
   (`~/.gradle/gradle.properties`). Only written when missing, so your edits
   win. Codex's first configuration, the same way (see above).
4. Starts the extension watcher in the background, as above.
5. Starts VS Code. Any arguments replace it: `hardcode compose run --rm vscode bash`.

## Using Docker from the VS Code container

`docker`, `docker compose` and `docker buildx` talk to the rootless daemon in
`docker` over mutual TLS (`DOCKER_HOST=tcp://docker:2376`, certificates in
`/certs/client`).

- **Bind mounts** are resolved on the `docker` side, so only paths that exist
  there work: `/shared`, plus host folders mounted with `hardcode mount`
  (which go into both). `docker run -v ~/git/project:/src …` works; a path
  that exists only in the VS Code container doesn't.
- **File ownership**: `dev` (uid 1000) shows up as `root` inside containers,
  and files containers create show up as owned by `dev`.
- **Published ports** listen on the docker container: `docker run -p 8080:80`
  is reachable from the VS Code container as `http://docker:8080`, and from your browser
  after `hardcode forward 8080 docker`.
- **Proxy**: containers and builds get `HTTP(S)_PROXY` from
  `~/.docker/config.json`. They have no other way out.
- **HTTPS inside containers** fails unless the container trusts the proxy CA,
  because Squid decrypts it. The certificate is at `/proxy-ca` in both the
  vscode and `docker`:
  ```sh
  docker run -v /proxy-ca:/proxy-ca:ro -e SSL_CERT_FILE=/proxy-ca/ca-bundle.pem …   # Go, OpenSSL, curl, apk, Ruby, .NET
  docker run -v /proxy-ca:/proxy-ca:ro -e NODE_EXTRA_CA_CERTS=/proxy-ca/ca.pem …     # Node (npm, yarn; fetch() ignores proxy variables)
  ```
  For `RUN` steps in a build, copy `/proxy-ca/ca.pem` into the build context
  and in the Dockerfile:
  ```dockerfile
  COPY ca.pem /etc/proxy-ca.pem
  ENV SSL_CERT_FILE=/etc/proxy-ca.pem NODE_EXTRA_CA_CERTS=/etc/proxy-ca.pem PIP_CERT=/etc/proxy-ca.pem
  ```
  Plain-HTTP mirrors such as `deb.debian.org` need nothing.
- **Pushing images isn't possible**: registries are read-only in the default
  allowlist.
- Testcontainers works: `TESTCONTAINERS_HOST_OVERRIDE=docker` is set.

## Rebuilding and updating

```sh
hardcode build --pull vscode && hardcode up    # newer base image and toolchains; the home volume is kept
hardcode rebuild                               # after editing the Dockerfile
hardcode down -v                               # a fresh home directory (extensions, settings, caches gone)
```

The Dockerfile's last layers (extensions, the entrypoint) are ordered so that
editing `entrypoint.sh` doesn't rebuild the toolchains.
