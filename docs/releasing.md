# Releasing

A release is a tag. Pushing `vX.Y.Z` runs
[`.github/workflows/release.yml`](../.github/workflows/release.yml), which
builds everything, and publishes a GitHub Release only when every build
succeeded.

```sh
git tag v1.2.0
git push origin v1.2.0
```

- **Versions** are `X.Y.Z`, or `X.Y.Z-N` for a prerelease. The prerelease
  part is a number, because Windows installers (MSI) take nothing else; the
  workflow refuses any other tag before it publishes anything.
- **A prerelease** is published as one: the app's updater, the images'
  `latest` and `X.Y` tags, and GitHub's "latest release" all skip it.
- **The version** lives in the tag. Each build job writes it into every
  file that carries one (`release/release.py stamp`: the CLI's
  `__version__` and `pyproject.toml`, the admin's two, the app's `Cargo.toml`,
  `Cargo.lock` and `tauri.conf.json`, and the chart's `Chart.yaml`). In the
  repository they stay at the development version; `tests/release.py`
  checks they agree.
- **A manual run** (Actions → Release → Run workflow) from `main` is a
  [development build](#development-builds); from any other branch it
  builds everything and publishes nothing.

## What a release publishes

| | Files |
|---|---|
| The desktop app, with the `hardcode` command as its sidecar | `HardCode_X.Y.Z_amd64.deb`, `HardCode-X.Y.Z-1.x86_64.rpm`, `HardCode_X.Y.Z_amd64.AppImage` (Linux, built in the build image); `HardCode_X.Y.Z_aarch64.dmg` (macOS); `HardCode_X.Y.Z_x64-setup.exe`, `HardCode_X.Y.Z_x64_en-US.msi` (Windows) |
| Its updates, once the updater is set up (below) | a `.sig` beside each installer, `HardCode_aarch64.app.tar.gz` (macOS's update) and `latest.json` |
| The `hardcode` command | `hardcode-linux-x64`, `hardcode-macos-arm64`, `hardcode-win-x64.exe` (one file each, the stack inside), and the Python package `hardcode_cli-X.Y.Z-py3-none-any.whl` (`pipx install` it) |
| Kubernetes | the chart `hardcode-X.Y.Z.tgz`, also at `oci://ghcr.io/<owner>/charts/hardcode`, with its images set to `ghcr.io/<owner>/hardcode-{vscode,squid,gateway,admin}:X.Y.Z` (also tagged `X.Y` and `latest`) |
| The source | `hardcode-X.Y.Z-source.tar.gz`, the tag's files (`git archive`); GitHub attaches its own archives as well |
| Known vulnerabilities | `vulnerabilities.json`, each finding in the lock files and the boundary's images, and a summary at the end of the description ([below](#known-vulnerabilities)) |
| Checksums | `SHA256SUMS`, every file's: `sha256sum -c SHA256SUMS --ignore-missing` |

The release's description is [`release/notes.md`](../release/notes.md)
(how to install each of these), followed by GitHub's generated notes: the
pull requests merged since the last release, Dependabot's under their own
heading ([`.github/release.yml`](../.github/release.yml)).

The Linux builds, the chart and the release's assembly run in the build
image, as everything does. The macOS and Windows builds can only run on
those platforms; they install the same pinned PyInstaller
(`images/build/requirements.txt`), PyYAML (`cli/requirements.txt`) and
Tauri CLI (the build image's version) as the build image has. macOS builds
are unsigned unless the `APPLE_CERTIFICATE`, `APPLE_CERTIFICATE_PASSWORD`
and `APPLE_SIGNING_IDENTITY` secrets hold a certificate.

Nothing in a release (or a development build) is restored from a cache:
any job on `main` can write one, so the build image, the images, the app
and its crates are built from scratch each time
([security](security.md)). That makes a release slower than CI's runs,
which keep their caches.

## Known vulnerabilities

Every release and development build is scanned before it is published, by
the *Known vulnerabilities* job: the lock files (`Cargo.lock`, the Python
pins), and the images that are HardCode's boundary as the run published
them (squid, the gateway, the admin) and as `compose.yaml` pins them
(CoreDNS, the Docker sidecar). The VS Code image is what the sandbox
contains, and isn't scanned.

The job holds no secret and can only read. `release/scan.py databases`
downloads OSV's databases (crates.io, PyPI, Go, Alpine, Debian, Ubuntu),
`release/scan.py save` copies each image, the runner's platform only,
from its registry into an archive (skopeo, from the archive, with the
job's read-only token for ghcr.io; no Docker daemon, whose image stores
each save a multi-platform image differently), and then `release/scan.sh` runs
OSV-Scanner (the build image's, checksum-pinned) in a container with no
network, no token, no capabilities and a read-only filesystem: a
compromised scanner could misreport, and nothing more.
`release/scan.py summary` turns its reports into the description's last
section (a count by severity for each thing scanned, and what a newer
version fixes) and `vulnerabilities.json`. The findings are reported, not
blocking; the job's own failure stops the release, as any job's does.

To scan a checkout: `release/scan.py databases DIR`, `release/scan.py save
--prefix ghcr.io/<owner>/hardcode --tag <tag> IMAGES` (or any image archives
in IMAGES), then `release/scan.sh DATABASES IMAGES REPORTS`, all but the
last through `images/build/run.sh`.

## Development builds

A manual run from `main` (Actions → Release → Run workflow, or
`gh workflow run release.yml --ref main`) builds everything a release does,
from `main` as it is, and publishes it for testing:

| | Where |
|---|---|
| The app, the `hardcode` command, its Python package, the chart | the **`dev` prerelease** on the Releases page, with `SHA256SUMS`. Each development build deletes the last one and its `dev` tag, and publishes itself at its own commit |
| The images | `ghcr.io/<owner>/hardcode-{vscode,squid,gateway,admin}`, tagged `sha-<commit>` and `edge` (the newest development build); never `latest`, which is a release's |
| The chart | `oci://ghcr.io/<owner>/charts/hardcode`, version `X.Y.Z-dev.N` (the checkout's version, which is the next release's, and the run's number), pointing at the `sha-<commit>` images. Helm takes it only when asked: `--devel`, or its exact version |

Its files carry the checkout's version, since Windows installers take
nothing but numbers, and say they are a development build where people
look: `hardcode --version` and the app's sidebar say
`0.1.0 (dev, 2c419ae)` (`release/release.py dev` marks the build). The app
has no updater, like any build without a release's version, and nothing
is signed: macOS refuses the app until its quarantine is cleared, which
the prerelease's description
([`release/dev-notes.md`](../release/dev-notes.md)) says how to do. Being a
prerelease, it is never GitHub's latest release: the app's updater, and
`releases/latest`, never see it.

While the repository is private, a run's macOS minutes count ten times
against the account's Actions minutes and its Windows minutes twice, so
development builds are started by hand.

## The app's updater

A release's app asks the repository's newest release for `latest.json`
(`https://github.com/<owner>/<repo>/releases/latest/download/latest.json`)
as it starts. When that is a newer version, the sidebar offers it; **Install
and restart** downloads the installer of the kind the app was installed
from (a `.deb` or `.rpm` installs with a password prompt, an AppImage is
replaced in place, the macOS app is replaced, the Windows installer runs
without questions), and installs it only when:

- its signature checks out against the public key the app was built with,
  `release/updater.pub`, and
- the signature was made for the version `latest.json` announces
  (`requireSignedVersion`): a forged `latest.json` can't offer an older,
  genuinely signed release as a newer one.

Then the app starts again. The sandboxes keep running throughout; they
don't belong to the app. An app offline, or with GitHub down, asks again at
its next start. Development and local builds (`app/build.sh build`,
`app/build.sh bundle` without a release's configuration) have no updater:
the configuration that sets it up is added by the release workflow only
(`release/release.py updater-config`), and a test keeps it out of the
checkout's.

**The signing key is in one job only.** The app's builds run every crate's
build script and proc macro, so none of them holds the key: they make the
installers unsigned (and, on macOS, the update archive,
`HardCode_aarch64.app.tar.gz`). After them, *Sign the app's updates*, a job
that builds nothing, downloads the installers and runs only the Tauri CLI's
signer over them (`release/release.py sign`, in the build image, whose
Tauri CLI is checksum-pinned), and the release publishes the `.sig` files
it makes beside them.

### Setting it up, once

Until `release/updater.pub` exists, releases are published without the
`.sig` files and `latest.json`, and their app has no updater.

1. Make a key pair, with a password, in the build image's scratch folder
   (mounted at the same path on your machine):

   ```sh
   images/build/run.sh cargo tauri signer generate \
     --write-keys "${TMPDIR:-/tmp}/hardcode-build-$(id -u)/updater.key"
   ```

2. Copy `updater.key.pub` to `release/updater.pub` and commit it. Every
   app built from then on trusts this key and no other: changing it is a
   security change, and its commit says why.
3. In the repository's settings (Secrets and variables → Actions), add the
   secrets `TAURI_SIGNING_PRIVATE_KEY` (the content of `updater.key`) and
   `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`. Only the signing job is given them.
4. Keep `updater.key` and its password somewhere safe (a password
   manager), and delete the scratch copy.

The next tag's installers are signed and its `latest.json` published. Apps
from releases before that have no updater: their users install the new
release by hand, once.

**Losing the key** means no installed app can be updated again: a new key
is a new `release/updater.pub`, and every user installs by hand once.
**A leaked key** lets whoever holds it sign an update that every installed
app accepts, if they can also publish it as this repository's newest
release: rotate the key (the same steps), and publish a release signed with
the new one.

## Testing the steps

`tests/release.py` tests `release/release.py` for real: the version written
into every file (and nothing else), a development build's mark (and the
checkout without one), the updater's configuration, the builds' files
gathered out of the folders they arrive in, a `latest.json`
assembled from installers `release/release.py sign` signed with a throwaway key (each signature
carrying its version), both descriptions and the checksums.
`tests/app.py` builds the app with a release's updater configuration,
pointed at a `latest.json` it serves, and checks that the app offers the
newer version. CI runs both, and actionlint is worth running over the
workflows after changing them:

```sh
docker run --rm --memory 512m -v "$PWD:/repo:ro" -w /repo rhysd/actionlint:1.7.12
```
