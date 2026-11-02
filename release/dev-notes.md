A development build of `main` at {{commit}}, for testing: not a release.
The next development build replaces it. `hardcode --version` and the app's
sidebar say `{{version}} (dev, {{commit}})`.

Nothing here is signed, and the app doesn't update itself: install the next
build by hand. A newer development build's `.deb` or `.rpm` installs over
this one (`sudo apt install ./HardCode_*.deb`), and a release over both; for
the Python package, `pipx install --force`. macOS refuses an unsigned app
from the internet; after copying it to Applications, run
`xattr -dr com.apple.quarantine /Applications/HardCode.app`. Windows asks
first: *More info*, then *Run anyway*.

## Install

**The desktop app**, which brings the `hardcode` command with it:

- Linux: the `.deb` (Debian, Ubuntu), the `.rpm` (Fedora, openSUSE) or the
  `.AppImage` (anything else)
- macOS (Apple silicon): the `.dmg`
- Windows: the `-setup.exe`, or the `.msi`

**The `hardcode` command on its own**: `hardcode-linux-x64`,
`hardcode-macos-arm64` or `hardcode-win-x64.exe`. Or, with Python 3.9 or
newer, the package: `pipx install hardcode_cli-{{wheel}}-py3-none-any.whl`.

**Kubernetes**: the chart,
`helm install hardcode oci://ghcr.io/{{owner}}/charts/hardcode --version {{chart}}`,
runs the images `ghcr.io/{{owner}}/hardcode-{vscode,squid,gateway,admin}:sha-{{commit}}`.
Their `edge` tag is always the newest development build.

`SHA256SUMS` has every file's checksum: `sha256sum -c SHA256SUMS --ignore-missing`
in the folder you downloaded them to.
