#!/usr/bin/env python3
"""
The ``hardcode`` command.

First its unit tests (every function, with ``docker compose`` faked), then the
whole command against a throwaway home, with the checkout's stack, where the
real ``docker compose config`` validates every file it writes; then the
standalone binary and the Python package, as a release builds them, each
making a profile from the stack it carries. Nothing is started, and your
own profiles are never touched.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

import harness
from harness import ROOT, Output, Suite

# The scenario's own profiles, besides "default": named so that nobody else's
# can be called the same. Removing a profile runs `docker compose down -v`
# and `docker image rm` for its name, and "old-node" might be yours.
TAG = "t" + secrets.token_hex(3)
NODE = f"{TAG}-node"
COPY = f"{TAG}-copy"

# Nothing in the scenario needs a Docker daemon, except removing a profile:
# without one, `compose ps` reports nothing (not your stack's containers),
# and `compose config` works as it does anywhere.
NO_DAEMON = "unix:///nonexistent/hardcode-cli-scenario.sock"
REAL_DAEMON = os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")

# The stand-in docker whose Docker has NVIDIA's runtime (the rest is the real one's).
DOCKER_WITH_NVIDIA = ROOT / "tests" / "fixtures" / "docker_with_nvidia.py"

# Every language and tool: the scenario switches some of them off later.
LANGUAGES = [
    "python", "node", "java", "android", "dotnet", "go", "rust", "cpp", "ruby", "perl",
    "build_essentials", "docker", "kubernetes", "claude_cli", "codex_cli",
]  # fmt: skip

# A new profile's .env: the header, the port, subnet and DNS address, the
# languages switched on, then the commented reference of every setting.
ENV_SETTINGS = re.compile(
    r"^(#[^\n]*\n)+(VSCODE_PORT=\d+\nDEVNET_SUBNET=[^\n]+\nDNS_IP=[^\n]+\n(INSTALL_[A-Z_]+=true\n)+)\n# --- Every setting",
)


class Home:
    """
    A throwaway HardCode home, which the command runs against with
    ``--config``, using the checkout's own stack. Outside the default home,
    its profiles' Compose projects and images carry the home's id, so none
    of them can be yours, "default" included.
    """

    def __init__(self, root: Path):
        self.root = root / "home"

        # The command picks a profile from HARDCODE_PROFILE when it's set;
        # the checks below choose theirs explicitly.
        self.environment = {key: value for key, value in os.environ.items() if key != "HARDCODE_PROFILE"}
        self.environment["DOCKER_HOST"] = NO_DAEMON

        sys.path.insert(0, str(ROOT / "cli"))
        from hardcode_cli import home

        self.id = home.home_id(self.root)

    def hardcode(self, *arguments: str, profile: str = "", env: Optional[dict[str, str]] = None) -> Output:
        """
        Run the command against this home, for *profile* when one is named,
        with *env* added to the environment.
        """
        command = [sys.executable, ROOT / "cli" / "hardcode", "--config", self.root]
        if profile:
            command += ["-p", profile]

        return harness.run([*command, *arguments], env={**self.environment, **(env or {})})

    def project(self, profile: str) -> str:
        """
        The Compose project of one of this home's profiles.
        """
        return f"hardcode-{self.id}-{profile}"

    def profile_file(self, profile: str, name: str) -> str:
        """
        The text of one of a profile's files.
        """
        return (self.root / "profiles" / profile / name).read_text(encoding="utf-8")

    def port(self, profile: str) -> str:
        """
        The VS Code port the profile was given (free on this machine then).
        """
        match = re.search(r"^VSCODE_PORT=(\d+)$", self.profile_file(profile, ".env"), re.MULTILINE)
        return match.group(1) if match else ""

    def profiles_whose(self, name: str, pattern: str) -> list[str]:
        """
        The profiles whose file *name* has a line matching *pattern*.
        """
        matching = []

        for directory in sorted((self.root / "profiles").iterdir()):
            path = directory / name
            if path.exists() and re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE):
                matching.append(directory.name)

        return matching


def count_lines(text: str, pattern: str) -> int:
    """
    How many lines of *text* match *pattern*.
    """
    return sum(1 for line in text.splitlines() if re.search(pattern, line))


def check_profiles(suite: Suite, home: Home) -> None:
    hc = home.hardcode

    suite.section("profiles")
    suite.expect("a new home has no profiles", hc("profiles"), r"^no profiles yet in ")
    suite.expect_failure("nothing makes one by itself", hc("up"), r"hardcode profile add default --lang")
    suite.expect_failure("from the template, one needs its languages", hc("profile", "add", "default"), r"--lang")
    suite.expect(
        "profile add default with every language and tool",
        hc("profile", "add", "default", "--lang", ",".join(LANGUAGES)),
        r"created from the template \(VS Code on port \d+, subnet 172.31.253.0/24",
    )
    suite.check(".env holds only what differs", ENV_SETTINGS.match(home.profile_file("default", ".env")) is not None)
    suite.expect("profiles lists it", hc("profiles"), r"^default +\d+ +172.31.253.0/24 +stopped")
    suite.expect(
        "profile add allocates the next port and subnet",
        hc("profile", "add", NODE, "--from", "default"),
        r"port \d+, subnet 172.31.252.0/24",
    )
    suite.check("a port of its own", home.port(NODE) not in ("", home.port("default")), home.port(NODE))
    suite.expect_failure(
        "profile add rejects a bad name",
        hc("profile", "add", "Bad_Name"),
        r"not a valid profile name",
    )
    suite.expect_failure("profile add rejects a duplicate", hc("profile", "add", NODE, "--lang", ""), r"already exists")
    suite.expect(
        "profile add copies another profile",
        hc("profile", "add", COPY, "--from", NODE),
        rf"created from {NODE}",
    )
    suite.expect("three profiles listed", hc("profiles"), rf"^{COPY} +\d+ +172.31.251.0/24")

    suite.expect(
        "compose uses the profile's project name, with the home's id",
        hc("compose", "config", profile=NODE),
        rf"^name: {home.project(NODE)}",
    )
    suite.expect(
        "compose uses the profile's image",
        hc("compose", "config", profile=NODE),
        rf"image: hardcode-vscode:{home.id}-{NODE}",
    )
    suite.expect(
        "HARDCODE_PROFILE selects a profile",
        hc("compose", "config", env={"HARDCODE_PROFILE": COPY}),
        rf"^name: {home.project(COPY)}",
    )
    suite.expect("compose config validates", hc("compose", "config", "--quiet", profile=COPY))
    suite.expect("--json status", hc("--json", "status"), r'"state": "stopped"')


def check_allowlist(suite: Suite, home: Home) -> None:
    hc = home.hardcode

    def allowlist(profile: str = "default") -> Output:
        return Output(0, home.profile_file(profile, "allowlist.txt"))

    def given(profile: str = "default") -> Output:
        return Output(0, home.profile_file(profile, "proxy/allowlist.txt"))

    suite.section("allowlist")
    suite.expect("allow appends to the Custom section", hc("allow", "GitHub.com/Me"), r"allowed github.com/me")
    suite.expect("entry is in the file", allowlist(), r"^github.com/me$")
    suite.expect("and in what the proxy is given", given(), r"^github.com/me$")
    suite.expect("allow is idempotent", hc("allow", "github.com/me"), r"already allowed")
    suite.expect(
        "allow upgrades the flag in place",
        hc("allow", "github.com/me", "rw"),
        r"allowed github.com/me \(rw\)",
    )
    suite.expect("one line, with rw", allowlist(), r"^github.com/me +rw$")

    suite.expect_failure("allow rejects a bare TLD", hc("allow", "com"), r"not a valid hostname")
    suite.expect_failure("allow rejects a wildcard with a path", hc("allow", ".github.com/x"), r"can't have a path")
    suite.expect_failure("allow rejects a bad flag", hc("allow", "github.com", "bogus"), r"invalid choice")
    suite.expect("allow ssh entries", hc("allow", "ssh.github.com:443", "ssh"), r"allowed ssh.github.com:443 \(ssh\)")

    suite.expect("deny comments the entry out", hc("deny", "github.com/me"), r"denied github.com/me")
    suite.expect("commented line kept", allowlist(), r"^#github.com/me +rw$")
    suite.expect_failure("deny of an inactive entry fails", hc("deny", "github.com/me"), r"not an active entry")
    suite.expect("allow re-enables a denied entry", hc("allow", "github.com/me"), r"allowed github.com/me")
    suite.expect("re-enabled, flag reset", allowlist(), r"^github.com/me$")

    # --all applies an edit to every profile, reporting each one.
    suite.expect(
        "allow --all touches every profile",
        hc("--all", "allow", "github.com/ourcompany"),
        rf"\[default\] allowed[\s\S]*\[{COPY}\] allowed[\s\S]*\[{NODE}\] allowed",
    )
    suite.check(
        "entry present in all three",
        len(home.profiles_whose("allowlist.txt", r"^github.com/ourcompany")) == 3,
    )
    suite.expect("deny --all", hc("--all", "deny", "github.com/ourcompany"), rf"\[{NODE}\] denied")
    suite.check(
        "entry commented in all three",
        len(home.profiles_whose("allowlist.txt", r"^#github.com/ourcompany")) == 3,
    )
    suite.expect(
        "deny --all reports profiles without the entry",
        hc("--all", "deny", "github.com/ourcompany"),
        r"not an active entry",
    )
    suite.check(
        "a profile's allowlist is independent",
        count_lines(home.profile_file(COPY, "allowlist.txt"), r"^ssh.github.com") == 0,
    )

    # The entries HardCode provides: listed, switched off and on, never deleted.
    suite.expect(
        "allowlist lists every entry, provided ones by block",
        hc("allowlist"),
        r"^VS Code \(every profile\)$[\s\S]*^Python \(comes with the language\)$\n  on   pypi\.org$",
    )
    suite.expect("deny switches a provided entry off", hc("deny", "pypi.org"), r"denied pypi\.org")
    suite.expect("with an off line", allowlist(), r"^off pypi\.org$")
    suite.expect("which the full list shows", hc("allowlist"), r"^  off  pypi\.org$")
    suite.expect("and the proxy's file has commented out", given(), r"^#pypi\.org ")
    suite.expect_failure(
        "a provided entry can't be removed",
        hc("allowlist", "remove", "pypi.org"),
        r"can't be deleted: switch it off instead",
    )
    suite.expect("allow switches it back on", hc("allow", "pypi.org"), r"switched pypi\.org back on")

    # The proxy runs as its own user and must still be able to read its file.
    mode = (home.root / "profiles" / "default" / "proxy" / "allowlist.txt").stat().st_mode & 0o777
    suite.check("the proxy's file is readable by the proxy user", mode == 0o644, f"mode {oct(mode)}")


def check_forwards(suite: Suite, home: Home) -> None:
    hc = home.hardcode

    suite.section("forwards")
    suite.expect("forward writes the override", hc("forward", "3000"), r"forwarding localhost:3000 -> vscode:3000")
    suite.expect(
        "forward to docker with a target port",
        hc("forward", "8080", "docker", "80"),
        r"forwarding localhost:8080 -> docker:80",
    )
    suite.expect(
        "GATEWAY_FORWARDS keeps VS Code first",
        hc("compose", "config"),
        r"GATEWAY_FORWARDS: 8000:vscode:8000 3000:vscode:3000 8080:docker:80",
    )
    suite.expect(
        "VS Code published on the profile's port", hc("compose", "config"), rf'published: "{home.port("default")}"'
    )
    suite.expect("ports published", hc("compose", "config"), r'published: "3000"')
    suite.expect("forward is idempotent", hc("forward", "3000"), r"already forwarded")
    suite.expect_failure("forward refuses the VS Code port", hc("forward", home.port("default")), r"VS Code port")
    suite.expect_failure(
        "forward refuses another profile's port",
        hc("forward", "3000", profile=COPY),
        r"used by profile 'default'",
    )
    suite.expect_failure("forward refuses --all", hc("--all", "forward", "3001"), r"isn't supported")

    suite.expect("unforward", hc("unforward", "3000"), r"forward for port 3000 removed")
    suite.expect("remove the last forward", hc("unforward", "8080"), r"forward for port 8080 removed")

    # With no forwards left, the override is back to an empty services map.
    override = home.profile_file("default", "compose.override.yaml")
    content = [line for line in override.splitlines() if line.strip() and not line.strip().startswith("#")]
    suite.check("no gateway section left", content[:1] == ["services: {}"], override)

    suite.expect_failure("unforward of an unknown port fails", hc("unforward", "4444"), r"isn't forwarded")


def check_image_steps(suite: Suite, home: Home) -> None:
    """
    A profile's own Dockerfile steps, as the real ``docker compose config``
    resolves them: HardCode's image built under a name of its own, the
    profile's built on it from its folder, and that folder never mounted
    into a container. Without the file, none of it.
    """
    hc = home.hardcode
    profile_dir = home.root / "profiles" / "default"
    folder = profile_dir / "image"

    suite.section("the profile's own Dockerfile steps")
    suite.check("a new profile has none", not folder.exists())
    suite.expect(
        "...and builds HardCode's image alone",
        hc("compose", "config", "--services"),
        r"^vscode$",
    )
    suite.check("...with no base to build", "vscode-base" not in hc("compose", "config", "--services").text.split())

    starter = hc("image", "save", "--", "FROM hardcode\nUSER root\nUSER dev\n")
    suite.expect("image save writes the first steps", starter, r"^saved .*Dockerfile")
    suite.expect("image says where it is", hc("image"), rf"Dockerfile steps: {re.escape(str(folder / 'Dockerfile'))}")

    config = hc("compose", "config")
    suite.expect("compose config validates", config)
    suite.expect(
        "HardCode's image is built under a name of its own",
        config,
        rf"image: hardcode-vscode-base:{home.id}-default$",
    )
    suite.expect("...and the profile's steps on it, from their folder", config, rf"context: {re.escape(str(folder))}$")
    suite.expect("...as FROM hardcode", config, r"^\s+hardcode: service:vscode-base$")
    suite.expect(
        "...labelled with the folder's hash",
        config,
        r"io\.github\.hardcode-app\.dockerfile: \"?[0-9a-f]{12}\"?$",
    )
    suite.check(
        "the base is built, never run",
        "vscode-base" in hc("compose", "config", "--services").text.split()
        and re.search(r"^  vscode-base:\n(    .*\n)*?    scale: 0$", config.text, re.MULTILINE) is not None,
        config.text,
    )

    # Every bind mount from the profile's folder is one of the two
    # compose.yaml hands over, the proxy folder and the extension list; none
    # is the folder, holds it, or is the steps'.
    sources = re.findall(r"^\s+source: (/.*)$", config.text, re.MULTILINE)
    from_profile = sorted({Path(source).name for source in sources if Path(source).is_relative_to(profile_dir)})
    holding = [source for source in sources if profile_dir.is_relative_to(Path(source))]
    suite.check(
        "no container mounts the profile's folder, or its Dockerfile steps",
        from_profile == ["proxy", "vscode-extensions.txt"] and holding == [],
        "\n".join(sources),
    )

    def label(text: str) -> str:
        """
        The hash of the steps in a resolved config's label; "" without one.
        """
        found = re.search(r"dockerfile: \"?([0-9a-f]{12})\"?$", text, re.MULTILINE)
        return found.group(1) if found else ""

    saved = hc("image", "save", "--", "FROM hardcode\nUSER root\nRUN apt-install jq\nUSER dev\n")
    suite.expect("image save writes the steps", saved, r"^saved .*Dockerfile")
    moved = label(hc("compose", "config").text)
    suite.check("...and the hash moves", moved not in ("", label(config.text)), moved)

    (folder / "Dockerfile").unlink()
    without = hc("compose", "config")
    suite.check(
        "without the file, the image is HardCode's alone",
        "vscode-base" not in without.text and "context: " + str(folder) not in without.text,
        without.text,
    )
    suite.expect("compose config still validates", hc("compose", "config", "--quiet"))


def check_packages(suite: Suite, home: Home) -> None:
    """
    A profile's own Ubuntu packages: in its .env, and a build arg of the
    VS Code image as the real ``docker compose config`` resolves it.
    """
    hc = home.hardcode

    suite.section("Ubuntu packages")
    suite.expect("package add", hc("package", "add", "jq", "postgresql-client"), r"added packages jq postgresql-client")
    suite.expect(
        ".env has them",
        Output(0, home.profile_file("default", ".env")),
        r"^VSCODE_PACKAGES=jq postgresql-client$",
    )
    suite.expect("the image is built with them", hc("compose", "config"), r"^\s+VSCODE_PACKAGES: jq postgresql-client$")
    suite.expect("packages lists them", hc("packages"), r"^jq\npostgresql-client$")
    suite.expect_failure("a name that isn't one", hc("package", "add", "Not_One"), r"not an Ubuntu package name")
    suite.expect("package remove", hc("package", "remove", "jq", "postgresql-client"), r"removed packages")
    suite.check(
        "none left is unset",
        re.search(r"^VSCODE_PACKAGES=", home.profile_file("default", ".env"), re.MULTILINE) is None,
    )


def check_devices(suite: Suite, home: Home) -> None:
    """
    Devices of this computer, as the real ``docker compose config``
    resolves the file the command writes for them: NVIDIA's request, with a
    stand-in docker whose Docker has NVIDIA's runtime, and KVM's node and
    group where this computer has /dev/kvm. Only the VS Code container gets
    them; with none on, there is no file.
    """
    hc = home.hardcode
    devices_file = home.root / "profiles" / "default" / "compose.devices.yaml"
    resolved_file = home.root.parent / "resolved.json"

    def resolved(env: Optional[dict[str, str]] = None) -> dict:
        """
        The profile's services, as Compose resolves its files: {} when it can't.
        """
        output = hc("compose", "config", "--format", "json", "--output", resolved_file, env=env)
        if not output.ok:
            return {}
        return json.loads(resolved_file.read_text(encoding="utf-8"))["services"]

    def reservations(service: dict) -> list:
        """
        The devices a service asks Docker for (deploy.resources.reservations).
        """
        return (((service.get("deploy") or {}).get("resources") or {}).get("reservations") or {}).get("devices") or []

    suite.section("devices")
    listed = hc("devices")
    suite.expect("devices lists the three, all off", listed, r"^off  kvm .*\n.*\noff  gpu .*\n.*\noff  nvidia ")
    suite.check("no file without devices", not devices_file.exists())
    suite.expect_failure(
        "a Docker that can't say it has NVIDIA's runtime doesn't give NVIDIA GPUs",
        hc("device", "on", "nvidia", "--yes"),
        r"can't be given to the VS Code container here: Docker can't say whether it has NVIDIA's runtime",
    )

    # A docker that says it has NVIDIA's runtime, and is the real one otherwise.
    bin_dir = home.root.parent / "nvidia-bin"
    bin_dir.mkdir()
    shutil.copyfile(DOCKER_WITH_NVIDIA, bin_dir / "docker")
    (bin_dir / "docker").chmod(0o755)
    nvidia = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "REAL_DOCKER": shutil.which("docker") or "docker"}

    suite.expect_failure(
        "without a terminal, on needs --yes",
        hc("device", "on", "nvidia", env=nvidia),
        r"weakens the sandbox, and there is no terminal to ask on",
    )
    suite.expect("device on nvidia --yes", hc("device", "on", "nvidia", "--yes", env=nvidia), r"^device nvidia on")
    suite.check("the file is written into the profile's folder", devices_file.is_file())
    suite.expect("compose config takes it", hc("compose", "config", "--quiet"))

    services = resolved()
    suite.check(
        "the VS Code container asks for every NVIDIA GPU",
        reservations(services.get("vscode", {})) == [{"capabilities": ["gpu"], "driver": "nvidia", "count": -1}],
        json.dumps(services.get("vscode", {}).get("deploy")),
    )
    suite.check(
        "...keeping its memory cap",
        services.get("vscode", {}).get("mem_limit") == str(8 * 1024**3),
        str(services.get("vscode", {}).get("mem_limit")),
    )
    suite.check(
        "the Docker daemon gets nothing",
        reservations(services.get("docker", {})) == [] and not services.get("docker", {}).get("group_add"),
        json.dumps(services.get("docker", {}).get("deploy")),
    )
    suite.expect_failure(
        "a start refuses a device the computer can't give, before anything starts",
        hc("up"),
        r"has device nvidia on, but Docker can't say whether it has NVIDIA's runtime",
    )

    # KVM's node and its group, where this computer has them; else the refusal.
    kvm = Path("/dev/kvm")
    if kvm.exists() and stat.S_ISCHR(kvm.stat().st_mode):
        suite.expect("device on kvm --yes", hc("device", "on", "kvm", "--yes"), r"^device kvm on")

        vscode = resolved().get("vscode", {})
        group = kvm.stat().st_gid
        suite.check(
            "the VS Code container gets /dev/kvm",
            {"source": "/dev/kvm", "target": "/dev/kvm", "permissions": "rwm"} in (vscode.get("devices") or []),
            json.dumps(vscode.get("devices")),
        )
        suite.check(
            "...and its group",
            group == 0 or str(group) in (vscode.get("group_add") or []),
            json.dumps(vscode.get("group_add")),
        )
        suite.expect("device off kvm", hc("device", "off", "kvm"), r"^device kvm off")
    else:
        suite.expect_failure(
            "a computer without /dev/kvm can't give it",
            hc("device", "on", "kvm", "--yes"),
            r"this computer has no /dev/kvm",
        )

    suite.expect("device off nvidia asks nothing", hc("device", "off", "nvidia"), r"^device nvidia off")
    suite.check("the last one off takes the file away", not devices_file.exists())
    suite.check(
        "and the VS Code container is as it was",
        reservations(resolved().get("vscode", {})) == [],
        resolved_file.read_text(encoding="utf-8")[:2000],
    )


def check_mounts(suite: Suite, home: Home) -> None:
    hc = home.hardcode

    # Beside the home, not in it: a folder in HardCode's home is never mounted.
    source = home.root.parent / "src"
    source.mkdir()

    suite.section("mounts")
    suite.expect(
        "mount (relative remote, under the home)",
        hc("mount", str(source), "work"),
        r"mounted .* at /home/dev/work",
    )

    # A mount goes into both the VS Code container and the Docker daemon, so that paths
    # in `docker run -v` from inside the VS Code container mean the same thing.
    config = hc("compose", "config").text
    suite.check("mounted in vscode and docker", count_lines(config, r"target: /home/dev/work") == 2, config)

    suite.expect("mount read-only", hc("mount", str(source), "/opt/data", "--ro"), r"read-only")
    config_lines = hc("compose", "config").text.splitlines()
    target = next((index for index, line in enumerate(config_lines) if "target: /opt/data" in line), None)
    near_target = config_lines[target : target + 4] if target is not None else []
    suite.check(
        "read_only in the resolved config",
        any("read_only: true" in line for line in near_target),
        "\n".join(near_target),
    )

    suite.expect_failure("mount duplicate", hc("mount", str(source), "work"), r"already mounted")
    suite.expect_failure("mount of a missing folder", hc("mount", "/nonexistent-xyz", "x"), r"not a folder")
    suite.expect_failure(
        "no mount of a folder that holds HardCode's home, whose Dockerfile steps run here",
        hc("mount", str(home.root.parent), "x"),
        r"holds HardCode's home .* or is in it",
    )
    suite.expect_failure(
        "...or of one in it",
        hc("mount", str(home.root / "profiles" / "default" / "image"), "x"),
        r"holds HardCode's home .* or is in it",
    )

    suite.expect("mount --all", hc("--all", "mount", str(source), "tools", "--ro"), rf"\[{NODE}\] mounted")
    suite.check(
        "mount present in every profile",
        len(home.profiles_whose("compose.override.yaml", r"/tools:ro")) == 3,
    )
    suite.expect("unmount --all", hc("--all", "unmount", "tools"), rf"\[{NODE}\] mount at /home/dev/tools removed")
    suite.expect("unmount by absolute path", hc("unmount", "/opt/data"), r"mount at /opt/data removed")
    suite.expect("unmount by relative path", hc("unmount", "work"), r"mount at /home/dev/work removed")
    suite.expect_failure("unmount of nothing", hc("unmount", "work"), r"nothing is mounted")


def check_docker_daemon(suite: Suite, home: Home) -> None:
    hc = home.hardcode

    def services() -> list[str]:
        return hc("compose", "config", "--services").text.split()

    suite.section("docker daemon")
    suite.check("docker is part of the project by default", "docker" in services())

    suite.expect("docker off edits .env", hc("docker", "off"), r"docker off")
    suite.check("the docker service is gone from the project", "docker" not in services())
    suite.expect("the VS Code container no longer depends on it", hc("compose", "config", "--quiet"))
    suite.expect("the VS Code container is told", hc("compose", "config"), r'ENABLE_DOCKER: "false"')
    suite.expect_failure(
        "no forwards to a daemon that isn't there",
        hc("forward", "8081", "docker", "80"),
        r"without the Docker daemon",
    )

    suite.expect("docker on", hc("docker", "on"), r"docker on")
    suite.check("the docker service is back", "docker" in services())


def check_languages(suite: Suite, home: Home) -> None:
    hc = home.hardcode

    suite.section("languages (no build)")
    suite.expect("disable two at once", hc("disable", "c#", "java", "--no-build"), r"dotnet off, java off")
    suite.check("off is unset in .env", "INSTALL_DOTNET=" not in home.profile_file("default", ".env"))

    # C#'s entries go. Java's all come with Android too, which is still on,
    # so they stay: an entry goes when no language that's on provides it.
    given = home.profile_file("default", "proxy/allowlist.txt")
    suite.check(
        "the proxy is no longer given their entries, but still those a language that's on provides",
        "api.nuget.org" not in given and "repo1.maven.org" in given,
        given,
    )
    suite.expect("languages says so", hc("languages"), r"^off\s+java ")

    suite.expect("enable with an alias", hc("enable", "c#", "--no-build"), r"dotnet on")
    suite.expect(
        "its entries are back", hc("allowlist"), r"^C# / \.NET \(comes with the language\)$\n  on   api\.nuget\.org$"
    )
    suite.expect_failure("unknown language", hc("enable", "cobol", "--no-build"), r"unknown language")
    suite.expect("--all enable", hc("--all", "enable", "python", "--no-build"), rf"\[{COPY}\] already so: python on")

    suite.section("extensions")
    suite.expect("extension add", hc("extension", "add", "eamodio.gitlens"), r"added extension eamodio\.gitlens")
    suite.expect(
        ".env has it", Output(0, home.profile_file("default", ".env")), r"^VSCODE_EXTENSIONS=eamodio\.gitlens$"
    )
    suite.expect("extensions lists it as the profile's own", hc("extensions"), r"^eamodio\.gitlens\s+custom$")
    suite.expect("one a language brings", hc("extension", "add", "golang.go"), r"already comes with Go")
    suite.expect_failure("not an id", hc("extension", "add", "gitlens"), r"not an extension id")
    suite.expect("extension remove", hc("extension", "remove", "eamodio.gitlens"), r"removed extension")


def check_removal(suite: Suite, home: Home) -> None:
    hc = home.hardcode

    # Removal takes the containers, volumes and image down first, so it needs
    # the daemon; the profiles' names are the scenario's own (see TAG).
    daemon = {"DOCKER_HOST": REAL_DAEMON}

    suite.section("remove")
    suite.expect("profile remove", hc("profile", "remove", COPY, "--yes", env=daemon), rf"profile '{COPY}' removed")
    suite.expect("profile remove", hc("profile", "remove", NODE, "--yes", env=daemon), rf"profile '{NODE}' removed")
    suite.expect_failure(
        "profile remove of an unknown profile",
        hc("profile", "remove", "nope", "--yes"),
        r"no profile",
    )
    suite.expect("default goes like any other", hc("profile", "remove", "default", "--yes", env=daemon), r"removed")
    suite.expect("and the home is empty again", hc("profiles"), r"^no profiles yet in ")

    suite.section("the port")
    suite.expect("a profile again", hc("profile", "add", "default", "--lang", ""), r"created from the template")
    suite.expect("port moves VS Code", hc("port", "12345"), r"VS Code on port 12345")
    suite.check("in .env", home.port("default") == "12345", home.port("default"))
    suite.expect("compose publishes it", hc("compose", "config"), r'published: "12345"')
    suite.expect("profile remove", hc("profile", "remove", "default", "--yes", env=daemon), r"removed")


def check_binary(suite: Suite) -> None:
    """
    The standalone binary, built as the release builds it, carries the
    stack: in a home and a cache of its own, run from a folder that isn't a
    checkout, it makes a profile from it, writes it out readable by
    everyone, and Compose takes the result.
    """
    suite.section("the standalone binary")

    with tempfile.TemporaryDirectory(prefix="hc-binary-") as temporary:
        folder = Path(temporary)
        built = harness.run([ROOT / "app" / "build.sh", "cli", folder / "dist"], timeout=900)
        suite.require("app/build.sh cli builds it", built.ok, built.tail(30))

        binary = next((folder / "dist").glob("hardcode-linux-*"))
        environment = {
            **os.environ,
            "HARDCODE_CONFIG": str(folder / "home"),
            "XDG_CACHE_HOME": str(folder / "cache"),
            "DOCKER_HOST": NO_DAEMON,
        }

        made = harness.run([binary, "profile", "add", "probe", "--lang", "docker"], env=environment, cwd=folder)
        suite.expect("it makes a profile from the stack it carries", made, r"created from the template")

        written = sorted((folder / "cache" / "hardcode" / "stack").glob("*/compose.yaml"))
        suite.check("...written out to its cache", len(written) == 1, "\n".join(map(str, written)))

        if written:
            squid_conf = written[0].parent / "images" / "squid" / "squid.conf"
            mode = squid_conf.stat().st_mode & 0o777
            suite.check("...readable by everyone (squid reads its config as squid)", mode == 0o644, oct(mode))

        config = harness.run([binary, "-p", "probe", "compose", "config", "--quiet"], env=environment, cwd=folder)
        suite.expect("and Compose takes it", config)


def check_wheel(suite: Suite) -> None:
    """
    The Python package, built as the release builds it, carries the stack
    too: installed into a virtual environment of its own (nothing fetched),
    and run from a folder that isn't a checkout, it makes a profile from it.
    """
    suite.section("the Python package")

    with tempfile.TemporaryDirectory(prefix="hc-wheel-") as temporary:
        folder = Path(temporary)
        built = harness.run([ROOT / "app" / "build.sh", "wheel", folder / "dist"], timeout=600)
        wheels = sorted((folder / "dist").glob("hardcode_cli-*-py3-none-any.whl"))
        suite.require("app/build.sh wheel builds it", built.ok and len(wheels) == 1, built.tail(30))

        with zipfile.ZipFile(wheels[0]) as wheel:
            names = set(wheel.namelist())
        carried = [
            "hardcode_cli/stack/compose.yaml",
            "hardcode_cli/stack/compose.image.yaml",
            "hardcode_cli/stack/images/squid/squid.conf",
            "hardcode_cli/stack/images/vscode/Dockerfile",
            "hardcode_cli/stack/template/.env",
        ]
        suite.check(
            "it carries the stack",
            all(name in names for name in carried),
            "\n".join(name for name in carried if name not in names),
        )

        venv = folder / "venv"
        created = harness.run([sys.executable, "-m", "venv", venv])
        installed = harness.run([venv / "bin" / "pip", "install", "--quiet", "--no-index", "--no-deps", wheels[0]])
        suite.require(
            "it installs into a virtual environment", created.ok and installed.ok, created.text + installed.text
        )

        environment = {
            **os.environ,
            "HARDCODE_CONFIG": str(folder / "home"),
            "XDG_CACHE_HOME": str(folder / "cache"),
            "DOCKER_HOST": NO_DAEMON,
        }
        hardcode = venv / "bin" / "hardcode"
        made = harness.run([hardcode, "profile", "add", "probe", "--lang", "docker"], env=environment, cwd=folder)
        suite.expect("installed, it makes a profile from the stack it carries", made, r"created from the template")

        written = sorted((folder / "cache" / "hardcode" / "stack").glob("*/compose.yaml"))
        suite.check("...written out to its cache", len(written) == 1, "\n".join(map(str, written)))


def run_checks(suite: Suite) -> None:
    suite.section("unit tests")
    cli = ROOT / "cli"
    if not harness.run_unittests(suite, "cli/tests", start=cli, top=cli):
        suite.abort("the unit tests failed; the scenario would only repeat that")

    with tempfile.TemporaryDirectory(prefix="hc-cli-") as temporary:
        home = Home(Path(temporary))

        # Each part builds on the profiles and files the previous ones left.
        check_profiles(suite, home)
        check_allowlist(suite, home)
        check_forwards(suite, home)
        check_mounts(suite, home)
        check_docker_daemon(suite, home)
        check_languages(suite, home)
        check_packages(suite, home)
        check_devices(suite, home)
        check_image_steps(suite, home)
        check_removal(suite, home)

    check_binary(suite)
    check_wheel(suite)


if __name__ == "__main__":
    harness.main("cli", run_checks)
