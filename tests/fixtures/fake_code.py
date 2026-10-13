#!/usr/bin/env python3
"""
A stand-in for the VS Code CLI's extension commands, for tests/images.py.

The VS Code container's scripts run ``code --user-data-dir D --extensions-dir E`` with
``--list-extensions``, ``--install-extension ID`` or ``--uninstall-extension
ID``. This does the same against a fake Marketplace: a directory
($FAKE_MARKETPLACE) of ``<id>.json`` manifests, each with ``publisher``,
``name`` and, optionally, ``extensionDependencies`` and ``extensionPack``.
Installing copies the manifest to ``E/<id>-1.0.0/package.json``, and installs
what it depends on and the members of a pack first, as VS Code does.

Every install and uninstall is appended to $FAKE_CODE_LOG, one per line.
"""

import json
import os
import shutil
import sys
from pathlib import Path


def installed(extensions: Path) -> dict[str, Path]:
    """
    The installed extensions, by id, and where each is.
    """
    found = {}

    for manifest in extensions.glob("*/package.json"):
        data = json.loads(manifest.read_text())
        found[f"{data['publisher']}.{data['name']}".lower()] = manifest.parent

    return found


def log(line: str) -> None:
    """
    Note an install or an uninstall for the test to read.
    """
    with open(os.environ["FAKE_CODE_LOG"], "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def install(extensions: Path, market: Path, extension_id: str) -> bool:
    """
    Install *extension_id* and what it needs; whether it exists.
    """
    if extension_id in installed(extensions):
        return True

    manifest = market / f"{extension_id}.json"
    if not manifest.exists():
        print(f"Extension '{extension_id}' not found.", file=sys.stderr)
        return False

    data = json.loads(manifest.read_text())
    for needed in data.get("extensionDependencies", []) + data.get("extensionPack", []):
        if not install(extensions, market, needed.lower()):
            return False

    target = extensions / f"{extension_id}-1.0.0"
    target.mkdir(parents=True)
    shutil.copyfile(manifest, target / "package.json")
    log(f"install {extension_id}")
    return True


def main(argv: list[str]) -> int:
    """
    Run one of the three commands; the exit code VS Code's CLI would give.
    """
    extensions = Path(argv[argv.index("--extensions-dir") + 1])
    extensions.mkdir(parents=True, exist_ok=True)
    market = Path(os.environ["FAKE_MARKETPLACE"])

    if "--list-extensions" in argv:
        for extension_id in sorted(installed(extensions)):
            print(extension_id)
        return 0

    if "--install-extension" in argv:
        extension_id = argv[argv.index("--install-extension") + 1].lower()
        return 0 if install(extensions, market, extension_id) else 1

    if "--uninstall-extension" in argv:
        extension_id = argv[argv.index("--uninstall-extension") + 1].lower()
        where = installed(extensions).get(extension_id)
        if where is None:
            return 1

        shutil.rmtree(where)
        log(f"uninstall {extension_id}")
        return 0

    print(f"fake code: unsupported arguments {argv}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
