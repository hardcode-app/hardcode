#!/usr/bin/env python3
"""
The desktop app (Tauri): its Rust unit tests, a build, a headless launch
(``--smoke``) that renders every view against the real CLI and a throwaway
checkout, under a virtual X server, the launcher ``app/build.sh desktop``
writes for the app grid, and a build configured as a release's, which asks
for a newer release and offers it.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import re
import secrets
import sys
import tempfile
import threading
from pathlib import Path
from typing import Iterator

import harness
from harness import ROOT, Suite

APP = ROOT / "app" / "src-tauri"
BINARY = APP / "target" / "debug" / "hardcode-app"

# Where Tauri keeps the app's settings: a folder named after its identifier.
APP_IDENTIFIER = json.loads((APP / "tauri.conf.json").read_text(encoding="utf-8"))["identifier"]


def check_unit_tests(suite: Suite) -> None:
    output = harness.run(["cargo", "test", "--quiet"], cwd=APP, timeout=1800)

    # One "test result" line per test binary; only the library has tests.
    results = re.findall(r"^test result: .*$", output.text, re.MULTILINE)
    passed = sum(int(count) for count in re.findall(r"(\d+) passed", " ".join(results)))

    suite.check(f"cargo test: {passed} passed", output.ok, output.tail(40))


def check_build(suite: Suite) -> None:
    output = harness.run(["cargo", "build", "--quiet"], cwd=APP, timeout=1800)
    suite.require("cargo build", output.ok and BINARY.exists(), output.tail(40))


def check_headless_launch(suite: Suite) -> None:
    """
    Start the app with ``--smoke``: it checks the overview, opens a
    container's logs, opens every view, and prints "SMOKE OK" or what went
    wrong, then exits.
    """
    with tempfile.TemporaryDirectory(prefix="hc-app-") as temporary:
        # A throwaway home, which the app is given with --config, so the CLI
        # it drives never sees your profiles; outside the default home, a
        # profile's Compose project carries the home's id besides.
        home = Path(temporary) / "home"
        environment = {
            **os.environ,
            "HARDCODE_CLI": f"python3 {ROOT / 'cli' / 'hardcode'}",
            "XDG_CONFIG_HOME": str(Path(temporary) / "config"),
        }

        def launch() -> harness.Output:
            return harness.run(
                ["xvfb-run", "--auto-servernum", "--server-args=-screen 0 1280x860x24", BINARY, "--smoke"]
                + ["--config", str(home)],
                env=environment,
                timeout=300,
            )

        # An empty home: the app asks for the first profile, and nothing else;
        # the smoke run then makes it, as someone new does, with Create.
        output = launch()
        report = "\n".join(line for line in output.text.splitlines() if "SMOKE" in line or "panicked" in line)
        suite.check(
            f"an empty home: the first profile is asked for, and made with Create: {report or 'no report'}",
            "SMOKE OK: views=0 profile=default" in output.text,
            output.tail(40),
        )

        hardcode = [sys.executable, ROOT / "cli" / "hardcode", "--config", home]
        listed = harness.run([*hardcode, "--json", "profiles"])
        names = [item["name"] for item in json.loads(listed.text)["profiles"]] if listed.ok else []
        suite.require("...and the home has it, default, made by the app", names == ["default"], listed.text)

        profile = f"smoke-{secrets.token_hex(4)}"
        created = harness.run([*hardcode, "profile", "add", profile, "--lang", "python,docker"])
        suite.require(f"the smoke run's profile {profile} is created", created.ok, created.text)

        # The app opens on the profile its settings name, the last one used,
        # though there is a default too.
        settings = Path(temporary) / "config" / APP_IDENTIFIER / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"profile": profile}), encoding="utf-8")

        output = launch()

    report = "\n".join(line for line in output.text.splitlines() if "SMOKE" in line or "panicked" in line)
    suite.check(report or "the headless launch reported", "SMOKE OK" in output.text, output.tail(40))
    suite.check(
        f"it opens on the profile last used, {profile}, not default",
        f"profile={profile}" in output.text,
        report,
    )


def check_launcher(suite: Suite) -> None:
    """
    ``app/build.sh desktop`` writes a launcher that GNOME matches to the
    app's window (its app id is ``hardcode-app``) and shows with the app's
    icon, pointing at the build it was given. Written into a scratch
    ``XDG_DATA_HOME``, never a real home.
    """
    with tempfile.TemporaryDirectory(prefix="hc-launcher-") as data:
        environment = {**os.environ, "XDG_DATA_HOME": data, "HARDCODE_APP_BINARY": str(BINARY)}
        output = harness.run(["bash", ROOT / "app" / "build.sh", "desktop"], env=environment)

        launcher = Path(data) / "applications" / "HardCode.desktop"
        text = launcher.read_text(encoding="utf-8") if launcher.exists() else ""
        fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)

        suite.check(
            "app/build.sh desktop writes the launcher",
            output.ok and fields.get("Type") == "Application" and fields.get("Name") == "HardCode",
            f"{output.text}\n{text}",
        )
        suite.check(
            "...matched to the app's window, starting the given build",
            fields.get("StartupWMClass") == "hardcode-app" and fields.get("Exec") == f'"{BINARY}"',
            text,
        )

        icons = [
            Path(data) / "icons" / "hicolor" / size / "apps" / "hardcode-app.png"
            for size in ("32x32", "128x128", "256x256")
        ]
        suite.check(
            "...with the app's icon in every size",
            fields.get("Icon") == "hardcode-app" and all(icon.exists() for icon in icons),
            "\n".join(str(icon) for icon in icons if not icon.exists()),
        )

        missing = harness.run(
            ["bash", ROOT / "app" / "build.sh", "desktop"],
            env={**environment, "HARDCODE_APP_BINARY": str(Path(data) / "no-such-build")},
        )
        suite.check(
            "...and refuses a build that isn't there", not missing.ok and "no build at" in missing.text, missing.text
        )


@contextlib.contextmanager
def serve(files: dict[str, bytes]) -> Iterator[tuple[int, list[str]]]:
    """
    An HTTP server on the loopback with *files* by path: its port, and the
    paths asked for, as they are.
    """
    asked: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        """
        *files*, and 404 for anything else.
        """

        def do_GET(self) -> None:
            asked.append(self.path)
            body = files.get(self.path)

            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, *arguments) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        yield server.server_address[1], asked
    finally:
        server.shutdown()
        server.server_close()


def check_updater(suite: Suite) -> None:
    """
    A build with the configuration a release adds (release/release.py
    updater-config, with a throwaway key), but asking a latest.json this
    suite serves, over plain HTTP, which only this test allows. On an empty
    home, the app asks for the newer release as it starts, and offers it.
    The debug build is built again without it afterwards.
    """
    release = harness.release_steps()

    with tempfile.TemporaryDirectory(prefix="hc-updater-") as temporary:
        key = Path(temporary) / "throwaway.key"
        generated = harness.run(["cargo", "tauri", "signer", "generate", "--ci", "--password", "", "--write-keys", key])
        suite.require("a throwaway signing key", generated.ok, generated.text)

        # A release's updater, asking this suite instead of GitHub. Nothing
        # is downloaded: the offer is what's tested.
        latest = {
            "version": "99.0.0",
            "notes": "https://example.com/releases/99.0.0",
            "pub_date": "2026-10-13T00:00:00Z",
            "platforms": {"linux-x86_64": {"url": "https://example.com/never-downloaded", "signature": "unused"}},
        }

        with serve({"/latest.json": json.dumps(latest).encode()}) as (port, asked):
            config = release.updater_config("example/hardcode", Path(f"{key}.pub"))
            updater = config["plugins"]["updater"]
            updater["endpoints"] = [f"http://127.0.0.1:{port}/latest.json"]
            updater["dangerousInsecureTransportProtocol"] = True

            built = harness.run(
                ["cargo", "build", "--quiet"],
                cwd=APP,
                env={**os.environ, "TAURI_CONFIG": json.dumps(config)},
                timeout=1800,
            )
            suite.require("a build configured as a release's", built.ok, built.tail(40))

            output = harness.run(
                ["xvfb-run", "--auto-servernum", "--server-args=-screen 0 1280x860x24", BINARY, "--smoke"]
                + ["--config", str(Path(temporary) / "home")],
                env={
                    **os.environ,
                    "HARDCODE_CLI": f"python3 {ROOT / 'cli' / 'hardcode'}",
                    "XDG_CONFIG_HOME": str(Path(temporary) / "config"),
                },
                timeout=300,
            )

        report = "\n".join(line for line in output.text.splitlines() if "SMOKE" in line or "panicked" in line)
        suite.check(
            f"it asks for a newer release as it starts, and offers it: {report or 'no report'}",
            # An empty home, so the run makes the first profile too.
            "SMOKE OK: views=0 profile=default update=99.0.0" in output.text and asked == ["/latest.json"],
            f"asked for {asked}\n{output.tail(40)}",
        )

    rebuilt = harness.run(["cargo", "build", "--quiet"], cwd=APP, timeout=1800)
    suite.check("the debug build is built again without it", rebuilt.ok, rebuilt.tail(40))


def run_checks(suite: Suite) -> None:
    suite.section("rust unit tests")
    check_unit_tests(suite)

    suite.section("build")
    check_build(suite)

    suite.section("headless launch")
    check_headless_launch(suite)

    suite.section("launcher")
    check_launcher(suite)

    suite.section("updater")
    check_updater(suite)


if __name__ == "__main__":
    harness.main("app", run_checks)
