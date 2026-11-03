"""
Devices of the user's computer that a profile's VS Code container may use:
KVM, an AMD or Intel GPU, and NVIDIA GPUs.

Each one weakens the sandbox: code inside can reach a part of the computer's
kernel, or a driver, that the sandbox otherwise keeps it from. So each is a
switch of its own, off unless the profile turns it on, and turning one on
says what it gives code in the sandbox.

They are ``VSCODE_DEVICES`` in the profile's ``.env``, keys separated by
commas. compose.yaml knows nothing of them: for a profile with any on,
``hardcode`` writes ``compose.devices.yaml`` into the profile's folder (by
HardCode, never by hand, as it writes ``proxy-allowlist.txt``), at every
start and every change, and merges it after the stack's files. Only the
VS Code container gets them; the Docker daemon beside it doesn't.

Whether this computer has a device is asked of a ``Host`` (its platform and
its device nodes) and of what ``docker info`` says, both replaceable, so
that tests can be any computer. Only Linux gives containers its devices:
Docker on macOS and Windows runs them in a virtual machine.

Only on Compose: Kubernetes profiles have no devices.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

# The .env key, and the file written into the profile's folder from it.
ENV_KEY = "VSCODE_DEVICES"
FRAGMENT = "compose.devices.yaml"

# The device nodes each device passes in.
KVM = "/dev/kvm"
DRI = "/dev/dri"
KFD = "/dev/kfd"

# The runtime NVIDIA's container toolkit adds to Docker, as docker info names it.
NVIDIA_RUNTIME = "nvidia"

# The platforms the error messages name; any other is "this system".
PLATFORM_NAMES = {"darwin": "macOS", "win32": "Windows", "cygwin": "Windows"}


class DeviceError(ValueError):
    """
    A device that isn't one, or that this computer can't give the VS Code
    container.
    """


@dataclass(frozen=True)
class Device:
    """
    One device: its key, its name, what it gives the VS Code container (in
    a line), and the warning said before it is turned on (a paragraph).
    """

    key: str
    name: str
    summary: str
    warning: str


CATALOGUE = (
    Device(
        key="kvm",
        name="KVM",
        summary="/dev/kvm: virtual machines, such as the Android emulator",
        warning=(
            "Code in the sandbox can use your kernel's virtualisation interface (/dev/kvm), to run virtual "
            "machines such as the Android emulator. A bug in that interface is a way out of the sandbox, onto "
            "your computer."
        ),
    ),
    Device(
        key="gpu",
        name="GPU",
        summary="/dev/dri, and /dev/kfd for ROCm: AMD and Intel GPUs",
        warning=(
            "Code in the sandbox can use your graphics driver (/dev/dri, and /dev/kfd for ROCm), a large part "
            "of your kernel: a bug in it is a way out of the sandbox. GPU memory may not be cleared between "
            "programs, so code in the sandbox may see what your other programs left in it."
        ),
    ),
    Device(
        key="nvidia",
        name="NVIDIA",
        summary="every NVIDIA GPU, through NVIDIA's container toolkit",
        warning=(
            "Code in the sandbox can use your NVIDIA GPUs. To give them to the container, NVIDIA's container "
            "toolkit runs as root on your computer, and it has had container escapes: CVE-2024-0132 and "
            "CVE-2025-23266."
        ),
    ),
)

BY_KEY = {device.key: device for device in CATALOGUE}
KEYS = tuple(device.key for device in CATALOGUE)


@dataclass(frozen=True)
class Host:
    """
    This computer, as far as the devices need to know it: its platform
    (``sys.platform``'s name for it) and its device nodes, through ``stat``
    and ``listdir`` as ``os`` has them. Tests give a computer of their own.
    """

    platform: str = sys.platform
    stat: Callable[[str], os.stat_result] = os.stat
    listdir: Callable[[str], list[str]] = os.listdir

    def node(self, path: str) -> Optional[os.stat_result]:
        """
        What ``stat`` says of *path*, or ``None`` when it isn't there.
        """
        try:
            return self.stat(path)
        except OSError:
            return None

    def is_device(self, path: str) -> bool:
        """
        Whether *path* is a character device: what a device node is.
        """
        found = self.node(path)
        return found is not None and stat.S_ISCHR(found.st_mode)

    def is_folder(self, path: str) -> bool:
        """
        Whether *path* is a folder.
        """
        found = self.node(path)
        return found is not None and stat.S_ISDIR(found.st_mode)


@dataclass(frozen=True)
class Runtimes:
    """
    The runtimes this computer's Docker has, as ``docker info`` lists them;
    ``names`` is ``None`` when it couldn't say, and ``problem`` why.
    """

    names: Optional[tuple[str, ...]]
    problem: str = ""


def parse_runtimes(output: str) -> Runtimes:
    """
    The runtimes in what ``docker info --format '{{json .Runtimes}}'``
    printed: an object, one key per runtime.
    """
    try:
        listed = json.loads(output or "null")
    except json.JSONDecodeError:
        return Runtimes(None, "docker info printed something that isn't its list of runtimes")

    if not isinstance(listed, dict):
        return Runtimes(None, "docker info listed no runtimes")

    return Runtimes(tuple(sorted(listed)))


def check(name: str) -> str:
    """
    The device's key for *name* (spaces and case aside); an error when it
    isn't one.
    """
    key = name.strip().lower()
    if key not in BY_KEY:
        raise DeviceError(f"'{name}' is not a device: {', '.join(KEYS)} (hardcode devices lists them)")

    return key


def parse(text: str) -> list[str]:
    """
    The keys a ``VSCODE_DEVICES`` value names, lower-cased, in their order
    and without repeats; read, not checked (see ``unknown``).
    """
    named = [word.strip().lower() for word in text.replace(",", " ").split()]
    return list(dict.fromkeys(word for word in named if word))


def ordered(keys: Iterable[str]) -> list[str]:
    """
    The devices among *keys*, in the catalogue's order; what isn't one is
    left out.
    """
    wanted = set(keys)
    return [key for key in KEYS if key in wanted]


def unknown(keys: Iterable[str]) -> list[str]:
    """
    What *keys* name that isn't a device.
    """
    return [key for key in keys if key not in BY_KEY]


def join(keys: Iterable[str]) -> str:
    """
    The ``VSCODE_DEVICES`` value for *keys*: in the catalogue's order,
    comma-separated.
    """
    return ",".join(ordered(keys))


def unavailable(key: str, host: Host, runtimes: Callable[[], Runtimes]) -> str:
    """
    Why this computer can't give the VS Code container the device *key*,
    and what to do about it; ``""`` when it can.

    *runtimes* is asked only when it matters (NVIDIA's, on Linux): it runs
    ``docker info``.
    """
    if host.platform != "linux":
        where = PLATFORM_NAMES.get(host.platform, "this system")
        return (
            f"devices are given to containers on Linux only: on {where}, Docker runs them in a virtual machine "
            "that doesn't have this computer's devices"
        )

    if key == "kvm" and not host.is_device(KVM):
        return (
            f"this computer has no {KVM}: switch virtualisation (Intel VT-x or AMD-V) on in its firmware "
            "settings, and load the kvm_intel or kvm_amd module"
        )

    if key == "gpu" and not host.is_folder(DRI):
        return f"this computer has no {DRI}: no GPU driver with render nodes is loaded (amdgpu, i915 or xe)"

    if key == "nvidia":
        found = runtimes()
        if found.names is None:
            return f"Docker can't say whether it has NVIDIA's runtime ({found.problem}): start Docker, then try again"
        if NVIDIA_RUNTIME not in found.names:
            return (
                "Docker has no NVIDIA runtime: install NVIDIA's container toolkit, "
                "run sudo nvidia-ctk runtime configure --runtime=docker, and restart Docker"
            )

    return ""


def states(enabled: Iterable[str], host: Host, runtimes: Callable[[], Runtimes]) -> list[dict]:
    """
    Every device, for ``hardcode devices`` and the app: its key, name,
    summary and warning, whether the profile has it on (``enabled``), and
    whether this computer can give it (``available``, with the ``reason``
    when it can't).
    """
    on = set(enabled)
    rows = []

    for device in CATALOGUE:
        reason = unavailable(device.key, host, runtimes)
        rows.append(
            {
                "key": device.key,
                "name": device.name,
                "summary": device.summary,
                "warning": device.warning,
                "enabled": device.key in on,
                "available": not reason,
                "reason": reason,
            }
        )

    return rows


def nodes(keys: Iterable[str], host: Host) -> list[str]:
    """
    The device nodes *keys* pass into the VS Code container: /dev/kvm for
    KVM; /dev/dri for a GPU, with /dev/kfd when the computer has it (AMD's
    ROCm computes through it). NVIDIA's are the toolkit's to give.
    """
    wanted = ordered(keys)
    found = []

    if "kvm" in wanted:
        found.append(KVM)

    if "gpu" in wanted:
        found.append(DRI)
        if host.is_device(KFD):
            found.append(KFD)

    return found


def groups(keys: Iterable[str], host: Host) -> list[int]:
    """
    The groups that own the device nodes *keys* pass in (for a GPU, every
    node in /dev/dri: the render group's and the video group's), so that
    the VS Code container's user, who isn't root, can open them.

    Root's group is never one: it would give the user more than a device.
    A node only root's group may open is reached with sudo, inside.
    """
    paths = []

    for node in nodes(keys, host):
        if node != DRI:
            paths.append(node)
            continue

        try:
            entries = sorted(host.listdir(DRI))
        except OSError:
            entries = []
        paths.extend(f"{DRI}/{entry}" for entry in entries)

    found = set()
    for path in paths:
        details = host.node(path)
        if details is not None and stat.S_ISCHR(details.st_mode) and details.st_gid != 0:
            found.add(details.st_gid)

    return sorted(found)


def fragment(keys: Iterable[str], host: Host) -> str:
    """
    The Compose file that gives the VS Code container the devices *keys*:
    their nodes and their groups, and for NVIDIA a request for every GPU,
    which NVIDIA's toolkit answers. ``""`` when there are none.

    Only the VS Code container's settings are in it: the Docker daemon
    beside it gets nothing.
    """
    wanted = ordered(keys)
    if not wanted:
        return ""

    lines = [
        "# Written by HardCode from VSCODE_DEVICES in this profile's .env, at every",
        "# start and every change (hardcode device on|off NAME): edits here are lost.",
        f"# It gives the VS Code container devices of this computer: {', '.join(wanted)}.",
        "# Each one weakens the sandbox.",
        "services:",
        "  vscode:",
    ]

    passed = nodes(wanted, host)
    if passed:
        lines.append("    devices:")
        lines.extend(f"      - {node}:{node}" for node in passed)

    owners = groups(wanted, host)
    if owners:
        lines.append("    group_add:")
        lines.extend(f'      - "{gid}"' for gid in owners)

    if "nvidia" in wanted:
        lines += [
            "    deploy:",
            "      resources:",
            "        reservations:",
            "          devices:",
            "            - driver: nvidia",
            "              count: all",
            "              capabilities: [gpu]",
        ]

    return "\n".join(lines) + "\n"


def write(path: Path, keys: Iterable[str], host: Host) -> bool:
    """
    Write the Compose file for *keys* to *path*, or delete it when there
    are none: whether that changed anything.
    """
    text = fragment(keys, host)
    before = path.read_text(encoding="utf-8") if path.exists() else None

    if not text:
        if before is None:
            return False
        path.unlink()
        return True

    if before == text:
        return False

    path.write_text(text, encoding="utf-8")
    return True
