"""
The operations behind the commands.

Each operation takes a profile, edits its files, applies the change to the
running containers when there are any, and returns a ``Result`` that the
command line prints, or emits as JSON for the app.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import (
    allowlist,
    blocked,
    bounded,
    devices,
    dockerfile,
    envfile,
    lan,
    langs,
    override,
    packages,
    profiles,
    reloader,
    stack,
)
from .compose import Compose, ComposeError, Runner

# Where the VS Code container's extension sync says how far it has got,
# relative to the home directory.
EXTENSION_STATUS = ".vscode-server/.hardcode-extensions-status.json"

# The VS Code image's label naming the languages and tools it was built
# with (images/vscode/Dockerfile sets it), in the project's namespace.
IMAGE_LANGUAGES_LABEL = "io.github.hardcode-app.languages"

# ... the one naming the profile's own Ubuntu packages (the Dockerfile's
# too), and the one with the hash of the profile's own Dockerfile steps
# (compose.image.yaml's, on the image built from them).
IMAGE_PACKAGES_LABEL = "io.github.hardcode-app.packages"
IMAGE_DOCKERFILE_LABEL = "io.github.hardcode-app.dockerfile"

# The image a profile's own Dockerfile steps are built on, HardCode's
# (compose.image.yaml builds it under this name), and HardCode's Dockerfile.
BASE_IMAGE = "hardcode-vscode-base"
BASE_DOCKERFILE = "images/vscode/Dockerfile"

# How much of the extension status is read. Anything in the VS Code
# container can write that file; the sync's own is a few kilobytes.
STATUS_BYTES = 65536

# The most extensions a status may list, and the longest name it may hold.
STATUS_ITEMS = 1000
STATUS_ITEM_LENGTH = 256


@dataclass
class Result:
    """
    What an operation did, for one profile.

    ``applied`` says whether the running sandbox took the change: ``True`` if
    it did, ``False`` when the profile isn't running (the change applies at its
    next start), and ``None`` when there was nothing to apply. ``reloading``
    says that the running proxy is given it in the background, a few seconds
    after the profile's last change (see ``reloader``): it isn't in force yet.
    """

    profile: str
    message: str
    changed: bool = True
    applied: Optional[bool] = None
    data: dict = field(default_factory=dict)
    reloading: bool = False


def _checked_status(status: object) -> Optional[dict]:
    """
    *status* as the extension sync writes it, with only its own fields, or
    ``None`` when it isn't that: a state, two counts, three lists of
    extension names of a sensible size, and when it was written.
    """
    if not isinstance(status, dict):
        return None

    state = status.get("state")
    counts = [status.get("total"), status.get("installed")]
    if state not in ("installing", "done"):
        return None
    if not all(isinstance(count, int) and not isinstance(count, bool) and count >= 0 for count in counts):
        return None

    checked: dict = {"state": state, "total": counts[0], "installed": counts[1]}

    for name in ("pending", "refused", "failed"):
        items = status.get(name, [])
        if not isinstance(items, list) or len(items) > STATUS_ITEMS:
            return None
        if not all(isinstance(item, str) and len(item) <= STATUS_ITEM_LENGTH for item in items):
            return None
        checked[name] = items

    updated = status.get("updated")
    if isinstance(updated, str) and len(updated) <= 40:
        checked["updated"] = updated

    return checked


@dataclass
class Image:
    """
    What Docker says about a profile's VS Code image.

    ``built``: whether it exists (``None`` when Docker can't say).
    ``languages``: the languages and tools it was built with, from its label;
    ``[]`` when there is no image, and ``None`` when nobody can tell (no
    answer from Docker, or an image without the label). An image built with
    none at all is built, with ``[]``.
    ``packages``: the profile's own Ubuntu packages it was built with, from
    its label; ``[]`` when there is none (no image, or one built without),
    and ``None`` when Docker can't say.
    ``dockerfile``: the hash of the profile's own Dockerfile steps it was
    built with, from its label; ``""`` when there are none (no image, or one
    built without), and ``None`` when Docker can't say.
    """

    built: Optional[bool]
    languages: Optional[list[str]]
    packages: Optional[list[str]]
    dockerfile: Optional[str]


def _print_warning(message: str) -> None:
    print(message, file=sys.stderr)


def _flag_of(entry: str) -> str:
    """
    ``ssh`` for a typed ``host:port``, which only an SSH entry has; ``""``
    for the rest.
    """
    return "ssh" if ":" in entry and "/" not in entry else ""


def _pinned_name(entry: str) -> str:
    """
    The name a pin for the typed *entry* is for: its host, without a path or
    a port. Refused for a wildcard, an address or anything else that isn't
    one exact name.
    """
    name = allowlist.pin_name(entry, _flag_of(entry))
    if not name:
        raise allowlist.AllowlistError(
            f"only an exact name can reach your network, and {entry} isn't one (no wildcard or address)"
        )

    return name


class Ops:
    """
    The operations on one profile of a checkout.

    *host* is this computer as the devices see it (``devices.Host``): its
    platform and its device nodes, which tests replace. *start_reloader*
    starts the background reload of a running proxy (``reloader.start``).
    """

    def __init__(
        self,
        root: Path,
        profile: profiles.Profile,
        runner: Runner = bounded.run,
        docker: Optional[str] = None,
        sleep: Callable[[float], None] = time.sleep,
        warn: Callable[[str], None] = _print_warning,
        resolve: lan.Resolver = lan.resolve,
        this_computer: lan.ThisComputer = lan.is_this_computer,
        host: Optional[devices.Host] = None,
        start_reloader: Callable[[Path, profiles.Profile], None] = reloader.start,
    ):
        self.root = root
        self.profile = profile
        self.warn = warn
        self.resolve = resolve
        self.this_computer = this_computer
        self.host = host or devices.Host()
        self.start_reloader = start_reloader
        self.compose = Compose(profile, runner=runner, docker=docker, sleep=sleep)

    # -- helpers --------------------------------------------------------------

    def _recreate(self, *services: str) -> bool:
        """
        Recreate *services* if the profile is running; returns whether it was.

        ``docker`` is left out while the profile runs without the daemon: naming
        a service on the command line starts it, Compose profile or not.
        """
        if not self.compose.running():
            return False

        wanted = [service for service in services if service != "docker" or self.compose.docker_enabled()]
        self.compose.run("--progress", "quiet", "up", "-d", *wanted, capture=True, check=True)

        return True

    def reload_allowlist(self) -> bool:
        """
        Render the proxy's allowlist file into squid's rules again, and
        restart squid with them.

        Returns ``False`` when the profile isn't running: the change applies at
        its next start. Raises when it is running but the proxy doesn't come
        back healthy, because then nobody knows which policy is in force.
        """
        if not self.compose.running():
            return False

        # Every allowlist and squid container of the profile goes first, a
        # leftover from a recreate that was cut short included (Compose
        # renames the old container out of the way and leaves it behind when
        # it fails, and a later recreate trips over it); then both start
        # fresh, the renderer from the file as it now is.
        self.compose.run(
            "--progress", "quiet", "rm", "--stop", "--force", "allowlist", "squid",
            capture=True,
            check=True,
        )  # fmt: skip
        self.compose.run("--progress", "quiet", "up", "-d", "allowlist", "squid", capture=True, check=True)

        if not self.compose.wait_healthy("squid"):
            raise ComposeError(
                "the allowlist was written, but squid didn't come back healthy after the reload, "
                "so the new policy may not be in force: "
                f"hardcode -p {self.profile.name} logs allowlist squid"
            )

        profiles.mark_proxy_allowlist_applied(self.profile)
        return True

    def proxy_started(self, was_running: bool) -> bool:
        """
        After a start that succeeded, make sure the proxy has the policy the
        proxy allowlist holds; whether it was reloaded for that.

        A proxy that started from nothing read the file as it started. One
        that was running keeps the rules it last loaded, and is reloaded when
        those aren't the file's: the file changed since (an edit, a newer
        HardCode's entries) and the start or reload that should have given it
        failed before it did. Rewriting the file is no proof of what the
        proxy enforces.
        """
        if not was_running:
            profiles.mark_proxy_allowlist_applied(self.profile)
            return False

        return self.reload_if_behind()

    def reload_if_behind(self) -> bool:
        """
        Reload a running proxy whose rules aren't the proxy allowlist's;
        whether it was reloaded.
        """
        if profiles.proxy_allowlist_applied(self.profile):
            return False

        return self.reload_allowlist()

    def _change_override(self, edit: Callable[[Path], object], *services: str) -> bool:
        """
        Apply *edit* to the override file and recreate *services* with it if
        the profile is running; returns whether it was. Either the whole
        change happens, or none of it.

        The old file goes back if the edit fails, if Compose rejects the
        result, or if the services can't be recreated with it (a host port
        that something else holds, say). In that last case they are
        recreated again from the old file, so the profile isn't left stopped,
        or unable to start, by a change that didn't work.
        """
        path = self.profile.override_file
        if not path.exists():
            shutil.copyfile(stack.template_dir() / "compose.override.yaml", path)

        backup = path.read_bytes()

        try:
            edit(path)
            ok, complaint = self.compose.config_check()
        except BaseException:
            # Docker missing, a check that timed out: nothing was applied, so
            # nothing may stay written either.
            path.write_bytes(backup)
            raise

        if not ok:
            path.write_bytes(backup)
            raise override.OverrideError(f"compose rejected the change; file restored: {complaint}")

        try:
            return self._recreate(*services)
        except ComposeError as error:
            path.write_bytes(backup)
            self._restart_as_before(*services)
            raise ComposeError(f"{error}; the change was undone") from error

    def _restart_as_before(self, *services: str) -> None:
        """
        Recreate *services* from the files as they were, after a change that
        couldn't be applied. A failure here is reported, not raised: the
        error worth showing is the one that made the change fail.
        """
        try:
            self._recreate(*services)
        except ComposeError as error:
            self.warn(f"warning: {', '.join(services)} couldn't be started again as before: {error}")

    def _dev_user(self) -> str:
        """
        The VS Code container user's name, as mount paths under its home
        should spell it: the profile's, else compose.yaml's default.
        """
        return envfile.get(self.profile.env_file, "DEV_USER")

    # -- allowlist -------------------------------------------------------------

    def _languages(self) -> list[str]:
        """
        The languages and tools the profile has on.
        """
        return langs.enabled_keys(envfile.read(self.profile.env_file))

    def allowlist_entries(self) -> list[allowlist.Entry]:
        """
        Everything the profile's allowlist has: the provided entries, then
        its own (``allowlist.effective``).
        """
        text = self.profile.allowlist_file.read_text(encoding="utf-8")
        return allowlist.effective(text, self._languages())

    def _edit_allowlist(self, edit: Callable[[str], tuple[str, str]]) -> tuple[bool, str, Optional[bool]]:
        """
        Apply *edit* to ``allowlist.txt``, and when it changed the file,
        see that the proxy is given the new allowlist (``give_proxy_later``):
        whether it changed, what *edit* said about it, and whether the
        running proxy takes it.

        It runs under the profile's files lock alone (``profiles.files_lock``),
        so it doesn't wait for a reload of the proxy under way.
        """
        path = self.profile.allowlist_file
        before = path.read_text(encoding="utf-8")

        after, message = edit(before)
        if after == before:
            return False, message, None

        path.write_text(after, encoding="utf-8")

        return True, message, self.give_proxy_later()

    def give_proxy_later(self) -> bool:
        """
        After an edit of the allowlist: a running profile's proxy is given it
        in the background, a few seconds after the profile's last edit
        (``reloader``), and ``True`` says so. A profile that isn't running
        gives its proxy the allowlist at its next start (``False``); the
        proxy allowlist is written now all the same, unless a holder of the
        containers lock may be giving the proxy that file this moment.
        """
        if self.compose.running():
            self.start_reloader(self.root, self.profile)
            return True

        with profiles.try_lock(self.profile, profiles.CONTAINERS_LOCK_FILE) as free:
            if free:
                self._write_proxy_allowlist()

        return False

    def _allowlist_result(
        self,
        message: str,
        changed: bool,
        applied: Optional[bool],
        data: Optional[dict] = None,
    ) -> Result:
        """
        The result of an allowlist edit: one a running profile took is given
        to its proxy in the background.
        """
        return Result(
            self.profile.name,
            message,
            changed=changed,
            applied=applied,
            data=data or {},
            reloading=applied is True,
        )

    def _write_proxy_allowlist(self) -> tuple[str, str]:
        """
        Write the allowlist the proxy is given (``profiles.write_proxy_allowlist``):
        its text before and after. A pin written by hand to one of this
        computer's own addresses is left out, and said so, as a name that
        moved is.
        """
        return profiles.write_proxy_allowlist(
            self.profile,
            this_computer=self.this_computer,
            warn=lambda line: self.warn(f"on your network: {line}"),
        )

    def allow(
        self,
        entry: str,
        flag: str = "",
        lan_ok: bool = False,
        confirm: Optional[Callable[[str, str], bool]] = None,
    ) -> Result:
        """
        Allow an entry, or change the flag of one of the profile's own; a
        provided entry switched off is switched back on.

        When the entry's name (its host, for a path or an SSH entry) resolves
        to an address on your network and isn't on it yet, the proxy would
        refuse it there: it is let reach that address only with *lan_ok*, or
        when *confirm* (asked with the name and the address) says yes, and
        in the same change. Otherwise the message says how, and the result's
        ``data["lan"]`` names the name and the address, for the app to ask.

        An address no name may reach (this computer's, or one in the
        sandbox's own network) is never offered, nor asked about: the
        message says why the proxy refuses the name there.
        """
        # Only a valid entry's name is looked up: a refused one never leaves.
        valid = allowlist.validate(allowlist.canonical(entry, flag), flag) is None
        name = allowlist.pin_name(entry, flag) if valid else None

        offer, refused = self._lan_offer(name)
        pin = offer is not None and (lan_ok or (confirm is not None and confirm(*offer)))

        def edit(text: str) -> tuple[str, str]:
            after, message = allowlist.allow(text, entry, flag, self._languages())
            if pin:
                after, pinned = allowlist.add_pin(after, offer[0], offer[1], self._internal_ranges(), self._devnet())
                message = f"{message}; {pinned}"
            return after, message

        changed, message, applied = self._edit_allowlist(edit)
        data: dict = {}

        if offer:
            name, address = offer
            data["lan"] = {"name": name, "address": address, "pinned": pin}
            if not pin:
                message = (
                    f"{message}; {name} resolves to {address}, on your network, where the proxy refuses it: "
                    f"hardcode lan add {name} {address} lets it reach that address"
                )
        elif refused:
            message = f"{message}; {refused}"
        elif lan_ok:
            message = f"{message}; {self._why_no_offer(name, entry)}"

        return self._allowlist_result(message, changed, applied, data)

    def deny(self, entry: str, ssh: bool = False) -> Result:
        """
        Stop allowing an entry: one of the profile's own is commented out,
        keeping its line for later, and a provided one is switched off.
        """
        shown = allowlist.canonical(entry) + (" (ssh)" if ssh else "")

        changed, _, applied = self._edit_allowlist(
            lambda text: allowlist.switch(text, entry, False, ssh, self._languages()),
        )
        if not changed:
            return Result(self.profile.name, f"'{shown}' is not an active entry", changed=False)

        return self._allowlist_result(f"denied {shown}", True, applied)

    def set_entry_enabled(self, entry: str, enabled: bool, ssh: bool = False) -> Result:
        """
        Switch an entry on or off: its HTTPS form, or with *ssh* its SSH form.
        One of the profile's own is commented back in or out; a provided one
        loses or gets its ``off`` line.
        """
        changed, message, applied = self._edit_allowlist(
            lambda text: allowlist.switch(text, entry, enabled, ssh, self._languages()),
        )
        return self._allowlist_result(message, changed, applied)

    def remove_entry(self, entry: str, ssh: bool = False) -> Result:
        """
        Delete one of the profile's own entries. A provided one can only be
        switched off, which the refusal says.
        """
        changed, message, applied = self._edit_allowlist(
            lambda text: allowlist.delete(text, entry, ssh, self._languages()),
        )
        return self._allowlist_result(message, changed, applied)

    # -- names on your network ---------------------------------------------------

    def _internal_ranges(self) -> list[str]:
        """
        The profile's internal ranges (``profiles.internal_ranges``).
        """
        return profiles.internal_ranges(self.profile)

    def _devnet(self) -> list[str]:
        """
        The sandbox's own network (``profiles.devnet``): no name may be
        pinned into it.
        """
        return profiles.devnet(self.profile)

    def pins(self) -> list[allowlist.Pin]:
        """
        The names the profile lets reach your network, as its allowlist has them.
        """
        text = self.profile.allowlist_file.read_text(encoding="utf-8")
        return allowlist.pins(text, self._internal_ranges(), self._devnet())

    def _pinnable(self, addresses: list[str]) -> Optional[str]:
        """
        The first of *addresses* a name may be pinned to in this profile, on
        this computer (``lan.pinnable``); ``None`` when there is none.
        """
        return lan.pinnable(addresses, self._internal_ranges(), self._devnet(), self.this_computer)

    def _refused_inside(self, name: str, addresses: list[str]) -> str:
        """
        When none of *addresses* (what *name* resolves to) may be pinned but
        one is inside the profile's internal ranges, where the proxy refuses
        the name: what it resolves to there, and why it can't be pinned
        (``lan.refusal``). ``""`` otherwise.
        """
        inside = [address for address in addresses if lan.in_ranges(address, self._internal_ranges())]
        if not inside:
            return ""

        address = lan.canonical_address(inside[0])
        why = lan.refusal(address, self._internal_ranges(), self._devnet(), self.this_computer)
        return f"{name} resolves to {address}, and {why}"

    def _lan_offer(self, name: Optional[str]) -> tuple[Optional[tuple[str, str]], str]:
        """
        What allowing *name* offers on your network, when it isn't on it
        yet: *name* and the address there it resolves to and may be pinned
        to, or ``None``; and when there is none but it resolves inside the
        profile's internal ranges, why not (``_refused_inside``), else ``""``.
        """
        if not name or any(pin.name == name for pin in self.pins()):
            return None, ""

        resolves = self.resolve(name)

        address = self._pinnable(resolves)
        if address:
            return (name, address), ""

        return None, self._refused_inside(name, resolves)

    def _why_no_offer(self, name: Optional[str], entry: str) -> str:
        """
        Why ``allow --lan`` let nothing reach your network.
        """
        if not name:
            return f"nothing to let reach your network: only an exact name can, and {entry} isn't one"
        if any(pin.name == name for pin in self.pins()):
            return f"{name} is already on your network"

        return f"nothing to let reach your network: {name} doesn't resolve to an address on it from here"

    def lan_states(self) -> list[dict]:
        """
        The names on your network, each looked up again: its pin
        (``Pin.as_dict``), what it resolves to now (``resolves``), and its
        ``state``: ``ok`` while that includes its address, ``moved`` when it
        resolves only elsewhere, ``unresolved`` when it doesn't resolve here,
        and ``this-computer`` for a pin (written by hand) to one of this
        computer's addresses, which the proxy is never given.
        A moved name's ``moveTo`` is the address it could be moved to: one
        it may be pinned to, ``""`` when it resolves to none.

        A pin never follows its name: a name that moves looks the same as
        one someone pointed somewhere else.
        """
        states = []

        for pin in self.pins():
            resolves = self.resolve(pin.name)

            state = "ok" if pin.address in resolves else "moved"
            if not resolves:
                state = "unresolved"
            if not pin.problem and self.this_computer(pin.address):
                state = "this-computer"

            move_to = (self._pinnable(resolves) or "") if state == "moved" else ""
            states.append({**pin.as_dict(), "resolves": resolves, "state": state, "moveTo": move_to})

        return states

    def lan_warnings(self, states: Optional[list[dict]] = None) -> list[str]:
        """
        A line for each name on your network that now resolves elsewhere.
        """
        lines = []

        for item in self.lan_states() if states is None else states:
            if item["state"] != "moved":
                continue

            now = ", ".join(item["resolves"])
            line = (
                f"{item['name']} now resolves to {now}, not {item['address']}: it still reaches only {item['address']}"
            )

            # Only an address a name may be pinned to is offered.
            if item["moveTo"]:
                line = f"{line}; if the move is right, hardcode lan add {item['name']} {item['moveTo']}"
            else:
                refused = self._refused_inside(item["name"], item["resolves"])
                line = f"{line}; {refused}" if refused else line

            lines.append(line)

        return lines

    def add_pin(self, entry: str, address: str = "") -> Result:
        """
        Let *entry*'s name (its host, without a path or a port) reach
        *address* on your network; without an address, the one on your
        network it resolves to. A name on your network already is moved.

        This computer's own addresses are refused, as are the sandbox's own
        network's (the profile's ``DEVNET_SUBNET``): HardCode never lets a
        sandbox reach either.
        """
        name = _pinned_name(entry)

        if not address:
            resolves = self.resolve(name)
            address = self._pinnable(resolves) or ""

            refused = "" if address else self._refused_inside(name, resolves)
            if refused:
                raise allowlist.AllowlistError(refused)
        if not address:
            raise allowlist.AllowlistError(
                f"{name} doesn't resolve to an address on your network from here; "
                f"name the address: hardcode lan add {name} ADDRESS"
            )

        problem = lan.refusal(address, self._internal_ranges(), self._devnet(), self.this_computer)
        if problem:
            raise allowlist.AllowlistError(problem)

        changed, message, applied = self._edit_allowlist(
            lambda text: allowlist.add_pin(text, name, address, self._internal_ranges(), self._devnet()),
        )

        if changed and not allowlist.entry_ports(self.allowlist_entries(), name):
            message = f"{message}; it allows nothing until {name} has an allowlist entry"

        return self._allowlist_result(message, changed, applied)

    def remove_pin(self, entry: str) -> Result:
        """
        Stop letting *entry*'s name reach your network.
        """
        name = allowlist.pin_name(entry, _flag_of(entry)) or entry.strip().lower()

        changed, message, applied = self._edit_allowlist(lambda text: allowlist.remove_pin(text, name))
        return self._allowlist_result(message, changed, applied)

    def check_name(self, entry: str) -> dict:
        """
        What *entry*'s name resolves to from here, and whether it could reach
        your network: ``name``, ``resolves``, ``address`` (the one on your
        network it would be pinned to, or ``""``), ``pinned`` (the address
        it is pinned to already, or ``""``) and ``refused`` (when it resolves
        inside but to no address it may be pinned to, such as this
        computer's, why; ``""`` otherwise).
        """
        name = _pinned_name(entry)

        resolves = self.resolve(name)
        pinned = next((pin.address for pin in self.pins() if pin.name == name), "")
        address = self._pinnable(resolves) or ""

        return {
            "name": name,
            "resolves": resolves,
            "address": address,
            "pinned": pinned,
            "refused": "" if address else self._refused_inside(name, resolves),
        }

    def update_proxy_allowlist(self) -> list[str]:
        """
        Write the allowlist the proxy is given, from ``allowlist.txt`` and the
        profile's languages and tools as they are now: what that changed, one
        line per entry (``Rust: + newrustsite.com``), when there was a file
        before. A newer HardCode's provided entries arrive this way, at the
        next start or reload. The running proxy isn't touched.
        """
        before, after = self._write_proxy_allowlist()
        if not before:
            return []

        return allowlist.changes(before, after)

    def reload(self) -> Result:
        """
        Give the running proxy the allowlist again, after a hand edit, with
        the provided entries as this HardCode has them.
        """
        changes = self.update_proxy_allowlist()

        applied = self.reload_allowlist()
        message = "proxy reloaded" if applied else "profile isn't running"
        if changes:
            message = f"allowlist: {'; '.join(changes)}; {message}"

        # The names on your network are looked up again, and one that has
        # moved is only reported: its pin stays as it is.
        states = self.lan_states()
        warnings = self.lan_warnings(states)
        if warnings:
            message = f"{message}; {'; '.join(warnings)}"

        data = {"changes": changes, "lan": states}
        return Result(self.profile.name, message, changed=bool(changes), applied=applied, data=data)

    # -- the VS Code port ------------------------------------------------------

    def set_port(self, port: int) -> Result:
        """
        Publish VS Code on *port* instead: ``VSCODE_PORT`` in the ``.env``,
        and the gateway recreated with it if the profile is running (only the
        gateway: VS Code itself carries on).

        A port another profile publishes, or one of this profile's forwards,
        is refused. If the gateway can't be recreated on it (another program
        holds it), the old port goes back, and the gateway with it.
        """
        if not 1 <= port <= 65535:
            raise profiles.ProfileError(f"{port} is not a port (1 to 65535)")

        env_file = self.profile.env_file
        current = envfile.get(env_file, "VSCODE_PORT")
        if str(port) == current:
            return Result(self.profile.name, f"VS Code is already on port {port}", changed=False)

        others = profiles.ports_by_profile(self.root, exclude=self.profile.name, warn=self.warn)
        owners = [name for name, ports in others.items() if port in ports]
        if owners:
            raise profiles.ProfileError(f"port {port} is used by profile {', '.join(repr(owner) for owner in owners)}")

        # Reading the override needs a YAML backend; only when it can hold forwards.
        path = self.profile.override_file
        if path.exists() and "gateway" in path.read_text(encoding="utf-8"):
            _, document = override.load(path)
            if port in {forward.port for forward in override.get_forwards(document)}:
                raise profiles.ProfileError(f"port {port} is one of this profile's forwards")

        backup = env_file.read_bytes()
        envfile.set_value(env_file, "VSCODE_PORT", str(port))

        if not self.compose.running():
            return Result(self.profile.name, f"VS Code on port {port}", applied=False)

        try:
            self.compose.run("--progress", "quiet", "up", "-d", "--no-deps", "gateway", capture=True, check=True)
        except ComposeError as error:
            env_file.write_bytes(backup)
            self._restart_as_before("gateway")
            raise ComposeError(f"{error}; VS Code stays on port {current}") from error

        return Result(self.profile.name, f"VS Code on port {port}", applied=True)

    # -- forwards --------------------------------------------------------------

    def forward(self, port: int, host: str = "vscode", target: Optional[int] = None) -> Result:
        """
        Forward localhost:*port* to *target* on the VS Code container or the Docker daemon.
        """
        if str(port) == envfile.get(self.profile.env_file, "VSCODE_PORT"):
            raise override.OverrideError(f"port {port} is this profile's VS Code port")
        if host == "docker" and not self.compose.docker_enabled():
            raise override.OverrideError("this profile runs without the Docker daemon (hardcode docker on)")

        # A host port can only be published by one profile at a time.
        others = profiles.ports_by_profile(self.root, exclude=self.profile.name, warn=self.warn)
        owners = [name for name, ports in others.items() if port in ports]
        if owners:
            raise override.OverrideError(f"port {port} is used by profile {', '.join(repr(owner) for owner in owners)}")

        _, document = override.load(self.profile.override_file)
        for existing in override.get_forwards(document):
            if existing.port == port and existing.host == host and existing.target == (target or port):
                return Result(self.profile.name, f"localhost:{port} is already forwarded", changed=False)

        added: list[override.Forward] = []
        applied = self._change_override(
            lambda path: added.append(override.add_forward(path, port, host, target)),
            "gateway",
        )
        forward = added[0]

        return Result(
            self.profile.name,
            f"forwarding localhost:{port} -> {forward.host}:{forward.target}",
            applied=applied,
        )

    def unforward(self, port: int) -> Result:
        """
        Stop forwarding localhost:*port*.
        """
        applied = self._change_override(lambda path: override.remove_forward(path, port), "gateway")

        return Result(self.profile.name, f"forward for port {port} removed", applied=applied)

    # -- mounts ----------------------------------------------------------------

    def mount(self, local: str, remote: str, read_only: bool = False) -> Result:
        """
        Mount a folder of this machine into the VS Code container and the
        Docker daemon; never one that holds HardCode's home, or is in it.
        """
        self._refuse_the_home(local)

        added: list[override.Mount] = []
        applied = self._change_override(
            lambda path: added.append(override.add_mount(path, local, remote, read_only, self._dev_user())),
            "vscode",
            "docker",
        )
        mounted = added[0]

        note = " (read-only)" if read_only else ""
        return Result(
            self.profile.name,
            f"mounted {mounted.local} at {mounted.remote}{note}",
            applied=applied,
        )

    def _refuse_the_home(self, local: str) -> None:
        """
        Refuse to mount *local* when it is HardCode's home, holds it, or is
        in it.

        The home has every profile's files. Mounted, they would be the
        sandbox's to change, and some of them run on this computer: a
        profile's own Dockerfile steps (image/Dockerfile) at its next
        rebuild, its compose.override.yaml at its next start.
        """
        if not local.strip():
            return

        folder = Path(os.path.expanduser(local.strip())).resolve()
        home_dir = self.root.resolve()

        if folder == home_dir or folder.is_relative_to(home_dir) or home_dir.is_relative_to(folder):
            raise override.OverrideError(
                f"'{local}' holds HardCode's home ({home_dir}) or is in it: mounted, the sandbox could change "
                "the profiles' files, whose Dockerfile steps run on this computer at the next rebuild. "
                "Mount a folder outside it"
            )

    def unmount(self, remote: str) -> Result:
        """
        Remove a mount, by its path in the VS Code container.
        """
        removed: list[str] = []
        applied = self._change_override(
            lambda path: removed.append(override.remove_mount(path, remote, self._dev_user())),
            "vscode",
            "docker",
        )

        return Result(self.profile.name, f"mount at {removed[0]} removed", applied=applied)

    # -- the Docker daemon -----------------------------------------------------

    def set_docker(self, enabled: bool) -> Result:
        """
        Switch the rootless Docker daemon beside the VS Code container on, or off for a
        harder sandbox: nothing privileged runs for the profile, and there is
        no ``docker`` inside.
        """
        word = "on" if enabled else "off"
        if self.compose.docker_enabled() == enabled:
            return Result(self.profile.name, f"docker is already {word}", changed=False)

        self._write_docker(enabled)

        applied = self.compose.running()
        if applied:
            try:
                self._switch_docker(enabled)
            except ComposeError as error:
                # Back to how it was, file and containers, so that what the
                # profile says is what runs.
                self._write_docker(not enabled)
                try:
                    self._switch_docker(not enabled)
                except ComposeError as second:
                    self.warn(f"warning: the sandbox couldn't be started again as before: {second}")
                raise ComposeError(f"{error}; the change was undone") from error

        return Result(self.profile.name, f"docker {word}", applied=applied)

    def _write_docker(self, enabled: bool) -> None:
        """
        Record the Docker daemon's switch in the ``.env``: on is unset,
        compose.yaml's default; off is ``ENABLE_DOCKER=false``.
        """
        if enabled:
            envfile.unset(self.profile.env_file, "ENABLE_DOCKER")
        else:
            envfile.set_value(self.profile.env_file, "ENABLE_DOCKER", "false")

    def _switch_docker(self, enabled: bool) -> None:
        """
        Bring the running profile in line with ``ENABLE_DOCKER``, already written.
        """
        if not enabled:
            # The container of a Compose profile that is switched off isn't
            # an orphan to `up`, so it is stopped and removed by name. Its
            # data volume stays, for `docker on`.
            self.compose.run(
                "--profile", "docker", "--progress", "quiet", "rm", "--stop", "--force", "docker",
                capture=True,
                check=True,
            )  # fmt: skip

        # The VS Code container is recreated because its environment changed; when
        # switching on, the daemon starts beside it.
        self.compose.run("--progress", "quiet", "up", "-d", capture=True, check=True)

    # -- devices of this computer ----------------------------------------------

    def devices_on(self) -> list[str]:
        """
        The devices the profile has on, from ``VSCODE_DEVICES``, in the
        catalogue's order; what isn't a device is left out.
        """
        return devices.ordered(self._devices_named())

    def _devices_named(self) -> list[str]:
        """
        Everything ``VSCODE_DEVICES`` names, a device or not.
        """
        return devices.parse(envfile.read(self.profile.env_file).get(devices.ENV_KEY, ""))

    def _docker_runtimes(self) -> devices.Runtimes:
        """
        The runtimes this computer's Docker has, as ``docker info`` lists
        them; or why it couldn't say (no docker command, no daemon).
        """
        try:
            result = self.compose.run_docker("info", "--format", "{{json .Runtimes}}", timeout=30)
        except (ComposeError, bounded.OutputTooLarge, OSError, subprocess.TimeoutExpired) as error:
            return devices.Runtimes(None, str(error) or "docker info failed")

        if result.returncode != 0:
            complaint = (result.stderr or result.stdout or "").strip().splitlines()
            return devices.Runtimes(None, complaint[-1] if complaint else "docker info failed")

        return devices.parse_runtimes(result.stdout)

    def _device_problem(self, key: str) -> str:
        """
        Why this computer can't give the VS Code container the device *key*;
        ``""`` when it can.
        """
        return devices.unavailable(key, self.host, self._docker_runtimes)

    def device_states(self) -> list[dict]:
        """
        Every device, on or off for the profile, and whether this computer
        has it (``devices.states``).
        """
        return devices.states(self.devices_on(), self.host, self._docker_runtimes)

    def write_devices(self) -> bool:
        """
        Write the Compose file that gives the VS Code container the devices
        the profile has on, with their groups as this computer has them now;
        none is no file. Whether that changed it.
        """
        return devices.write(self.profile.devices_file, self.devices_on(), self.host)

    def check_devices(self) -> None:
        """
        Before a start: refuse to start with a device this computer can't
        give, saying why and what to do, or with ``VSCODE_DEVICES`` naming
        something that isn't a device; then write the Compose file for the
        devices (see ``write_devices``). A profile without any asks nothing
        of Docker.
        """
        name = self.profile.name
        named = self._devices_named()

        strangers = devices.unknown(named)
        if strangers:
            raise devices.DeviceError(
                f"{devices.ENV_KEY} in profile '{name}''s .env names {', '.join(strangers)}, which isn't a device: "
                f"the devices are {', '.join(devices.KEYS)} (hardcode -p {name} devices lists them)"
            )

        for key in devices.ordered(named):
            problem = self._device_problem(key)
            if problem:
                raise devices.DeviceError(
                    f"profile '{name}' has device {key} on, but {problem}; "
                    f"or switch it off: hardcode -p {name} device off {key}"
                )

        self.write_devices()

    def set_device(self, key: str, enabled: bool, confirm: Callable[[devices.Device], bool]) -> Result:
        """
        Switch a device of this computer on or off for the VS Code
        container: ``VSCODE_DEVICES`` in the ``.env``, the Compose file
        written from it, and the VS Code container recreated with them if
        the profile is running (only it: the Docker daemon gets no devices).

        On is refused for a device this computer can't give, and needs
        *confirm*'s yes, asked with the device; off asks nothing. Either the
        whole change happens, or none of it: when Compose rejects the file,
        or the container can't be recreated with it, both files go back,
        and the container is recreated from them again.
        """
        key = devices.check(key)
        word = "on" if enabled else "off"

        current = self.devices_on()
        if (key in current) == enabled:
            return Result(self.profile.name, f"device {key} is already {word}", changed=False)

        if enabled:
            problem = self._device_problem(key)
            if problem:
                raise devices.DeviceError(f"device {key} can't be given to the VS Code container here: {problem}")

            if not confirm(devices.BY_KEY[key]):
                return Result(self.profile.name, f"device {key} stays off", changed=False)

        wanted = [other for other in current if other != key] + ([key] if enabled else [])

        # What the two files were, to put back when the change can't be made.
        env_file = self.profile.env_file
        fragment_file = self.profile.devices_file
        env_before = env_file.read_bytes()
        fragment_before = fragment_file.read_bytes() if fragment_file.exists() else None

        def restore() -> None:
            """
            Put both files back as they were; a file that wasn't there goes.
            """
            env_file.write_bytes(env_before)
            if fragment_before is None:
                fragment_file.unlink(missing_ok=True)
            else:
                fragment_file.write_bytes(fragment_before)

        try:
            self._write_device_list(wanted)
            self.write_devices()
            ok, complaint = self.compose.config_check()
        except BaseException:
            # Docker missing, a check that timed out: nothing was applied, so
            # nothing may stay written either.
            restore()
            raise

        if not ok:
            restore()
            raise devices.DeviceError(f"compose rejected the change; it was undone: {complaint}")

        try:
            applied = self._recreate("vscode")
        except ComposeError as error:
            restore()
            self._restart_as_before("vscode")
            raise ComposeError(f"{error}; the change was undone") from error

        return Result(self.profile.name, f"device {key} {word}", applied=applied)

    def _write_device_list(self, keys: list[str]) -> None:
        """
        Record the devices the profile has on in its ``.env``; none is
        unset, which is off.
        """
        if keys:
            envfile.set_value(self.profile.env_file, devices.ENV_KEY, devices.join(keys))
        else:
            envfile.unset(self.profile.env_file, devices.ENV_KEY)

    # -- languages -------------------------------------------------------------

    def set_language(self, lang: str, enabled: bool, build: bool = True) -> Result:
        """
        Switch one language on or off; see ``set_languages``.
        """
        return self.set_languages({langs.normalise(lang): enabled}, build=build)

    def set_languages(self, changes: dict[str, bool], build: bool = True) -> Result:
        """
        Switch languages and tools on or off, with everything that follows
        from them: the ``.env``; the entries they provide in the proxy's
        allowlist (a running proxy is given them in the background, a few
        seconds after the profile's last change); the extension list, which
        a running VS Code container picks up within seconds and installs
        from in the background; and, unless *build* is false, the image,
        rebuilt with the toolchains and the VS Code container recreated with
        it.
        """
        env_file = self.profile.env_file
        before = langs.enabled_keys(envfile.read(env_file))

        # On is "true"; off is unset, compose.yaml's default.
        for key, enabled in changes.items():
            if enabled:
                envfile.set_value(env_file, langs.BY_KEY[key].env_key, "true")
            else:
                envfile.unset(env_file, langs.BY_KEY[key].env_key)

        after = langs.enabled_keys(envfile.read(env_file))
        words = [f"{key} {'on' if enabled else 'off'}" for key, enabled in changes.items()]

        allowlist_changed = bool(self.update_proxy_allowlist())
        if after == before and not allowlist_changed:
            return Result(self.profile.name, f"already so: {', '.join(words)}", changed=False)

        list_changed = profiles.write_extension_list(self.profile)

        # What the running sandbox takes by itself: the proxy's new policy,
        # in the background, and the extensions the VS Code container
        # installs or removes.
        applied: Optional[bool] = None
        reloading = False
        if allowlist_changed or list_changed:
            applied = self.compose.running()
        if allowlist_changed and applied:
            self.start_reloader(self.root, self.profile)
            reloading = True

        if build:
            self.compose.run("build", "vscode", check=True, to_stderr=True)
            applied = self._recreate("vscode")

        note = "" if build else " (the image is rebuilt with: hardcode rebuild)"
        return Result(self.profile.name, f"{', '.join(words)}{note}", applied=applied, reloading=reloading)

    # -- extensions ------------------------------------------------------------

    def _extras(self) -> list[str]:
        """
        The profile's own extensions, from ``VSCODE_EXTENSIONS``.
        """
        return langs.parse_extensions(envfile.read(self.profile.env_file).get("VSCODE_EXTENSIONS", ""))

    def extensions(self) -> list[dict]:
        """
        Every extension the profile's VS Code container gets, and why; see
        ``langs.extension_sources``.
        """
        enabled = langs.enabled_keys(envfile.read(self.profile.env_file))
        return langs.extension_sources(enabled, self._extras())

    def extension_status(self) -> Optional[dict]:
        """
        What the running VS Code container's extension sync last said: its
        ``state`` (``installing`` or ``done``), how many of the wanted
        extensions are ``installed`` out of the ``total``, and which are
        ``pending``, ``refused`` (a publisher without a verified domain) or
        ``failed`` (tried again later). ``None`` when the container isn't
        running or hasn't said yet.
        """
        # Cut off in the container, and kept to the limit here too: the
        # sandbox's user can replace head.
        command = f"head -c {STATUS_BYTES} ~/{EXTENSION_STATUS} 2>/dev/null || true"
        try:
            output = self.compose.exec("vscode", command, limit=STATUS_BYTES)
        except ComposeError:
            return None

        try:
            status = json.loads(output) if output.strip() else None
        except json.JSONDecodeError:
            return None

        return _checked_status(status)

    def add_extension(self, extension: str) -> Result:
        """
        Add a VS Code extension of the profile's own. A running VS Code
        container picks the new list up within seconds and installs it in the
        background; a stopped one, when it starts.
        """
        extension_id = langs.normalise_extension(extension)
        enabled = langs.enabled_keys(envfile.read(self.profile.env_file))

        provider = langs.provider(extension_id, enabled)
        if provider:
            return Result(self.profile.name, f"{extension_id} already comes with {provider}", changed=False)

        extras = self._extras()
        if extension_id in extras:
            return Result(self.profile.name, f"{extension_id} is already added", changed=False)

        envfile.set_value(self.profile.env_file, "VSCODE_EXTENSIONS", ",".join([*extras, extension_id]))
        profiles.write_extension_list(self.profile)

        # A running VS Code container installs it by itself, in the background.
        return Result(self.profile.name, f"added extension {extension_id}", applied=self.compose.running())

    def remove_extension(self, extension: str) -> Result:
        """
        Remove one of the profile's own extensions; the VS Code container
        removes it as it does an addition.
        """
        extension_id = langs.normalise_extension(extension)

        extras = self._extras()
        if extension_id not in extras:
            enabled = langs.enabled_keys(envfile.read(self.profile.env_file))
            refusal = langs.removal_refusal(extension_id, enabled)
            raise langs.LangError(refusal or f"{extension_id} isn't one of this profile's extensions")

        remaining = [extra for extra in extras if extra != extension_id]
        envfile.set_value(self.profile.env_file, "VSCODE_EXTENSIONS", ",".join(remaining))
        profiles.write_extension_list(self.profile)

        return Result(self.profile.name, f"removed extension {extension_id}", applied=self.compose.running())

    # -- the profile's own Ubuntu packages ----------------------------------------

    def own_packages(self) -> list[str]:
        """
        The profile's own Ubuntu packages, from ``VSCODE_PACKAGES``.
        """
        return packages.parse(envfile.read(self.profile.env_file).get(packages.ENV_KEY, ""))

    def _write_packages(self, wanted: list[str]) -> None:
        """
        Record the profile's own Ubuntu packages in its ``.env``; none is
        unset, compose.yaml's default.
        """
        if wanted:
            envfile.set_value(self.profile.env_file, packages.ENV_KEY, packages.join(wanted))
        else:
            envfile.unset(self.profile.env_file, packages.ENV_KEY)

    def add_packages(self, names: list[str]) -> Result:
        """
        Add Ubuntu packages of the profile's own. The image installs them,
        so they arrive with its next rebuild (``hardcode rebuild``), which is
        due until then; a name the archive doesn't have fails that build.
        """
        named = list(dict.fromkeys(packages.check(name) for name in names))
        current = self.own_packages()

        new = [name for name in named if name not in current]
        if not new:
            return Result(self.profile.name, f"already added: {' '.join(named)}", changed=False)

        self._write_packages([*current, *new])

        words = "package" if len(new) == 1 else "packages"
        message = f"added {words} {' '.join(new)} (installed at the image's next rebuild: hardcode rebuild)"
        return Result(self.profile.name, message)

    def remove_packages(self, names: list[str]) -> Result:
        """
        Remove Ubuntu packages of the profile's own; the image loses them at
        its next rebuild.
        """
        named = list(dict.fromkeys(packages.check(name) for name in names))
        current = self.own_packages()

        missing = [name for name in named if name not in current]
        if missing:
            raise packages.PackageError(f"not one of this profile's packages: {' '.join(missing)}")

        self._write_packages([name for name in current if name not in named])

        words = "package" if len(named) == 1 else "packages"
        message = f"removed {words} {' '.join(named)} (gone from the image at its next rebuild: hardcode rebuild)"
        return Result(self.profile.name, message)

    # -- the profile's own Dockerfile steps -----------------------------------------

    def image_steps(self) -> dict:
        """
        The profile's own Dockerfile steps, for ``hardcode image`` and the
        app's Image page: where the file is and whether it exists, its text,
        the starter to begin one from, HardCode's Dockerfile they are built
        on, and whether a rebuild is due (for them, or anything else the
        image is built with).
        """
        image = self.image()
        reasons = self._rebuild_reasons(image)
        base = stack.current() / BASE_DOCKERFILE

        return {
            "profile": self.profile.name,
            "path": str(self.profile.dockerfile),
            "folder": str(self.profile.image_dir),
            "exists": self.profile.dockerfile.is_file(),
            "dockerfile": dockerfile.read(self.profile.image_dir),
            "starter": dockerfile.STARTER,
            "digest": dockerfile.digest(self.profile.image_dir),
            "base": base.read_text(encoding="utf-8"),
            "basePath": str(base),
            "image": asdict(image),
            "rebuildNeeded": None if reasons is None else bool(reasons),
            "rebuildReasons": reasons,
        }

    def save_image_steps(self, text: str) -> Result:
        """
        Write the profile's own Dockerfile steps: the next rebuild builds
        them on HardCode's image, on this computer's Docker.
        """
        path = dockerfile.save(self.profile.image_dir, text)

        message = f"saved {path} (built at the image's next rebuild: hardcode rebuild)"
        return Result(self.profile.name, message, data={"path": str(path)})

    # -- VS Code's token ---------------------------------------------------------

    def rotate_token(self) -> Result:
        """
        Give VS Code a new connection token, so that every copy of the old one
        (a screenshot, a shared screen, a browser's history) stops working.

        The token is a file in the home volume, which the VS Code container
        writes at start when there is none: it is deleted, and a running
        container restarted to write the next one; a stopped profile gets it
        at its next start. A token fixed in the profile's ``.env`` is changed
        there instead.
        """
        if envfile.read(self.profile.env_file).get("VSCODE_CONNECTION_TOKEN"):
            raise profiles.ProfileError(
                "this profile's token is fixed (VSCODE_CONNECTION_TOKEN in its .env): change it there"
            )

        remove = "rm -f ~/.vscode-server/connection-token"
        running = any(service.service == "vscode" and service.state == "running" for service in self.compose.ps())

        if running:
            self.compose.run("exec", "-T", "vscode", "sh", "-c", remove, capture=True, check=True)
            self.compose.run("--progress", "quiet", "restart", "vscode", capture=True, check=True)
            return Result(
                self.profile.name, "new VS Code token; VS Code restarted with it (hardcode open)", applied=True
            )

        # Stopped: a container of its own, on the home volume, without
        # starting anything else.
        self.compose.run(
            "--progress", "quiet", "run", "--rm", "--no-deps", "--entrypoint", "sh", "vscode", "-c", remove,
            capture=True,
            check=True,
        )  # fmt: skip
        return Result(self.profile.name, "new VS Code token at the next start", applied=False)

    # -- the image --------------------------------------------------------------

    def image(self) -> Image:
        """
        Whether the profile's VS Code image exists, and the languages and
        tools, packages and Dockerfile steps it was built with; see ``Image``.
        """
        image = f"hardcode-vscode:{self.profile.vscode_tag}"

        try:
            result = self.compose.run_docker("image", "inspect", "--format", "{{json .Config.Labels}}", image)
        except (ComposeError, subprocess.TimeoutExpired):
            return Image(built=None, languages=None, packages=None, dockerfile=None)

        if result.returncode != 0:
            return Image(built=False, languages=[], packages=[], dockerfile="")

        try:
            labels = json.loads(result.stdout or "null") or {}
        except json.JSONDecodeError:
            labels = {}

        # Without its label, an image says nothing of its languages; without
        # the others, it was built without packages or steps of the profile's.
        built_with = packages.parse(labels.get(IMAGE_PACKAGES_LABEL) or "")
        steps = labels.get(IMAGE_DOCKERFILE_LABEL) or ""

        label = labels.get(IMAGE_LANGUAGES_LABEL)
        if not label:
            return Image(built=True, languages=None, packages=built_with, dockerfile=steps)

        pairs = dict(word.split("=", 1) for word in label.split() if "=" in word)
        languages = [key for key in langs.LANGS if pairs.get(key) == "true"]
        return Image(built=True, languages=languages, packages=built_with, dockerfile=steps)

    def _rebuild_reasons(self, image: Image) -> Optional[list[str]]:
        """
        What the profile says that its image wasn't built with:
        ``languages``, ``packages`` (its own Ubuntu packages) and
        ``dockerfile`` (its own Dockerfile steps), those that differ. ``[]``
        when nothing does, or there is no image yet (the first start builds
        it); ``None`` when nobody can tell.
        """
        if image.built is None or (image.built and image.languages is None):
            return None
        if not image.built:
            return []

        env = envfile.read(self.profile.env_file)
        reasons = []

        if image.languages != langs.enabled_keys(env):
            reasons.append("languages")
        if not packages.same(image.packages or [], self.own_packages()):
            reasons.append("packages")
        if image.dockerfile != dockerfile.digest(self.profile.image_dir):
            reasons.append("dockerfile")

        return reasons

    def rebuild(self) -> Result:
        """
        Rebuild the VS Code image, and recreate the container if it is running.

        The proxy's allowlist is brought up to date too, as at every start,
        and a running proxy is given it when it doesn't have it.
        """
        changes = self.update_proxy_allowlist()

        self.compose.run("build", "vscode", check=True, to_stderr=True)
        applied = self._recreate("vscode")

        message = "VS Code image rebuilt"
        if changes:
            message += f"; allowlist: {'; '.join(changes)}"

        self.reload_if_behind()

        return Result(self.profile.name, message, applied=applied)

    # -- state for the app -----------------------------------------------------

    def state(self) -> dict:
        """
        Everything the desktop app shows for this profile.
        """
        env = envfile.read(self.profile.env_file)
        _, document = override.load(self.profile.override_file)

        enabled = langs.enabled_keys(env)
        image = self.image()

        # Whether the image has each language: unknown when nobody can tell,
        # and false for all of them while there is no image.
        languages = langs.enabled_in(env)
        for lang in languages:
            lang["inImage"] = None if image.languages is None else lang["key"] in image.languages

        # A rebuild is due when an image exists and was built with other
        # languages, packages or Dockerfile steps; with no image yet, the
        # first start builds it.
        reasons = self._rebuild_reasons(image)

        return {
            "profile": self.profile.name,
            "env": env,
            "vscodePort": envfile.get(self.profile.env_file, "VSCODE_PORT"),
            "allowlist": allowlist.as_dicts(self.allowlist_entries()),
            "lan": [pin.as_dict() for pin in self.pins()],
            "forwards": [asdict(forward) for forward in override.get_forwards(document)],
            "mounts": [asdict(mount) for mount in override.get_mounts(document)],
            "langs": languages,
            "extensions": langs.extension_sources(enabled, self._extras()),
            "packages": self.own_packages(),
            "dockerfile": {"path": str(self.profile.dockerfile), "exists": self.profile.dockerfile.is_file()},
            "image": asdict(image),
            "rebuildNeeded": None if reasons is None else bool(reasons),
            "rebuildReasons": reasons,
            "docker": self.compose.docker_enabled(),
            "devices": self.device_states(),
        }

    def services(self) -> list[dict]:
        """
        The profile's containers and their states.
        """
        return [asdict(service) for service in self.compose.ps()]

    def status(self) -> dict:
        """
        The profile's containers and their states, whether Docker is there at
        all, and why Compose couldn't list the containers when it couldn't
        (``""`` when it could): a daemon that isn't running is not the same
        as a sandbox that was never created.
        """
        services, error = self.compose.status()

        # While the VS Code container runs, how far its extensions have got.
        extensions = None
        if any(service.service == "vscode" and service.state == "running" for service in services):
            status = self.extension_status()
            if status:
                extensions = {key: status.get(key) for key in ("state", "installed", "total")}

        proxy_running = any(service.service == "squid" and service.state == "running" for service in services)

        return {
            "services": [asdict(service) for service in services],
            "docker": self.compose.has_docker(),
            "error": error,
            "extensions": extensions,
            "proxy": self.proxy_state(proxy_running),
        }

    def proxy_state(self, running: bool) -> dict:
        """
        How the proxy stands with the allowlist: ``reloading`` while the
        background reload runs (``reloader``); ``behind`` while a *running*
        proxy hasn't the allowlist the files say (an edit waiting for that
        reload, a hand edit nobody reloaded it for, a reload that failed);
        and ``error``, why the last background reload failed, while it
        stays behind.
        """
        behind = running and not profiles.proxy_allowlist_current(self.profile, self.this_computer)

        return {
            "reloading": reloader.running(self.profile),
            "behind": behind,
            "error": profiles.proxy_reload_failure(self.profile) if behind else "",
        }

    def blocked(self, since: str = "30m") -> list[dict]:
        """
        What this profile's squid and CoreDNS refused recently.
        """
        window = blocked.since_arg(since)

        def logs(service: str) -> str:
            result = self.compose.run("logs", "--since", window, "--no-log-prefix", service, capture=True, timeout=60)
            return result.stdout or ""

        return [item.as_dict() for item in blocked.parse(logs("squid"), logs("coredns"))]

    def logs(self, services: list[str], tail: int = 200) -> str:
        """
        The last *tail* lines each of *services* logged; every service's when
        none is named.

        Colours are left out, and so is the service-name prefix when there is
        only one service to tell apart.
        """
        arguments = ["logs", "--no-color", "--tail", str(tail)]
        if len(services) == 1:
            arguments.append("--no-log-prefix")

        return self.compose.output(*arguments, *services, timeout=60)

    # -- profile lifecycle -----------------------------------------------------

    def destroy(self) -> Result:
        """
        Remove the profile's containers, volumes and images, then its directory.

        The directory goes only once the containers and volumes are gone: a
        teardown that fails leaves the files needed to look and try again.
        """
        name = self.profile.name

        # `--profile docker` takes the daemon's container too, whether or not it
        # is part of the project at the moment.
        down = self.compose.run(
            "--profile", "docker", "--progress", "quiet", "down", "-v", "--remove-orphans", capture=True
        )
        if down.returncode != 0:
            detail = (down.stderr or down.stdout or "").strip()
            message = f"docker compose down failed, so profiles/{name} is kept"
            raise ComposeError(f"{message}: {detail}" if detail else message)

        # Its image, then HardCode's that its own Dockerfile steps were built
        # on, when they ever were.
        images = [f"hardcode-vscode:{self.profile.vscode_tag}", f"{BASE_IMAGE}:{self.profile.vscode_tag}"]
        removals = [(image, self.compose.run_docker("image", "rm", image)) for image in images]
        profiles.remove(self.root, name)

        notes = []
        for image, removal in removals:
            if removal.returncode != 0 and "No such image" not in (removal.stderr or ""):
                detail = (removal.stderr or removal.stdout or "").strip()
                notes.append(f"image {image} not removed: {detail}")

        note = f" ({'; '.join(notes)})" if notes else ""
        return Result(name, f"profile '{name}' removed{note}")
