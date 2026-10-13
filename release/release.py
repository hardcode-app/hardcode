#!/usr/bin/env python3
"""
The steps of a release that aren't a build: the release workflow
(.github/workflows/release.yml) runs them, and tests/release.py tests them.

    release/release.py stamp VERSION
        write VERSION into every file that carries HardCode's version
    release/release.py updater-config --repository OWNER/REPO OUT
        the Tauri configuration a release builds the app with: the updater,
        when release/updater.pub holds the key it trusts (else nothing)
    release/release.py manifest --version VERSION --repository OWNER/REPO DIR
        DIR/latest.json, which the app's updater reads, from DIR's signed installers
    release/release.py notes --version VERSION --repository OWNER/REPO OUT
        the release's description, from release/notes.md
    release/release.py checksums DIR
        DIR/SHA256SUMS, for every file in DIR

Standard library only: it runs in the build image and on the macOS and
Windows runners alike.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent

# The public key the app trusts its updates to be signed with (minisign, as
# `cargo tauri signer generate` writes it). Without it, a release has no
# updater; see docs/releasing.md.
UPDATER_KEY = ROOT / "release" / "updater.pub"

# A release's version: X.Y.Z, or X.Y.Z-N for a prerelease. The prerelease
# part is a number because Windows installers (MSI) only take numbers.
VERSION = re.compile(r"^\d+\.\d+\.\d+(-\d+)?$")

# Every file that carries HardCode's version, and how to find it there: the
# pattern's one group is the version.
VERSIONED = [
    ("cli/hardcode_cli/__init__.py", r'^__version__ = "([^"]+)"$'),
    ("cli/pyproject.toml", r'^version = "([^"]+)"$'),
    ("admin/hardcode_admin/__init__.py", r'^__version__ = "([^"]+)"$'),
    ("admin/pyproject.toml", r'^version = "([^"]+)"$'),
    ("app/src-tauri/Cargo.toml", r'^version = "([^"]+)"$'),
    ("app/src-tauri/Cargo.lock", r'^name = "hardcode-app"\nversion = "([^"]+)"$'),
    ("app/src-tauri/tauri.conf.json", r'^  "version": "([^"]+)",$'),
    ("helm/hardcode/Chart.yaml", r"^version: (\S+)$"),
    ("helm/hardcode/Chart.yaml", r'^appVersion: "([^"]+)"$'),
]

# The installers the app's updater can apply, by the key it looks for in
# latest.json ({os}-{arch}-{installer}), and the end of their file names.
# An install that doesn't know its installer looks for {os}-{arch}: the
# first of its platform here.
INSTALLERS = [
    ("linux-x86_64-appimage", "_amd64.AppImage"),
    ("linux-x86_64-deb", "_amd64.deb"),
    ("linux-x86_64-rpm", ".x86_64.rpm"),
    ("darwin-aarch64-app", "_aarch64.app.tar.gz"),
    ("windows-x86_64-nsis", "_x64-setup.exe"),
    ("windows-x86_64-msi", "_x64_en-US.msi"),
]

# Where a release's files are downloaded from.
DOWNLOAD = "https://github.com/{repository}/releases/download/v{version}/{name}"

# The release's own page.
PAGE = "https://github.com/{repository}/releases/tag/v{version}"

# Where the app looks for the newest release: GitHub redirects "latest" to
# the newest release that is neither a draft nor a prerelease.
LATEST = "https://github.com/{repository}/releases/latest/download/latest.json"


class ReleaseError(Exception):
    """
    A release step can't be done; the message says why.
    """


def check_version(version: str) -> str:
    """
    *version*, if a release can carry it.
    """
    if not VERSION.match(version):
        raise ReleaseError(
            f"{version!r} is not a release version: X.Y.Z, or X.Y.Z-N for a prerelease "
            "(a number: Windows installers take nothing else)"
        )

    return version


def stamp(version: str, root: Path = ROOT) -> list[str]:
    """
    Write *version* into every file that carries HardCode's version; the
    files changed, as ``path: old -> new``.
    """
    check_version(version)
    changes = []

    for name, pattern in VERSIONED:
        path = root / name
        text = path.read_text(encoding="utf-8")

        match = re.search(pattern, text, re.MULTILINE)
        if match is None:
            raise ReleaseError(f"no version in {name} (looked for {pattern})")

        start, end = match.span(1)
        path.write_text(text[:start] + version + text[end:], encoding="utf-8")
        changes.append(f"{name}: {match.group(1)} -> {version}")

    return changes


def versions(root: Path = ROOT) -> dict[str, str]:
    """
    The version each file that carries one says, by ``path`` (and the line's
    pattern, for a file with two).
    """
    found = {}

    for name, pattern in VERSIONED:
        text = (root / name).read_text(encoding="utf-8")
        match = re.search(pattern, text, re.MULTILINE)
        found[f"{name} {pattern}"] = match.group(1) if match else ""

    return found


def updater_config(repository: str, key: Path = UPDATER_KEY) -> dict:
    """
    The Tauri configuration a release adds to the app's: the updater, which
    checks the repository's newest release, trusts *key*, and refuses a
    release signed for another version than it says it is; and the signed
    update files the build makes for it. Nothing without *key*: that release
    has no updater.
    """
    if not key.exists():
        return {}

    if not re.match(r"^[\w.-]+/[\w.-]+$", repository):
        raise ReleaseError(f"{repository!r} is not a GitHub repository (OWNER/REPO)")

    return {
        "bundle": {"createUpdaterArtifacts": True},
        "plugins": {
            "updater": {
                "pubkey": key.read_text(encoding="utf-8").strip(),
                "endpoints": [LATEST.format(repository=repository)],
                "requireSignedVersion": True,
            },
        },
    }


def _installer(name: str) -> Optional[str]:
    """
    The updater's key for the installer called *name*, if it is one.
    """
    for key, ending in INSTALLERS:
        if name.endswith(ending):
            return key

    return None


def manifest(version: str, repository: str, directory: Path, now: Optional[datetime] = None) -> Optional[dict]:
    """
    latest.json for the signed installers in *directory*: for each, where to
    download it and its signature (its ``.sig`` file). None when nothing in
    *directory* is signed: a release without an updater.

    Every installer the updater knows must be there and signed: the updater
    rejects the whole file when one entry is incomplete.
    """
    check_version(version)

    signed = sorted(path for path in directory.iterdir() if path.name.endswith(".sig"))
    if not signed:
        return None

    platforms = {}
    for signature in signed:
        installer = signature.with_name(signature.name[: -len(".sig")])
        key = _installer(installer.name)

        if key is None:
            continue
        if not installer.exists():
            raise ReleaseError(f"{signature.name} signs {installer.name}, which isn't in {directory}")
        if key in platforms:
            raise ReleaseError(f"two installers for {key}: {platforms[key]['url']} and {installer.name}")

        platforms[key] = {
            "url": DOWNLOAD.format(repository=repository, version=version, name=installer.name),
            "signature": signature.read_text(encoding="utf-8").strip(),
        }

    missing = [key for key, _ in INSTALLERS if key not in platforms]
    if missing:
        raise ReleaseError(f"no signed installer for {', '.join(missing)} in {directory}")

    # An install that doesn't know its installer: the first of its platform.
    for key, _ in INSTALLERS:
        platform = key.rsplit("-", 1)[0]
        platforms.setdefault(platform, platforms[key])

    published = (now or datetime.now(timezone.utc)).replace(microsecond=0)

    return {
        "version": version,
        "notes": PAGE.format(repository=repository, version=version),
        "pub_date": published.isoformat().replace("+00:00", "Z"),
        "platforms": platforms,
    }


def notes(version: str, repository: str, template: Path = ROOT / "release" / "notes.md") -> str:
    """
    The release's description: *template* with its ``{{name}}``s filled in.
    """
    check_version(version)

    values = {
        "version": version,
        "repository": repository,
        # Image and chart names are lowercase, whatever the owner's name is.
        "owner": repository.split("/")[0].lower(),
    }

    text = template.read_text(encoding="utf-8")
    for name, value in values.items():
        text = text.replace("{{" + name + "}}", value)

    unknown = re.findall(r"\{\{\s*(\w+)\s*\}\}", text)
    if unknown:
        raise ReleaseError(f"{template.name} names {', '.join(sorted(set(unknown)))}, which a release doesn't have")

    return text


def checksums(directory: Path) -> str:
    """
    ``SHA256SUMS`` for every file in *directory* (but itself), in the format
    ``sha256sum -c`` reads.
    """
    lines = []

    for path in sorted(directory.iterdir()):
        if path.name == "SHA256SUMS" or not path.is_file():
            continue

        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)

        lines.append(f"{digest.hexdigest()}  {path.name}\n")

    return "".join(lines)


def main(argv: list[str]) -> int:
    """
    The command line; see the module's docstring.
    """
    parser = argparse.ArgumentParser(prog="release.py", description="The steps of a release that aren't a build.")
    commands = parser.add_subparsers(dest="command", required=True)

    command = commands.add_parser("stamp", help="write VERSION into every file that carries the version")
    command.add_argument("version")

    command = commands.add_parser("updater-config", help="the Tauri configuration a release builds the app with")
    command.add_argument("--repository", required=True)
    command.add_argument("out", type=Path)

    command = commands.add_parser("manifest", help="DIR/latest.json, from DIR's signed installers")
    command.add_argument("--version", required=True)
    command.add_argument("--repository", required=True)
    command.add_argument("directory", type=Path)

    command = commands.add_parser("notes", help="the release's description")
    command.add_argument("--version", required=True)
    command.add_argument("--repository", required=True)
    command.add_argument("out", type=Path)

    command = commands.add_parser("checksums", help="DIR/SHA256SUMS")
    command.add_argument("directory", type=Path)

    arguments = parser.parse_args(argv)

    try:
        if arguments.command == "stamp":
            for change in stamp(arguments.version):
                print(change)

        elif arguments.command == "updater-config":
            config = updater_config(arguments.repository)
            arguments.out.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            print("the updater, checking " + LATEST.format(repository=arguments.repository) if config else "no updater")

        elif arguments.command == "manifest":
            latest = manifest(arguments.version, arguments.repository, arguments.directory)
            if latest is None:
                print("nothing is signed: no latest.json")
            else:
                (arguments.directory / "latest.json").write_text(json.dumps(latest, indent=2) + "\n", encoding="utf-8")
                print(f"latest.json: {', '.join(sorted(latest['platforms']))}")

        elif arguments.command == "notes":
            arguments.out.write_text(notes(arguments.version, arguments.repository), encoding="utf-8")

        elif arguments.command == "checksums":
            (arguments.directory / "SHA256SUMS").write_text(checksums(arguments.directory), encoding="utf-8")

    except (ReleaseError, OSError) as error:
        print(f"release.py: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
