#!/usr/bin/env python3
"""
The steps of a release that aren't a build: the release workflow
(.github/workflows/release.yml) runs them, and tests/release.py tests them.

    release/release.py stamp VERSION
        write VERSION into every file that carries HardCode's version
    release/release.py dev COMMIT
        mark a development build of COMMIT: the command's --version, and the
        app, say "0.1.0 (dev, COMMIT)"
    release/release.py updater-config --repository OWNER/REPO OUT
        the Tauri configuration a release builds the app with: the updater,
        when release/updater.pub holds the key it trusts (else nothing)
    release/release.py sign --version VERSION DIR
        sign DIR's installers for the app's updater (each FILE.sig), with
        the key in TAURI_SIGNING_PRIVATE_KEY: a job of its own, after the
        builds, so that no build ever holds the key
    release/release.py gather DIR
        move the files in DIR's folders into DIR: the builds' files, as
        they are downloaded, into the one list a release publishes
    release/release.py manifest --version VERSION --repository OWNER/REPO DIR
        DIR/latest.json, which the app's updater reads, from DIR's signed installers
    release/release.py notes --version VERSION --repository OWNER/REPO OUT
        the release's description, from release/notes.md
    release/release.py dev-notes --commit COMMIT --chart VERSION --repository OWNER/REPO OUT
        the development build's description, from release/dev-notes.md
    release/release.py checksums DIR
        DIR/SHA256SUMS, for every file in DIR

Standard library only: it runs in the build image and on the macOS and
Windows runners alike.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

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

# Where a development build is marked as one, and how to find the mark
# there: the pattern's one group is the mark, empty in a release.
BUILD_MARK = ("cli/hardcode_cli/__init__.py", r'^__build__ = "([^"]*)"$')

# A commit, as git names it: its hash, or the start of it.
COMMIT = re.compile(r"^[0-9a-f]{7,40}$")

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


def mark_dev(commit: str, root: Path = ROOT) -> str:
    """
    Mark the files at *root* as a development build of *commit*: the
    command's ``--version``, and the app, which shows the command's, say
    "0.1.0 (dev, *commit*)". The change, as ``path: old -> new``.
    """
    if not COMMIT.match(commit):
        raise ReleaseError(f"{commit!r} is not a commit: its hash, or the start of it, in lowercase hex")

    name, pattern = BUILD_MARK
    path = root / name
    text = path.read_text(encoding="utf-8")

    match = re.search(pattern, text, re.MULTILINE)
    if match is None:
        raise ReleaseError(f"no build mark in {name} (looked for {pattern})")

    mark = f"dev, {commit}"
    start, end = match.span(1)
    path.write_text(text[:start] + mark + text[end:], encoding="utf-8")

    return f"{name}: {match.group(1)!r} -> {mark!r}"


def build_mark(root: Path = ROOT) -> str:
    """
    The development build's mark the files at *root* carry: empty in a
    release, and in the checkout.
    """
    name, pattern = BUILD_MARK
    match = re.search(pattern, (root / name).read_text(encoding="utf-8"), re.MULTILINE)

    if match is None:
        raise ReleaseError(f"no build mark in {name} (looked for {pattern})")

    return match.group(1)


def updater_config(repository: str, key: Path = UPDATER_KEY) -> dict:
    """
    The Tauri configuration a release adds to the app's: the updater, which
    checks the repository's newest release, trusts *key*, and refuses a
    release signed for another version than it says it is. Nothing without
    *key*: that release has no updater.

    The build doesn't sign (no createUpdaterArtifacts): it runs every
    crate's build script, and the signing key is never where they can read
    it. ``sign`` signs the finished installers, in a job of its own.
    """
    if not key.exists():
        return {}

    if not re.match(r"^[\w.-]+/[\w.-]+$", repository):
        raise ReleaseError(f"{repository!r} is not a GitHub repository (OWNER/REPO)")

    return {
        "plugins": {
            "updater": {
                "pubkey": key.read_text(encoding="utf-8").strip(),
                "endpoints": [LATEST.format(repository=repository)],
                "requireSignedVersion": True,
            },
        },
    }


def sign(
    version: str,
    directory: Path,
    environment: Optional[dict] = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[str]:
    """
    Sign each installer in *directory* the app's updater can apply (each
    gets its FILE.sig), for *version*, with the Tauri CLI's signer and the
    key in *environment*'s TAURI_SIGNING_PRIVATE_KEY (and its password in
    TAURI_SIGNING_PRIVATE_KEY_PASSWORD); the names signed.

    It runs nothing else: the release workflow gives the key to this step
    alone, in a job that builds nothing.
    """
    check_version(version)
    environment = dict(os.environ if environment is None else environment)

    if not environment.get("TAURI_SIGNING_PRIVATE_KEY"):
        raise ReleaseError("TAURI_SIGNING_PRIVATE_KEY isn't set: the updater's key signs the installers")
    environment.setdefault("TAURI_SIGNING_PRIVATE_KEY_PASSWORD", "")

    installers = sorted(path for path in directory.iterdir() if path.is_file() and _installer(path.name))
    if not installers:
        raise ReleaseError(f"no installer the updater can apply in {directory}")

    for installer in installers:
        signed = runner(
            ["cargo", "tauri", "signer", "sign", "--app-version", version, str(installer)],
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if signed.returncode != 0 or not installer.with_name(f"{installer.name}.sig").exists():
            raise ReleaseError(f"signing {installer.name} failed: {(signed.stderr or signed.stdout).strip()[-500:]}")

    return [installer.name for installer in installers]


def gather(directory: Path) -> list[str]:
    """
    Move every file in *directory*'s folders into *directory* itself, and
    remove the folders; the names of the files moved. The builds' files
    are downloaded in the folders they were built in (``deb/``,
    ``appimage/``, ``dmg/``...), and a release publishes one flat list, so
    two files of one name are refused before anything is moved.
    """
    files = sorted(path for path in directory.rglob("*") if path.is_file())

    seen: dict[str, Path] = {}
    for path in files:
        if path.name in seen:
            raise ReleaseError(f"two files called {path.name}: {seen[path.name]} and {path}")
        seen[path.name] = path

    moved = []
    for path in files:
        if path.parent != directory:
            path.rename(directory / path.name)
            moved.append(path.name)

    # The folders, now empty: in reverse order, a folder's own come first.
    for folder in sorted((path for path in directory.rglob("*") if path.is_dir()), reverse=True):
        folder.rmdir()

    return moved


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


def _fill(template: Path, values: dict[str, str]) -> str:
    """
    *template* with its ``{{name}}``s replaced by *values*; a name that
    isn't one of them is refused.
    """
    text = template.read_text(encoding="utf-8")
    for name, value in values.items():
        text = text.replace("{{" + name + "}}", value)

    unknown = re.findall(r"\{\{\s*(\w+)\s*\}\}", text)
    if unknown:
        raise ReleaseError(f"{template.name} names {', '.join(sorted(set(unknown)))}, which a release doesn't have")

    return text


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

    return _fill(template, values)


def dev_notes(
    commit: str,
    chart: str,
    repository: str,
    root: Path = ROOT,
    template: Path = ROOT / "release" / "dev-notes.md",
) -> str:
    """
    The development build's description: *template* with the commit it was
    built from, the version its files carry (the checkout's, at *root*),
    and its chart's version, filled in.
    """
    if not COMMIT.match(commit):
        raise ReleaseError(f"{commit!r} is not a commit: its hash, or the start of it, in lowercase hex")

    values = {
        "commit": commit,
        "version": next(iter(versions(root).values())),
        "chart": chart,
        "repository": repository,
        "owner": repository.split("/")[0].lower(),
    }

    return _fill(template, values)


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

    command = commands.add_parser("dev", help="mark a development build of COMMIT")
    command.add_argument("commit")

    command = commands.add_parser("updater-config", help="the Tauri configuration a release builds the app with")
    command.add_argument("--repository", required=True)
    command.add_argument("out", type=Path)

    command = commands.add_parser("sign", help="sign DIR's installers for the app's updater")
    command.add_argument("--version", required=True)
    command.add_argument("directory", type=Path)

    command = commands.add_parser("gather", help="move the files in DIR's folders into DIR")
    command.add_argument("directory", type=Path)

    command = commands.add_parser("manifest", help="DIR/latest.json, from DIR's signed installers")
    command.add_argument("--version", required=True)
    command.add_argument("--repository", required=True)
    command.add_argument("directory", type=Path)

    command = commands.add_parser("notes", help="the release's description")
    command.add_argument("--version", required=True)
    command.add_argument("--repository", required=True)
    command.add_argument("out", type=Path)

    command = commands.add_parser("dev-notes", help="the development build's description")
    command.add_argument("--commit", required=True)
    command.add_argument("--chart", required=True)
    command.add_argument("--repository", required=True)
    command.add_argument("out", type=Path)

    command = commands.add_parser("checksums", help="DIR/SHA256SUMS")
    command.add_argument("directory", type=Path)

    arguments = parser.parse_args(argv)

    try:
        if arguments.command == "stamp":
            for change in stamp(arguments.version):
                print(change)

        elif arguments.command == "dev":
            print(mark_dev(arguments.commit))

        elif arguments.command == "updater-config":
            config = updater_config(arguments.repository)
            arguments.out.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            print("the updater, checking " + LATEST.format(repository=arguments.repository) if config else "no updater")

        elif arguments.command == "sign":
            signed = sign(arguments.version, arguments.directory)
            print(f"signed {', '.join(signed)}")

        elif arguments.command == "gather":
            moved = gather(arguments.directory)
            print(f"{len(moved)} files moved into {arguments.directory}")

        elif arguments.command == "manifest":
            latest = manifest(arguments.version, arguments.repository, arguments.directory)
            if latest is None:
                print("nothing is signed: no latest.json")
            else:
                (arguments.directory / "latest.json").write_text(json.dumps(latest, indent=2) + "\n", encoding="utf-8")
                print(f"latest.json: {', '.join(sorted(latest['platforms']))}")

        elif arguments.command == "notes":
            arguments.out.write_text(notes(arguments.version, arguments.repository), encoding="utf-8")

        elif arguments.command == "dev-notes":
            text = dev_notes(arguments.commit, arguments.chart, arguments.repository)
            arguments.out.write_text(text, encoding="utf-8")

        elif arguments.command == "checksums":
            (arguments.directory / "SHA256SUMS").write_text(checksums(arguments.directory), encoding="utf-8")

    except (ReleaseError, OSError) as error:
        print(f"release.py: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
