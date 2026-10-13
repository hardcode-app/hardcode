"""
The operations behind the commands.

Each operation takes a profile, edits its files, applies the change to the
running containers when there are any, and returns a ``Result`` that the
command line prints, or emits as JSON for the app.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import allowlist, blocked, envfile, langs, override, profiles, stack
from .compose import Compose, ComposeError, Runner

# Where the VS Code container's extension sync says how far it has got,
# relative to the home directory.
EXTENSION_STATUS = ".vscode-server/.hardcode-extensions-status.json"

# The VS Code image's label naming the languages and tools it was built
# with (images/vscode/Dockerfile sets it), in the project's namespace.
IMAGE_LANGUAGES_LABEL = "io.github.hardcode-app.languages"

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
    next start), and ``None`` when there was nothing to apply.
    """

    profile: str
    message: str
    changed: bool = True
    applied: Optional[bool] = None
    data: dict = field(default_factory=dict)


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
    """

    built: Optional[bool]
    languages: Optional[list[str]]


def _print_warning(message: str) -> None:
    print(message, file=sys.stderr)


class Ops:
    """
    The operations on one profile of a checkout.
    """

    def __init__(
        self,
        root: Path,
        profile: profiles.Profile,
        runner: Runner = subprocess.run,
        docker: Optional[str] = None,
        sleep: Callable[[float], None] = time.sleep,
        warn: Callable[[str], None] = _print_warning,
    ):
        self.root = root
        self.profile = profile
        self.warn = warn
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
        give the proxy the new allowlist (at once if it is running): whether
        it changed, what *edit* said about it, and whether the running proxy
        took it.
        """
        path = self.profile.allowlist_file
        before = path.read_text(encoding="utf-8")

        after, message = edit(before)
        if after == before:
            return False, message, None

        path.write_text(after, encoding="utf-8")
        profiles.write_proxy_allowlist(self.profile)

        return True, message, self.reload_allowlist()

    def allow(self, entry: str, flag: str = "") -> Result:
        """
        Allow an entry, or change the flag of one of the profile's own; a
        provided entry switched off is switched back on.
        """
        changed, message, applied = self._edit_allowlist(
            lambda text: allowlist.allow(text, entry, flag, self._languages()),
        )
        return Result(self.profile.name, message, changed=changed, applied=applied)

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

        return Result(self.profile.name, f"denied {shown}", applied=applied)

    def set_entry_enabled(self, entry: str, enabled: bool, ssh: bool = False) -> Result:
        """
        Switch an entry on or off: its HTTPS form, or with *ssh* its SSH form.
        One of the profile's own is commented back in or out; a provided one
        loses or gets its ``off`` line.
        """
        changed, message, applied = self._edit_allowlist(
            lambda text: allowlist.switch(text, entry, enabled, ssh, self._languages()),
        )
        return Result(self.profile.name, message, changed=changed, applied=applied)

    def remove_entry(self, entry: str, ssh: bool = False) -> Result:
        """
        Delete one of the profile's own entries. A provided one can only be
        switched off, which the refusal says.
        """
        changed, message, applied = self._edit_allowlist(
            lambda text: allowlist.delete(text, entry, ssh, self._languages()),
        )
        return Result(self.profile.name, message, changed=changed, applied=applied)

    def update_proxy_allowlist(self) -> list[str]:
        """
        Write the allowlist the proxy is given, from ``allowlist.txt`` and the
        profile's languages and tools as they are now: what that changed, one
        line per entry (``Rust: + newrustsite.com``), when there was a file
        before. A newer HardCode's provided entries arrive this way, at the
        next start or reload. The running proxy isn't touched.
        """
        before, after = profiles.write_proxy_allowlist(self.profile)
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

        return Result(self.profile.name, message, changed=bool(changes), applied=applied, data={"changes": changes})

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
        Mount a folder of this machine into the VS Code container and the Docker daemon.
        """
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
        allowlist (the proxy reloads at once if running); the extension
        list, which a running VS Code container picks up within seconds and
        installs from in the background; and, unless *build* is false, the
        image, rebuilt with the toolchains and the VS Code container
        recreated with it.
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

        # What took effect at once: the proxy's new policy, or else the
        # extensions the running container installs or removes by itself.
        applied: Optional[bool] = None
        if allowlist_changed:
            applied = self.reload_allowlist()
        elif list_changed:
            applied = self.compose.running()

        if build:
            self.compose.run("build", "vscode", check=True, to_stderr=True)
            applied = self._recreate("vscode")

        note = "" if build else " (the image is rebuilt with: hardcode rebuild)"
        return Result(self.profile.name, f"{', '.join(words)}{note}", applied=applied)

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
        # Cut off in the container: a larger file never reaches this process.
        try:
            output = self.compose.exec("vscode", f"head -c {STATUS_BYTES} ~/{EXTENSION_STATUS} 2>/dev/null || true")
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
        tools it was built with; see ``Image``.
        """
        image = f"hardcode-vscode:{self.profile.vscode_tag}"

        try:
            result = self.compose.run_docker("image", "inspect", "--format", "{{json .Config.Labels}}", image)
        except (ComposeError, subprocess.TimeoutExpired):
            return Image(built=None, languages=None)

        if result.returncode != 0:
            return Image(built=False, languages=[])

        try:
            labels = json.loads(result.stdout or "null") or {}
        except json.JSONDecodeError:
            return Image(built=True, languages=None)

        label = labels.get(IMAGE_LANGUAGES_LABEL)
        if not label:
            return Image(built=True, languages=None)

        pairs = dict(word.split("=", 1) for word in label.split() if "=" in word)
        return Image(built=True, languages=[key for key in langs.LANGS if pairs.get(key) == "true"])

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
        # languages; with no image yet, the first start builds it.
        rebuild_needed: Optional[bool] = False
        if image.built is None or (image.built and image.languages is None):
            rebuild_needed = None
        elif image.built:
            rebuild_needed = image.languages != enabled

        return {
            "profile": self.profile.name,
            "env": env,
            "vscodePort": envfile.get(self.profile.env_file, "VSCODE_PORT"),
            "allowlist": allowlist.as_dicts(self.allowlist_entries()),
            "forwards": [asdict(forward) for forward in override.get_forwards(document)],
            "mounts": [asdict(mount) for mount in override.get_mounts(document)],
            "langs": languages,
            "extensions": langs.extension_sources(enabled, self._extras()),
            "image": {"built": image.built, "languages": image.languages},
            "rebuildNeeded": rebuild_needed,
            "docker": self.compose.docker_enabled(),
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

        return {
            "services": [asdict(service) for service in services],
            "docker": self.compose.has_docker(),
            "error": error,
            "extensions": extensions,
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
        Remove the profile's containers, volumes and image, then its directory.

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

        image = self.compose.run_docker("image", "rm", f"hardcode-vscode:{self.profile.vscode_tag}")
        profiles.remove(self.root, name)

        note = ""
        if image.returncode != 0 and "No such image" not in (image.stderr or ""):
            detail = (image.stderr or image.stdout or "").strip()
            note = f" (image hardcode-vscode:{self.profile.vscode_tag} not removed: {detail})"

        return Result(name, f"profile '{name}' removed{note}")
