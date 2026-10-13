# HardCode

<img src="assets/hardcode.png" align="right" width="140" alt="">

A sandboxed development environment: an Ubuntu 26.04 container running VS Code
in the browser with Claude Code, a rootless Docker daemon of its own, and
network egress restricted to an allowlist of URLs.

Three ways to drive it: the scripts in [`cli/`](cli/), the desktop
[HardCode app](app/README.md) (Linux, macOS, Windows), or the
[Helm chart](helm/README.md) for Kubernetes, where an operator and an admin
UI manage sandboxes for a team.

Everything is organised in **profiles**: separate environments, each built
around its **languages**, with its own allowlist, extensions, ports, mounts,
containers and volumes. A profile's languages decide what its image installs,
which package registries its allowlist opens, and which VS Code extensions
its VS Code container gets, so a Python service's sandbox reaches PyPI and not npm.

```
browser ─► gateway ─┐
                    │      devnet (internal: no route to anywhere)
                    ├─► vscode  VS Code web as user "dev" (password-less sudo)
                    │      │ docker CLI, TLS
                    │      ▼
                    ├─► docker    rootless dockerd   ── /shared volume in both
                    │
                    ├─► squid ───► internet   allowlisted URLs only (HTTPS inspected)
                    └─► coredns ─► internet   DNS for allowlisted names only
```

| Service     | Image                                     | Role |
|-------------|-------------------------------------------|------|
| `vscode`    | built from `images/vscode/Dockerfile`     | VS Code (`code serve-web`), toolchains, Claude Code |
| `docker`      | `docker:29-dind-rootless`                 | Docker daemon for the VS Code container (no `--privileged`) |
| `squid`     | built from `images/squid/Dockerfile` (archive `squid-openssl`) | Egress proxy; decrypts HTTPS and enforces the allowlist per URL and method |
| `coredns`   | `coredns/coredns:1.14.7`                  | DNS, enforces the same allowlist |
| `allowlist` | `ubuntu:26.04` (one-shot)                 | Renders the allowlist into CoreDNS and Squid rule files |
| `pki`       | the squid image (one-shot)                | Creates the proxy CA once; Squid gets the key, vscode and docker the certificate |
| `gateway`   | built from `images/gateway/Dockerfile`    | Publishes VS Code to the host; Docker can't publish ports from internal networks |

Image sources live under `images/`; each profile is a directory under
`profiles/`, run as its own Compose project `hardcode-<profile>` with its own
`hardcode-vscode:<profile>` image.

## Documentation

The full documentation is in [docs/](docs/README.md): getting started,
architecture, profiles, the allowlist, the command, the desktop app and the
admin UI with screenshots, Kubernetes, the security model, testing and
development. [AGENTS.md](AGENTS.md) sets the ground rules for changing the
code.

## Requirements

- Docker Engine 28 or newer with Compose v2 (tested on 29.8): the isolated
  gateway mode that keeps the sandbox away from host services arrived in 28,
  and older engines also forward DNS out of internal networks
  (CVE-2024-29018).
- An Ubuntu 23.10+ host works as-is. Other hosts: set
  `DOCKER_APPARMOR_PROFILE=unconfined` (see [Troubleshooting](#troubleshooting)).
- amd64 or arm64. The full image is about 12 GB.

## Quick start

```sh
hardcode up                  # asks what the "default" profile is built around, then builds and starts it
hardcode open                # VS Code's URL, with its token, in your browser
```

The first `up` asks for the profile's languages and tools (`python,docker`,
say): they decide its image, its allowlist and its extensions, so pick only
what you use. In a script, make it first: `hardcode profile add default
--lang python,docker`. Profiles live in HardCode's home,
`~/.config/hardcode` on Linux (`--config DIR` or `HARDCODE_CONFIG` for
another folder); no checkout is needed, as the command carries what it runs
them with.

`hardcode open` opens `http://localhost:11111/?tkn=...` (each profile gets
its own port from 11111 up; `hardcode port <number>` moves it). The token is a
credential: generated on first start, kept in the home volume, never written
to the logs, and replaced by `hardcode token rotate`; set
`VSCODE_CONNECTION_TOKEN` in the profile's `.env` to choose your own. On first
open, VS Code downloads its server (about 30 s). It updates itself later, on
start or when you open a new window.

The first build takes a few minutes for one language, roughly 15–20 for
all of them. `hardcode up` is `docker compose up -d` over HardCode's
`compose.yaml`, with the profile's project name, env file and override, and
the Docker daemon's Compose profile while `ENABLE_DOCKER` is on;
`hardcode compose <args>` runs any other Compose command that way.

## Profiles

```sh
hardcode profiles
hardcode profile add api --lang python,docker  # built around Python: its toolchain, PyPI, its extensions
hardcode profile add old-node --from default   # copies its languages, allowlist, settings and mounts
hardcode -p old-node disable dotnet java       # then make it different
hardcode -p old-node up --build                # its own containers, volumes, port, image
hardcode profile remove old-node               # down -v and delete its folder
```

A profile is `profiles/<name>/` in the home, with `.env` (only what differs
from the defaults: port, subnet, languages, your own settings),
`allowlist.txt` (its own entries) and `compose.override.yaml` (mounts,
forwards), and two files `hardcode` writes from them: `proxy-allowlist.txt`
and `vscode-extensions.txt`. `profile add` starts from the template or
another profile and picks a free VS Code port and internal subnet. `--profile NAME` (or `-p`) selects the profile for
every script; `HARDCODE_PROFILE` in the environment does the same.

**All profiles at once:** `allow`, `deny`, `mount`, `unmount`, `enable`,
`disable` and `extension` take `--all`, e.g. `hardcode --all allow github.com/ourcompany` adds
it to every profile and
reloads the ones that are running. Port forwards are per profile, since a
host port can belong to only one.

## Where things live

| Path         | Storage                 | Notes |
|--------------|-------------------------|-------|
| `/home/dev`  | `home` volume (per profile) | Seeded from the image on first start: VS Code server and extensions (`~/.vscode-server`), Claude Code (`~/.local`), caches, settings. |
| `/shared`    | `shared` volume (per profile) | Also mounted at `/shared` in `docker`, so containers can bind-mount it. `~/shared` links here. |
| `~/git` (optional) | host folder        | Added with `hardcode mount` ([profile settings](#profile-settings)), mounted in `docker` as well. |
| Everything else | container filesystem | Reset when the container is recreated, including `sudo apt install`s. |

Language-level installs go to the home volume and persist: `pip --user`/venvs,
`npm i -g` (`~/.local`), `cargo install` (`~/.cargo`), `go install` (`~/go`),
`gem install` (`~/.gem`), `cpanm` (`~/perl5`, via local::lib).

## Using Docker from the VS Code container

`docker`, `docker compose` and `docker buildx` talk to the rootless daemon in
`docker` over mutual TLS (`DOCKER_HOST=tcp://docker:2376`).

- **Bind mounts** are resolved on the `docker` side, so only paths that exist
  there work: `/shared`, plus any host folders your override mounts in both
  places, e.g. `docker run -v ~/git/project:/src ...`.
- **File ownership:** `dev` (uid 1000) shows up as `root` inside containers,
  and files containers create show up as owned by `dev`.
- **Published ports** listen on the docker container: `docker run -p 8080:80 ...`
  is reachable from the VS Code container as `http://docker:8080`.
- **Proxy:** `~/.docker/config.json` makes the CLI pass `HTTP(S)_PROXY` to
  every container and build it starts. Containers have no other way out.
- **HTTPS inside containers** fails unless the container trusts the proxy CA,
  because Squid decrypts it. The certificate is at `/proxy-ca` in both the
  vscode and `docker`:
  ```sh
  docker run -v /proxy-ca:/proxy-ca:ro -e SSL_CERT_FILE=/proxy-ca/ca-bundle.pem ...   # Go, OpenSSL, curl, apk, Ruby, .NET
  docker run -v /proxy-ca:/proxy-ca:ro -e NODE_EXTRA_CA_CERTS=/proxy-ca/ca.pem ...     # Node (npm, yarn; note `fetch()` ignores proxy env vars)
  ```
  For `RUN` steps in a `docker build`, copy `/proxy-ca/ca.pem` into the build
  context, then in the Dockerfile:
  ```dockerfile
  COPY ca.pem /etc/proxy-ca.pem
  ENV SSL_CERT_FILE=/etc/proxy-ca.pem NODE_EXTRA_CA_CERTS=/etc/proxy-ca.pem PIP_CERT=/etc/proxy-ca.pem
  ```
  Plain-HTTP mirrors such as `deb.debian.org` need nothing. The proxy itself
  reaches containers and builds automatically via `~/.docker/config.json`.
- **Pushing images isn't possible:** registries are read-only in the allowlist.
- Testcontainers works: `TESTCONTAINERS_HOST_OVERRIDE=docker` is set.

## Profile settings

Mounts and extra ports live in the profile's `compose.override.yaml`, which
Compose merges into `compose.yaml`; `hardcode mount`, `hardcode forward` and
the app maintain it, and its comments show the layout if you'd rather edit
by hand. Anything else you'd change in Compose goes there too: HardCode's
`compose.yaml` comes with the version you run, so an update reaches every
profile.

`hardcode mount ~/Git git` mounts `~/Git` at `~/git` in the VS Code container and at
the same path in `docker`, so `docker run -v ~/git/app:/app` works from the
vscode. Files you or your containers create there are owned by you on the
host, as long as your host uid is 1000. Otherwise see `DEV_UID` in the
profile's `.env`.

> **A writable host mount is a way out of the sandbox.** Anything inside,
> Claude included, can edit `.git/hooks`, `.git/config`, build scripts and the
> like, and your host runs them the next time you use that repo there. Mount
> read-only (`:ro`, in both places) what you only need to read, and review
> changes before running them on the host.

To reach dev servers from your browser: `hardcode forward 3000` for a server
in the VS Code container, `hardcode forward 8080 docker` for a container's
published port. Servers inside the VS Code container must listen on `0.0.0.0`.

## The `hardcode` command (`cli/`)

One command, Python, nothing to install beyond Python 3.9 itself: YAML goes
through mikefarah's `yq` if you have it, else PyYAML, else `yq`'s container
image (Docker is required anyway). Three ways to run it:

```sh
hardcode-linux-x64 …               # a standalone binary from the Releases page (also macOS, Windows)
pipx install ./cli                 # or: pip install ./cli, from a checkout -> `hardcode` on your PATH
cli/hardcode …                     # straight from a checkout
```

```
hardcode [-p NAME | --all] [--json] <command> [args]

  up [--build] · down [-v] · start · stop · restart · build · logs [-f] [--tail N] [svc] · ps
  shell                   a shell in the VS Code container       open     VS Code's URL, opened in your browser
  compose <args>          any docker compose command for the profile
  profiles                list profiles (port, subnet, state)
  profile add NAME [--from P] · profile remove NAME [--yes]
  allow <entry> [rw|ssh]  allow a site                deny <entry>                     --all
  allowlist               every entry: provided, by block, and the profile's own
  forward <port> [vscode|docker] [target]               unforward <port>
  mount <local> <remote> [--ro]                       unmount <remote>                 --all
  enable <lang> [--no-build]                          disable <lang> [--no-build]      --all
```

`-p` / `--profile` (or `HARDCODE_PROFILE`) selects the profile, before or after
the command; `--all` runs allow, deny, mount, unmount, enable and disable on
every profile; `--json` prints results for scripts and the app.

`allow` appends to the **Custom** section of the profile's `allowlist.txt`
(or uncomments an entry `deny` had commented out), validates the entry, then
reloads Squid if the profile is running. `deny` works on any active entry:
one HardCode provides is switched off (an `off` line), and stays off.
`allowlist` lists every entry, provided and the profile's own. `forward` and `mount` edit the profile's
`compose.override.yaml`, validate it with `docker compose config` (restoring
the file if Compose objects), and recreate the affected containers if the
profile is running; mounts go into both `vscode` and `docker` so `docker run -v`
works for them. `enable`/`disable` set `INSTALL_<LANG>` in the profile's
`.env`, rebuild its image and recreate its vscode.

The [HardCode app](app/README.md) does all of the above from a window, with a
profile switcher, "New profile" (copy of any profile) and an *all profiles*
scope, and shows what the proxy has been refusing, with one-click allow.

## The allowlist

Each profile's allowlist is its egress policy: the entries HardCode
provides, in blocks (every profile's, and those of its languages and
tools), and the profile's own, in its `allowlist.txt` (new profiles start
from [template/allowlist.txt](template/allowlist.txt)),
which also switches provided entries off where the profile doesn't want
them. The provided entries aren't copied into profiles, so a newer HardCode's
reach every profile at its next start, switched on, while one a profile
switched off stays off. `hardcode allowlist` lists everything. The
`allowlist` service renders it all into the CoreDNS config and Squid's rule
files. Because Squid decrypts HTTPS (with the stack's own CA, trusted inside
the vscode), rules apply to full URLs and HTTP methods:

```
example.com               # that host, any URL
.example.com              # that host and every subdomain
github.com/myuser         # only URLs under that path (/myuser, /myuser/...)
github.com/myuser   rw    # uploads allowed too
github.com          ssh   # an SSH tunnel to port 22 (ssh.github.com:443 ssh for 443)
```

Entries are **read-only unless marked `rw`**: GET, HEAD, OPTIONS, and the POST
git uses to clone/fetch/pull. So package indexes and registries can be
downloaded from but not published or pushed to, and a git namespace can be
cloned but not pushed to. `rw` is needed for APIs that take POST (the Claude
API, the VS Code Marketplace), sign-in flows, and anywhere you push.

HardCode provides VS Code (server updates and Marketplace),
Ubuntu/Debian/Alpine mirrors, the package registries of the profile's
languages and the hosts of its coding agents, Claude Code and Codex (each
language and tool has a block, which comes and goes with it), GitHub's raw
files, and read-only pulls from the major container registries: Docker
Hub, GHCR, Quay, MCR, gcr.io/Artifact Registry, registry.k8s.io, ECR Public and
Red Hat. No git host is allowed by default: add the namespaces you work in
(there are commented examples), never the whole host.

**SSH** is tunnelled, not inspected, so an `ssh` entry is whole-host and
read-write; namespace rules only apply over HTTPS. Prefer HTTPS with a
namespace where you can. Inside the VS Code container, `ssh` and `git@` URLs go through
the proxy automatically (`/etc/ssh/ssh_config.d/10-hardcode-proxy.conf`).

Apply changes with `hardcode allow` / `hardcode deny` (above), or by hand:

```sh
hardcode reload                                            # after a hand edit; CoreDNS reloads on its own within 10 s
```

To see what's being blocked (the app shows this under *Blocked traffic*):

```sh
hardcode logs -f squid   | grep TCP_DENIED
hardcode logs -f coredns | grep BLOCKED
```

## Languages and the image

A profile's languages and tools are build args of its image, and more:
switching one with `hardcode enable` / `hardcode disable` (or the app's
*Languages / Tools*) also opens or closes its allowlist entries at once, and the
running VS Code container installs or removes its extensions within seconds.
Only the toolchain waits for the image: without `--no-build` it is rebuilt
there and then; `hardcode languages` (and the app) say when the image was
built with other languages. Each profile builds its own
`hardcode-vscode:<profile>`.

| Build arg                  | Installs (Ubuntu 26.04 archive unless noted) |
|----------------------------|-----------------------------------------------|
| `INSTALL_BUILD_ESSENTIALS` | build-essential, autotools, pkg-config, common `-dev` libraries |
| `INSTALL_PYTHON`           | Python 3.14, pip, venv, pipx, black; uv and ruff (PyPI) |
| `INSTALL_CPP`              | GCC 15, Clang/clangd/clang-tidy 21, LLDB, GDB, CMake, Ninja, ccache, Valgrind, cppcheck |
| `INSTALL_RUST`             | rustc/cargo 1.93, rustfmt, clippy, rust-src |
| `INSTALL_GO`               | Go 1.26, gopls, delve, x/tools |
| `INSTALL_PERL`             | Perl 5.40, cpanminus, local::lib, perltidy, Perl::Critic |
| `INSTALL_RUBY`             | Ruby 3.3, bundler |
| `INSTALL_NODE`             | Node.js 22, npm, TypeScript (archive: 5.2), yarn; pnpm (npm registry) |
| `INSTALL_JAVA`             | OpenJDK 25, Maven; Gradle from gradle.org (`GRADLE_VERSION`) |
| `INSTALL_DOTNET`           | .NET SDK 10 |
| `INSTALL_DOCKER`           | Docker CLI, buildx, compose |
| `INSTALL_CLAUDE_CLI`       | Claude Code CLI (claude.ai native installer) |
| `INSTALL_CODEX_CLI`        | Codex CLI, OpenAI's coding agent (GitHub release binary, sha256-verified; `CODEX_VERSION`) |

Vendor sources are used only where the archive has nothing usable: VS Code
(Microsoft .deb, checksum-verified), uv/ruff/pnpm (not packaged), Gradle (the
archive's 4.4.1 can't run on JDK 25), Claude Code, the Codex CLI.

VS Code extensions aren't in the image. The VS Code container installs the
profile's list in the background while it runs: Red Hat YAML, the
extensions of its languages and tools, Claude Code's and Codex's included
(the catalogue in
`cli/hardcode_cli/langs.py`), and its own (`hardcode extension add
publisher.name`, or the app's *Extensions*). It follows the list as it
changes, so a first start is quick and nothing needs a restart. Only
publishers with a verified domain on the Marketplace are allowed: every
extension is checked, dependencies included, as it is installed; the one
reviewed exception is CodeLLDB, the Rust debugger VS Code's Rust docs
recommend.

## Updating and resetting

```sh
hardcode build --pull vscode && hardcode up   # new toolchains; home is kept
hardcode down -v                              # this profile's containers and volumes
hardcode profile remove NAME                  # ... and the profile itself
```

## Security model

**Enforced**

- `vscode` and `docker` are only attached to an `internal` network: no default
  route, and no access to the host or LAN (verified on Docker 29).
- HTTP/HTTPS leaves only through Squid, ports 80/443, no IP literals. Squid
  decrypts HTTPS, so every request is checked against the allowlist by full
  URL and method, and read-only entries can't be used to upload. Denied
  requests cause no DNS lookups.
- The proxy CA is generated by the `pki` service into a volume. Squid alone
  holds the key; the VS Code container and `docker` get only the certificate. Squid
  verifies upstream certificates against the public roots, so a bad upstream
  certificate is an error, never silently accepted.
- DNS for anything not allowlisted gets NXDOMAIN at CoreDNS, so DNS can't
  carry data out. Direct queries to outside resolvers have no route.
- Docker runs rootless and isn't `--privileged`. Containers you start, including
  `--privileged` and `--network host` ones, are inside the same network sandbox.
- `dev` has password-less sudo, but the container drops `NET_RAW` and never had
  `NET_ADMIN`. Even root can't change routes or forge packets towards the
  other containers. The dual-homed sidecars have IP forwarding disabled.
- VS Code is published on `127.0.0.1` only and needs the connection token.
- Claude Code telemetry and error reporting are turned off (their hosts
  aren't on the allowlist).

**Not covered**

- **`rw` and `ssh` entries can receive data.** With credentials, anything
  allowed `rw` (the Claude API, a git namespace you push to) can be written
  to, and an `ssh` entry is a whole host with nothing inspected. The allowlist
  limits where traffic can go, not what is sent. Keep both short.
- **Read-only is by HTTP method.** A service that accepts uploads via GET
  query strings would get through; none of the default entries do.
- **Squid sees plaintext.** It logs URLs, not bodies, but anyone with access
  to the `squid` container can see decrypted traffic.
- **DNS allows subdomains** of every listed name, including exact-host
  entries. Their nameservers belong to the same operator.
- **sudo is root in the VS Code container.** The boundary is the container and the
  network, not the user account.
- **`docker` runs with `seccomp=unconfined` and `systempaths=unconfined`.** Rootless
  user namespaces need this; the daemon itself runs as an unprivileged user.

## Tests

Everything is built and tested in the **build image** (`images/build`, Ubuntu
26.04 with every toolchain, pinned); the machine needs Docker and nothing
else. Each runner re-runs itself in the image through `images/build/run.sh`.

```sh
tests/run_all.py          # lint, cli, app, admin, proxy, images and helm suites
E2E=1 tests/run_all.py    # ... plus tests/e2e_kind.py on a local kind cluster
tests/lint.py --fix       # format the Python (ruff, then isort) and the Rust (rustfmt)
```

| Suite | What it covers |
|---|---|
| `tests/lint.py` | formatting and lint: the Python (`pyproject.toml`: ruff with relaxed settings, then isort) and the Rust (rustfmt) |
| `tests/cli.py` | the `hardcode` command: unit tests for every function (`docker compose` faked), then the whole command against a throwaway home with real `docker compose config` |
| `tests/app.py` | the desktop app: Rust unit tests (`--config`, CLI resolution, settings), a build, then headless launches against the real CLI: on an empty home (the first-profile dialog), then rendering every view, the Languages / Tools and Extensions views included; and a build with a release's updater configuration, which must offer a newer release it is served |
| `tests/admin.py` | TOTP (RFC 6238 vectors), passwords, users, sessions; manifests; the operator's reconcile; the admin API end to end (Flask test client), against an in-memory Kubernetes API |
| `tests/proxy.py` | the squid image for real: path rules can't be escaped, read-only refuses uploads, an empty list denies everything, the renderer matches the CLI's grammar |
| `tests/images.py` | the VS Code image's start-up scripts: the SSH proxy stanza through the real OpenSSH client, home seeding onto a root-owned volume |
| `tests/helm.py` | chart lint and rendered objects |
| `tests/release.py` | a release's steps: the version stamped everywhere, the updater's configuration, `latest.json` from installers signed with a throwaway key, the checksums |
| `tests/e2e_kind.py` | chart install on kind, TOTP sign-in through the API, profiles created, bulk-edited, suspended and deleted by the operator for real |

## CI and releases

GitHub Actions ([.github/workflows/](.github/workflows/)):

- **CI** on every push and pull request runs every suite above in the
  build image (lint, CLI, app, operator/admin, proxy and image scripts, chart,
  the operator end-to-end on a kind cluster) plus the image builds (squid,
  gateway, admin, and a minimal VS Code image). One job runs outside it: the CLI's
  unit tests on macOS, Windows and Python 3.9, which is where users run it.
- **Release** on a `v*` tag ([docs/releasing.md](docs/releasing.md)): the
  tag's version written into everything; images to GHCR as
  `ghcr.io/<owner>/hardcode-{vscode,squid,gateway,admin}:<version>`; the
  desktop app's installers for the three platforms, which bundle the
  `hardcode` binary as a sidecar and, once the signing key is set up, update
  themselves from the newest release; standalone `hardcode` binaries and the
  CLI's Python package; the Helm chart as
  `oci://ghcr.io/<owner>/charts/hardcode`; all attached to a GitHub Release
  with the source, `SHA256SUMS` and notes. The Linux builds and the chart
  come out of the build image; macOS and Windows can only be built natively.

Dependabot keeps the Rust crates, Python packages, actions, base images and
the pinned coredns and docker images current.

## Repository layout

```
compose.yaml              the stack, parameterised by profile (ships inside the hardcode command, with images/ and template/)
template/                 what a new profile starts from: .env (every setting, commented), allowlist.txt, compose.override.yaml
images/vscode/            the VS Code image: Dockerfile, entrypoint, extension sync and check
images/squid/             Squid image: config, CA generation, the allowlist renderer
images/gateway/           publishes ports from the internal network to the host
images/admin/             the Kubernetes operator + admin UI image
images/build/             the build image: the Ubuntu 26.04 environment everything is built and tested in (images/build/run.sh)
cli/                      the hardcode command (Python package hardcode_cli) and its tests
app/                      the HardCode desktop app (Tauri; a window over the CLI)
admin/                    the Kubernetes operator and admin web UI (Python, Flask; shares cli/ code)
helm/hardcode/            Helm chart: CRD, operator, admin, profiles
release/                  a release's steps (release.py: version, updater configuration, latest.json, checksums), its notes, and the updater's public key
assets/                   the icon
docs/                     the documentation (screenshots in docs/images, generated by docs/tools)
tests/                    the test suites (Python, run in the build image) and their fixtures
pyproject.toml            Python formatting and lint settings (ruff, then isort; tests/lint.py)
AGENTS.md                 ground rules for changing the code
```

## Troubleshooting

**`docker` keeps restarting with `[rootlesskit:parent] error: failed to start the child: fork/exec /proc/self/exe: operation not permitted`.**
The host's AppArmor is blocking unprivileged user namespaces
(`kernel.apparmor_restrict_unprivileged_userns=1`, the default on Ubuntu 23.10+).
Compose applies Ubuntu's stock `rootlesskit` profile, which allows them. If
your host doesn't have that profile (`ls /etc/apparmor.d/rootlesskit`), set
`DOCKER_APPARMOR_PROFILE=unconfined` in the profile's `.env`.

**VS Code's port is already in use.** The start fails with Docker's "port is
already allocated"; `hardcode port <number>` (or *change* on the app's
Overview) moves the profile to another. New profiles take the next one from
11111 up that no other profile uses.

**A download fails inside the VS Code container.** Check the Squid and CoreDNS logs (see
[The allowlist](#the-allowlist)) and add the host it needs. A 403 on a POST or
PUT to an allowlisted host means the entry needs `rw`.

**`certificate verify failed` / `unable to get local issuer certificate`.**
The tool doesn't trust the proxy CA. In the VS Code container the system store and the
common env vars (`SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS`, `REQUESTS_CA_BUNDLE`,
`PIP_CERT`) are set; a tool with its own bundle needs pointing at
`/proxy-ca/ca-bundle.pem`. Inside containers, see
[Using Docker from the VS Code container](#using-docker-from-the-vscode).

**Java tools ignore the proxy.** Maven and Gradle are configured through
`~/.m2/settings.xml` and `~/.gradle/gradle.properties`, which are generated on
start if missing. Plain `java` programs need
`-Dhttps.proxyHost=squid -Dhttps.proxyPort=3128`.

**The `172.31.253.0/24` subnet clashes with one of your networks.** Change
`DEVNET_SUBNET` and `DNS_IP` together in the profile's `.env`.
