"""
Changes that recreate containers, made a few seconds after the last one.

A change to a running profile that its containers take only by being
recreated (VS Code's port, a forward, a mount, the Docker daemon on or off,
a device, a new VS Code token) returns as soon as it is checked and its
files are written, saying which containers restart, and when. They are
recreated by a process of its own, ``hardcode _applier``, which the change
starts when the profile has none: ``QUIET_SECONDS`` after the profile's last
such change, all of them at once. A change made meanwhile pushes the time
out, and adds its containers.

What waits, and what became of the last changes, is in the profile's
``changes.json`` (``Waiting``), which ``ps`` and the app read; what the
applier did is in ``applier.log`` (``hardcode logs applier``). When the
containers can't be recreated with the changes (a port another program
holds, say), the profile's settings and files go back to what they were
before the first of them, the containers are recreated from those, and
``changes.json`` says which changes were undone and why, until the next
change. ``hardcode apply`` makes the waiting changes at once.

A profile has one applier at most: it holds the profile's applier lock while
it runs. It ends once nothing waits. It recreates containers holding the
profile's containers lock, which every change that recreates containers
takes too (with the files' lock): such a change waits for the containers
being recreated, and is the next batch. An edit of the allowlist takes the
files' lock alone and never waits for it; nor goes through here: the proxy
reloads with the allowlist by itself (the stack's ``allowlist-watch``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, ContextManager, Optional

from . import devices, envfile, profiles

# How long a profile must go without a change before the containers its
# changes recreate are recreated, and how often the time is looked at
# meanwhile. The proxy's allowlist-watch waits as long for the allowlist.
QUIET_SECONDS = 5.0
POLL_SECONDS = 0.5

# How long an applier tries for its lock before it leaves the work to the
# one holding it: another command may be looking at the lock (ps does, to
# say whether one runs), which takes it for a moment.
LOCK_SECONDS = 2.0

# The command an applier runs as, which nobody else needs: it isn't listed in
# the usage or the docs' reference.
COMMAND = "_applier"

# The settings in the profile's .env that changes made here write, and the
# files: what goes back when they can't be made.
ENV_KEYS = ("VSCODE_PORT", "ENABLE_DOCKER", devices.ENV_KEY)
FILES = ("compose.override.yaml", devices.FRAGMENT)

# How long the applier's log may grow before its older half goes.
LOG_BYTES = 64 * 1024

Spawn = Callable[[list[str]], None]
Clock = Callable[[], float]


@dataclass
class Waiting:
    """
    The changes a profile has waiting, and what became of the last ones.

    ``changes`` are what each change said (``VS Code on port 12000``);
    ``services`` the containers they recreate, in the order they were
    named; ``token`` whether VS Code is given a new token as it restarts;
    ``due`` when they are made, in seconds since the epoch (the app's clock
    counts the same); ``before`` the profile's settings and files as they
    were before the first of them (``snapshot``); ``applying`` while they
    are being made.

    ``undone`` and ``error``: the last changes that couldn't be made, and
    why, until the next change.
    """

    changes: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    token: bool = False
    due: float = 0.0
    applying: bool = False
    before: dict = field(default_factory=dict)
    undone: list[str] = field(default_factory=list)
    error: str = ""

    def report(self) -> dict:
        """
        What ``ps --json`` says of it: everything but ``before``, which is
        the profile's own files.
        """
        report = asdict(self)
        report.pop("before")
        return report


# -- what waits ---------------------------------------------------------------


def load(profile: profiles.Profile) -> Waiting:
    """
    What the profile has waiting; nothing when there is no ``changes.json``,
    or one that isn't what this module writes.
    """
    try:
        found = json.loads(profile.changes_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Waiting()

    if not isinstance(found, dict):
        return Waiting()

    try:
        waiting = Waiting(**found)
    except TypeError:
        return Waiting()

    lists = (waiting.changes, waiting.services, waiting.undone)
    if not all(isinstance(items, list) and all(isinstance(item, str) for item in items) for items in lists):
        return Waiting()

    return waiting


def save(profile: profiles.Profile, waiting: Waiting) -> None:
    """
    Write what the profile has waiting, beside its final name and moved
    into place, so that ``ps``, which reads it without a lock, never finds
    it half-written. Nothing waiting and nothing to say is no file.
    """
    path = profile.changes_file

    if waiting == Waiting():
        path.unlink(missing_ok=True)
        return

    written = path.with_name(path.name + ".tmp")
    written.write_text(json.dumps(asdict(waiting), indent=2) + "\n", encoding="utf-8")
    os.replace(written, path)


def snapshot(profile: profiles.Profile) -> dict:
    """
    The profile's settings and files that changes made here write, as they
    are now: what ``restore`` puts back.
    """
    env = envfile.read(profile.env_file)

    files = {}
    for name in FILES:
        path = profile.dir / name
        files[name] = path.read_text(encoding="utf-8") if path.exists() else None

    return {"env": {key: env.get(key) for key in ENV_KEYS}, "files": files}


def restore(profile: profiles.Profile, before: dict) -> None:
    """
    Put back the profile's settings and files as *before* (a ``snapshot``)
    has them: a setting that wasn't set is unset, a file that wasn't there
    goes. The rest of the ``.env`` stays as it is.
    """
    for key, value in (before.get("env") or {}).items():
        if value is None:
            envfile.unset(profile.env_file, key)
        else:
            envfile.set_value(profile.env_file, key, value)

    for name, text in (before.get("files") or {}).items():
        path = profile.dir / name
        if text is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(text, encoding="utf-8")


def record(
    profile: profiles.Profile,
    change: str,
    services: list[str],
    before: dict,
    token: bool = False,
    clock: Clock = time.time,
) -> Waiting:
    """
    Add a change to what the profile has waiting, under its files' lock
    (the caller's), and push the time out to ``QUIET_SECONDS`` from now.
    *before* is the profile as the change found it (``snapshot``): what
    goes back if the batch can't be made, when the change is its first.
    The last changes undone are forgotten.
    """
    waiting = load(profile)

    if not waiting.changes:
        waiting = Waiting(before=before)

    waiting.changes.append(change)
    waiting.services += [service for service in services if service not in waiting.services]
    waiting.token = waiting.token or token
    waiting.due = clock() + QUIET_SECONDS

    save(profile, waiting)
    note(profile, f"waiting: {change} ({', '.join(waiting.services)} in {QUIET_SECONDS:g} seconds)", clock)

    return waiting


def forget(profile: profiles.Profile) -> None:
    """
    Forget what waits, and what was undone: a start, a stop or a removal
    of the containers made it moot. The files stay as they are, and the
    next start takes them.
    """
    if profile.changes_file.exists():
        profile.changes_file.unlink(missing_ok=True)
        note(profile, "the changes waiting are left to the next start")


# -- the log ------------------------------------------------------------------


def note(profile: profiles.Profile, line: str, clock: Clock = time.time) -> None:
    """
    Add a line to the applier's log, with when; the log's older half goes
    once it is ``LOG_BYTES`` long.
    """
    path = profile.applier_log
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(clock()))

    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {line}\n")

    if path.stat().st_size > LOG_BYTES:
        kept = path.read_text(encoding="utf-8")[-(LOG_BYTES // 2) :]
        path.write_text(kept[kept.find("\n") + 1 :], encoding="utf-8")


def log(profile: profiles.Profile, tail: int = 200) -> str:
    """
    The last *tail* lines of the applier's log; ``""`` without one.
    """
    try:
        lines = profile.applier_log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""

    return "".join(f"{line}\n" for line in lines[-tail:])


# -- making the changes ---------------------------------------------------------


def make(
    profile: profiles.Profile,
    recreate: Callable[[Waiting], None],
    running: Callable[[], bool],
    hold_files: Callable[[], ContextManager] = nullcontext,
    clock: Clock = time.time,
    now: bool = False,
) -> Optional[Waiting]:
    """
    Make the changes the profile has waiting, under its containers' lock
    (the caller's): *recreate* recreates the containers they name, from the
    files as they are. Not before they are due, unless *now*. *hold_files*
    holds the files' lock while ``changes.json`` and the files are read or
    put back, unless the caller holds it all along.

    Returns what became of them: nothing waiting, with ``undone`` and
    ``error`` when they couldn't be made, in which case the files went back
    as they were before the first of them and the containers were recreated
    from those; ``None`` when there was nothing to make, or they aren't due
    yet. While the profile isn't running they are left to its next start.
    """
    with hold_files():
        if not _still_there(profile):
            return None

        waiting = load(profile)
        if not waiting.changes:
            return None
        if not now and clock() < waiting.due:
            return None

        if not running():
            save(profile, Waiting())
            note(profile, f"the profile isn't running: {'; '.join(waiting.changes)} at its next start", clock)
            return Waiting()

        waiting.applying = True
        save(profile, waiting)

    note(profile, f"recreating {', '.join(waiting.services)} for: {'; '.join(waiting.changes)}", clock)

    try:
        recreate(waiting)

    # Whatever went wrong, the changes are undone: the profile is left as it
    # was before them, running, and why is kept for ps and the app.
    except Exception as error:  # noqa: BLE001 - undone, and kept for ps and the app: nobody else would see it
        reason = str(error) or type(error).__name__

        # Not for a profile removed meanwhile: its files would make its
        # folder, all but its lock files gone, look like a profile again.
        with hold_files():
            if not _still_there(profile):
                return None

            note(profile, f"couldn't make the changes, so they were undone: {reason}", clock)
            restore(profile, waiting.before)

        try:
            recreate(Waiting(services=waiting.services))
        except Exception as second:  # noqa: BLE001 - kept in the log: the reason worth showing is the first
            note(profile, f"{', '.join(waiting.services)} couldn't be started again as before: {second}", clock)

        undone = Waiting(undone=waiting.changes, error=reason)
        with hold_files():
            if _still_there(profile):
                save(profile, undone)

        return undone

    with hold_files():
        if not _still_there(profile):
            return None

        save(profile, Waiting())
        note(profile, f"done: {', '.join(waiting.services)} recreated", clock)

    return Waiting()


def run(
    profile: profiles.Profile,
    recreate: Callable[[Waiting], None],
    running: Callable[[], bool],
    clock: Clock = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """
    The applier: under the profile's applier lock, wait until what the
    profile has waiting is due, and make it (``make``), under the profile's
    containers' lock; again for whatever was recorded meanwhile. It ends
    once nothing waits, or another applier holds the lock.

    A change that finds an applier running leaves its change to it. One
    made as this one lets go may have found it running: ``changes.json`` is
    looked at again once the lock is let go, and the lock taken again for
    whatever waits.
    """
    while _hold_and_make(profile, recreate, running, clock, sleep):
        if not load(profile).changes:
            return


def _hold_and_make(
    profile: profiles.Profile,
    recreate: Callable[[Waiting], None],
    running: Callable[[], bool],
    clock: Clock,
    sleep: Callable[[float], None],
) -> bool:
    """
    Take the applier lock, and make the changes waiting, until none are;
    whether the lock was taken.
    """
    with profiles.try_lock(profile, profiles.APPLIER_LOCK_FILE, LOCK_SECONDS, clock, sleep) as held:
        if not held:
            return False

        while _still_there(profile):
            waiting = load(profile)
            if not waiting.changes:
                break

            # Pushed out by every change: looked at again until it is due.
            left = waiting.due - clock()
            if left > 0:
                sleep(min(left, POLL_SECONDS))
                continue

            with profiles.containers_lock(profile):
                make(profile, recreate, running, lambda: profiles.files_lock(profile), clock)

        return True


def _still_there(profile: profiles.Profile) -> bool:
    """
    Whether the profile hasn't been removed: its ``allowlist.txt`` is there.
    """
    return profile.allowlist_file.exists()


# -- the process ------------------------------------------------------------------


def applying(profile: profiles.Profile) -> bool:
    """
    Whether the profile's applier is running: someone holds its lock.
    """
    with profiles.try_lock(profile, profiles.APPLIER_LOCK_FILE) as free:
        return not free


def start(root: Path, profile: profiles.Profile, spawn: Optional[Spawn] = None) -> None:
    """
    After a change recorded for a running profile: start its applier,
    unless one is running already, which picks the change up by itself.
    """
    if applying(profile):
        return

    (spawn or _spawn)(command(root, profile))


def command(root: Path, profile: profiles.Profile) -> list[str]:
    """
    The command line of the profile's applier: this same command as it was
    started, the app's executable or Python with this package, for the
    profile in the home *root*.
    """
    arguments = ["--config", str(root), "-p", profile.name, COMMAND]

    # The app's own copy is one executable, which is the command.
    if getattr(sys, "frozen", False):
        return [sys.executable, *arguments]

    # Anywhere else, the package is imported from the folder this one is
    # in, whichever started the command: the checkout's script, an installed
    # one, a test.
    package_parent = str(Path(__file__).resolve().parent.parent)
    program = f"import sys; sys.path.insert(0, {package_parent!r}); from hardcode_cli.cli import main; sys.exit(main())"
    return [sys.executable, "-c", program, *arguments]


def _spawn(command: list[str]) -> None:
    """
    Start *command* on its own: with none of this process's input or output,
    which the app reads to the end, and in a session (on Windows, a process
    group) of its own, so that it carries on after this command ends.
    """
    options: dict = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True

    # The app's command is a single-file build (PyInstaller's --onefile): it
    # runs from a folder it unpacks itself into, and deletes as it ends. A
    # copy started from inside it would, by the environment it inherits, run
    # from that same folder, and die with it a moment later, before it had
    # made anything. Told to start afresh, the copy unpacks its own.
    if getattr(sys, "frozen", False):
        options["env"] = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}

    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **options,
    )
