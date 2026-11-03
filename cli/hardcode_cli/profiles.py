"""
Profiles: the folders under ``profiles/`` in the user's HardCode home (see
``home``).

Each profile is a separate environment with its own ``.env`` (only what
differs from compose.yaml's defaults), ``allowlist.txt`` and
``compose.override.yaml``, and its own containers, volumes, port and image.
Two more files are written from those for the containers: the allowlist the
proxy is given, and the VS Code extension list; and, while it has devices of
this computer on, the Compose file that gives them to the VS Code container
(see ``devices``). Its own Dockerfile steps, if it has any, are in
``image/`` (see ``dockerfile``). A new profile starts from the stack's
template (see ``stack``), or as a copy of another.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from . import allowlist, devices, dockerfile, envfile, lan, langs, override, stack, yamlio

# A DNS label of up to 32 characters, because the name ends up in container
# and resource names. The admin and the CRD also want a leading letter
# (Kubernetes Service names must start with one); Compose doesn't.
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
NAME_RULE = "a-z, 0-9 and -, up to 32 characters, not ending in -"

# The files a new profile copies from the template or from its source; its
# .env is written apart (see _new_env).
FILES = ("allowlist.txt", "compose.override.yaml")

# The first VS Code port profiles get: clear of the ports dev servers use,
# and of the ranges systems hand out to outgoing connections.
FIRST_PORT = 11111

# What a new profile's .env starts with, above the reference of every setting.
ENV_HEADER = """\
# This profile's settings: only what differs from HardCode's defaults.
# HardCode picked the port and the internal subnet, free when the profile was
# made; change them if you like (hardcode port <number> moves the port).
"""

Warn = Callable[[str], None]


class ProfileError(ValueError):
    """
    A profile that doesn't exist, can't be created, or has a bad name.
    """


@dataclass(frozen=True)
class Profile:
    """
    One profile and where its files are.
    """

    name: str
    dir: Path
    home_id: str = ""

    @property
    def env_file(self) -> Path:
        return self.dir / ".env"

    @property
    def allowlist_file(self) -> Path:
        return self.dir / "allowlist.txt"

    @property
    def override_file(self) -> Path:
        return self.dir / "compose.override.yaml"

    @property
    def proxy_allowlist_file(self) -> Path:
        """
        The allowlist the proxy is given (mounted read-only into its
        renderer): ``allowlist.txt`` with the entries HardCode provides.
        """
        return self.dir / "proxy-allowlist.txt"

    @property
    def proxy_applied_file(self) -> Path:
        """
        A digest of the proxy allowlist the running proxy last loaded, as
        far as this command saw it load one: written after a healthy start
        or reload, never before.
        """
        return self.dir / "proxy-allowlist.applied"

    @property
    def extensions_file(self) -> Path:
        """
        The extension list the VS Code container watches (mounted read-only),
        written from the profile's languages and its own extensions.
        """
        return self.dir / "vscode-extensions.txt"

    @property
    def devices_file(self) -> Path:
        """
        The Compose file that gives the VS Code container the devices of
        this computer the profile has on (``devices.fragment``), merged
        after the stack's files; there is none while it has none on.
        """
        return self.dir / devices.FRAGMENT

    @property
    def image_dir(self) -> Path:
        """
        The folder of the profile's own Dockerfile steps, and the context
        they are built in: the Dockerfile, with the files it copies beside
        it. Never mounted into a container: a rebuild runs what is in it.
        """
        return self.dir / dockerfile.FOLDER

    @property
    def dockerfile(self) -> Path:
        """
        The profile's own Dockerfile steps, built on HardCode's image; the
        profile has none while it doesn't exist.
        """
        return self.image_dir / dockerfile.FILE

    @property
    def project_name(self) -> str:
        """
        The Compose project name: one set of containers and volumes per
        profile, with the home's id outside the default home.
        """
        return f"hardcode-{self.home_id}-{self.name}" if self.home_id else f"hardcode-{self.name}"

    @property
    def vscode_tag(self) -> str:
        """
        The tag of the profile's VS Code image, ``hardcode-vscode:<tag>``,
        with the home's id outside the default home.
        """
        return f"{self.home_id}-{self.name}" if self.home_id else self.name


@dataclass(frozen=True)
class Created:
    """
    What ``create`` made: the profile, where it was copied from, the port
    and subnet it was given, and the languages it is built around.
    """

    name: str
    source: str
    port: int
    subnet: str
    languages: tuple[str, ...] = ()


def profiles_dir(root: Path) -> Path:
    """
    Where the home *root* keeps its profiles.
    """
    return root / "profiles"


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name))


def list_profiles(root: Path) -> list[str]:
    """
    The profiles' names: ``default`` first, the rest in alphabetical order.
    """
    base = profiles_dir(root)
    if not base.is_dir():
        return []

    names = [path.name for path in base.iterdir() if _is_profile(path) and valid_name(path.name)]
    return sorted(names, key=lambda name: (name != "default", name))


# The file a profile's lock is taken on, in its folder.
LOCK_FILE = ".lock"


def _is_profile(folder: Path) -> bool:
    """
    Whether *folder* holds a profile: anything but a lock file. A profile
    removed under its lock keeps that file until the lock is let go.
    """
    return folder.is_dir() and any(path.name != LOCK_FILE for path in folder.iterdir())


def exists(root: Path, name: str) -> bool:
    return valid_name(name) and _is_profile(profiles_dir(root) / name)


def ports_by_profile(root: Path, exclude: str = "", warn: Optional[Warn] = None) -> dict[str, set[int]]:
    """
    The host ports each profile other than *exclude* publishes: its VS Code
    port and its extra forwards.

    Keys a ``.env`` leaves unset count as compose.yaml's defaults. An override
    that can't be parsed is reported through *warn* and its forwards left out,
    so one damaged file doesn't stop the command for every other profile.
    Without *warn*, the error is raised.
    """
    ports: dict[str, set[int]] = {}

    for name in list_profiles(root):
        if name == exclude:
            continue

        other = Profile(name, profiles_dir(root) / name)
        published: set[int] = set()

        try:
            published.add(int(envfile.get(other.env_file, "VSCODE_PORT")))
        except ValueError:
            pass

        # Parsing the override needs a YAML backend, which may mean starting a
        # container; only do it when the file can hold forwards at all.
        if other.override_file.exists() and "gateway" in other.override_file.read_text(encoding="utf-8"):
            try:
                _, document = override.load(other.override_file)
                published.update(forward.port for forward in override.get_forwards(document))
            except yamlio.YamlError as error:
                if warn is None:
                    raise
                warn(f"warning: {error}; its forwards aren't counted as taken")

        ports[name] = published

    return ports


def reserved_ports(root: Path, exclude: str = "", warn: Optional[Warn] = None) -> set[int]:
    """
    Every host port the profiles other than *exclude* publish.
    """
    return set().union(*ports_by_profile(root, exclude, warn).values())


def _overlaps(subnet: str, taken: Iterable[str]) -> bool:
    """
    Whether *subnet* shares any address with one of *taken* (what isn't a
    subnet is ignored).
    """
    candidate = ipaddress.ip_network(subnet, strict=False)

    for other in taken:
        try:
            network = ipaddress.ip_network(other, strict=False)
        except ValueError:
            continue

        if network.version == candidate.version and network.overlaps(candidate):
            return True

    return False


def allocate(
    root: Path,
    exclude: str = "",
    first: int = FIRST_PORT,
    last: int = 65535,
    warn: Optional[Warn] = None,
    taken_subnets: Iterable[str] = (),
) -> tuple[int, str]:
    """
    A VS Code port and an internal subnet for a profile.

    The port is the lowest from *first* that no other profile publishes (for
    VS Code or a forward); one another program holds shows when the profile
    starts, and ``hardcode port`` moves it. The subnet is
    ``172.31.N.0/24``, counting N down from 253, skipping the other
    profiles' and any that overlaps *taken_subnets* (Docker's networks,
    which Docker would refuse to overlap).
    """
    others = [Profile(name, profiles_dir(root) / name) for name in list_profiles(root) if name != exclude]
    reserved = reserved_ports(root, exclude, warn)

    port = first
    while port <= last and port in reserved:
        port += 1
    if port > last:
        raise ProfileError(f"no free port between {first} and {last}")

    taken = {envfile.get(profile.env_file, "DEVNET_SUBNET") for profile in others} | set(taken_subnets)
    third = 253
    while third > 0 and _overlaps(f"172.31.{third}.0/24", taken):
        third -= 1
    if third == 0:
        # Say what takes them: a Docker network is the user's to remove.
        in_docker = sorted({subnet for subnet in taken_subnets if _overlaps("172.31.0.0/16", [subnet])})
        if in_docker:
            raise ProfileError(
                f"no free internal subnet in 172.31.0.0/16: Docker's networks use {', '.join(in_docker)} "
                "(docker network ls lists them; docker network prune removes the unused ones)"
            )
        raise ProfileError("no free internal subnet in 172.31.0.0/16: the other profiles use every one")

    return port, f"172.31.{third}.0/24"


def _new_env(path: Path) -> None:
    """
    A new profile's ``.env``: the header, and the reference of every setting
    (the template's ``.env``, all of it commented out). What the profile
    sets goes between the two.
    """
    reference = (stack.template_dir() / ".env").read_text(encoding="utf-8")
    path.write_text(f"{ENV_HEADER}\n{reference}", encoding="utf-8")


def create(
    root: Path,
    name: str,
    source: str = "",
    warn: Optional[Warn] = None,
    languages: Optional[list[str]] = None,
    taken_subnets: Iterable[str] = (),
) -> Created:
    """
    Create ``profiles/<name>`` in the home *root*: from the template, or as a
    copy of the profile *source*, with a port and subnet of its own (see
    ``allocate`` for *taken_subnets*).

    From the template, *languages* (catalogue keys) are what the profile is
    built around, and must be given: choosing them is the first thing a
    profile asks for. A copy keeps its source's unless *languages* are
    given, and its source's own allowlist entries and the provided ones it
    switches off.

    A copy also gets its source's ``image/`` folder, its own Dockerfile
    steps, when it has one; a profile from the template has none. It gets
    none of its source's devices: each one weakens the sandbox, and is
    turned on for a profile of its own, with its warning.
    """
    if not valid_name(name):
        raise ProfileError(f"'{name}' is not a valid profile name ({NAME_RULE})")

    target = profiles_dir(root) / name
    if target.exists():
        raise ProfileError(f"profile '{name}' already exists")

    origin = profiles_dir(root) / source if source else None
    if origin is not None and not origin.is_dir():
        raise ProfileError(f"no such profile to copy from: {source}")
    if origin is None and languages is None:
        raise ProfileError(
            f"profile '{name}' needs its languages and tools: --lang python,docker "
            '(hardcode languages lists them; --lang "" for none)'
        )

    target.mkdir(parents=True)

    for filename in FILES:
        # A file the source profile lacks comes from the template.
        candidates = [origin / filename] if origin is not None else []
        candidates.append(stack.template_dir() / filename)

        chosen = next(candidate for candidate in candidates if candidate.exists())
        shutil.copyfile(chosen, target / filename)
        (target / filename).chmod(0o644)

    if origin is not None and (origin / ".env").exists():
        shutil.copyfile(origin / ".env", target / ".env")
    else:
        _new_env(target / ".env")

    # .env can hold a fixed VS Code token.
    (target / ".env").chmod(0o600)

    # Its own Dockerfile steps: a copy gets its source's. A profile without
    # any builds HardCode's image alone; the app and `hardcode image` offer
    # the starter until the first steps are saved.
    if origin is not None and (origin / dockerfile.FOLDER).is_dir():
        shutil.copytree(origin / dockerfile.FOLDER, target / dockerfile.FOLDER)

    if origin is not None:
        _drop_forwards(target / "compose.override.yaml")

        # A copy gets a token of its own, generated when it first starts.
        envfile.unset(target / ".env", "VSCODE_CONNECTION_TOKEN")

        # A device weakens the sandbox: one profile's choice isn't another's.
        envfile.unset(target / ".env", devices.ENV_KEY)

    port, subnet = _assign_network(root, name, target / ".env", warn, taken_subnets)

    if languages is not None:
        set_languages(target, languages)

    write_proxy_allowlist(Profile(name, target), warn=warn)
    write_extension_list(Profile(name, target))

    chosen = langs.enabled_keys(envfile.read(target / ".env"))
    return Created(name=name, source=source, port=port, subnet=subnet, languages=tuple(chosen))


def set_languages(directory: Path, languages: list[str]) -> None:
    """
    Build the profile in *directory* around exactly *languages*: each one
    switched on in its ``.env``, and the others left out, which is off.
    """
    env_file = directory / ".env"
    for lang in langs.CATALOGUE:
        if lang.key in languages:
            envfile.set_value(env_file, lang.env_key, "true")
        else:
            envfile.unset(env_file, lang.env_key)


def _assign_network(
    root: Path,
    name: str,
    env_file: Path,
    warn: Optional[Warn] = None,
    taken_subnets: Iterable[str] = (),
) -> tuple[int, str]:
    """
    Give a profile's ``.env`` a VS Code port and a subnet (see
    ``allocate``), and return them.
    """
    port, subnet = allocate(root, exclude=name, warn=warn, taken_subnets=taken_subnets)

    envfile.set_value(env_file, "VSCODE_PORT", str(port))
    envfile.set_value(env_file, "DEVNET_SUBNET", subnet)
    envfile.set_value(env_file, "DNS_IP", subnet.replace(".0/24", ".53"))

    return port, subnet


def _drop_forwards(path: Path) -> None:
    """
    Remove a copied profile's port forwards, keeping its mounts: a host port
    belongs to one profile.
    """
    if not path.exists() or "gateway" not in path.read_text(encoding="utf-8"):
        return

    header, document = override.load(path)
    gateway = document["services"].get("gateway") or {}

    if override.get_forwards(document) or gateway.get("ports"):
        override.set_forwards(document, [])
        override.save(path, header, document)


def get(root: Path, name: str, home_id: str = "") -> Profile:
    """
    The profile called *name* in the home *root*, whose id (see
    ``home.home_id``) goes into its project and image names.
    """
    if not valid_name(name):
        raise ProfileError(f"'{name}' is not a valid profile name ({NAME_RULE})")

    if not list_profiles(root):
        raise ProfileError(
            f"no profiles yet (in {root}): make one with hardcode profile add {name} --lang python,docker "
            "(hardcode languages lists the languages and tools), or hardcode up on a terminal to choose"
        )
    if not exists(root, name):
        raise ProfileError(f"no profile '{name}' (hardcode profiles; hardcode profile add {name} --lang …)")

    profile = Profile(name, profiles_dir(root) / name, home_id)

    # Restore the files Compose can't start without, should they be deleted.
    if not (profile.dir / "allowlist.txt").exists():
        shutil.copyfile(stack.template_dir() / "allowlist.txt", profile.dir / "allowlist.txt")

    # A restored .env gets a port and subnet of its own, as a new profile
    # does; its languages are gone with it.
    if not profile.env_file.exists():
        _new_env(profile.env_file)
        profile.env_file.chmod(0o600)
        _assign_network(root, name, profile.env_file)

    # The proxy's allowlist is there for Compose to mount (a missing file
    # would be mounted as an empty directory). It is brought up to date only
    # where the proxy is given it too: written here, the running proxy
    # would no longer match it, and nothing would notice.
    if not profile.proxy_allowlist_file.exists():
        write_proxy_allowlist(profile)

    # Always current, so that a catalogue that changed with the command
    # reaches the VS Code container without anything else happening.
    write_extension_list(profile)

    return profile


@contextmanager
def lock(
    profile: Profile,
    waiting: Optional[Callable[[str], None]] = None,
    timeout: float = 3600,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[None]:
    """
    Hold the profile's lock for the length of the ``with`` block: one change
    to a profile at a time, whichever command or front end makes it.

    Two changes at once would edit the same files from the same old content,
    so that one is lost, and ask Compose to recreate the same containers, so
    that one removes what the other is renaming. A command that finds the
    lock taken says so through *waiting*, once, and waits (an image rebuild
    can hold it for many minutes) up to *timeout* seconds. The lock is the
    operating system's on ``.lock`` in the profile's directory, so it goes
    with the process that held it, however that process ends.
    """
    path = profile.dir / LOCK_FILE
    deadline = clock() + timeout
    told = False

    try:
        with open(path, "a+b") as handle:
            while not _try_lock(handle):
                if clock() >= deadline:
                    raise ProfileError(f"profile '{profile.name}' is still being changed by another command; try again")

                if waiting and not told:
                    waiting(f"waiting for another change to profile '{profile.name}' to finish...")
                    told = True

                sleep(0.2)

            try:
                yield
            finally:
                _unlock(handle)
    finally:
        # Removed while it was held: what is left is the lock file, which
        # Windows couldn't delete while it was open.
        _remove_if_only_the_lock(profile.dir)


def _try_lock(handle) -> bool:
    """
    Take the lock on an open file without waiting; whether it was free.
    """
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle) -> None:
    """
    Let go of the lock taken by ``_try_lock``.
    """
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def internal_ranges(profile: Profile) -> list[str]:
    """
    The profile's internal ranges: ``EGRESS_INTERNAL_RANGES``, else the
    proxy's default ones.
    """
    return lan.internal_ranges(envfile.read(profile.env_file).get("EGRESS_INTERNAL_RANGES", ""))


def devnet(profile: Profile) -> list[str]:
    """
    The sandbox's own network: the profile's ``DEVNET_SUBNET``, else
    compose.yaml's default. No name may be pinned into it.
    """
    return [envfile.get(profile.env_file, "DEVNET_SUBNET") or lan.DEFAULT_DEVNET_SUBNET]


def pins_to_this_computer(
    profile: Profile,
    text: str,
    this_computer: lan.ThisComputer = lan.is_this_computer,
) -> list[allowlist.Pin]:
    """
    The pins of the allowlist *text* whose address is one of this computer's
    own (*this_computer*, the bind test): written by hand, since the
    command refuses them as they are added. Squid can't tell them from any
    other address on your network, so only the command, which runs on
    this computer, can keep them from it. A pin with a problem of its own
    isn't tested: the proxy refuses to start with it anyway.
    """
    found = allowlist.pins(text, internal_ranges(profile), devnet(profile))
    return [pin for pin in found if not pin.problem and this_computer(pin.address)]


def write_proxy_allowlist(
    profile: Profile,
    this_computer: lan.ThisComputer = lan.is_this_computer,
    warn: Optional[Warn] = None,
) -> tuple[str, str]:
    """
    Write the allowlist the proxy is given, from the profile's
    ``allowlist.txt`` and its languages and tools (``allowlist.render``):
    the file's text before (``""`` when there was none) and after.

    A pin to one of this computer's own addresses (``pins_to_this_computer``)
    is left out, as the operator leaves out the pins it ignores, and *warn*
    is told of each, every time. The operator never makes this test: on a
    cluster it would test the operator's own pod.

    It is written in place, as the renderer's bind mount follows the file
    it was made with, and readable by the renderer's user.
    """
    enabled = langs.enabled_keys(envfile.read(profile.env_file))
    own = profile.allowlist_file.read_text(encoding="utf-8")

    left_out = pins_to_this_computer(profile, own, this_computer)
    for pin in left_out:
        if warn is not None:
            warn(f"allowlist.txt pins {pin.name} to {pin.address}, which is this computer: left out")

    reasons = {pin.name: f"{pin.address} is this computer" for pin in left_out}
    text = allowlist.render(allowlist.ignore_pins(own, reasons), enabled)

    path = profile.proxy_allowlist_file
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    if before == text:
        return before, text

    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    path.chmod(0o644)

    return before, text


def _proxy_allowlist_digest(profile: Profile) -> str:
    """
    The sha256 of the proxy allowlist as it is now; ``""`` without one.
    """
    path = profile.proxy_allowlist_file
    if not path.exists():
        return ""

    return hashlib.sha256(path.read_bytes()).hexdigest()


def mark_proxy_allowlist_applied(profile: Profile) -> None:
    """
    Record that the proxy has loaded the proxy allowlist as it is now: it
    started from nothing and read it, or was reloaded with it and came back
    healthy.
    """
    profile.proxy_applied_file.write_text(_proxy_allowlist_digest(profile) + "\n", encoding="utf-8")


def proxy_allowlist_applied(profile: Profile) -> bool:
    """
    Whether the proxy last loaded the proxy allowlist as it is now. Not when
    the file changed since, by an edit or a newer HardCode, and a start or
    reload failed before the proxy had it: a proxy keeps the rules it
    loaded, so a file that is right says nothing about the proxy.
    """
    path = profile.proxy_applied_file
    if not path.exists():
        return False

    return path.read_text(encoding="utf-8").strip() == _proxy_allowlist_digest(profile)


def write_extension_list(profile: Profile) -> bool:
    """
    Write the profile's extension list from its ``.env`` (its languages and
    ``VSCODE_EXTENSIONS``); whether it changed.

    It is written in place, never replaced: the VS Code container has the
    file bind-mounted, and a mount follows the file it was made with. The
    container reads it only when it ends with the end marker, so a list
    caught half-written is never taken for a shorter one.
    """
    env = envfile.read(profile.env_file)
    enabled = langs.enabled_keys(env)
    extras = langs.parse_extensions(env.get("VSCODE_EXTENSIONS", ""))
    text = langs.extension_list(enabled, extras)

    path = profile.extensions_file
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False

    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)

    return True


def remove(root: Path, name: str) -> None:
    """
    Delete the profile's directory. Its containers and volumes are the caller's job.

    A lock file someone holds (the command removing the profile, usually)
    stays: Windows can't delete a file that is open. ``lock`` deletes it,
    and the folder, as it lets go.
    """
    if not exists(root, name):
        raise ProfileError(f"no profile '{name}'")

    folder = profiles_dir(root) / name
    for path in folder.iterdir():
        if path.name == LOCK_FILE:
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()

    _remove_if_only_the_lock(folder)


def _remove_if_only_the_lock(folder: Path) -> None:
    """
    Delete *folder* when all it holds is a lock file nobody holds; leave it
    otherwise, for the lock's holder to delete as it lets go.
    """
    lock_file = folder / LOCK_FILE
    if not folder.is_dir() or [path.name for path in folder.iterdir()] != [LOCK_FILE]:
        return

    try:
        with open(lock_file, "a+b") as handle:
            if not _try_lock(handle):
                return
            _unlock(handle)

        lock_file.unlink()
        folder.rmdir()
    except OSError:
        # Opened by another command meanwhile (Windows): it deletes them.
        pass
