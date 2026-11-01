#!/usr/bin/env python3
"""
The steps of a release that aren't a build (release/release.py): the version
written into every file that carries it, a development build's mark, the
updater's configuration, the latest.json the app's updater reads (from
installers signed for real, with a throwaway key), the release's and the
development build's descriptions, and the checksums.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import harness
from harness import ROOT, Suite

REPOSITORY = "Example-Owner/hardcode"

release = harness.release_steps()


def copy_versioned(root: Path) -> None:
    """
    Copy every file that carries the version into *root*, at its own path.
    """
    for name, _ in release.VERSIONED:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)


def check_versions(suite: Suite) -> None:
    found = release.versions()
    suite.check(
        f"every file carries the same version ({', '.join(sorted(set(found.values())))})",
        len(set(found.values())) == 1 and "" not in found.values(),
        "\n".join(f"{key}: {value!r}" for key, value in found.items()),
    )

    with tempfile.TemporaryDirectory(prefix="hc-stamp-") as temporary:
        root = Path(temporary)
        copy_versioned(root)
        before = {name: (root / name).read_text(encoding="utf-8") for name, _ in release.VERSIONED}

        changes = release.stamp("12.34.56-7", root)
        stamped = release.versions(root)
        suite.check(
            "stamp writes the version into every one of them",
            set(stamped.values()) == {"12.34.56-7"} and len(changes) == len(release.VERSIONED),
            "\n".join(changes),
        )

        # Nothing but the version changed: put the old one back, and the
        # files are as they were (Cargo.lock's other packages included).
        old = next(iter(found.values()))
        unchanged = all(
            (root / name).read_text(encoding="utf-8").replace("12.34.56-7", old) == before[name]
            for name, _ in release.VERSIONED
        )
        suite.check("...and nothing else", unchanged)

        config = json.loads((root / "app/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
        suite.check("...and tauri.conf.json is still JSON", config["version"] == "12.34.56-7", str(config)[:200])

    refused = []
    for version in ("1.2", "v1.2.3", "1.2.3-rc.1", "1.2.3 "):
        try:
            release.check_version(version)
        except release.ReleaseError:
            refused.append(version)
    suite.check(
        "a version a release can't carry is refused (MSI wants numbers)",
        len(refused) == 4,
        f"refused {refused}",
    )


def check_dev_mark(suite: Suite) -> None:
    suite.check(
        "the checkout isn't marked as a development build",
        release.build_mark() == "",
        repr(release.build_mark()),
    )

    with tempfile.TemporaryDirectory(prefix="hc-dev-") as temporary:
        root = Path(temporary)
        name, _ = release.BUILD_MARK
        (root / name).parent.mkdir(parents=True)
        shutil.copy2(ROOT / name, root / name)
        before = (root / name).read_text(encoding="utf-8")

        change = release.mark_dev("2c419ae", root)
        after = (root / name).read_text(encoding="utf-8")
        suite.check(
            "dev marks the build with its commit, and changes nothing else",
            release.build_mark(root) == "dev, 2c419ae" and after.replace('"dev, 2c419ae"', '""') == before,
            change,
        )

        # The marked module, loaded: what --version and the app show.
        spec = importlib.util.spec_from_file_location("marked_cli", root / name)
        marked = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(marked)
        suite.check(
            f"...so the command says {marked.version_text()!r}",
            marked.version_text() == f"{marked.__version__} (dev, 2c419ae)",
        )

        refused = []
        for commit in ("2C419AE", "2c419", "2c419ae; true", "", "g2c419ae"):
            try:
                release.mark_dev(commit, root)
            except release.ReleaseError:
                refused.append(commit)
        suite.check("a commit that isn't a hash is refused", len(refused) == 5, f"refused {refused}")


def check_updater_config(suite: Suite) -> None:
    with tempfile.TemporaryDirectory(prefix="hc-updater-") as temporary:
        key = Path(temporary) / "updater.pub"

        suite.check(
            "without release/updater.pub, a release has no updater",
            release.updater_config(REPOSITORY, key) == {},
        )

        key.write_text("dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk=\n", encoding="utf-8")
        config = release.updater_config(REPOSITORY, key)
        updater = config.get("plugins", {}).get("updater", {})

        suite.check(
            "with it, the updater trusts that key and only the newest release",
            updater.get("pubkey") == key.read_text(encoding="utf-8").strip()
            and updater.get("endpoints") == [f"https://github.com/{REPOSITORY}/releases/latest/download/latest.json"],
            json.dumps(config, indent=2),
        )
        suite.check(
            "...requires each release signed for its own version",
            updater.get("requireSignedVersion") is True,
            json.dumps(config, indent=2),
        )
        suite.check(
            "...and the build makes the signed update files",
            config.get("bundle") == {"createUpdaterArtifacts": True},
            json.dumps(config, indent=2),
        )
        suite.check(
            "...over HTTPS only (nothing dangerous set)",
            not any(name.startswith("dangerous") for name in updater),
            json.dumps(updater),
        )

        try:
            release.updater_config("not a repository", key)
            suite.check("a repository that isn't OWNER/REPO is refused", False)
        except release.ReleaseError as error:
            suite.check("a repository that isn't OWNER/REPO is refused", True, str(error))

    # The command line: what the release workflow runs.
    with tempfile.TemporaryDirectory(prefix="hc-updater-") as temporary:
        out = Path(temporary) / "tauri.updater.conf.json"
        output = harness.run(
            [sys.executable, ROOT / "release" / "release.py", "updater-config", "--repository", REPOSITORY, out]
        )
        expected = "no updater" if not release.UPDATER_KEY.exists() else "the updater, checking"
        suite.check(
            f"release.py updater-config: {output.text.strip()}",
            output.ok
            and expected in output.text
            and out.exists()
            and isinstance(json.loads(out.read_text(encoding="utf-8")), dict),
            output.text,
        )


def sign(key: Path, path: Path, version: str) -> harness.Output:
    """
    Sign *path* with *key* for *version*, as a release's build does: *path*.sig.
    """
    return harness.run(
        [
            "cargo",
            "tauri",
            "signer",
            "sign",
            "--private-key-path",
            key,
            "--password",
            "",
            "--app-version",
            version,
            path,
        ]
    )


def check_manifest(suite: Suite) -> None:
    version = "2.3.4"

    with tempfile.TemporaryDirectory(prefix="hc-manifest-") as temporary:
        directory = Path(temporary) / "dist"
        directory.mkdir()

        suite.check("nothing signed: no latest.json", release.manifest(version, REPOSITORY, directory) is None)

        # A throwaway key, and an installer of every kind signed with it.
        key = Path(temporary) / "throwaway.key"
        generated = harness.run(["cargo", "tauri", "signer", "generate", "--ci", "--password", "", "--write-keys", key])
        suite.require("a throwaway signing key", generated.ok and key.exists(), generated.text)

        names = [
            f"HardCode_{version}_amd64.AppImage",
            f"HardCode_{version}_amd64.deb",
            f"HardCode-{version}-1.x86_64.rpm",
            "HardCode_aarch64.app.tar.gz",
            f"HardCode_{version}_x64-setup.exe",
            f"HardCode_{version}_x64_en-US.msi",
        ]
        for name in names:
            (directory / name).write_bytes(f"the {name} installer".encode())
            signed = sign(key, directory / name, version)
            if not signed.ok:
                suite.abort(f"signing {name}", signed.text)
        (directory / "hardcode-linux-x64").write_bytes(b"not an installer")

        published = datetime(2026, 10, 13, 1, 2, 3, 456, tzinfo=timezone.utc)
        latest = release.manifest(version, REPOSITORY, directory, now=published)
        platforms = latest["platforms"]

        suite.check(
            "latest.json has every installer, and a default for each platform",
            sorted(platforms)
            == sorted([key for key, _ in release.INSTALLERS] + ["linux-x86_64", "darwin-aarch64", "windows-x86_64"]),
            json.dumps(sorted(platforms)),
        )
        suite.check(
            "...the defaults: the AppImage, the app, the -setup.exe",
            platforms["linux-x86_64"] == platforms["linux-x86_64-appimage"]
            and platforms["darwin-aarch64"] == platforms["darwin-aarch64-app"]
            and platforms["windows-x86_64"] == platforms["windows-x86_64-nsis"],
        )
        suite.check(
            "...each downloaded from the release",
            platforms["linux-x86_64-deb"]["url"]
            == f"https://github.com/{REPOSITORY}/releases/download/v{version}/HardCode_{version}_amd64.deb",
            platforms["linux-x86_64-deb"]["url"],
        )
        suite.check(
            "...with its own signature",
            all(
                platforms[key]["signature"] == (directory / f"{name}.sig").read_text(encoding="utf-8").strip()
                for (key, _), name in zip(release.INSTALLERS, names)
            ),
        )

        # The signature is base64 of minisign's text. Its trusted comment,
        # which the signature covers, is tab-separated fields, one of them
        # the version it was signed for.
        decoded = base64.b64decode(platforms["windows-x86_64-msi"]["signature"]).decode()
        trusted = [line for line in decoded.splitlines() if line.startswith("trusted comment: ")]
        fields = trusted[0][len("trusted comment: ") :].split("\t") if trusted else []
        suite.check(
            f"...signed for the release's version (requireSignedVersion has it to check): {version}",
            f"version:{version}" in fields,
            decoded,
        )
        suite.check(
            "...the version, its page as the notes, the time to the second",
            (latest["version"], latest["notes"], latest["pub_date"])
            == (version, f"https://github.com/{REPOSITORY}/releases/tag/v{version}", "2026-10-13T01:02:03Z"),
            json.dumps({key: value for key, value in latest.items() if key != "platforms"}),
        )

        # The updater rejects a latest.json with an incomplete entry, so a
        # missing installer stops the release instead.
        (directory / f"HardCode-{version}-1.x86_64.rpm.sig").unlink()
        try:
            release.manifest(version, REPOSITORY, directory)
            suite.check("an installer without its signature stops the release", False)
        except release.ReleaseError as error:
            suite.check(
                "an installer without its signature stops the release",
                "linux-x86_64-rpm" in str(error),
                str(error),
            )


def check_notes_and_checksums(suite: Suite) -> None:
    text = release.notes("2.3.4", REPOSITORY)
    suite.check(
        "the release's description names its files, images and chart",
        "{{" not in text
        and "hardcode_cli-2.3.4-py3-none-any.whl" in text
        and "oci://ghcr.io/example-owner/charts/hardcode --version 2.3.4" in text,
        text,
    )

    with tempfile.TemporaryDirectory(prefix="hc-notes-") as temporary:
        template = Path(temporary) / "notes.md"
        template.write_text("{{version}} and {{nonsense}}", encoding="utf-8")
        try:
            release.notes("2.3.4", REPOSITORY, template)
            suite.check("a name a release doesn't have is refused", False)
        except release.ReleaseError as error:
            suite.check("a name a release doesn't have is refused", "nonsense" in str(error), str(error))

    version = next(iter(release.versions().values()))
    text = release.dev_notes("2c419ae", f"{version}-dev.7", REPOSITORY)
    suite.check(
        "the development build's description names its commit, files, images and chart",
        "{{" not in text
        and f"`{version} (dev, 2c419ae)`" in text
        and f"hardcode_cli-{version}-py3-none-any.whl" in text
        and f"oci://ghcr.io/example-owner/charts/hardcode --version {version}-dev.7" in text
        and "ghcr.io/example-owner/hardcode-{vscode,squid,gateway,admin}:sha-2c419ae" in text,
        text,
    )

    with tempfile.TemporaryDirectory(prefix="hc-dev-notes-") as temporary:
        out = Path(temporary) / "notes.md"
        output = harness.run(
            [
                sys.executable,
                ROOT / "release" / "release.py",
                "dev-notes",
                "--commit",
                "2c419ae",
                "--chart",
                f"{version}-dev.7",
                "--repository",
                REPOSITORY,
                out,
            ]
        )
        suite.check(
            "release.py dev-notes writes it",
            output.ok and out.exists() and out.read_text(encoding="utf-8") == text,
            output.text,
        )

    with tempfile.TemporaryDirectory(prefix="hc-sums-") as temporary:
        directory = Path(temporary)
        (directory / "a.txt").write_text("a", encoding="utf-8")
        (directory / "b.bin").write_bytes(bytes(range(256)) * 5000)

        output = harness.run([sys.executable, ROOT / "release" / "release.py", "checksums", directory])
        checked = harness.run(["sha256sum", "-c", "SHA256SUMS"], cwd=directory)
        suite.check(
            "SHA256SUMS is what sha256sum -c reads, and holds every file",
            output.ok and checked.ok and checked.text.count(": OK") == 2,
            output.text + checked.text,
        )


def run_checks(suite: Suite) -> None:
    suite.section("the version")
    check_versions(suite)

    suite.section("a development build's mark")
    check_dev_mark(suite)

    suite.section("the updater's configuration")
    check_updater_config(suite)

    suite.section("latest.json")
    check_manifest(suite)

    suite.section("the descriptions and the checksums")
    check_notes_and_checksums(suite)


if __name__ == "__main__":
    harness.main("release", run_checks)
