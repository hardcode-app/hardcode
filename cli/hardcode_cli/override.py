"""
A profile's ``compose.override.yaml``: port forwards and host mounts.

The comment lines at the top of the file are kept; the rest is rewritten from
the parsed document, in the layout ``template/compose.override.yaml``
describes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import yamlio

# The gateway always forwards VS Code; it is listed first and never removed.
VSCODE_FORWARD = "8000:vscode:8000"

# Forwarded ports are published on loopback unless the profile says otherwise.
BIND = "${VSCODE_BIND:-127.0.0.1}"

HEADER = "# This profile's mounts and extra ports, merged into compose.yaml (hardcode mount and forward write them).\n"

# The services a forward can point at and a mount goes into. Mounts go into
# both, so that a path in `docker run -v` from the VS Code container means the same
# thing to the daemon.
HOSTS = ("vscode", "docker")


class OverrideError(ValueError):
    """
    A forward or mount that can't be applied.
    """


@dataclass(frozen=True)
class Forward:
    """
    A host port forwarded by the gateway to a port on the VS Code container or docker.
    """

    port: int
    host: str
    target: int

    @property
    def spec(self) -> str:
        """
        The forward as ``GATEWAY_FORWARDS`` lists it: ``port:host:target``.
        """
        return f"{self.port}:{self.host}:{self.target}"


@dataclass(frozen=True)
class Mount:
    """
    A host folder mounted into the VS Code container (and the Docker daemon).
    """

    local: str
    remote: str
    mode: str = ""

    @property
    def spec(self) -> str:
        """
        The mount as a Compose volume: ``local:remote[:mode]``.
        """
        spec = f"{self.local}:{self.remote}"
        return f"{spec}:{self.mode}" if self.mode else spec


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------


def load(path: Path) -> tuple[str, dict]:
    """
    The file's leading comments and its parsed document.

    A file that doesn't exist reads as the standard header and an empty
    document. The document always has a ``services`` map.
    """
    if not path.exists():
        return HEADER, {"services": {}}

    text = path.read_text(encoding="utf-8")

    header_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            header_lines.append(line)
        else:
            break

    try:
        document = yamlio.load(text) or {}
    except yamlio.YamlError as error:
        raise yamlio.YamlError(f"{path}: {error}") from error

    # A hand-edited file may hold anything; only a map of services is ours.
    if not isinstance(document, dict):
        document = {}
    if not isinstance(document.get("services"), dict):
        document["services"] = {}

    header = "\n".join(header_lines).rstrip()
    return (header + "\n" if header else ""), document


def tidy(document: dict) -> dict:
    """
    Drop the empty lists, maps and services that removals leave behind.
    """
    services = document.setdefault("services", {})

    for name in list(services):
        service = services[name]
        if not isinstance(service, dict):
            del services[name]
            continue

        for key in ("volumes", "ports", "environment"):
            if key in service and not service[key]:
                del service[key]

        if not service:
            del services[name]

    return document


def save(path: Path, header: str, document: dict) -> None:
    """
    Write the header and the tidied document back to the file.
    """
    tidy(document)

    # Compose needs `services` to be a map, even an empty one.
    body = yamlio.dump(document) if document.get("services") else "services: {}\n"
    path.write_text(header + body, encoding="utf-8")


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------


def split_spec(spec: str) -> list[str]:
    """
    Split an ``a:b:c`` spec on its colons.

    Two kinds of colon don't split: those inside ``${...}``, as in
    ``/home/${DEV_USER:-dev}/git``, and the one after a Windows drive letter,
    as in ``C:\\src``.
    """
    parts: list[str] = []
    current = ""
    depth = 0  # how many ${ ... } we are inside

    for index, char in enumerate(spec):
        following = spec[index + 1 : index + 2]

        if char == "$" and following == "{":
            depth += 1
        elif char == "}" and depth:
            depth -= 1

        after_drive_letter = len(current) == 1 and current.isalpha() and following in ("\\", "/")

        if char == ":" and not depth and not after_drive_letter:
            parts.append(current)
            current = ""
        else:
            current += char

    parts.append(current)
    return parts


def parse_volume(spec: object) -> Optional[Mount]:
    """
    A ``local:remote[:mode]`` volume string as a ``Mount``.

    Anything else (a named volume, the long syntax as a map) is ``None``.
    """
    if not isinstance(spec, str):
        return None

    parts = split_spec(spec)

    mode = ""
    if len(parts) > 2 and parts[-1] in ("ro", "rw"):
        mode = parts.pop()

    if len(parts) < 2:
        return None

    remote = parts.pop()
    return Mount(local=":".join(parts), remote=remote, mode=mode)


def _valid_port(value: int) -> bool:
    """
    Whether *value* is a TCP port number.
    """
    return isinstance(value, int) and 1 <= value <= 65535


# ---------------------------------------------------------------------------
# Forwards
# ---------------------------------------------------------------------------


def get_forwards(document: dict) -> list[Forward]:
    """
    The forwards other than VS Code's, from the gateway's ``GATEWAY_FORWARDS``.
    """
    gateway = document.get("services", {}).get("gateway") or {}
    environment = gateway.get("environment") or {}
    value = str(environment.get("GATEWAY_FORWARDS") or "")

    forwards = []
    for item in value.split():
        if item == VSCODE_FORWARD:
            continue

        parts = item.split(":")
        if len(parts) != 3 or not parts[0].isdigit() or not parts[2].isdigit():
            continue  # hand-edited, and not in a form we manage

        forwards.append(Forward(int(parts[0]), parts[1], int(parts[2])))

    return forwards


def _host_port(spec: object) -> str:
    """
    The host port of a ``[ip:]host:container`` ports entry.
    """
    parts = split_spec(str(spec))
    return parts[-2] if len(parts) >= 2 else ""


def set_forwards(document: dict, forwards: list[Forward]) -> None:
    """
    Write ``GATEWAY_FORWARDS`` (VS Code's first) and the matching ``ports``.

    Ports entries that aren't strings (the long syntax) are left alone.
    """
    gateway = document.setdefault("services", {}).setdefault("gateway", {})

    if forwards:
        environment = gateway.setdefault("environment", {})
        environment["GATEWAY_FORWARDS"] = " ".join([VSCODE_FORWARD, *(forward.spec for forward in forwards)])
    elif "environment" in gateway:
        gateway["environment"].pop("GATEWAY_FORWARDS", None)

    # Keep the published ports that still belong to a forward...
    wanted = {str(forward.port) for forward in forwards}
    ports = [port for port in gateway.get("ports", []) if not isinstance(port, str) or _host_port(port) in wanted]

    # ...and publish the forwards that aren't yet.
    for forward in forwards:
        if not any(_host_port(port) == str(forward.port) for port in ports):
            ports.append(f"{BIND}:{forward.port}:{forward.port}")

    gateway["ports"] = ports


def add_forward(path: Path, port: int, host: str = "vscode", target: Optional[int] = None) -> Forward:
    """
    Forward host port *port* to *target* (the same port by default) on *host*.
    """
    target = port if target is None else target

    if not _valid_port(port) or not _valid_port(target):
        raise OverrideError("ports must be 1-65535")
    if host not in HOSTS:
        raise OverrideError("host must be vscode or docker")
    if port == 8000:
        raise OverrideError("8000 is the gateway's VS Code port inside it; hardcode port moves the one published here")

    header, document = load(path)
    forwards = get_forwards(document)
    if any(existing.port == port for existing in forwards):
        raise OverrideError(f"port {port} is already forwarded")

    forward = Forward(port, host, target)
    set_forwards(document, [*forwards, forward])
    save(path, header, document)

    return forward


def remove_forward(path: Path, port: int) -> None:
    """
    Stop forwarding host port *port*.
    """
    header, document = load(path)

    forwards = get_forwards(document)
    if not any(forward.port == port for forward in forwards):
        raise OverrideError(f"port {port} isn't forwarded")

    set_forwards(document, [forward for forward in forwards if forward.port != port])
    save(path, header, document)


# ---------------------------------------------------------------------------
# Mounts
# ---------------------------------------------------------------------------


def normalise_local(local: str, home: Optional[str] = None, windows: Optional[bool] = None) -> str:
    """
    A folder on this machine, as an absolute path for the override.

    The folder must exist. A path under the home directory is written with a
    ``${HOME}`` prefix, so the file stays portable between users (except on
    Windows, where Compose doesn't expand it the same way).
    """
    local = local.strip()
    if not local:
        raise OverrideError("local folder is required")

    home = os.path.expanduser("~") if home is None else home
    windows = os.name == "nt" if windows is None else windows

    if local == "~" or local.startswith("~/") or local.startswith("~\\"):
        local = home + local[1:]

    folder = Path(local)
    if not folder.is_dir():
        raise OverrideError(f"'{local}' is not a folder on this machine")

    resolved = folder.resolve()

    # Compared with the home as it resolves too: a home reached through a
    # symlink (macOS's /var is /private/var) still holds the folder.
    home_dir = Path(home).resolve() if home else None
    if not windows and home_dir and resolved != home_dir and resolved.is_relative_to(home_dir):
        return "${HOME}/" + resolved.relative_to(home_dir).as_posix()

    return str(resolved)


# How a path in the VS Code container may name the VS Code container user, for Compose to fill in.
DEV_USER_VARIABLES = ("${DEV_USER:-dev}", "${DEV_USER}")


def _with_user(remote: str, dev_user: str) -> str:
    """
    *remote* with the VS Code container user's name in place of ``${DEV_USER:-dev}``.

    *dev_user* is the name, or that variable itself when the profile's .env
    doesn't set one, in which case Compose's default applies.
    """
    name = "dev" if dev_user in DEV_USER_VARIABLES else dev_user

    for variable in DEV_USER_VARIABLES:
        remote = remote.replace(variable, name)

    return remote


def same_remote(first: str, second: str, dev_user: str) -> bool:
    """
    Whether two paths in the VS Code container are the same folder. A mount written by
    hand as ``/home/${DEV_USER:-dev}/git`` is the one ``unmount git`` names,
    and the one the app lists.
    """
    return _with_user(first, dev_user) == _with_user(second, dev_user)


def normalise_remote(remote: str, dev_user: str = "${DEV_USER:-dev}") -> str:
    """
    An absolute path in the VS Code container; a relative one lands under the home directory.
    """
    remote = remote.strip().rstrip("/")

    if not remote:
        raise OverrideError("remote path is required")

    # The ':' in ${DEV_USER:-dev} is Compose's; any other would split the volume.
    if ":" in _with_user(remote, dev_user):
        raise OverrideError("remote path can't contain ':'")

    if not remote.startswith("/"):
        relative = remote[2:] if remote.startswith("./") else remote
        remote = f"/home/{dev_user}/{relative}"

    return remote


def get_mounts(document: dict) -> list[Mount]:
    """
    The host mounts, as the VS Code container service lists them.
    """
    vscode = document.get("services", {}).get("vscode") or {}
    volumes = vscode.get("volumes") or []

    return [mount for mount in map(parse_volume, volumes) if mount]


def add_mount(
    path: Path,
    local: str,
    remote: str,
    read_only: bool = False,
    dev_user: str = "${DEV_USER:-dev}",
) -> Mount:
    """
    Mount folder *local* at *remote* in the VS Code container and the Docker daemon.
    """
    mount = Mount(
        local=normalise_local(local),
        remote=normalise_remote(remote, dev_user),
        mode="ro" if read_only else "",
    )

    header, document = load(path)

    for host in HOSTS:
        service = document["services"].setdefault(host, {})
        volumes = service.setdefault("volumes", [])

        for existing in map(parse_volume, volumes):
            if existing and same_remote(existing.remote, mount.remote, dev_user):
                raise OverrideError(f"{mount.remote} is already mounted in {host}")

        volumes.append(mount.spec)

    save(path, header, document)
    return mount


def remove_mount(path: Path, remote: str, dev_user: str = "${DEV_USER:-dev}") -> str:
    """
    Remove whatever is mounted at *remote*, from both services.

    Returns the absolute path that was unmounted.
    """
    target = normalise_remote(remote, dev_user)
    header, document = load(path)

    def mounted_at_target(volume: object) -> bool:
        mount = parse_volume(volume)
        return mount is not None and same_remote(mount.remote, target, dev_user)

    found = False
    for host in HOSTS:
        service = document["services"].get(host)
        if not service or not isinstance(service.get("volumes"), list):
            continue

        kept = [volume for volume in service["volumes"] if not mounted_at_target(volume)]
        if len(kept) != len(service["volumes"]):
            found = True

        service["volumes"] = kept

    if not found:
        raise OverrideError(f"nothing is mounted at {target}")

    save(path, header, document)
    return target
