#!/usr/bin/env python3
"""
What the repository pins, and how old each pin is: nothing that builds
HardCode is taken until it has been published for a week, the time a
compromised release usually takes to be found and pulled.

    maintenance/pins.py ages BASE
        every pin that differs from the git ref BASE's, and when it was
        published; fails when one is younger than a week, or its age can't
        be told (CI runs it on every pull request)
    maintenance/pins.py vendor [--write] [--message FILE]
        the build image's vendor binaries (kind, kubectl, helm, OSV-Scanner,
        the Tauri CLI): the newest release of each that is a week old, with its
        publisher's checksums; --write puts them in images/build/Dockerfile,
        and --message writes the commit message that proposes them

The actions the workflows use aren't aged here: Dependabot's cooldown holds
them back a week, and each one waits for a person before it is merged.

Standard library only. GITHUB_TOKEN, when set, is used for GitHub's API.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

ROOT = Path(__file__).resolve().parent.parent

# How long a release must have been published before it is taken.
MINIMUM_AGE = timedelta(days=7)

# The files that pin something, by what they pin.
CARGO_LOCK = "app/src-tauri/Cargo.lock"
REQUIREMENTS = ["cli/requirements.txt", "images/build/uv/requirements.txt"]
UV_LOCKS = ["images/admin/uv.lock", "images/build/uv.lock"]
IMAGE_FILES = [
    "compose.yaml",
    "helm/hardcode/values.yaml",
    "images/admin/Dockerfile",
    "images/build/Dockerfile",
    "images/gateway/Dockerfile",
    "images/squid/Dockerfile",
    "images/vscode/Dockerfile",
]
BUILD_DOCKERFILE = "images/build/Dockerfile"

CRATES_IO = "registry+https://github.com/rust-lang/crates.io-index"
PYPI = "https://pypi.org/simple"

# "image: coredns/coredns:1.15.0@sha256:..." in YAML, "FROM ubuntu:..." in a
# Dockerfile: the reference is the first word after either.
IMAGE_LINE = re.compile(r"^[ \t]*(?:image:|FROM)[ \t]+(\S+)", re.MULTILINE | re.IGNORECASE)

# "pyyaml==6.0.3" in a requirements file, alone on its line or followed by
# " \" and its hashes on the lines after.
REQUIREMENT_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\]+)[ \t]*\\?[ \t\r]*$", re.MULTILINE)

# "ARG HELM_VERSION=4.3.0" in the build image's Dockerfile.
ARG_LINE = "ARG {name}={value}"


@dataclass(frozen=True)
class Pin:
    """
    One thing the repository pins: a crate, a Python package, an image, or
    a vendor binary's release.
    """

    kind: str
    name: str
    version: str


@dataclass(frozen=True)
class Vendor:
    """
    A vendor binary the build image downloads: its ARG name in the
    Dockerfile, the GitHub repository it is released from, how its release
    tags are spelt, and where each architecture's checksum comes from.
    """

    arg: str
    repository: str
    tag: str
    checksums: dict[str, str]


# The build image's vendor binaries. A checksum is the release asset's
# digest, which GitHub publishes for it ("asset:NAME"), or a file the
# publisher serves beside the download ("url:URL").
VENDORS = [
    Vendor(
        arg="KIND",
        repository="kubernetes-sigs/kind",
        tag="v{version}",
        checksums={"AMD64": "asset:kind-linux-amd64", "ARM64": "asset:kind-linux-arm64"},
    ),
    Vendor(
        arg="KUBECTL",
        repository="kubernetes/kubernetes",
        tag="v{version}",
        checksums={
            "AMD64": "url:https://dl.k8s.io/release/v{version}/bin/linux/amd64/kubectl.sha256",
            "ARM64": "url:https://dl.k8s.io/release/v{version}/bin/linux/arm64/kubectl.sha256",
        },
    ),
    Vendor(
        arg="HELM",
        repository="helm/helm",
        tag="v{version}",
        checksums={
            "AMD64": "url:https://get.helm.sh/helm-v{version}-linux-amd64.tar.gz.sha256sum",
            "ARM64": "url:https://get.helm.sh/helm-v{version}-linux-arm64.tar.gz.sha256sum",
        },
    ),
    Vendor(
        arg="OSV_SCANNER",
        repository="google/osv-scanner",
        tag="v{version}",
        checksums={"AMD64": "asset:osv-scanner_linux_amd64", "ARM64": "asset:osv-scanner_linux_arm64"},
    ),
    Vendor(
        arg="TAURI_CLI",
        repository="tauri-apps/tauri",
        tag="tauri-cli-v{version}",
        checksums={"AMD64": "asset:cargo-tauri-x86_64-unknown-linux-gnu.tgz"},
    ),
]

Fetch = Callable[[str, dict], bytes]


class PinError(Exception):
    """
    A pin can't be read, aged or moved; the message says which and why.
    """


def fetch_url(url: str, headers: dict) -> bytes:
    """
    The body *url* answers with, asked with *headers* (and GITHUB_TOKEN, for
    GitHub's API).
    """
    headers = {"User-Agent": "hardcode-maintenance (https://github.com/hardcode-app/hardcode)", **headers}

    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError) as error:
        raise PinError(f"{url}: {error}") from error


# ---------------------------------------------------------------------------
# What is pinned
# ---------------------------------------------------------------------------


def crates(text: str) -> set[Pin]:
    """
    The crates.io packages a Cargo.lock holds.
    """
    pins = set()

    for block in text.split("[[package]]")[1:]:
        fields = dict(re.findall(r'^(name|version|source) = "([^"]*)"$', block, re.MULTILINE))

        if fields.get("source") == CRATES_IO:
            pins.add(Pin("crate", fields["name"], fields["version"]))

    return pins


def requirements(text: str) -> set[Pin]:
    """
    The Python packages a requirements file pins with ``==``, with their
    hashes or without.
    """
    return {Pin("pypi", name.lower(), version) for name, version in REQUIREMENT_LINE.findall(text)}


def uv_lock(text: str) -> set[Pin]:
    """
    The PyPI packages a uv.lock holds: not the project itself, nor anything
    from another source.
    """
    pins = set()

    for package in tomllib.loads(text).get("package", []):
        if package.get("source", {}).get("registry") == PYPI:
            pins.add(Pin("pypi", package["name"], package["version"]))

    return pins


def images(text: str) -> set[Pin]:
    """
    The images a Compose file, the chart's values or a Dockerfile names
    with a tag: not ours (hardcode-*), and not one put together from
    variables or build stages.
    """
    pins = set()

    for reference in IMAGE_LINE.findall(text):
        if "$" in reference or reference.startswith("hardcode-"):
            continue

        name, _, version = reference.partition(":")
        if version:
            pins.add(Pin("image", name, version))

    return pins


def vendor_pins(text: str) -> set[Pin]:
    """
    The vendor binaries' versions the build image's Dockerfile pins.
    """
    pins = set()

    for vendor in VENDORS:
        match = re.search(rf"^ARG {vendor.arg}_VERSION=(\S+)$", text, re.MULTILINE)
        if match is None:
            raise PinError(f"{BUILD_DOCKERFILE} has no ARG {vendor.arg}_VERSION")

        pins.add(Pin("release", vendor.repository, vendor.tag.format(version=match.group(1))))

    return pins


def pins_in(read: Callable[[str], Optional[str]]) -> set[Pin]:
    """
    Everything the repository pins, its files read with *read* (a path's
    text, or None when the file isn't there).
    """
    found: set[Pin] = set()

    lock = read(CARGO_LOCK)
    if lock is not None:
        found |= crates(lock)

    for name in REQUIREMENTS:
        text = read(name)
        if text is not None:
            found |= requirements(text)

    for name in UV_LOCKS:
        text = read(name)
        if text is not None:
            found |= uv_lock(text)

    for name in IMAGE_FILES:
        text = read(name)
        if text is not None:
            found |= images(text)

    dockerfile = read(BUILD_DOCKERFILE)
    if dockerfile is not None:
        found |= vendor_pins(dockerfile)

    return found


def reader_at(ref: str, root: Path = ROOT) -> Callable[[str], Optional[str]]:
    """
    A reader of the files as the git ref *ref* has them.
    """

    def read(name: str) -> Optional[str]:
        shown = subprocess.run(
            ["git", "show", f"{ref}:{name}"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return shown.stdout if shown.returncode == 0 else None

    return read


def reader_of(root: Path = ROOT) -> Callable[[str], Optional[str]]:
    """
    A reader of the files as they are in the folder *root*.
    """

    def read(name: str) -> Optional[str]:
        path = root / name
        return path.read_text(encoding="utf-8") if path.exists() else None

    return read


# ---------------------------------------------------------------------------
# When a pin was published
# ---------------------------------------------------------------------------


def _json(fetch: Fetch, url: str, headers: Optional[dict] = None) -> dict:
    """
    *url*'s answer, as JSON.
    """
    try:
        return json.loads(fetch(url, headers or {}))
    except ValueError as error:
        raise PinError(f"{url}: not JSON ({error})") from error


def _when(text: str) -> datetime:
    """
    An ISO 8601 time, as the registries write it ("...Z" included).
    """
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def docker_hub_repository(name: str) -> Optional[str]:
    """
    The Docker Hub repository an image name is pulled from (official images
    are library/NAME), or None for an image from another registry.
    """
    first = name.split("/")[0]
    if "." in first or ":" in first or first == "localhost":
        return None

    return name if "/" in name else f"library/{name}"


def published(pin: Pin, fetch: Fetch) -> datetime:
    """
    When *pin* was published, as its registry says.
    """
    if pin.kind == "crate":
        answer = _json(fetch, f"https://crates.io/api/v1/crates/{pin.name}/{pin.version}")
        return _when(answer["version"]["created_at"])

    if pin.kind == "pypi":
        answer = _json(fetch, f"https://pypi.org/pypi/{pin.name}/{pin.version}/json")
        uploads = [_when(file["upload_time_iso_8601"]) for file in answer.get("urls") or []]
        if not uploads:
            raise PinError(f"PyPI has no files for {pin.name} {pin.version}")
        return min(uploads)

    if pin.kind == "image":
        return _image_published(pin, fetch)

    if pin.kind == "release":
        answer = _json(fetch, f"https://api.github.com/repos/{pin.name}/releases/tags/{pin.version}")
        return _when(answer["published_at"])

    raise PinError(f"no registry for a {pin.kind} pin")


def _image_published(pin: Pin, fetch: Fetch) -> datetime:
    """
    When an image's tag was last pushed to Docker Hub. A pinned digest must
    be the one the tag has now: a digest the tag has moved on from can't be
    dated.
    """
    repository = docker_hub_repository(pin.name)
    if repository is None:
        raise PinError(f"{pin.name} isn't on Docker Hub, the only registry its age can be read from")

    tag, _, digest = pin.version.partition("@")
    answer = _json(fetch, f"https://hub.docker.com/v2/repositories/{repository}/tags/{tag}")

    if digest and answer.get("digest") != digest:
        raise PinError(f"{pin.name}:{tag} is {answer.get('digest')} now, not the pinned {digest}: its age can't be told")

    return _when(answer["tag_last_pushed"])


@dataclass
class Aged:
    """
    A pin, and how old it is (or why that can't be told).
    """

    pin: Pin
    published: Optional[datetime]
    problem: str = ""

    def old_enough(self, now: datetime) -> bool:
        """
        Whether it was published at least MINIMUM_AGE before *now*.
        """
        return self.published is not None and now - self.published >= MINIMUM_AGE


def age_all(
    pins: Iterable[Pin],
    fetch: Fetch = fetch_url,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Aged]:
    """
    Each pin with when it was published; one that can't be dated says why.
    crates.io asks for a request a second at most, and gets it.
    """
    aged = []

    for pin in sorted(pins, key=lambda pin: (pin.kind, pin.name, pin.version)):
        if pin.kind == "crate":
            sleep(1)

        try:
            aged.append(Aged(pin, published(pin, fetch)))
        except (PinError, KeyError, ValueError) as error:
            aged.append(Aged(pin, None, str(error) or type(error).__name__))

    return aged


def ages(
    base: str,
    root: Path = ROOT,
    fetch: Fetch = fetch_url,
    now: Optional[datetime] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """
    Report every pin that differs from *base*'s and how old it is; 1 when
    one is younger than MINIMUM_AGE or can't be dated, else 0.
    """
    now = now or datetime.now(timezone.utc)
    new = pins_in(reader_of(root)) - pins_in(reader_at(base, root))

    if not new:
        print(f"no pin differs from {base}'s")
        return 0

    refused = 0
    for item in age_all(new, fetch, sleep):
        label = f"{item.pin.kind} {item.pin.name} {item.pin.version}"

        if item.published is None:
            print(f"REFUSED  {label}: {item.problem}")
            refused += 1
        elif not item.old_enough(now):
            days = (now - item.published).days
            print(f"REFUSED  {label}: published {item.published:%Y-%m-%d}, {days} days ago (a week is needed)")
            refused += 1
        else:
            print(f"ok       {label}: published {item.published:%Y-%m-%d}")

    print(f"{len(new)} new pins, {refused} refused")

    return 1 if refused else 0


# ---------------------------------------------------------------------------
# The build image's vendor binaries
# ---------------------------------------------------------------------------


def _version_tuple(version: str) -> tuple[int, ...]:
    """
    "4.3.0" as (4, 3, 0), for comparing.
    """
    return tuple(int(part) for part in version.split("."))


def update_type(old: str, new: str) -> str:
    """
    Dependabot's word for the step from *old* to *new*.
    """
    old_parts = _version_tuple(old)
    new_parts = _version_tuple(new)

    if old_parts[0] != new_parts[0]:
        return "version-update:semver-major"
    if old_parts[1:2] != new_parts[1:2]:
        return "version-update:semver-minor"

    return "version-update:semver-patch"


def newest_release(vendor: Vendor, fetch: Fetch, now: datetime) -> Optional[dict]:
    """
    The newest release of *vendor* that is published, not a prerelease, at
    least MINIMUM_AGE old, and tagged as its releases are (a repository
    such as Tauri's releases several things): ``{"version", "published",
    "assets"}``, or None.
    """
    pattern = re.compile("^" + re.escape(vendor.tag).replace(re.escape("{version}"), r"(\d+\.\d+\.\d+)") + "$")
    best = None

    # The newest are first; five pages reach back far enough for any of them.
    for page in range(1, 6):
        url = f"https://api.github.com/repos/{vendor.repository}/releases?per_page=100&page={page}"
        releases = json.loads(fetch(url, {"Accept": "application/vnd.github+json"}))
        if not releases:
            break

        for release in releases:
            match = pattern.match(release.get("tag_name") or "")
            if match is None or release.get("draft") or release.get("prerelease"):
                continue

            when = _when(release["published_at"])
            if now - when < MINIMUM_AGE:
                continue

            version = match.group(1)
            if best is None or _version_tuple(version) > _version_tuple(best["version"]):
                best = {"version": version, "published": when, "assets": release.get("assets") or []}

    return best


def checksum(vendor: Vendor, arch: str, release: dict, fetch: Fetch) -> str:
    """
    The publisher's SHA-256 of *vendor*'s download for *arch*, in *release*.
    """
    source = vendor.checksums[arch].format(version=release["version"])
    kind, _, where = source.partition(":")

    if kind == "asset":
        for asset in release["assets"]:
            if asset.get("name") == where:
                digest = asset.get("digest") or ""
                if not digest.startswith("sha256:"):
                    raise PinError(f"GitHub publishes no SHA-256 for {where} in {vendor.repository}")
                return digest[len("sha256:") :]
        raise PinError(f"{vendor.repository} {release['version']} has no asset {where}")

    text = fetch(where, {}).decode("utf-8").split()
    if not text or not re.fullmatch(r"[0-9a-f]{64}", text[0]):
        raise PinError(f"{where} holds no SHA-256")

    return text[0]


def vendor_updates(dockerfile: str, fetch: Fetch, now: datetime) -> list[dict]:
    """
    For each vendor binary with a release newer than the one *dockerfile*
    pins and a week old: ``{"vendor", "old", "new", "published",
    "checksums"}``.
    """
    updates = []

    for vendor in VENDORS:
        current = re.search(rf"^ARG {vendor.arg}_VERSION=(\S+)$", dockerfile, re.MULTILINE).group(1)
        release = newest_release(vendor, fetch, now)

        if release is None or _version_tuple(release["version"]) <= _version_tuple(current):
            continue

        updates.append(
            {
                "vendor": vendor,
                "old": current,
                "new": release["version"],
                "published": release["published"],
                "checksums": {arch: checksum(vendor, arch, release, fetch) for arch in vendor.checksums},
            }
        )

    return updates


def apply_updates(dockerfile: str, updates: list[dict]) -> str:
    """
    *dockerfile* with each update's version and checksums in its ARGs.
    """
    for update in updates:
        arg = update["vendor"].arg
        values = {f"{arg}_VERSION": update["new"]}
        values.update({f"{arg}_SHA256_{arch}": digest for arch, digest in update["checksums"].items()})

        for name, value in values.items():
            dockerfile, count = re.subn(
                rf"^ARG {name}=\S+$",
                ARG_LINE.format(name=name, value=value),
                dockerfile,
                flags=re.MULTILINE,
            )
            if count != 1:
                raise PinError(f"{BUILD_DOCKERFILE} has {count} lines ARG {name}=, not one")

    return dockerfile


def commit_message(updates: list[dict]) -> str:
    """
    The commit that proposes *updates*: a Conventional Commits subject, a
    line per binary, and the metadata Dependabot writes, which
    maintenance/merge.py reads alike for both.
    """
    names = ", ".join(update["vendor"].arg.lower().replace("_", "-") for update in updates)
    lines = [f"build(deps): bump the build image's {names}", ""]

    for update in updates:
        lines.append(
            f"Bumps {update['vendor'].repository} from {update['old']} to {update['new']}, "
            f"released {update['published']:%Y-%m-%d}; checksums from its publisher."
        )

    lines += ["", "---", "updated-dependencies:"]
    for update in updates:
        lines += [
            f"- dependency-name: {update['vendor'].arg.lower().replace('_', '-')}",
            f"  dependency-version: {update['new']}",
            f"  update-type: {update_type(update['old'], update['new'])}",
        ]
    lines.append("...")

    return "\n".join(lines) + "\n"


def vendor(
    write: bool,
    message: Optional[Path],
    root: Path = ROOT,
    fetch: Fetch = fetch_url,
    now: Optional[datetime] = None,
) -> int:
    """
    Report the vendor binaries' newer releases; with *write*, put them in
    the Dockerfile, and with *message*, write the commit message there.
    """
    now = now or datetime.now(timezone.utc)
    path = root / BUILD_DOCKERFILE
    dockerfile = path.read_text(encoding="utf-8")

    updates = vendor_updates(dockerfile, fetch, now)
    if not updates:
        print("every vendor binary is at its newest release that is a week old")
        return 0

    for update in updates:
        print(f"{update['vendor'].repository}: {update['old']} -> {update['new']} ({update['published']:%Y-%m-%d})")

    if write:
        path.write_text(apply_updates(dockerfile, updates), encoding="utf-8")
    if message is not None:
        message.write_text(commit_message(updates), encoding="utf-8")

    return 0


def main(argv: list[str]) -> int:
    """
    The command line; see the module's docstring.
    """
    parser = argparse.ArgumentParser(prog="pins.py", description="What the repository pins, and how old it is.")
    commands = parser.add_subparsers(dest="command", required=True)

    command = commands.add_parser("ages", help="how old every pin that differs from BASE's is")
    command.add_argument("base")

    command = commands.add_parser("vendor", help="the build image's vendor binaries' newest week-old releases")
    command.add_argument("--write", action="store_true")
    command.add_argument("--message", type=Path)

    arguments = parser.parse_args(argv)

    try:
        if arguments.command == "ages":
            return ages(arguments.base)

        return vendor(arguments.write, arguments.message)

    except PinError as error:
        print(f"pins.py: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
