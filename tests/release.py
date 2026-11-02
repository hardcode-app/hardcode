#!/usr/bin/env python3
"""
The steps of a release that aren't a build (release/release.py): the version
written into every file that carries it, a development build's mark, the
updater's configuration, the builds' files gathered into one folder, the
latest.json the app's updater reads (from
installers signed for real, with a throwaway key), the release's and the
development build's descriptions, the checksums, the known vulnerabilities'
section, whether a nightly run builds and when a development build is
published, and what builds a release: the pinned BuildKit, the actions and
secrets each job gets, and the macOS and Windows builds' toolchains.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

import harness
from harness import ROOT, Suite

REPOSITORY = "Example-Owner/hardcode"

release = harness.release_steps()


def load_scan():
    """
    release/scan.py, as a module (its name is taken by nothing, but loaded
    alike).
    """
    spec = importlib.util.spec_from_file_location("release_scan", ROOT / "release" / "scan.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    return module


scan = load_scan()


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
            "...and nothing in the build signs: the key goes only to release.py sign",
            "bundle" not in config,
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


def check_gather(suite: Suite) -> None:
    with tempfile.TemporaryDirectory(prefix="hc-gather-") as temporary:
        directory = Path(temporary)

        # As the builds' files are downloaded: each in the folder it was built in.
        arrived = {
            "appimage/HardCode_1.2.3_amd64.AppImage": "a",
            "deb/HardCode_1.2.3_amd64.deb": "b",
            "macos/HardCode_aarch64.app.tar.gz": "c",
            "bundle/nsis/HardCode_1.2.3_x64-setup.exe": "d",
            "hardcode-linux-x64": "e",
        }
        for name, content in arrived.items():
            (directory / name).parent.mkdir(parents=True, exist_ok=True)
            (directory / name).write_text(content, encoding="utf-8")

        output = harness.run([sys.executable, ROOT / "release" / "release.py", "gather", directory])
        left = sorted(path.relative_to(directory).as_posix() for path in directory.rglob("*"))
        suite.check(
            "gather moves every file out of its folder, and removes the folders",
            output.ok
            and left == sorted(Path(name).name for name in arrived)
            and (directory / "HardCode_1.2.3_x64-setup.exe").read_text(encoding="utf-8") == "d",
            output.text + "\n".join(left),
        )

        (directory / "again").mkdir()
        (directory / "again" / "hardcode-linux-x64").write_text("f", encoding="utf-8")
        try:
            release.gather(directory)
            suite.check("two files of one name are refused, before anything moves", False)
        except release.ReleaseError as error:
            suite.check(
                "two files of one name are refused, before anything moves",
                (directory / "again" / "hardcode-linux-x64").exists()
                and (directory / "hardcode-linux-x64").read_text(encoding="utf-8") == "e",
                str(error),
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
        (directory / "hardcode-linux-x64").write_bytes(b"not an installer")

        # The release's own signing step, the key given as the workflow gives it.
        try:
            release.sign(version, directory, {**os.environ, "TAURI_SIGNING_PRIVATE_KEY": ""})
            suite.check("release.py sign refuses to run without the key", False)
        except release.ReleaseError as error:
            suite.check("release.py sign refuses to run without the key", "TAURI_SIGNING_PRIVATE_KEY" in str(error))

        environment = {
            **os.environ,
            "TAURI_SIGNING_PRIVATE_KEY": key.read_text(encoding="utf-8"),
            "TAURI_SIGNING_PRIVATE_KEY_PASSWORD": "",
        }
        try:
            signed = release.sign(version, directory, environment)
        except release.ReleaseError as error:
            suite.abort("release.py sign", str(error))
        suite.check(
            "release.py sign signs every installer, and nothing else",
            sorted(signed) == sorted(names) and not (directory / "hardcode-linux-x64.sig").exists(),
            ", ".join(signed),
        )

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


def report(*packages: tuple, path: str = "/repo/app/src-tauri/Cargo.lock") -> dict:
    """
    A scanner report (OSV-Scanner's JSON) with a finding per (package,
    version, id, CVSS score, fixed version or None), all found in *path*.
    """
    listed = []
    for name, version, key, score, fixed in packages:
        events = [{"introduced": "0"}] + ([{"fixed": fixed}] if fixed else [])
        listed.append(
            {
                "package": {"name": name, "version": version, "ecosystem": "crates.io"},
                "vulnerabilities": [
                    {
                        "id": key,
                        "summary": f"{name} is broken",
                        "affected": [{"package": {"name": name}, "ranges": [{"events": events}]}],
                    }
                ],
                "groups": [{"ids": [key, f"GHSA-{key}"], "max_severity": score}],
            }
        )

    return {"results": [{"source": {"path": path}, "packages": listed}]}


def check_scan(suite: Suite) -> None:
    with tempfile.TemporaryDirectory(prefix="hc-scan-") as temporary:
        directory = Path(temporary)

        asked = []
        written = scan.databases(directory, lambda url: asked.append(url) or b"zip")
        suite.check(
            "OSV's databases, one per ecosystem, where the scanner reads them offline",
            [path.relative_to(directory).as_posix() for path in written]
            == [f"osv-scalibr/{ecosystem}/all.zip" for ecosystem in scan.ECOSYSTEMS]
            and asked == [scan.DATABASES.format(ecosystem=ecosystem) for ecosystem in scan.ECOSYSTEMS],
            "\n".join(asked),
        )

    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    listed = dict(scan.images("ghcr.io/owner/hardcode", "sha-2c419ae", compose))
    suite.check(
        "the images scanned: ours as this run published them, CoreDNS and Docker as compose.yaml pins them",
        listed["squid"] == "ghcr.io/owner/hardcode-squid:sha-2c419ae"
        and set(listed) == {"squid", "gateway", "admin", "coredns", "docker"}
        and listed["coredns"].startswith("coredns/coredns:")
        and "@sha256:" in listed["docker"],
        str(listed),
    )

    suite.check(
        "references as skopeo takes them: with their registry, by digest alone when pinned by one",
        scan.qualified("coredns/coredns:1.15.0@sha256:abc") == "docker.io/coredns/coredns@sha256:abc"
        and scan.qualified("docker:29.9.2-dind-rootless@sha256:def") == "docker.io/library/docker@sha256:def"
        and scan.qualified("ghcr.io/owner/hardcode-squid:sha-2c419ae") == "ghcr.io/owner/hardcode-squid:sha-2c419ae"
        and scan.qualified("localhost:5000/thing:1") == "localhost:5000/thing:1",
    )

    with tempfile.TemporaryDirectory(prefix="hc-scan-") as temporary:
        directory = Path(temporary) / "images"
        commands = []

        def skopeo(command, **options):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        saved = scan.save(
            [("squid", "ghcr.io/owner/hardcode-squid:sha-2c419ae"), ("coredns", "coredns/coredns:1.15.0@sha256:abc")],
            directory,
            "linux/arm64",
            "someone:token",
            skopeo,
        )
        suite.check(
            "each image is copied from its registry, one platform, into a Docker archive the scanner reads",
            [path.name for path in saved] == ["squid.tar", "coredns.tar"]
            and commands[0][:7] == ["skopeo", "copy", "--quiet", "--override-os", "linux", "--override-arch", "arm64"]
            and commands[1][-2:]
            == [
                "docker://docker.io/coredns/coredns@sha256:abc",
                f"docker-archive:{directory}/coredns.tar:scan/coredns:latest",
            ],
            "\n".join(" ".join(command) for command in commands),
        )
        suite.check(
            "...the read-only token given to ghcr.io's, and to nothing else",
            "--src-creds" in commands[0] and "--src-creds" not in commands[1],
            "\n".join(" ".join(command) for command in commands),
        )

    # The lock files' report: the app's crates, and a Python package in the
    # admin's lock.
    source = report(
        ("zip", "4.6.1", "RUSTSEC-1", "10.0", "9.0.0"),
        ("zip", "4.6.1", "RUSTSEC-4", "9.1", "9.0.5"),
        ("glib", "0.18.5", "RUSTSEC-2", "6.9", "0.20.0"),
        ("zvariant", "5.15.0", "RUSTSEC-3", "", None),
    )
    source["results"] += report(
        ("werkzeug", "3.1.9", "PYSEC-1", "7.5", "3.1.10"),
        path="/repo/images/admin/uv.lock",
    )["results"]

    reports = {
        "source": source,
        "squid": report(("openssl", "3.5.0", "UBUNTU-1", "7.5", "3.5.1")),
        "docker": report(
            # The same module in two binaries: one issue.
            ("github.com/moby/go-archive", "0.2.0", "GO-1", "7.5", "0.3.0"),
            ("github.com/moby/go-archive", "0.2.0", "GO-1", "7.5", "0.3.0"),
            # A binary whose version the scanner can't read, and a "fix"
            # older than what is installed: neither is counted.
            ("github.com/moby/moby/v2", "(devel)", "GO-2", "8.8", "2.0.0"),
            ("github.com/coredns/coredns", "0.0.0-20261021021732-a6fde3fe339c", "GO-3", "7.5", "1.14.3"),
            ("glib", "2.88.1-r1", "ALPINE-1", "7.8", "2.66.6-r0"),
        ),
    }
    text, details = scan.summary(reports, "OSV-Scanner 9.9.9", datetime(2026, 11, 1, tzinfo=timezone.utc))
    suite.check(
        "the description's section counts each issue once, by severity, for each thing scanned",
        "| The app's crates, and the Python packages the command, the admin and the build use | 2 | 1 | 1 | 0 | 1 | 0 |"
        in text
        and "| The proxy and allowlist renderer (hardcode-squid) | 0 | 1 | 0 | 0 | 0 | 0 |" in text
        and "OSV-Scanner 9.9.9 against OSV's databases of 2026-11-01" in text,
        text,
    )
    suite.check(
        "...not counting a binary whose version can't be read, nor a fix older than what is installed",
        "| The Docker sidecar | 0 | 1 | 0 | 0 | 0 | 3 |" in text,
        text,
    )
    fixed = [line for line in text.splitlines() if line.startswith("- ")]
    suite.check(
        "...and the critical and high a newer version fixes, a line for each package, the most severe first, "
        "each named for its image or its lock",
        fixed
        == [
            "- zip 4.6.1 → 9.0.5, in the app's crates: 2 critical",
            "- github.com/moby/go-archive 0.2.0 → 0.3.0, in the Docker sidecar: 1 high",
            "- openssl 3.5.0 → 3.5.1, in the proxy: 1 high",
            "- werkzeug 3.1.9 → 3.1.10, in the admin's Python packages: 1 high",
        ]
        and [entry["scanned"] for entry in details] == ["source", "squid", "docker"]
        and len(details[2]["findings"]) == 4,
        text,
    )

    with tempfile.TemporaryDirectory(prefix="hc-scan-") as temporary:
        directory = Path(temporary)
        for name, content in reports.items():
            (directory / f"{name}.json").write_text(json.dumps(content), encoding="utf-8")

        out = directory / "vulnerabilities.md"
        output = harness.run(
            [
                sys.executable,
                ROOT / "release" / "scan.py",
                "summary",
                directory,
                "--scanner",
                "OSV-Scanner 9.9.9",
                "--out",
                out,
            ]
        )
        suite.check(
            "release/scan.py summary writes the section and vulnerabilities.json",
            output.ok
            and out.read_text(encoding="utf-8").startswith("## Known vulnerabilities")
            and len(json.loads((directory / "vulnerabilities.json").read_text(encoding="utf-8"))) == 3,
            output.text,
        )


def workflow(name: str) -> dict:
    """
    .github/workflows/NAME, as YAML: its "on" key under "on" (YAML 1.1,
    which PyYAML reads, takes it for true).
    """
    loaded = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))
    if True in loaded:
        loaded["on"] = loaded.pop(True)

    return loaded


def step(job: dict, name: str) -> dict:
    """
    The step of *job* called *name*.
    """
    return next(item for item in job["steps"] if item.get("name") == name)


def run_with_gh(script: str, variables: dict) -> tuple[subprocess.CompletedProcess, list[str]]:
    """
    A workflow step's script, run as GitHub runs a bash step, in a folder of
    its own, with tests/fixtures/fake_gh.py answering for gh: what it did,
    and the gh calls it made.
    """
    with tempfile.TemporaryDirectory(prefix="hc-step-") as temporary:
        folder = Path(temporary)
        (folder / "bin").mkdir()
        (folder / "bin" / "gh").symlink_to(ROOT / "tests" / "fixtures" / "fake_gh.py")
        log = folder / "gh.log"
        log.write_text("", encoding="utf-8")

        environment = {
            **os.environ,
            "PATH": f"{folder / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_REPOSITORY": "owner/hardcode",
            "GITHUB_OUTPUT": str(folder / "output"),
            "FAKE_GH_LOG": str(log),
            **variables,
        }
        done = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
            cwd=folder,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        output = (folder / "output").read_text(encoding="utf-8") if (folder / "output").exists() else ""
        calls = log.read_text(encoding="utf-8").splitlines()

    return subprocess.CompletedProcess(done.args, done.returncode, output, done.stderr), calls


def check_nightly(suite: Suite) -> None:
    release = workflow("release.yml")
    jobs = release["jobs"]

    # Every job but the check waits on the version job, which waits on the
    # check: skipping the version job skips the build.
    not_waiting = []
    for name, job in jobs.items():
        needs = job.get("needs", [])
        needs = [needs] if isinstance(needs, str) else needs
        if name not in ("changed", "version") and "version" not in needs:
            not_waiting.append(name)

    suite.check(
        "a nightly run is scheduled; every job waits on the version job, which waits on the check",
        len(release["on"]["schedule"]) == 1
        and jobs["version"]["needs"] == "changed"
        and jobs["version"]["if"] == "needs.changed.outputs.build == 'true'"
        and not not_waiting,
        f"not waiting on the version job: {not_waiting}",
    )

    # The check's own script, against the stand-in for gh.
    script = jobs["changed"]["steps"][0]["run"]
    head = "a" * 40

    def decided(event: str, dev_commit: str) -> str:
        variables = {
            "GITHUB_EVENT_NAME": event,
            "GITHUB_SHA": head,
            "FAKE_RELEASE_LIST": "dev-7" if dev_commit else "",
            "FAKE_DEV_COMMIT": dev_commit,
        }
        done, _ = run_with_gh(script, variables)
        return done.stdout.strip() if done.returncode == 0 else done.stderr

    decisions = {
        "nightly, main unchanged": decided("schedule", head),
        "nightly, main moved": decided("schedule", "b" * 40),
        "nightly, no development build": decided("schedule", ""),
        "by hand, main unchanged": decided("workflow_dispatch", head),
        "a tag": decided("push", head),
    }
    suite.check(
        "...which skips a nightly run while main is the last development build's commit, and builds otherwise",
        decisions
        == {
            "nightly, main unchanged": "build=false",
            "nightly, main moved": "build=true",
            "nightly, no development build": "build=true",
            "by hand, main unchanged": "build=true",
            "a tag": "build=true",
        },
        str(decisions),
    )

    # The development build's publishing, against the stand-in for gh.
    current = step(jobs["dev-release"], "Still main's newest")["run"]
    publish = step(jobs["dev-release"], "Publish, then retire the last development build")["run"]
    variables = {"GITHUB_SHA": head, "COMMIT": head[:7], "CHART": "0.1.0-dev.9"}

    moved, moved_calls = run_with_gh(current, {**variables, "FAKE_MAIN_COMMIT": "b" * 40})
    still, _ = run_with_gh(current, {**variables, "FAKE_MAIN_COMMIT": head})
    suite.check(
        "a development build is published only while main is its commit; otherwise the last one stays",
        moved.returncode == 0
        and moved.stdout.strip() == "publish=false"
        and not any(call.startswith("release ") for call in moved_calls)
        and still.stdout.strip() == "publish=true",
        f"main moved: {moved.stdout} {moved_calls}\nmain current: {still.stdout}",
    )

    published, calls = run_with_gh(publish, {**variables, "TAG": "dev-9", "FAKE_RELEASE_LIST": "dev-9\ndev-8\ndev-7"})
    releases = [call for call in calls if call.startswith(("release create", "release delete"))]
    suite.check(
        "...under a tag of its own, dev-<run>, before the ones it replaces are retired",
        published.returncode == 0
        and releases[0].startswith("release create dev-9 ")
        and sorted(call.split()[2] for call in releases[1:]) == ["dev-7", "dev-8"]
        and all(call.startswith("release delete") and "--cleanup-tag" in call for call in releases[1:]),
        "\n".join(calls) + published.stderr,
    )


def check_build_supply(suite: Suite) -> None:
    workflows = {name: workflow(name) for name in ("ci.yml", "release.yml", "dependencies.yml")}
    actions = sorted((ROOT / ".github" / "actions").glob("*/action.yml"))

    # Every docker/setup-buildx-action goes through .github/actions/buildx,
    # which gives it the BuildKit image .github/buildkit/Dockerfile pins.
    texts = {path: path.read_text(encoding="utf-8") for path in (ROOT / ".github").rglob("*.yml")}
    buildx_users = sorted(
        str(path.relative_to(ROOT)) for path, text in texts.items() if "docker/setup-buildx-action@" in text
    )
    buildx = (ROOT / ".github" / "actions" / "buildx" / "action.yml").read_text(encoding="utf-8")
    pinned = (ROOT / ".github" / "buildkit" / "Dockerfile").read_text(encoding="utf-8")
    suite.check(
        "every image builder runs the BuildKit image pinned by digest, not the moving buildx-stable-1",
        buildx_users == [".github/actions/buildx/action.yml"]
        and "driver-opts: image=${{ steps.buildkit.outputs.image }}" in buildx
        and re.search(r"^FROM moby/buildkit:v[\d.]+@sha256:[0-9a-f]{64}$", pinned, re.M) is not None,
        f"setup-buildx-action used in: {buildx_users}",
    )

    # A job that can write runs only GitHub's and Docker's actions and our own.
    third_party = []
    for name, loaded in workflows.items():
        for job_name, job in loaded["jobs"].items():
            permissions = job.get("permissions") or {}
            if "write" not in permissions.values():
                continue
            for item in job.get("steps", []):
                uses = item.get("uses", "")
                if uses and not uses.startswith(("actions/", "docker/", "./")):
                    third_party.append(f"{name} {job_name}: {uses}")
    suite.check(
        "a job that can write runs no third-party action",
        not third_party,
        "\n".join(third_party),
    )

    # No checkout leaves its token for the steps after it.
    kept = []
    for name in ("ci.yml", "release.yml"):
        for job_name, job in workflows[name]["jobs"].items():
            for item in job.get("steps", []):
                if item.get("uses", "").startswith("actions/checkout@"):
                    if (item.get("with") or {}).get("persist-credentials") is not False:
                        kept.append(f"{name} {job_name}")
    suite.check("no checkout in CI or the release keeps its token", not kept, "\n".join(kept))

    # No secret but the job's own token outside the job that signs.
    secrets = []
    for job_name, job in workflows["release.yml"]["jobs"].items():
        names = set(re.findall(r"secrets\.(\w+)", yaml.safe_dump(job))) - {"GITHUB_TOKEN"}
        if names and job_name != "sign":
            secrets.append(f"{job_name}: {sorted(names)}")
    suite.check(
        "no secret but the job's own token is handed to any release job but the one that signs",
        not secrets,
        "\n".join(secrets),
    )

    jobs = workflows["release.yml"]["jobs"]
    publish = step(jobs["release"], "Publish")["run"]
    suite.check(
        "a release is published with gh, at the tag that was pushed (none is made)",
        'gh release create "$GITHUB_REF_NAME"' in publish and "--verify-tag" in publish,
        publish,
    )

    # The updater's zip feature unpacks Tauri v1's zipped bundles; HardCode's
    # updates are the installers themselves (release.INSTALLERS).
    lock = (ROOT / "app" / "src-tauri" / "Cargo.lock").read_text(encoding="utf-8")
    suite.check(
        "the app carries no zip extractor: its updates are installers, never zipped bundles",
        '\nname = "zip"\n' not in lock and not any(name.endswith(".zip") for _, name in release.INSTALLERS),
    )

    def attests(job: dict) -> list[dict]:
        return [item.get("with") or {} for item in job["steps"] if "attest-build-provenance@" in item.get("uses", "")]

    def before(job: dict, first: str, then: str) -> bool:
        names = [item.get("uses", "") + " " + (item.get("name") or "") for item in job["steps"]]
        return min(i for i, name in enumerate(names) if first in name) < min(
            i for i, name in enumerate(names) if then in name
        )

    image, files, dev = attests(jobs["images"]), attests(jobs["release"]), attests(jobs["dev-release"])
    suite.check(
        "everything published is attested first: the images by digest, in the registry, and every release file",
        image
        == [
            {
                "subject-name": "${{ needs.version.outputs.prefix }}-${{ matrix.image.name }}",
                "subject-digest": "${{ steps.build.outputs.digest }}",
                "push-to-registry": True,
            }
        ]
        and files == [{"subject-path": "dist/release/*"}]
        and dev == [{"subject-path": "dist/dev/*"}]
        and before(jobs["release"], "attest-build-provenance@", "Publish")
        and before(jobs["dev-release"], "attest-build-provenance@", "Publish, then retire"),
        f"{image}\n{files}\n{dev}",
    )

    native = jobs["app-native"]
    uses = [item.get("uses", "") for item in native["steps"]]
    rust = next(item for item in native["steps"] if item.get("uses", "").startswith("dtolnay/rust-toolchain@"))
    suite.check(
        "the macOS and Windows builds use the pinned Rust and uv's Python, and Cargo.lock as it is",
        rust["with"]["toolchain"] == "${{ steps.rust.outputs.channel }}"
        and "app/src-tauri/rust-toolchain.toml"
        in next(item for item in native["steps"] if item.get("id") == "rust")["run"]
        and "./.github/actions/python" in uses
        and not any(item.startswith("actions/setup-python@") for item in uses)
        and "-- --locked" in step(native, "Build")["run"]
        and [path.parent.name for path in actions] == ["build-image", "buildx", "python"],
        str(uses),
    )


def run_checks(suite: Suite) -> None:
    suite.section("the version")
    check_versions(suite)

    suite.section("a development build's mark")
    check_dev_mark(suite)

    suite.section("the updater's configuration")
    check_updater_config(suite)

    suite.section("the builds' files, gathered")
    check_gather(suite)

    suite.section("latest.json")
    check_manifest(suite)

    suite.section("the descriptions and the checksums")
    check_notes_and_checksums(suite)

    suite.section("known vulnerabilities")
    check_scan(suite)

    suite.section("the nightly development build")
    check_nightly(suite)

    suite.section("what builds a release")
    check_build_supply(suite)


if __name__ == "__main__":
    harness.main("release", run_checks)
