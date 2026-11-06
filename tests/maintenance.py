#!/usr/bin/env python3
"""
The scripts that keep dependencies moving (maintenance/): what the
repository pins, how old a pin is (against fake registries), the images'
Python environments, the build image's vendor binaries' newest releases a
week old, and which dependency updates merge themselves (against a fake
GitHub). Nothing here reaches the network.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import tomllib
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import harness
from harness import ROOT, Suite

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def load(name: str) -> types.ModuleType:
    """
    maintenance/NAME.py, as a module of its own name (registered while it
    loads: its dataclasses look themselves up there).
    """
    spec = importlib.util.spec_from_file_location(f"maintenance_{name}", ROOT / "maintenance" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    return module


pins = load("pins")
merge = load("merge")


def days_ago(days: float) -> str:
    """
    The time *days* before NOW, as the registries write it.
    """
    return (NOW - timedelta(days=days)).isoformat().replace("+00:00", "Z")


class FakeRegistries:
    """
    crates.io, PyPI, Docker Hub and GitHub's releases, answering from a
    table of URL to JSON (or text); any other URL is refused.
    """

    def __init__(self, answers: dict):
        self.answers = answers
        self.asked: list[str] = []

    def __call__(self, url: str, headers: dict) -> bytes:
        self.asked.append(url)

        if url not in self.answers:
            raise pins.PinError(f"{url}: 404")

        answer = self.answers[url]
        return answer.encode() if isinstance(answer, str) else json.dumps(answer).encode()


def crate_answer(days: float) -> dict:
    """
    crates.io's answer for a version published *days* ago.
    """
    return {"version": {"created_at": days_ago(days)}}


# ---------------------------------------------------------------------------
# What is pinned
# ---------------------------------------------------------------------------


def check_pins_found(suite: Suite) -> None:
    found = pins.pins_in(pins.reader_of(ROOT))
    kinds = {kind: {pin for pin in found if pin.kind == kind} for kind in ("crate", "pypi", "image", "release")}

    suite.check(
        f"the checkout's pins: {', '.join(f'{len(value)} {key}' for key, value in kinds.items())}",
        len(kinds["crate"]) > 300
        and {pin.name for pin in kinds["pypi"]}
        >= {"uv", "pyinstaller", "pefile", "ruff", "pyyaml", "flask", "gunicorn", "markupsafe"}
        and {pin.name for pin in kinds["image"]} >= {"coredns/coredns", "docker", "moby/buildkit"}
        and {pin.name for pin in kinds["release"]}
        == {
            "kubernetes-sigs/kind",
            "kubernetes/kubernetes",
            "helm/helm",
            "google/osv-scanner",
            "tauri-apps/tauri",
            "tauri-apps/binary-releases",
            "linuxdeploy/linuxdeploy-plugin-appimage",
            "rust-lang/rust",
        },
        "\n".join(sorted(f"{pin.kind} {pin.name} {pin.version}" for pin in found if pin.kind != "crate")),
    )

    text = """
    image: coredns/coredns:1.15.0@sha256:abc
    image: hardcode-squid:${HARDCODE_SQUID_TAG:-latest}
    image: hardcode-vscode:latest
  image:
    repository: hardcode-admin
FROM ubuntu:resolute-20261006@sha256:def AS base
FROM base
FROM ubuntu:${UBUNTU_VERSION}
"""
    suite.check(
        "an image is read with its tag and digest; ours, variables, stages and nested keys are not",
        pins.images(text)
        == {
            pins.Pin("image", "coredns/coredns", "1.15.0@sha256:abc"),
            pins.Pin("image", "ubuntu", "resolute-20261006@sha256:def"),
        },
        str(pins.images(text)),
    )

    text = "# pyyaml==6.0.3\nflask==3.1.3 \\\n    --hash=sha256:aaa \\\n    --hash=sha256:bbb\nruff==0.17.2\n"
    suite.check(
        "a Python package is read with its hashes or without, and not from a comment",
        pins.requirements(text) == {pins.Pin("pypi", "flask", "3.1.3"), pins.Pin("pypi", "ruff", "0.17.2")},
        str(pins.requirements(text)),
    )

    text = """
[[package]]
name = "hardcode-admin-image"
version = "0"
source = { virtual = "." }

[[package]]
name = "flask"
version = "3.1.3"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "local"
version = "1.0"
source = { path = "../local" }
"""

    def toolchain(channel: str) -> str:
        try:
            return str(pins.rust_toolchain(f'[toolchain]\nchannel = "{channel}"\n'))
        except pins.PinError as error:
            return str(error)

    suite.check(
        "the Rust toolchain is read as rust-lang/rust's release; a moving channel is refused",
        toolchain("1.99.1") == str({pins.Pin("release", "rust-lang/rust", "1.99.1")})
        and "not a Rust release" in toolchain("stable"),
        toolchain("stable"),
    )

    suite.check(
        "a uv.lock's packages from PyPI are read; the environment itself and other sources are not",
        pins.uv_lock(text) == {pins.Pin("pypi", "flask", "3.1.3")},
        str(pins.uv_lock(text)),
    )


def git(directory: Path, *arguments: str) -> None:
    """
    Run git in *directory*, as a throwaway identity.
    """
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *arguments],
        cwd=directory,
        check=True,
        capture_output=True,
    )


def check_ages(suite: Suite) -> None:
    registries = FakeRegistries(
        {
            "https://crates.io/api/v1/crates/serde/1.0.300": crate_answer(30),
            "https://crates.io/api/v1/crates/fresh/0.2.0": crate_answer(2),
            "https://pypi.org/pypi/ruff/0.17.3/json": {"urls": [{"upload_time_iso_8601": days_ago(9)}]},
            # An old version's sdist, and a wheel uploaded for it this week.
            "https://pypi.org/pypi/flask/3.1.3/json": {
                "urls": [{"upload_time_iso_8601": days_ago(8)}, {"upload_time_iso_8601": days_ago(1)}]
            },
            "https://pypi.org/pypi/gunicorn/26.2.0/json": {
                "urls": [{"upload_time_iso_8601": days_ago(8)}, {"upload_time_iso_8601": days_ago(7)}]
            },
            "https://hub.docker.com/v2/repositories/library/ubuntu/tags/resolute-20261006": {
                "digest": "sha256:aaa",
                "tag_last_pushed": days_ago(15),
            },
            "https://hub.docker.com/v2/repositories/coredns/coredns/tags/1.16.0": {
                "digest": "sha256:new",
                "tag_last_pushed": days_ago(20),
            },
            "https://api.github.com/repos/helm/helm/releases/tags/v4.3.1": {"published_at": days_ago(17)},
            "https://api.github.com/repos/kubernetes-sigs/kind/releases/tags/v0.34.0": {
                "published_at": days_ago(30),
                "assets": [
                    {"name": "kind-linux-amd64", "created_at": days_ago(30), "updated_at": days_ago(30)},
                    {"name": "kind-linux-arm64", "created_at": days_ago(2), "updated_at": days_ago(2)},
                ],
            },
        }
    )

    def published(pin):
        try:
            return pins.published(pin, registries)
        except pins.PinError as error:
            return str(error)

    suite.check(
        "each kind of pin is dated where it was published",
        published(pins.Pin("crate", "serde", "1.0.300")) == NOW - timedelta(days=30)
        and published(pins.Pin("pypi", "ruff", "0.17.3")) == NOW - timedelta(days=9)
        and published(pins.Pin("image", "ubuntu", "resolute-20261006@sha256:aaa")) == NOW - timedelta(days=15)
        and published(pins.Pin("release", "helm/helm", "v4.3.1")) == NOW - timedelta(days=17),
    )
    suite.check(
        "a GitHub release is as old as its newest file: one uploaded again this week counts from then",
        published(pins.Pin("release", "kubernetes-sigs/kind", "v0.34.0")) == NOW - timedelta(days=2),
    )
    suite.check(
        "so is a PyPI version: an old sdist doesn't vouch for a wheel uploaded yesterday",
        published(pins.Pin("pypi", "flask", "3.1.3")) == NOW - timedelta(days=1)
        and published(pins.Pin("pypi", "gunicorn", "26.2.0")) == NOW - timedelta(days=7),
    )
    suite.check(
        "a digest the tag has moved on from can't be dated",
        "its age can't be told" in published(pins.Pin("image", "coredns/coredns", "1.16.0@sha256:old")),
    )
    suite.check(
        "nor an image from another registry",
        "isn't on Docker Hub" in published(pins.Pin("image", "ghcr.io/someone/thing", "1")),
    )

    # A pull request, as a git repository: what it changes against its base.
    with tempfile.TemporaryDirectory(prefix="hc-pins-") as temporary:
        root = Path(temporary)
        (root / "app/src-tauri").mkdir(parents=True)
        (root / "cli").mkdir(parents=True)

        lock = '[[package]]\nname = "serde"\nversion = "1.0.200"\nsource = "{source}"\n'
        (root / pins.CARGO_LOCK).write_text(lock.format(source=pins.CRATES_IO), encoding="utf-8")
        (root / "cli/requirements.txt").write_text("ruff==0.17.3\n", encoding="utf-8")
        git(root, "init", "-q", "-b", "main")
        git(root, "add", ".")
        git(root, "commit", "-q", "-m", "base")

        # An old crate moves: accepted.
        (root / pins.CARGO_LOCK).write_text(
            lock.replace("1.0.200", "1.0.300").format(source=pins.CRATES_IO),
            encoding="utf-8",
        )
        quiet = lambda seconds: None  # noqa: E731 - the fake registries don't ask for a pace
        accepted = pins.ages("main", root, registries, NOW, quiet)

        # And with it, a two-day-old one (a transitive crate, say): refused.
        (root / pins.CARGO_LOCK).write_text(
            (
                lock.replace("1.0.200", "1.0.300") + '\n[[package]]\nname = "fresh"\nversion = "0.2.0"\n'
                'source = "{source}"\n'
            ).format(source=pins.CRATES_IO),
            encoding="utf-8",
        )
        refused = pins.ages("main", root, registries, NOW, quiet)

        unchanged = registries.asked.count("https://pypi.org/pypi/ruff/0.17.3/json")
        suite.check(
            "only the pins a change brings are aged: a week old passes, two days old is refused",
            (accepted, refused) == (0, 1) and unchanged == 1,
            f"accepted={accepted} refused={refused}; asked {registries.asked}",
        )

    # The same versions, with other files: a uv.lock that takes a new wheel of
    # an old version, a requirements file whose hashes change, and a
    # change that moves nothing (a comment, the order of the lines).
    with tempfile.TemporaryDirectory(prefix="hc-pins-") as temporary:
        root = Path(temporary)
        (root / "cli").mkdir(parents=True)
        (root / "images/admin").mkdir(parents=True)

        uv_lock = (
            'version = 1\n\n[[package]]\nname = "{name}"\nversion = "{version}"\n'
            'source = {{ registry = "{registry}" }}\n'
            'sdist = {{ url = "https://files.example/{name}.tar.gz", hash = "sha256:sdist" }}\n'
            "wheels = [\n{wheels}]\n"
        )
        wheel = '    {{ url = "https://files.example/{name}-{tag}.whl", hash = "sha256:{tag}" }},\n'

        def lock(name: str, version: str, *tags: str) -> str:
            wheels = "".join(wheel.format(name=name, tag=tag) for tag in tags)
            return uv_lock.format(name=name, version=version, registry=pins.PYPI, wheels=wheels)

        (root / "images/admin/uv.lock").write_text(lock("flask", "3.1.3", "old"), encoding="utf-8")
        requirement = "gunicorn==26.2.0 \\\n    --hash=sha256:{first} \\\n    --hash=sha256:{second}\n"
        (root / "cli/requirements.txt").write_text(requirement.format(first="a", second="b"), encoding="utf-8")
        git(root, "init", "-q", "-b", "main")
        git(root, "add", ".")
        git(root, "commit", "-q", "-m", "base")

        quiet = lambda seconds: None  # noqa: E731 - the fake registries don't ask for a pace

        # A comment and the hashes' order: nothing a pin's files are changes.
        (root / "cli/requirements.txt").write_text(
            "# pinned\n" + requirement.format(first="b", second="a"),
            encoding="utf-8",
        )
        asked = len(registries.asked)
        moved_nothing = pins.ages("main", root, registries, NOW, quiet)
        suite.check(
            "a change that leaves every pin's files as they were dates nothing",
            moved_nothing == 0 and len(registries.asked) == asked,
            str(registries.asked[asked:]),
        )

        # The same version of flask, now with yesterday's wheel: refused.
        (root / "images/admin/uv.lock").write_text(lock("flask", "3.1.3", "old", "new"), encoding="utf-8")
        new_wheel = pins.ages("main", root, registries, NOW, quiet)

        # The same version of gunicorn with another hash, all its files a
        # week old: aged, and accepted.
        (root / "images/admin/uv.lock").write_text(lock("flask", "3.1.3", "old"), encoding="utf-8")
        (root / "cli/requirements.txt").write_text(requirement.format(first="a", second="c"), encoding="utf-8")
        asked = len(registries.asked)
        new_hash = pins.ages("main", root, registries, NOW, quiet)
        suite.check(
            "the same version with other files is aged as a new one: a wheel uploaded yesterday is refused",
            new_wheel == 1,
            f"new_wheel={new_wheel}",
        )
        suite.check(
            "...and a hash changed for a version a week old passes",
            new_hash == 0 and registries.asked[asked:] == ["https://pypi.org/pypi/gunicorn/26.2.0/json"],
            f"new_hash={new_hash}; asked {registries.asked[asked:]}",
        )

    # CI's own check, on the runs that bring pins: pull requests, and the
    # vendor binaries' pull request, whose CI its workflow starts by hand.
    job = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))["jobs"]["pin-ages"]
    command = job["steps"][-1]["run"]
    suite.check(
        "CI ages the pins of a pull request, and of a run started by hand on another branch, against main",
        "github.event_name == 'pull_request'" in job["if"]
        and "github.event_name == 'workflow_dispatch'" in job["if"]
        and "github.ref != 'refs/heads/main'" in job["if"]
        and 'maintenance/pins.py ages "origin/${GITHUB_BASE_REF:-main}"' in command,
        f"{job['if']}\n{command}",
    )


# ---------------------------------------------------------------------------
# The lock files, refreshed
# ---------------------------------------------------------------------------


def check_locks(suite: Suite) -> None:
    suite.check(
        "a step in a lock is told apart: patch, minor, major, a package added, a version that can't be read, "
        "and one going backwards",
        [
            pins.version_update("1.5.2", "1.5.3"),
            pins.version_update("6.0.1", "6.2.0"),
            pins.version_update("3.1.3", "4.0.0"),
            pins.version_update("", "1.0.3"),
            pins.version_update("2026.9", "2026.10"),
            pins.version_update("1.0", "latest"),
            pins.version_update("1.14.0", "1.13.3"),
            pins.version_update("2.0.0", "1.9.9"),
        ]
        == [
            "version-update:semver-patch",
            "version-update:semver-minor",
            "version-update:semver-major",
            "version-update:semver-minor",
            "version-update:semver-minor",
            "version-update:semver-major",
            pins.DOWNGRADE,
            pins.DOWNGRADE,
        ],
    )

    old = {pins.Pin("crate", "brotli", "9.0.0"), pins.Pin("crate", "windows-sys", "0.52.0")}
    old |= {pins.Pin("crate", "windows-sys", "0.59.0"), pins.Pin("crate", "gone", "1.0.0")}
    new = {pins.Pin("crate", "brotli", "9.1.0"), pins.Pin("crate", "windows-sys", "0.52.0")}
    new |= {pins.Pin("crate", "windows-sys", "0.60.2"), pins.Pin("crate", "added", "0.3.1")}
    changes = pins.lock_changes(old, new)
    suite.check(
        "...and each package's change is read from two versions of a lock, two versions of one crate included",
        changes
        == [
            {"name": "added", "old": "", "new": "0.3.1"},
            {"name": "brotli", "old": "9.0.0", "new": "9.1.0"},
            {"name": "gone", "old": "1.0.0", "new": ""},
            {"name": "windows-sys", "old": "0.59.0", "new": "0.60.2"},
        ],
        str(changes),
    )

    cargo = '[[package]]\nname = "{name}"\nversion = "{version}"\nsource = "{source}"\n'
    uv = '[[package]]\nname = "{name}"\nversion = "{version}"\nsource = {{ registry = "https://pypi.org/simple" }}\n'
    with tempfile.TemporaryDirectory(prefix="hc-locks-") as temporary:
        root = Path(temporary)
        (root / "app/src-tauri").mkdir(parents=True)
        (root / "images/admin").mkdir(parents=True)
        (root / pins.CARGO_LOCK).write_text(
            cargo.format(name="brotli", version="9.0.0", source=pins.CRATES_IO), encoding="utf-8"
        )
        (root / "images/admin/uv.lock").write_text(uv.format(name="flask", version="3.1.3"), encoding="utf-8")
        git(root, "init", "-q", "-b", "main")
        git(root, "add", ".")
        git(root, "commit", "-q", "-m", "base")

        message = root / "message.txt"
        unchanged = pins.locks("main", message, root)
        unchanged_written = message.exists()

        (root / pins.CARGO_LOCK).write_text(
            cargo.format(name="brotli", version="9.1.0", source=pins.CRATES_IO), encoding="utf-8"
        )
        (root / "images/admin/uv.lock").write_text(uv.format(name="flask", version="4.0.0"), encoding="utf-8")
        code = pins.locks("main", message, root)
        text = message.read_text(encoding="utf-8")

    suite.check(
        "the refresh's commit message: nothing when the locks are as they were; else a line per package, and "
        "Dependabot's metadata merge.py reads (a major left for a person)",
        (unchanged, unchanged_written, code) == (0, False, 0)
        and text.startswith("build(deps): refresh the lock files, 2 packages\n")
        and "- brotli 9.0.0 -> 9.1.0\n" in text
        and "- flask 3.1.3 -> 4.0.0\n" in text
        and merge.needs_a_person({"head": {"ref": "pins/locks"}}, [commit_of(text, "github-actions[bot]")])
        == "a major update (4.0.0)",
        text,
    )

    # A package taken back to an older version (its version withdrawn, the
    # next not a week old): the proposal says so, and waits for a person.
    backwards = pins.locks_message(
        {
            "app/src-tauri/Cargo.lock": [
                {"name": "brotli", "old": "9.0.0", "new": "9.1.0"},
                {"name": "async-executor", "old": "1.14.0", "new": "1.13.3"},
            ]
        }
    )
    suite.check(
        "a package the refresh takes backwards is marked in the proposal, and leaves it for a person",
        "- async-executor 1.14.0 -> 1.13.3 (backwards: waits for a person)\n" in backwards
        and "- brotli 9.0.0 -> 9.1.0\n" in backwards
        and merge.needs_a_person({"head": {"ref": "pins/locks"}}, [commit_of(backwards, "github-actions[bot]")])
        == "a version going backwards (1.13.3)",
        backwards,
    )

    # The workflow: a refresh that only reads, and a job that takes only the
    # three lock files from it.
    jobs = yaml.safe_load((ROOT / ".github/workflows/dependencies.yml").read_text(encoding="utf-8"))["jobs"]
    refresh, propose = jobs["locks"], jobs["locks-propose"]
    runs = "\n".join(item.get("run", "") for item in refresh["steps"])
    crates = next(item for item in refresh["steps"] if item.get("name") == "The crates")
    copied = next(item for item in propose["steps"] if item.get("name") == "Propose them")["run"]
    config = tomllib.loads((ROOT / "app/src-tauri/.cargo/config.toml").read_text(encoding="utf-8"))
    suite.check(
        "the nightly refresh only reads: Cargo as rust-toolchain.toml pins it, with a week's min-publish-age, "
        "and uv without a cache",
        "permissions" not in refresh
        and config["registry"]["global-min-publish-age"] == "7 days"
        and crates["run"] == "cargo -Zmin-publish-age update"
        and crates["env"] == {"RUSTC_BOOTSTRAP": "1"}
        and "app/src-tauri/rust-toolchain.toml" in runs
        and "uv lock --upgrade --project images/admin" in runs
        and "uv lock --upgrade --project images/build" in runs
        and {"uses": "./.github/actions/build-image", "with": {"cache": False}} in refresh["steps"],
        runs,
    )
    suite.check(
        "...and the job that proposes it takes only the three lock files, by name, and runs no third-party action",
        "for file in app/src-tauri/Cargo.lock images/admin/uv.lock images/build/uv.lock; do" in copied
        and all(item.get("uses", "actions/").startswith("actions/") for item in propose["steps"])
        and set(merge.PINS_FILES["pins/locks"])
        == {"app/src-tauri/Cargo.lock", "images/admin/uv.lock", "images/build/uv.lock"},
        copied,
    )


# ---------------------------------------------------------------------------
# The Python environments
# ---------------------------------------------------------------------------


def check_python_environments(suite: Suite) -> None:
    pin = (ROOT / "images/build/uv/requirements.txt").read_text(encoding="utf-8")
    hashed = re.compile(r"^uv==\S+ \\\n(?:    --hash=sha256:[0-9a-f]{64} \\\n)*    --hash=sha256:[0-9a-f]{64}$", re.M)
    suite.check(
        "uv is pinned with its files' hashes, alone in its file",
        len(hashed.findall(pin)) == 1 and {found.name for found in pins.requirements(pin)} == {"uv"},
        pin,
    )

    for directory in ("images/admin", "images/build"):
        project = tomllib.loads((ROOT / directory / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["uv"]
        locked = tomllib.loads((ROOT / directory / "uv.lock").read_text(encoding="utf-8"))

        # Every package but the environment itself, from PyPI, each with
        # wheels and a hash for every one.
        packages = [package for package in locked["package"] if "virtual" not in package["source"]]
        unhashed = [
            package["name"]
            for package in packages
            if package["source"].get("registry") != pins.PYPI
            or not package.get("wheels")
            or not all(wheel.get("hash", "").startswith("sha256:") for wheel in package["wheels"])
        ]
        suite.check(
            f"{directory}: an environment of packages a week old, each from PyPI with its wheels' hashes",
            project.get("package") is False
            and project.get("exclude-newer") == "1 week"
            and locked["options"].get("exclude-newer-span") == "P1W"
            and len(packages) == len(locked["package"]) - 1
            and not unhashed,
            f"not from PyPI with hashed wheels: {unhashed}",
        )

        checked = harness.run(["uv", "lock", "--check", "--offline", "--project", str(ROOT / directory)], cwd=ROOT)
        suite.check(f"{directory}: uv.lock is what pyproject.toml asks for", checked.ok, checked.text)

    for dockerfile, project in (
        ("images/admin/Dockerfile", "/tmp/admin-python"),
        ("images/build/Dockerfile", "/tmp/build-python"),
    ):
        text = (ROOT / dockerfile).read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        archive_python = set(re.findall(r"\bpython3-[a-z0-9.+-]+", code)) - {"python3-venv"}
        suite.check(
            f"{dockerfile} installs uv by hash, then its lock with uv: wheels only, on the archive's Python, "
            "beside none of the archive's Python libraries",
            "--require-hashes" in code
            and "-r /tmp/uv-requirements.txt" in code
            and f"--project {project}" in code
            and "--locked" in code
            and "--no-build" in code
            and "--python /usr/bin/python3" in code
            and "UV_PYTHON_DOWNLOADS=never" in code
            and not archive_python,
            f"from the archive: {sorted(archive_python)}",
        )

    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    python = (ROOT / ".github/actions/python/action.yml").read_text(encoding="utf-8")
    suite.check(
        "the release's macOS and Windows builds install uv by hash, then uv's Python and the build image's lock, "
        "wheels only",
        release.count("uses: ./.github/actions/python") == 2
        and "setup-python" not in release
        and "pip install -r" not in release
        and "--require-hashes" in python
        and "-r images/build/uv/requirements.txt" in python
        and "--project images/build" in python
        and "--locked" in python
        and "--no-build" in python
        and "--managed-python" in python,
    )

    updates = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text(encoding="utf-8"))["updates"]
    directories = {
        update["package-ecosystem"]: set(update.get("directories") or [update.get("directory")]) for update in updates
    }
    cooldowns = {update["package-ecosystem"]: update["cooldown"]["default-days"] for update in updates}
    ignored = {
        update["package-ecosystem"]: {rule["dependency-name"] for rule in update.get("ignore") or []}
        for update in updates
    }
    suite.check(
        "Dependabot moves uv's pin and both locks, a week after a release",
        "/images/build/uv" in directories["pip"]
        and directories["uv"] == {"/images/admin", "/images/build"}
        and cooldowns["pip"] == cooldowns["uv"] == 7,
        str(directories),
    )
    suite.check(
        "...and the macOS and Windows builds' Rust toolchain, a week after a release",
        directories.get("rust-toolchain") == {"/app/src-tauri"} and cooldowns.get("rust-toolchain") == 7,
        str(directories),
    )
    suite.check(
        "...uv's pin by the pip entry alone: the uv entry, whose /images/build holds it too, leaves it out",
        "uv" in ignored["uv"] and "uv" not in ignored["pip"],
        str(ignored),
    )


# ---------------------------------------------------------------------------
# The build image's vendor binaries
# ---------------------------------------------------------------------------


def release(tag: str, days: float, assets: list | None = None, prerelease: bool = False) -> dict:
    """
    A GitHub release, as the releases API lists it.
    """
    return {
        "tag_name": tag,
        "published_at": days_ago(days),
        "draft": False,
        "prerelease": prerelease,
        "assets": assets or [],
    }


def check_vendor(suite: Suite) -> None:
    def releases(repository: str, items: list) -> dict:
        page = f"https://api.github.com/repos/{repository}/releases?per_page=100&page="
        return {f"{page}1": items, f"{page}2": []}

    answers = {}
    answers.update(
        releases(
            "kubernetes-sigs/kind",
            [
                release(
                    "v0.34.0",
                    30,
                    [{"name": "kind-linux-amd64", "digest": "sha256:" + "d" * 64, "updated_at": days_ago(2)}],
                ),
                release("v0.33.0", 60),
            ],
        )
    )
    answers.update(releases("kubernetes/kubernetes", [release("v1.37.1", 40)]))
    answers.update(releases("google/osv-scanner", [release("v2.7.0", 20)]))
    answers.update(
        releases(
            "helm/helm",
            [
                release("v4.4.0", 2),
                release("v4.3.2-rc.1", 12, prerelease=True),
                release("v4.3.1", 17),
                release("v4.3.0", 50),
            ],
        )
    )
    answers.update(
        releases(
            "tauri-apps/tauri",
            [
                release("tauri-v2.13.0", 9),
                release(
                    "tauri-cli-v2.13.0",
                    9,
                    [{"name": "cargo-tauri-x86_64-unknown-linux-gnu.tgz", "digest": "sha256:" + "c" * 64}],
                ),
                release("tauri-cli-v2.12.2", 30),
            ],
        )
    )
    answers["https://get.helm.sh/helm-v4.3.1-linux-amd64.tar.gz.sha256sum"] = (
        "a" * 64 + "  helm-v4.3.1-linux-amd64.tar.gz\n"
    )
    answers["https://get.helm.sh/helm-v4.3.1-linux-arm64.tar.gz.sha256sum"] = (
        "b" * 64 + "  helm-v4.3.1-linux-arm64.tar.gz\n"
    )
    registries = FakeRegistries(answers)

    with tempfile.TemporaryDirectory(prefix="hc-vendor-") as temporary:
        root = Path(temporary)
        (root / "images/build").mkdir(parents=True)
        dockerfile = root / pins.BUILD_DOCKERFILE
        dockerfile.write_text(
            "\n".join(
                [
                    "ARG KIND_VERSION=0.33.0",
                    "ARG KIND_SHA256_AMD64=old",
                    "ARG KIND_SHA256_ARM64=old",
                    "ARG KUBECTL_VERSION=1.37.1",
                    "ARG KUBECTL_SHA256_AMD64=old",
                    "ARG KUBECTL_SHA256_ARM64=old",
                    "ARG HELM_VERSION=4.3.0",
                    "ARG HELM_SHA256_AMD64=old",
                    "ARG HELM_SHA256_ARM64=old",
                    "ARG OSV_SCANNER_VERSION=2.7.0",
                    "ARG OSV_SCANNER_SHA256_AMD64=old",
                    "ARG OSV_SCANNER_SHA256_ARM64=old",
                    "ARG TAURI_CLI_VERSION=2.12.2",
                    "ARG TAURI_CLI_SHA256_AMD64=old",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        message = root / "message.txt"

        code = pins.vendor(True, message, root, registries, NOW)
        text = dockerfile.read_text(encoding="utf-8")
        suite.check(
            "each binary moves to its newest release a week old: not a younger one, a prerelease, another "
            "package's, or one with a file uploaded again this week",
            code == 0
            and "ARG HELM_VERSION=4.3.1\n" in text
            and "ARG TAURI_CLI_VERSION=2.13.0\n" in text
            and "ARG KIND_VERSION=0.33.0\n" in text,
            text,
        )
        suite.check(
            "...with its publisher's checksums: GitHub's asset digest, or the file beside the download",
            f"ARG HELM_SHA256_AMD64={'a' * 64}\n" in text
            and f"ARG HELM_SHA256_ARM64={'b' * 64}\n" in text
            and f"ARG TAURI_CLI_SHA256_AMD64={'c' * 64}\n" in text
            and "ARG KIND_SHA256_AMD64=old\n" in text,
            text,
        )

        written = message.read_text(encoding="utf-8")
        suite.check(
            "...and a commit message in Conventional Commits form, with Dependabot's metadata",
            written.startswith("build(deps): bump the build image's helm, tauri-cli\n")
            and "update-type: version-update:semver-patch" in written
            and "update-type: version-update:semver-minor" in written
            and merge.needs_a_person(pins_pull(), [commit_of(written, "github-actions[bot]")]) == "",
            written,
        )

        again = pins.vendor(True, None, root, registries, NOW)
        suite.check(
            "nothing newer: the Dockerfile is left as it is",
            again == 0 and dockerfile.read_text(encoding="utf-8") == text,
        )


# ---------------------------------------------------------------------------
# Which updates merge themselves
# ---------------------------------------------------------------------------

REPOSITORY = "owner/repo"


def pins_pull(number: int = 1, ref: str = "pins/vendor-binaries", login: str = "github-actions[bot]") -> dict:
    """
    An open pull request, as GitHub lists it.
    """
    repository = {"full_name": REPOSITORY}

    return {
        "number": number,
        "user": {"login": login},
        "head": {"ref": ref, "sha": f"sha{number}", "repo": repository},
        "base": {"ref": "main", "repo": repository},
        "updated_at": days_ago(1),
    }


def commit_of(message: str, login: str = "dependabot[bot]", days: float = 1) -> dict:
    """
    A pull request's commit, as GitHub lists it.
    """
    return {"author": {"login": login}, "commit": {"message": message, "committer": {"date": days_ago(days)}}}


def metadata(*updates: tuple[str, str]) -> str:
    """
    A Dependabot commit message with an update per (version, update type).
    """
    lines = ["build(deps): bump things", "", "---", "updated-dependencies:"]
    for version, kind in updates:
        lines += [
            "- dependency-name: thing",
            f"  dependency-version: {version}",
            f"  update-type: version-update:semver-{kind}",
        ]

    return "\n".join(lines) + "\n...\n"


class FakeGitHub:
    """
    GitHub's API for one repository, from a table of GET paths to answers;
    what is sent otherwise is recorded, and a path in *refuse* is refused.
    """

    def __init__(self, answers: dict, refuse: tuple = ()):
        self.answers = answers
        self.refuse = refuse
        self.sent: list[tuple[str, str, object]] = []

    def __call__(self, method: str, path: str, body):
        if path in self.refuse:
            raise merge.GitHubError(f"{method} {path}: 405 not mergeable")

        if method != "GET":
            self.sent.append((method, path, body))
            return None

        return self.answers[path]


def world(pulls: list[dict], per_pull: dict) -> dict:
    """
    The GET answers for *pulls*: each one's commits, comparison with main,
    CI runs and their jobs, comments and changed files, from *per_pull*.
    """
    base = f"/repos/{REPOSITORY}"
    answers = {f"{base}/pulls?state=open&per_page=100": pulls}

    for pull in pulls:
        number = pull["number"]
        details = per_pull.get(number, {})
        head = pull["head"]["sha"]
        run = details.get("run")
        runs = details.get("runs") or ([run] if run else [])

        answers[f"{base}/pulls/{number}/commits?per_page=100"] = details.get("commits", [])
        answers[f"{base}/compare/main...{head}"] = {"behind_by": details.get("behind", 0)}
        answers[f"{base}/actions/workflows/ci.yml/runs?head_sha={head}&per_page=20"] = {"workflow_runs": runs}
        answers[f"{base}/issues/{number}/comments?per_page=100"] = details.get("comments", [])
        answers[f"{base}/pulls/{number}/files?per_page=100"] = [{"filename": name} for name in details.get("files", [])]
        for item in runs:
            answers[f"{base}/actions/runs/{item['id']}/jobs?per_page=100"] = {"jobs": details.get("jobs", [])}

    return answers


def ci_run(number: int, conclusion: str = "success", status: str = "completed", finished: float = 0.1) -> dict:
    """
    A finished CI run, as GitHub lists it.
    """
    return {
        "id": 1000 + number,
        "status": status,
        "conclusion": conclusion,
        "created_at": days_ago(finished + 0.05),
        "updated_at": days_ago(finished),
        "run_attempt": 1,
    }


def check_merging(suite: Suite) -> None:
    patch = metadata(("1.2.4", "patch"))
    dependabot = "dependabot[bot]"
    pulls = [
        pins_pull(1, "dependabot/cargo/app", dependabot),
        pins_pull(2, "dependabot/github_actions/x", dependabot),
        pins_pull(3, "dependabot/pip/y", dependabot),
        pins_pull(4, "dependabot/images-1", dependabot),
        pins_pull(5, "dependabot/cargo/z", dependabot),
        pins_pull(6, "dependabot/pip/behind", dependabot),
        pins_pull(7, "dependabot/pip/asked", dependabot),
        pins_pull(8, "dependabot/cargo/young", dependabot),
        pins_pull(9, "dependabot/cargo/young-again", dependabot),
        pins_pull(10, "dependabot/cargo/broken", dependabot),
        pins_pull(11, "dependabot/cargo/running", dependabot),
        pins_pull(12, "pins/vendor-binaries", "github-actions[bot]"),
        pins_pull(13, "feature/someones", "a-person"),
        pins_pull(14, "dependabot/cargo/conflict", dependabot),
        pins_pull(15, "pins/locks", "github-actions[bot]"),
        pins_pull(16, "pins/locks", "github-actions[bot]"),
        pins_pull(17, "pins/locks", "github-actions[bot]"),
    ]
    pin_ages_passed = [{"name": "Pin ages", "conclusion": "success"}, {"name": "CLI", "conclusion": "success"}]
    pin_ages_skipped = [{"name": "Pin ages", "conclusion": "skipped"}, {"name": "CLI", "conclusion": "success"}]
    pin_ages_failed = [{"name": "Pin ages", "conclusion": "failure"}, {"name": "CLI", "conclusion": "success"}]
    per_pull = {
        1: {"commits": [commit_of(patch)], "run": ci_run(1), "jobs": pin_ages_passed},
        2: {"commits": [commit_of(patch)], "run": ci_run(2)},
        3: {"commits": [commit_of(metadata(("3.0.0", "major")))], "run": ci_run(3)},
        4: {
            "commits": [commit_of(metadata(("resolute-20261024.1", "major"), ("1.16.0", "minor")))],
            "run": ci_run(4),
            "jobs": pin_ages_passed,
        },
        5: {"commits": [commit_of(patch), commit_of("fix: my own change", "a-person")], "run": ci_run(5)},
        6: {"commits": [commit_of(patch, days=2)], "behind": 3, "run": ci_run(6)},
        7: {
            "commits": [commit_of(patch, days=2)],
            "behind": 1,
            "comments": [{"body": "@dependabot rebase", "created_at": days_ago(1)}],
        },
        8: {"commits": [commit_of(patch)], "run": ci_run(8, "failure", finished=1), "jobs": pin_ages_failed},
        9: {"commits": [commit_of(patch)], "run": ci_run(9, "failure", finished=0.2), "jobs": pin_ages_failed},
        10: {
            "commits": [commit_of(patch)],
            "run": ci_run(10, "failure"),
            "jobs": pin_ages_failed + [{"name": "Desktop app (Tauri)", "conclusion": "failure"}],
        },
        11: {"commits": [commit_of(patch)], "run": ci_run(11, "in_progress", status="in_progress")},
        12: {
            "commits": [commit_of(metadata(("4.3.1", "patch")), "github-actions[bot]")],
            "behind": 2,
            "files": ["images/build/Dockerfile"],
        },
        14: {"commits": [commit_of(patch)], "run": ci_run(14), "jobs": pin_ages_passed},
        15: {
            "commits": [commit_of(metadata(("1.5.3", "patch")), "github-actions[bot]")],
            "run": ci_run(15),
            "jobs": pin_ages_skipped,
            "files": ["app/src-tauri/Cargo.lock"],
        },
        16: {
            "commits": [commit_of(metadata(("1.5.3", "patch")), "github-actions[bot]")],
            "run": ci_run(16),
            "jobs": pin_ages_passed,
            "files": ["app/src-tauri/Cargo.lock", ".github/workflows/ci.yml"],
        },
        # The run its job started, then the one GitHub holds for approval.
        17: {
            "commits": [commit_of(metadata(("1.5.3", "patch")), "github-actions[bot]")],
            "runs": [ci_run(17, finished=0.2), {**ci_run(117, "action_required", finished=0.1), "run_attempt": 1}],
            "jobs": pin_ages_passed,
            "files": ["app/src-tauri/Cargo.lock"],
        },
    }

    github = FakeGitHub(world(pulls, per_pull), refuse=(f"/repos/{REPOSITORY}/pulls/14/merge",))
    decisions = {decision.number: decision for decision in merge.Merger(REPOSITORY, github, NOW).run()}
    actions = {number: decision.action for number, decision in decisions.items()}
    detail = "\n".join(f"#{n}: {d.action}, {d.reason}" for n, d in sorted(decisions.items()))

    def sent(method: str, path: str) -> bool:
        return any(item[:2] == (method, f"/repos/{REPOSITORY}{path}") for item in github.sent)

    suite.check(
        "a green patch update, up to date with main, is merged by rebase at the commit CI ran",
        actions[1] == "merged"
        and ("PUT", f"/repos/{REPOSITORY}/pulls/1/merge", {"merge_method": "rebase", "sha": "sha1"}) in github.sent,
        detail,
    )
    suite.check(
        "an action, a major update, or a person's commit waits for a person",
        actions[2] == "left" and actions[3] == "left" and actions[5] == "left" and not sent("PUT", "/pulls/3/merge"),
        detail,
    )
    suite.check(
        "a newer dated build of an image (Ubuntu's resolute-YYYYMMDD) isn't taken for a major update",
        actions[4] == "merged",
        detail,
    )
    suite.check(
        "behind main: Dependabot is asked to rebase, once for each newest commit",
        actions[6] == "rebasing"
        and ("POST", f"/repos/{REPOSITORY}/issues/6/comments", {"body": "@dependabot rebase"}) in github.sent
        and actions[7] == "waiting"
        and not sent("POST", "/issues/7/comments"),
        detail,
    )
    suite.check(
        "a pin-age check that was all that failed runs again a day later, not before",
        actions[8] == "retried"
        and sent("POST", "/actions/runs/1008/rerun-failed-jobs")
        and actions[9] == "waiting"
        and not sent("POST", "/actions/runs/1009/rerun-failed-jobs"),
        detail,
    )
    suite.check(
        "anything else that failed is left; a running CI is waited for",
        actions[10] == "left" and "Desktop app" in decisions[10].reason and actions[11] == "waiting",
        detail,
    )
    suite.check(
        "the vendor binaries' pull request behind main waits for its workflow to make it afresh",
        actions[12] == "waiting" and not sent("POST", "/issues/12/comments"),
        detail,
    )
    suite.check(
        "a green run whose pin-age check didn't run merges nothing",
        actions[15] == "left" and "ages" in decisions[15].reason and not sent("PUT", "/pulls/15/merge"),
        detail,
    )
    suite.check(
        "the run GitHub holds for approval on our own jobs' pull requests is passed over for the one that ran",
        actions[17] == "merged" and sent("PUT", "/pulls/17/merge"),
        detail,
    )
    suite.check(
        "our own workflows' pull requests merge only when they change just what those workflows make",
        actions[16] == "left"
        and ".github/workflows/ci.yml" in decisions[16].reason
        and not sent("PUT", "/pulls/16/merge"),
        detail,
    )
    suite.check(
        "a person's pull request isn't touched; a refusal stays with its pull request",
        13 not in decisions and actions[14] == "left" and "405" in decisions[14].reason,
        detail,
    )


def run_checks(suite: Suite) -> None:
    suite.section("what is pinned")
    check_pins_found(suite)

    suite.section("how old a pin is")
    check_ages(suite)

    suite.section("the lock files, refreshed")
    check_locks(suite)

    suite.section("the Python environments")
    check_python_environments(suite)

    suite.section("the build image's vendor binaries")
    check_vendor(suite)

    suite.section("which updates merge themselves")
    check_merging(suite)


if __name__ == "__main__":
    harness.main("maintenance", run_checks)
