## Install

**The desktop app**, which brings the `hardcode` command with it, and
offers the next release itself when one is out:

- Linux: the `.deb` (Debian, Ubuntu), the `.rpm` (Fedora, openSUSE) or the
  `.AppImage` (anything else)
- macOS (Apple silicon): the `.dmg`
- Windows: the `-setup.exe`, or the `.msi`

**The `hardcode` command on its own**, one file with nothing else to
install (Docker is all it needs): `hardcode-linux-x64`,
`hardcode-macos-arm64` or `hardcode-win-x64.exe`. Or, with Python 3.9 or
newer, the package: `pipx install hardcode_cli-{{version}}-py3-none-any.whl`.

**Kubernetes**: the chart,
`helm install hardcode oci://ghcr.io/{{owner}}/charts/hardcode --version {{version}}`,
runs the images `ghcr.io/{{owner}}/hardcode-{vscode,squid,gateway,admin}:{{version}}`.

**The source**: `hardcode-{{version}}-source.tar.gz`.

`SHA256SUMS` has every file's checksum: `sha256sum -c SHA256SUMS --ignore-missing`
in the folder you downloaded them to.
