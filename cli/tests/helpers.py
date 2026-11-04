"""
Shared fixtures for the CLI's tests: a throwaway home and stack, a fake
``docker compose`` runner, a computer with the device nodes a test wants,
and the catalogue as a newer HardCode might have it.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Iterable, Optional
from unittest import mock

from hardcode_cli import devices, home, langs, reloader, stack, yamlio

REPO = Path(__file__).resolve().parent.parent.parent


def updated_catalogue(
    key: str = "rust",
    added: Iterable[str] = (),
    dropped: Iterable[str] = (),
    rw: Optional[tuple[str, ...]] = None,
) -> tuple[langs.Lang, ...]:
    """
    The catalogue as a newer HardCode might have it: the block of language
    *key* with *added* entries, without *dropped* ones, and with *rw* as its
    read-write entries if given. Patched in for ``langs.CATALOGUE``.
    """
    lang = langs.BY_KEY[key]
    domains = tuple(domain for domain in lang.domains if domain not in dropped) + tuple(added)
    newer = dataclasses.replace(lang, domains=domains, rw=lang.rw if rw is None else rw)

    return tuple(newer if item.key == key else item for item in langs.CATALOGUE)


# A cut-down compose.yaml: just the defaults the command reads from it.
TEMPLATE_COMPOSE = """\
# ${VSCODE_BIND:-127.0.0.1}:${VSCODE_PORT:-11111}  ${DEVNET_SUBNET:-172.31.253.0/24}  ${DNS_IP:-172.31.253.53}
# ${ENABLE_DOCKER:-true}  ${DEV_USER:-dev}
name: ${HARDCODE_PROJECT}
services: {}
"""

# A cut-down compose.image.yaml, for a profile with Dockerfile steps of its own.
TEMPLATE_IMAGE_COMPOSE = """\
services: {}
"""

# A cut-down template/.env: the reference of every setting, commented out.
TEMPLATE_ENV = """\
# --- Every setting, with its default -------------------------------------------
#VSCODE_PORT=11111
#DEV_USER=dev
"""

# A cut-down template/allowlist.txt: a few sections, entries with and
# without flags and comments, and commented-out entries.
TEMPLATE_ALLOWLIST = """\
# --- Anthropic / Claude Code ---------------------------------------------
api.anthropic.com           rw    # the API
claude.com                        # sign-in redirect page
# --- Git hosting: add the namespaces you work in, never the whole host ------
#github.com/myuser          rw
#ssh.github.com:443         ssh
raw.githubusercontent.com
# --- Container registries --------------------------------------------------
.gcr.io

# --- Custom ----------------------------------------------------------------
# Entries added by `hardcode allow` or the HardCode app land here.
"""

TEMPLATE_OVERRIDE = """\
# This profile's mounts and extra ports.
services: {}
"""


def fake_host(nodes: Optional[dict[str, int]] = None, platform: str = "linux") -> devices.Host:
    """
    A computer for the device tests: *platform* (``sys.platform``'s name
    for it), and the device nodes *nodes*, each with the group that owns
    it (``{"/dev/kvm": 108}``). The folders they are in are there too, and
    nothing else is: this machine's own /dev is never looked at.
    """
    nodes = dict(nodes or {})

    folders = set()
    for path in nodes:
        folders.update(str(parent) for parent in PurePosixPath(path).parents)

    def stat_of(path: str) -> SimpleNamespace:
        if path in nodes:
            return SimpleNamespace(st_mode=stat.S_IFCHR | 0o660, st_gid=nodes[path])
        if path in folders:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_gid=0)
        raise FileNotFoundError(path)

    def listdir(path: str) -> list[str]:
        if path not in folders:
            raise FileNotFoundError(path)

        inside = [PurePosixPath(item) for item in (*nodes, *folders) if item != path]
        return sorted({item.relative_to(path).parts[0] for item in inside if item.is_relative_to(path)})

    return devices.Host(platform=platform, stat=stat_of, listdir=listdir)


# Device nodes as a Linux computer with KVM and an AMD GPU has them: KVM's
# group, the video group's card and the render group's render node (and a
# folder of links beside them), and ROCm's compute node.
KVM_NODES = {"/dev/kvm": 108}
GPU_NODES = {
    "/dev/dri/card0": 44,
    "/dev/dri/renderD128": 992,
    "/dev/dri/by-path/pci-0000:03:00.0-render": 992,
}
KFD_NODES = {"/dev/kfd": 992}

# The runtimes docker info lists for a Docker without, and with, NVIDIA's.
RUNTIMES = {"io.containerd.runc.v2": {"path": "runc"}, "runc": {"path": "runc"}}
NVIDIA_RUNTIMES = {**RUNTIMES, "nvidia": {"path": "nvidia-container-runtime"}}


def yaml_available() -> bool:
    """
    Whether a YAML backend (yq, PyYAML or Docker) exists here.

    The override tests need one, as the command does.
    """
    try:
        yamlio.backend()
    except yamlio.YamlError:
        return False

    return True


def make_stack(path: Path) -> Path:
    """
    A minimal stack in *path*: compose.yaml, the template, and the (empty)
    image build contexts compose.yaml refers to.
    """
    path.mkdir(parents=True, exist_ok=True)
    (path / "compose.yaml").write_text(TEMPLATE_COMPOSE, encoding="utf-8")
    (path / "compose.image.yaml").write_text(TEMPLATE_IMAGE_COMPOSE, encoding="utf-8")

    for image in ("squid", "gateway", "docker", "vscode"):
        (path / "images" / image).mkdir(parents=True)
        (path / "images" / image / "Dockerfile").write_text(f"FROM scratch  # {image}\n", encoding="utf-8")

    template = path / "template"
    template.mkdir(parents=True)
    (template / ".env").write_text(TEMPLATE_ENV, encoding="utf-8")
    (template / "allowlist.txt").write_text(TEMPLATE_ALLOWLIST, encoding="utf-8")
    (template / "compose.override.yaml").write_text(TEMPLATE_OVERRIDE, encoding="utf-8")

    return path


class FakeRunner:
    """
    Stands in for ``subprocess.run``: records every command, and answers the
    few whose output the CLI reads.

    ``running`` and ``healthy`` describe the profile ``docker compose ps``
    reports, ``published`` is where ``docker compose port`` says the running
    gateway publishes VS Code, ``image_labels`` are the VS Code image's labels
    (``None``: no image), ``log_output`` is what ``docker compose logs``
    prints, ``networks`` are Docker's networks, with their subnets, and
    ``runtimes`` the runtimes ``docker info`` lists (``None``: it can't say,
    the daemon isn't running). A command containing any string in
    ``fail_on`` fails.
    """

    def __init__(self, running: bool = False, healthy: bool = True):
        self.calls: list[list[str]] = []
        self.running = running
        self.healthy = healthy
        self.published = "127.0.0.1:8000"
        self.image_labels: Optional[dict] = None  # the VS Code image's labels; None: there is no image
        self.extension_status: Optional[dict] = None  # what the running container's extension sync wrote
        self.log_output = ""
        self.networks: dict[str, str] = {}  # Docker's networks: name -> their subnets, space-separated
        self.runtimes: Optional[dict] = dict(RUNTIMES)  # what docker info lists; None: it can't say
        self.fail_on: set[str] = set()
        self.stdout_targets: list = []  # where each command's stdout went: "captured", a file, or None (inherited)

    def __call__(self, argv, cwd=None, env=None, capture_output=False, text=True, timeout=None, **options):
        self.calls.append(list(argv))
        self.stdout_targets.append("captured" if capture_output else options.get("stdout"))
        joined = " ".join(argv)

        for needle in self.fail_on:
            if needle in joined:
                return subprocess.CompletedProcess(argv, 1, "", f"simulated failure for {needle}")

        stdout, code = "", 0

        if " ps " in joined and "--format json" in joined:
            if self.running:
                health = "healthy" if self.healthy else "starting"
                stdout = (
                    f'{{"Service":"squid","State":"running","Status":"Up","Health":"{health}"}}\n'
                    '{"Service":"vscode","State":"running","Status":"Up","Health":""}\n'
                )
        elif " exec -T vscode " in joined and "hardcode-extensions-status" in joined:
            stdout = json.dumps(self.extension_status) if self.running and self.extension_status else ""
            code = 0 if self.running else 1
        elif " exec -T vscode " in joined and "connection-token" in joined:
            stdout = "tok3n\n" if self.running else ""
            code = 0 if self.running else 1
        elif " image inspect " in joined:
            stdout = json.dumps(self.image_labels) if self.image_labels is not None else ""
            code = 0 if self.image_labels is not None else 1
        elif " port gateway 8000" in joined:
            stdout = f"{self.published}\n" if self.running else ""
            code = 0 if self.running else 1
        elif " logs " in joined:
            stdout = self.log_output
        elif argv[1:2] == ["info"]:
            if self.runtimes is None:
                return subprocess.CompletedProcess(argv, 1, "", "Cannot connect to the Docker daemon. Is it running?\n")
            stdout = json.dumps(self.runtimes)
        elif argv[1:4] == ["network", "ls", "-q"]:
            stdout = "".join(f"{name}\n" for name in self.networks)
        elif argv[1:3] == ["network", "inspect"]:
            stdout = "".join(f"{self.networks[name]} \n" for name in argv[5:])

        return subprocess.CompletedProcess(argv, code, stdout, "")

    def commands(self, needle: str) -> list[str]:
        """
        The commands run so far that contain *needle*, as strings.
        """
        joined = (" ".join(call) for call in self.calls)
        return [command for command in joined if needle in command]


class HomeTestCase(unittest.TestCase):
    """
    A test case with a fresh home in ``self.root``, taken for the default
    one (its profiles keep the plain project names), and a minimal stack in
    ``self.stack``.
    """

    def setUp(self) -> None:
        if not yaml_available():
            self.skipTest("no YAML backend (yq, PyYAML or Docker); the command needs one too")

        # Resolved, as the command resolves a home: Windows hands out short
        # names (RUNNER~1), and macOS's temporary folder is under /private.
        self._tmp = str(Path(tempfile.mkdtemp(prefix="hardcode-test-")).resolve())
        self.root = Path(self._tmp) / "home"
        self.root.mkdir()
        self.stack = make_stack(Path(self._tmp) / "stack")
        self.runner = FakeRunner()

        stack.use(self.stack)
        self.addCleanup(stack.use, None)

        default = mock.patch.object(home, "default_home", return_value=self.root)
        default.start()
        self.addCleanup(default.stop)

        # No test starts a real background reload of a proxy: one that would
        # says so, rather than leave a process behind.
        spawn = mock.patch.object(reloader, "_spawn", side_effect=AssertionError("a test started a real reloader"))
        spawn.start()
        self.addCleanup(spawn.stop)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)
