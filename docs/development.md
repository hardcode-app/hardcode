# Development

## Repository layout

```
compose.yaml                the stack, parameterised by profile; with images/ and template/ it ships inside the hardcode command
template/                   what a new profile starts from: .env (every setting, commented out), allowlist.txt, compose.override.yaml
images/vscode/              the VS Code image: Dockerfile, entrypoint.sh, sync-extensions.sh, verify-extensions.sh, ssh_config
images/squid/               the proxy image: squid.conf, entrypoint, make-ca.sh (pki), allowlist-render.sh
images/gateway/             publishes ports from the internal network to the host (socat)
images/admin/               the Kubernetes operator + admin UI image
images/build/               the build image: the Ubuntu 26.04 environment everything is built and tested in (run.sh runs a command there)
cli/hardcode                the command, runnable from the checkout (with the checkout's stack)
cli/hardcode_cli/           the Python package; cli/tests/ its unit tests; cli/pyproject.toml, and cli/setup.py, which puts the stack into the package
app/src/                    the desktop app's page; app/src-tauri/ the Rust shell
admin/hardcode_admin/       the operator and admin API (Flask); admin/tests/
helm/hardcode/              the chart: CRD, operator, profiles
tests/                      the test suites, in Python (commits.py, lint.py, cli.py, app.py, admin.py, proxy.py, images.py, helm.py, release.py, maintenance.py, e2e_kind.py, run_all.py), their shared harness.py and their fixtures/
release/                    a release's steps (release.py), its known vulnerabilities (scan.py, scan.sh), its notes (notes.md, dev-notes.md), and the updater's public key (updater.pub, once there is one)
maintenance/                what keeps dependencies moving: what is pinned and how old (pins.py), and which updates merge themselves (merge.py)
docs/                       this documentation; docs/images/ the screenshots; docs/tools/ how they are made
assets/                     the icon
.github/workflows/          CI, the release, and the dependency updates (dependencies.yml)
pyproject.toml              Python formatting and lint settings (ruff, then isort; tests/lint.py --fix applies them)
AGENTS.md                   ground rules for anyone changing the code
```

## Conventions

- **The CLI is the source of behaviour.** The app shells out to
  `hardcode --json`; the admin imports `hardcode_cli` for the allowlist,
  blocked-traffic and language code. A rule about an allowlist entry, a port
  or a profile name lives once, in `cli/hardcode_cli`, with a unit test.
- **Python 3.9+, standard library only** in the CLI (YAML through `yq`,
  PyYAML or `yq`'s image, whichever exists). The admin adds Flask and,
  optionally, `qrcode`; both from the Ubuntu archive. No new runtime
  dependency without a discussion first.
- **Packages from the Ubuntu 26.04 archive first**; a vendor source only
  where the archive has nothing usable, with the reason written next to it.
  VS Code extensions only from domain-verified publishers; an exception is
  marked `unverified-ok` on its line and justified in a comment.
- **Every function has a test**; `docker compose`, the Kubernetes API and
  the clock are injected so tests are fast and need nothing installed.
- **Shell scripts** start with `set -euo pipefail` (or `-uo` where a
  failure must be reported, not aborted on) and quote everything.
- **The page code is plain JS**, no bundler, no framework; `esc()` every
  value you put in HTML.
- **Rust stays minimal**: find the CLI, run it, stream output, store
  settings. New behaviour goes into the CLI and the page.
- **Hard memory caps** on everything that runs locally during development
  and tests: `docker run --memory`, `docker update --memory` for kind nodes,
  `systemd-run --user --scope -p MemoryMax=` for host processes.
- **Everything builds and tests in Ubuntu 26.04 containers.** The build
  image (`images/build`) is the environment: the archive's toolchains plus
  a few pinned binaries. `tests/*.py` and `app/build.sh` re-run themselves
  in it through `images/build/run.sh`; nothing is installed on
  workstations or CI runners, so every build is the same build. What's
  missing goes into the Dockerfile, pinned, not onto the machine.
- **Code is written to be read**: full docstrings, spaced-out functions,
  comments where the code isn't obvious; the rules for each language are in
  [AGENTS.md](../AGENTS.md#6-code-style).
- **Formatting is not argued about**: `tests/lint.py --fix` runs ruff
  (format and its default rules, a 120-column line) and then isort on the
  Python, as `pyproject.toml` configures them, and `cargo fmt` on the Rust;
  `tests/lint.py` checks.
- **Never touch a user's own profiles** from tests or tooling; use a
  temporary home (`--config`, as `tests/cli.py` does): outside the default
  home, profiles' Compose projects carry the home's id, so they can't be
  the user's.
- Documentation lives in `docs/` and is part of every change
  ([AGENTS.md](../AGENTS.md)).

## How to…

### Add a language to the image

1. `images/vscode/Dockerfile`: a section guarded by `INSTALL_<LANG>`, archive
   packages first; add the `ARG`.
2. `compose.yaml`: pass the build arg from `.env`, off by default
   (`${INSTALL_<LANG>:-false}`); `template/.env`: the key in the list of
   languages.
3. `cli/hardcode_cli/langs.py`: a `Lang` in `CATALOGUE` (key, display name,
   what it installs, the allowlist entries it opens and which take uploads,
   the VS Code extensions it brings), notes for entries whose name doesn't
   say what they are for (`NOTES`), and any aliases;
   `cli/tests/test_langs.py`. Its allowlist entries are nowhere
   else: every profile with the language gets them from here.
4. The key in the CRD's `spec.languages` enum.
5. `docs/vscode-image.md` and the README's table.

### Add a setting

A profile's `.env` holds only what differs, so a setting is its default in
`compose.yaml` (`${NAME:-default}`), which is where the CLI reads it from
too (`stack.defaults`), and a commented line with that default in
`template/.env`, which `cli/tests/test_langs.py` checks is there. Existing
profiles get the default without anything being written to them.

### Add a CLI command

1. `cli/hardcode_cli/ops.py`: the operation on one profile, returning a
   `Result`; tests in `cli/tests/test_ops.py` with `FakeRunner`.
2. `cli/hardcode_cli/cli.py`: the subparser and `cmd_*`, through `_for_each`
   if it should support `--all` (add it to `ALL_CAPABLE`); the `USAGE`
   block; `cli/tests/test_cli.py`.
3. If the app should expose it: a channel in `app/src/bridge.js` and the
   view in `app/src/app.js`; a scenario step in `tests/cli.py` if it writes
   files Compose must accept.
4. `docs/cli.md` (and `docs/app.md` with a regenerated screenshot).

### Add an admin API route

1. `admin/hardcode_admin/web.py`: the route, `require("full")`, `body()`,
   `HttpError` for client errors; `admin/tests/test_web.py` with the Flask
   test client and `FakeK8s`.
2. The page: `admin/hardcode_admin/web/app.js`.
3. `docs/admin.md`: the API table and, for UI changes, a regenerated
   screenshot (`docs/tools/screenshots.sh admin-…`, plus a fixture response
   in `docs/tools/admin-fixture.js` if the route is new).

### Change what a Profile creates

`admin/hardcode_admin/manifests.py` (`render`) with
`admin/tests/test_manifests_operator.py`; the CRD schema in
`helm/hardcode/crds/` for new spec fields; the Role in
`helm/hardcode/templates/operator.yaml` for new resource kinds;
`tests/e2e_kind.py`'s resource list; `docs/kubernetes.md`.

### Change the allowlist format

Four places must agree, in this order: `images/squid/allowlist-render.sh`
(what the proxy enforces), `cli/hardcode_cli/allowlist.py` (`validate`,
`parse_line`) with `cli/tests/test_allowlist.py`, the template's comments,
and `docs/allowlist.md`. The admin and the app inherit the CLI's rules.

### Regenerate the screenshots

`docs/tools/screenshots.sh` ([how it works](testing.md#screenshots)). Add a
new view to the `SHOTS` table and, if it needs data, to the fixtures.

## Running things from the checkout

```sh
cli/hardcode --config /tmp/hc status                            # the CLI, with the checkout's stack and a home of its own
tests/app.py                                                   # the app: tests, build, headless smoke
app/build.sh bundle                                            # ... the installers (.deb, .rpm, AppImage) with the CLI sidecar, and the launcher
app/build.sh desktop                                           # a launcher for the release build in your app grid (Linux)
app/build.sh cli                                               # the standalone hardcode binary -> dist/
app/build.sh wheel                                             # the CLI's Python package, with its stack -> dist/
images/build/run.sh                                            # a shell in the build image, checkout and caches mounted
cd admin && K8S_API=http://127.0.0.1:8001 K8S_NAMESPACE=sandbox ADMIN_EMAIL=… ADMIN_PASSWORD=… python3 -m hardcode_admin   # the admin, via kubectl proxy
docker build -f images/admin/Dockerfile -t hardcode-admin .    # the admin image (context: the repository root)
hardcode build squid && hardcode up squid                      # after editing squid.conf
```

The app is run where it's installed, not in the container (it drives the
Docker of the machine it runs on): from the launcher `app/build.sh bundle`
writes on a Linux desktop (it starts the release build), or install the
`.deb`, or run the AppImage.

## Dependencies

Nobody pins by hand. Everything that builds HardCode is pinned exactly (a
crate's version in `Cargo.lock`, a Python package's `==`, an action's
commit, an image's tag, a vendor binary's version and checksums), and
something moves each pin, at a pace that leaves time for a compromised
release to be found first:

| What | Moved by |
|---|---|
| The app's crates, the Python pins (`cli/requirements.txt`, the build image's), the actions | Dependabot, weekly, one grouped pull request per ecosystem |
| The images: CoreDNS and the Docker sidecar (`compose.yaml` and the chart's `docker.image`, `coredns.image`), the Dockerfiles' base | Dependabot, weekly, one pull request for all of them |
| The build image's vendor binaries: kind, kubectl, helm, OSV-Scanner, the Tauri CLI | `maintenance/pins.py vendor`, weekly ([`dependencies.yml`](../.github/workflows/dependencies.yml)), as the `pins/vendor-binaries` pull request, with each publisher's checksums |

**A week old, always.** Dependabot proposes a release only once it is seven
days old (its `cooldown`; security updates aren't held back), and so does
the vendor binaries' workflow. CI's *Pin ages* job then looks up every pin a
pull request changes where it was published (crates.io, PyPI, Docker Hub,
GitHub's releases), the crates a lock file update pulls in included, and
fails if one is younger than a week or can't be dated
(`maintenance/pins.py ages`). The actions aren't aged there: Dependabot's
cooldown holds them, and a person merges them.

**Merged without anyone, when nothing needs anyone.** After every CI run,
and daily, `maintenance/merge.py` merges, by rebase, each dependency update
whose CI passed on its newest commit, that is up to date with `main`, and
whose updates are all patch or minor ones (or a newer dated build of the
same image). One behind `main` gets `@dependabot rebase`; one whose only
failure was its pin-age check gets that job run again a day later. What is
left for a person: a major update, any action (each is new code the
workflows run with their tokens), a pull request with anyone else's
commit, and anything that failed. `maintenance/merge.py --dry-run` says
what it would do.

The two jobs that act hold a write token, so they run nothing but those
two scripts, on the runner's own Python: no build image, no cache. Pushes
and merges made with a workflow's token start no workflow, so the vendor
binaries' workflow starts CI on its branch itself; GitHub must also allow
workflows to open pull requests (Settings → Actions → General → *Allow
GitHub Actions to create and approve pull requests*).

## Versioning

The version lives in the release's tag. The files that carry one (the
CLI's and the admin's `__init__.py` and `pyproject.toml`, the app's
`Cargo.toml`, `Cargo.lock` and `tauri.conf.json`, the chart's `Chart.yaml`)
stay at the development version in the repository, and the release
workflow writes the tag's into all of them before it builds
(`release/release.py stamp`; `release.VERSIONED` lists them, and
`tests/release.py` checks they agree). [releasing.md](releasing.md) has
the rest.
