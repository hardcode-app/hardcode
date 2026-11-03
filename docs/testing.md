# Testing, CI and releases

```sh
tests/run_all.py          # every suite, in the build image
E2E=1 tests/run_all.py    # … plus the operator end to end on a local kind cluster
```

| Suite | Covers | Needs | Where the tests are |
|---|---|---|---|
| `tests/commits.py` | every commit's message: the subject in Conventional Commits form (`type(scope)!: summary`, AGENTS.md's types), and no attribution lines; and Dependabot's commit prefix, a type, set where Dependabot accepts it (on a multi-ecosystem group, not its updates) | the build image (PyYAML there) | the script |
| `tests/lint.py` | formatting and lint: the Python as `pyproject.toml` configures it (`ruff format --check`, `ruff check` with the default rules and a relaxed line length, then `isort --check-only`), and the Rust with `cargo fmt --check`; and that every file the Python reads or writes as text names its encoding (`read_text`, `write_text`, `open`: Windows' default isn't UTF-8); `tests/lint.py --fix` rewrites the formatting | the build image | `pyproject.toml` |
| `tests/cli.py` | the `hardcode` command: unit tests for every function with `docker compose` faked, then the whole command against a throwaway home (`--config`), with the checkout's stack, where real `docker compose config` validates every file it writes: the first profile asked for, `.env` holding only what differs, ports from 11111 and `hardcode port`; then the standalone binary and the Python package (the wheel, installed into a throwaway virtual environment), built as a release builds them, each making a profile from the stack it carries. The scenario has no Docker daemon (so your containers are never seen), except for removing its profiles; outside the default home, their Compose projects and images carry the home's id, so none can be yours | the build image (Docker through the socket for the scenario; nothing is started) | `cli/tests/test_*.py` (unittest) and the scenario in the script |
| `tests/app.py` | the desktop app: Rust unit tests (`--config`, CLI resolution, running the CLI, stopping process trees, settings), a build, then headless launches against the real CLI and a throwaway home given with `--config`: on the empty home, the first-profile dialog (named `default`, the tools ticked, not dismissable); with the profile last used and a `default` beside it, one that must open on the profile last used, checks the overview, opens a container's logs and renders every view (the Languages / Tools view with a card per catalogue entry, the Extensions view with no status from a stopped container, the new-profile dialog that ＋ opens and Cancel closes, its language picker); and the launcher `app/build.sh desktop` writes (its fields, its icons, a missing build refused), into a scratch `XDG_DATA_HOME`; then a build with a release's updater configuration (a throwaway key) asking a `latest.json` the suite serves, which must offer the newer version on start, and the debug build again without it. No configuration in the checkout may set the updater up | the build image | `app/src-tauri/src/lib.rs` (`mod tests`), `--smoke` in `app/src/app.js` |
| `tests/admin.py` | TOTP (RFC 6238 vectors), passwords (never more than two hashed at once), admins, sessions, the throttle (`X-Forwarded-For` read from the right); the manifests; the operator's reconcile; the HTTP API end to end with Flask's test client, against an in-memory Kubernetes API; then eight sign-ins at once in a container capped at the chart's memory limit for the admin, without swap, as in a pod | the build image (Docker through the socket for the sign-ins) | `admin/tests/test_*.py`, and `tests/fixtures/admin_login_burst.py` |
| `tests/proxy.py` | the squid image built from the tree, for real, in a throwaway isolated network against an origin that normalises paths (dot segments and backslashes alike): a path entry can't be escaped with dot segments, encoded separators or backslashes over any method, over plain HTTP and through an inspected HTTPS tunnel; read-only refuses uploads and `rw` allows them; an upstream certificate nobody vouches for is refused; tunnels to unlisted hosts, internal addresses or non-TLS ports are reset while an `ssh` entry opens exactly its port; an allowlisted name resolving inside is refused; an empty allowlist denies everything; the renderer agrees with the CLI's grammar (`tests/allowlist-corpus.txt`); an isolated internal network has no host-side address or default route | the build image (Docker through the socket) | the script, and its helpers in `tests/fixtures/` (the origin server and the client) |
| `tests/images.py` | the VS Code image's start-up scripts without the image: the SSH proxy stanza expanded by the real OpenSSH client; the sandbox's own SSH key, made by the real ssh-keygen once and only for a home with none, readable by its owner alone; GitHub one repository at a time (hardcode-github saving each repository's token in a file of its own and setting git up, the real git then answered with the right token, gh's wrapper handing gh the right one, and a missing entry, an unseen repository, a token that may not push and a classic token each said plainly), against a fake proxy and GitHub; the Kubernetes home-seeding script run as uid 1000 onto a root-owned, world-writable volume root; the VS Code extensions the running container installs from the profile's list (the first start with dependencies and pack members, a language switched off, the profile's own extensions, an unverified publisher refused before it is ever installed and remembered, a verified extension refused before anything is installed when something it pulls in isn't, a package pulling in more than the Marketplace lists removed whole on arrival, a time limit on every Marketplace query, a reviewed exception, an unreachable Marketplace and the retry, extensions the user installed left alone, a list caught half-written, and the watcher picking up a change within seconds), run for real against a fake VS Code CLI and a fake Marketplace; and `images/build/run.sh`'s CPU cap, never above what the Docker daemon has, against a fake `docker` | the build image (`ssh` there; Docker through the socket) | the script, and `tests/fixtures/fake_code.py`, `fake_marketplace_curl.py`, `fake_docker.py`, `fake_github_curl.py`, `gh_token_probe.py` |
| `tests/docker.py` | the Docker sidecar (`images/docker`): its runc wrapper's unit tests, on the build image's Python and on the sidecar's own (Alpine's, musl); the image (Docker's `docker:dind-rootless` as it is: the same entrypoint, user and environment, and its daemon, containerd, runc, rootlesskit and entrypoint byte for byte; the wrapper first on PATH, Docker's runc where it runs it, Docker's dockerd still with BuildKit's own runc lookup, compose.yaml running it as the suite does; `DOCKER_SIDECAR_IMAGE` tests one built already); the wrapper with the real runc, without a daemon, on Alpine (node:alpine), Debian, UBI, distroless and Temurin, as a build step (`runc run`: the CA added and taken out again, the image left exactly as it was, nothing of the CA anywhere in it) and as a container (`runc create`: one merged copy mounted, from the bundle folder), with a test CA and an origin with a certificate from it; a read-only container, the container's own mount over the bundle or its folder, `exec`, `HARDCODE_PROXY_CA=off`, `update-ca-certificates` in a step with its company CA kept, an image with no bundle, hostile bundle paths (symlinks out of the image, a FIFO, a huge file) and Java's trust store and proxy settings; then the daemon itself, started as compose.yaml starts it, with the same images loaded: `docker run` and a `RUN` step on each (the step proving the daemon's BuildKit still runs runc from PATH), the built images holding nothing of the CA and nothing else changed, `docker exec`, the container's own mounts, `update-ca-certificates` in a build, the bundle folders gone afterwards, and the Java proxy switched on. The daemon section needs a Docker daemon that can run rootless Docker in a container: on a rootless one it says so and is skipped | the build image (Docker through the socket; the images, pinned by digest, are pulled when missing) | the script, `images/docker/test_hardcode_runc.py` (unittest), and `tests/fixtures/docker/` (the cases, the certificates, the clients) |
| `tests/helm.py` | chart lint (with and without a bootstrap admin), rendering, the rendered objects | the build image | the script |
| `tests/release.py` | the steps of a release that aren't a build (`release/release.py`): the version in every file that carries one, and stamped into all of them and nothing else; a development build's mark, and its packages' versions (the Python package's and the RPM's in the files, a real `.deb` repacked with `dpkg-deb`, each sorting after the last development build's and before the release's); the updater's configuration (none without `release/updater.pub`; with it, HTTPS, the newest release, versions signed, and nothing that signs during the build); the builds' files gathered out of their folders; `release.py sign` with a throwaway key (refused without one; every installer signed, nothing else) and `latest.json` from what it signed (every installer, the defaults, each signature carrying its version, a missing one stopping the release); both descriptions and `SHA256SUMS`; the known vulnerabilities' steps (OSV's databases where the scanner reads them offline, the images scanned, copied one platform each by skopeo with the token for ghcr.io only, the description's section counting each issue once by severity, setting aside binaries whose version the scanner can't read and fixes older than what is installed, with a line for each package a newer version fixes, named for its image or its lock, and `vulnerabilities.json`); the nightly development build (scheduled, every job waiting on the check, and the check's own script, run against a stand-in for gh, skipping only a nightly run while `main` is the newest development build's commit, and the development build published only while `main` is its commit, under its own `dev-<run>` tag, before the ones it replaces are retired); what builds a release (everything published attested first, every image builder on the pinned BuildKit, no third-party action in a job that can write, no checkout keeping its token, no secret outside the signing job, a release published with gh at the pushed tag, the macOS and Windows builds on the pinned Rust, uv's Python and `--locked`) | the build image (the Tauri CLI signs) | the script |
| `tests/maintenance.py` | the scripts that keep dependencies moving (`maintenance/`), without the network: the checkout's pins (crates, Python packages, images and the pinned BuildKit, the build image's vendor binaries and AppImage tools, the Rust toolchain, refusing a moving channel) and what isn't one (our images, variables, build stages, a uv.lock's own environment, a hash on the line after a pin); each pin dated where it was published (fake crates.io, PyPI, Docker Hub and GitHub), a GitHub release as old as its newest file, a digest the tag has moved on from or another registry refused; only what a change brings aged, in a throwaway git repository (a week old passes, two days old fails), and CI aging the vendor binaries' pull request too; each vendor binary moved to its newest release a week old (not a younger one, a prerelease, another package's or one with a file uploaded again this week), with its publisher's checksums and a commit message carrying Dependabot's metadata; the lock refresh (each package's step told apart, two versions of a crate included, a commit message whose majors wait for a person, a refresh that only reads with a week's `min-publish-age`, and a proposing job that takes only the three lock files); the images' Python environments (uv pinned by hash; each uv.lock what its pyproject.toml asks for, a week old, every package from PyPI with its wheels' hashes; the admin and build images and the release's macOS and Windows builds installing uv by hash and then a lock with uv, wheels only, on the archive's Python and beside none of its Python libraries; Dependabot moving uv and both locks); and which updates merge themselves against a fake GitHub (a green patch update by rebase at the commit CI ran, never one whose pin-age check didn't run, nor one of our own jobs' that changes what that job doesn't make, and passing over the run GitHub holds for approval on those; actions, majors and a person's commit left; a dated image build not taken for a major; one rebase request per commit; a pin-age failure run again a day later; a refusal kept to its pull request) | the build image (git) | the script |
| `tests/e2e_kind.py` | the chart installed on kind with the admin and squid images built from the checkout (`SQUID_IMAGE` to use another): TOTP sign-in through the API, the chart's profile reconciled (all 15 resources, the DNS ClusterIP in the VS Code container pod, squid and CoreDNS ready for real), a profile created with an Ingress and the Ingress removed when its host is cleared, bulk edits rolled into the ConfigMap, with the entries HardCode provides added by the operator and one switched off through the API commented out there, **the NetworkPolicies enforced** (a capped probe pod in the VS Code container's network position reaches its own squid and DNS and nothing else: not another profile's proxy, the admin, cluster DNS or the internet; kind's own CNI enforces policies), suspend, delete. What no test on kind can cover is the node-exception traffic Kubernetes allows regardless of policy | the build image (kind, kubectl and helm there; Docker through the socket) | the script, and the probe in `tests/fixtures/network_probe.py` |

Every suite is a Python script that prints `ok`/`FAIL` lines and ends with
a count; `run_all.py` reports which suites failed. They share
`tests/harness.py`: the hop into the build image, the check reporting,
running commands, and a Docker helper that caps every container's memory
and removes what a suite started.

## Running them here

Everything runs in the **build image**, `images/build/Dockerfile`: Ubuntu
26.04 with the archive's Python, Flask, Rust, Tauri libraries, OpenSSH and
Docker CLI, plus pinned kind, kubectl, helm, the Tauri CLI, PyInstaller and
ruff. The machine needs Docker and nothing else; every `tests/*.py` (and
`app/build.sh`) re-runs itself in the image through `images/build/run.sh`,
which builds the image the first time (`BUILD_IMAGE_REBUILD=1` after
editing the Dockerfile), mounts the checkout and a scratch directory at
their own paths (so paths handed to Docker from inside resolve on the host
too), mounts the Docker socket, shares the host's network (kind's API port,
port-forwards) and caps the container (`BUILD_MEMORY`, 6g; `BUILD_CPUS`,
4 or what the machine has; no core dumps, which would land in the checkout). Caches (cargo's registry, helm's, Tauri's linuxdeploy) persist in
`~/.cache/hardcode-build` (`HARDCODE_BUILD_CACHE`); cargo's target
directory stays in `app/src-tauri/target`.

```sh
tests/run_all.py                    # every suite
tests/lint.py --fix                 # format the Python (ruff, then isort)
images/build/run.sh                 # a shell in the image
images/build/run.sh helm lint helm/hardcode
```

- **CLI**: the unit tests run in about a second; the scenario drives
  `docker compose config` but starts nothing and never touches your own
  profiles (a temporary home is used).
- **Admin**: run from `admin/` (the repository's `tests/` and `cli/tests/`
  would shadow the package from anywhere else).
- **App**: the first run compiles Tauri (a few minutes); later runs take
  seconds thanks to the caches.
- **Proxy**: `tests/proxy.py` builds `hardcode-squid:test` (cached) and runs
  everything in containers capped at 64–512 MB; nothing of your own stack is
  touched.
- **kind**: the node container is capped (`KIND_NODE_MEMORY`, default 8g),
  the VS Code image isn't loaded (12 GB; those pods stay Pending), the runner
  uses a kubeconfig of its own (your contexts are never changed), and the
  cluster is deleted afterwards (`KEEP_CLUSTER=1` to keep it, `CLUSTER=name`
  to use one you have).

Run everything you can with a hard memory limit. Squid once consumed all the
memory of a host from inside a kind node; the limits in the compose file,
the manifests and the test scripts exist for that reason.

## Screenshots

The pictures in `docs/images/` are generated, not hand-made:

```sh
docs/tools/screenshots.sh                    # all of them
docs/tools/screenshots.sh app-blocked admin-users
```

The script serves the real pages (`app/src`, `admin/hardcode_admin/web`)
with a fixture in place of the Tauri bridge / the API
(`docs/tools/app-fixture.js`, `docs/tools/admin-fixture.js`), with data
built by the real parsers (`docs/tools/fixture-data.py`), and captures each
view with headless Chrome at 1280×860. Regenerate after changing either page
and check that the docs still describe what the pictures show.

## Continuous integration

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml), on every push to
`main` and every pull request. Every job builds the build image (a BuildKit
cache makes that seconds) and runs its suite inside it, exactly as on a
workstation; a runner needs nothing but Docker:

| Job | Does |
|---|---|
| Commit messages | `tests/commits.py`, over the whole history |
| Python formatting and lint | `tests/lint.py` |
| CLI | `tests/cli.py`: unit tests and the scenario |
| CLI unit tests (macOS, Windows, Python 3.9) | the one job outside the image: the command runs on users' macOS and Windows machines with whatever Python they have (3.9 is the floor, checked on Ubuntu 24.04: GitHub has no 3.9 for 26.04), and only those platforms can check that |
| Desktop app | `tests/app.py`: Rust tests, build, headless launch; cargo's caches and target directory are cached between runs |
| Operator and admin API | `tests/admin.py` |
| Proxy policy and image scripts | `tests/proxy.py`, then `tests/images.py` |
| Docker sidecar | `tests/docker.py`: the runc wrapper on real images, then the rootless daemon itself |
| Helm chart | `tests/helm.py` |
| Release steps | `tests/release.py` |
| Maintenance scripts | `tests/maintenance.py` |
| Pin ages | pull requests only: `maintenance/pins.py ages` against the base branch, so every pin the pull request changes must be a week old where it was published ([development.md](development.md#dependencies)) |
| Images build | squid, gateway, the Docker sidecar, admin, and a minimal VS Code image (every language off) with a BuildKit cache |
| Operator end-to-end (kind) | `tests/e2e_kind.py`: the cluster is created in the job, its node capped at 6g |

Dependencies move by themselves, a week old at the earliest, and the
updates that need no one merge themselves: [development.md](development.md#dependencies)
says how. The admin tests check that `compose.yaml` and the chart pin the
same CoreDNS and Docker images, which Dependabot moves in one pull
request. The release's macOS and Windows builds take the Tauri CLI's
version and PyInstaller's pin from the build image's files. The release
workflow's jobs hold only the permissions they publish with.

## Releases

[`.github/workflows/release.yml`](../.github/workflows/release.yml), on a
`v*` tag, writes the tag's version into everything and builds the images
(to GHCR), the desktop app for Linux, macOS and Windows (with its updater,
once the signing key is set up), the `hardcode` command as binaries and as
a Python package, and the Helm chart (as an OCI artifact), then publishes a
GitHub Release with all of it, the source, `latest.json` for the app's
updater, `SHA256SUMS` and notes. The Linux builds, the chart and the
release's assembly run in the build image; macOS and Windows can only be
built natively, the exception, not the rule. [releasing.md](releasing.md)
has the process, what each file is, and the updater's one-time setup.
