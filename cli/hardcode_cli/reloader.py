"""
The proxy's reload after edits of the allowlist, in the background.

An edit of a running profile's allowlist returns once its files are
written. The proxy is given the new allowlist by a process of its own,
``hardcode _reloader``, which the edit starts when the profile has none: it
waits until the profile's files have gone ``QUIET_SECONDS`` without a
change, and reloads the proxy then. An edit made meanwhile starts the wait
again, and so does one made during the reload, once the reload is over: a
script can make a hundred edits, and the proxy is reloaded once, after the
last.

A profile has one at most: it holds the profile's reloader lock while it
runs. It ends once the proxy has what the files say, or when the profile
isn't running (its next start gives the proxy the allowlist). A reload that
fails is kept for ``ps`` and the app to show (``profiles.proxy_reload_failure``),
and the next edit tries again, as ``hardcode reload`` does at once.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from . import profiles

# How long the profile's files must go without a change before the proxy is
# reloaded, and how often they are looked at meanwhile.
QUIET_SECONDS = 5.0
POLL_SECONDS = 0.5

# How long a reloader tries for its lock before it leaves the work to the
# one holding it: another command may be looking at the lock (ps does, to
# say whether one runs), which takes it for a moment.
LOCK_SECONDS = 2.0

# The command a reloader runs as, which nobody else needs: it isn't listed
# in the usage or the docs' reference.
COMMAND = "_reloader"

Spawn = Callable[[list[str]], None]


def running(profile: profiles.Profile) -> bool:
    """
    Whether the profile's reloader is running: someone holds its lock.
    """
    with profiles.try_lock(profile, profiles.RELOADER_LOCK_FILE) as free:
        return not free


def start(root: Path, profile: profiles.Profile, spawn: Optional[Spawn] = None) -> None:
    """
    After an edit of a running profile's allowlist: start its reloader,
    unless one is running already, which picks the edit up by itself. A
    failed reload before is forgotten, since this one tries again.
    """
    profiles.forget_proxy_reload_failure(profile)

    if running(profile):
        return

    (spawn or _spawn)(command(root, profile))


def command(root: Path, profile: profiles.Profile) -> list[str]:
    """
    The command line of the profile's reloader: this same command as it was
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

    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **options,
    )


def fingerprint(profile: profiles.Profile) -> str:
    """
    A digest of the files an allowlist edit writes, ``allowlist.txt`` and the
    ``.env`` (its languages and tools); ``""`` once the profile is gone.
    They are read without a lock: a file caught half-written only looks
    like one more change.
    """
    digest = hashlib.sha256()

    for path in (profile.allowlist_file, profile.env_file):
        try:
            digest.update(path.read_bytes())
        except OSError:
            return ""

        digest.update(b"\0")

    return digest.hexdigest()


def wait_until_quiet(
    look: Callable[[], str],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """
    Wait until *look* (a ``fingerprint``) has stayed the same for
    ``QUIET_SECONDS``: every change starts the wait again.
    """
    seen = look()
    since = clock()

    while clock() - since < QUIET_SECONDS:
        sleep(POLL_SECONDS)

        now = look()
        if now != seen:
            seen = now
            since = clock()


def reload_step(ops) -> bool:
    """
    One reload of the reloader's, for the profile of *ops* (an ``Ops``):
    give the running proxy the allowlist if it doesn't have it. Whether the
    profile is running, and there may be more to do; not once it has been
    removed.

    The profile is looked at, and the proxy allowlist written, under both of
    its locks, so nothing changes either meanwhile. The reload itself holds
    the containers' alone: allowlist edits go on during it, and are the next
    reload's. Nobody writes the proxy allowlist without the containers'
    lock, so the reload records as loaded what the proxy was given.
    """
    profile = ops.profile

    with profiles.containers_lock(profile):
        with profiles.files_lock(profile):
            if not _still_there(profile) or not ops.compose.running():
                return False

            ops.update_proxy_allowlist()
            if profiles.proxy_allowlist_applied(profile):
                return True

        ops.reload_allowlist()

    return True


def run(
    profile: profiles.Profile,
    step: Callable[[], bool],
    behind: Callable[[], bool],
    look: Callable[[], str],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """
    The reloader: under the profile's reloader lock, wait until its files
    are quiet (*look*, a ``fingerprint``), reload (*step*, a ``reload_step``),
    and again for as long as the proxy is *behind* the files afterwards
    (an edit made during the reload). It ends when the proxy is caught up,
    or the profile isn't running; a failure is recorded, for ``ps`` and
    the app.

    An edit that finds the lock taken leaves its change to the reloader
    holding it. One made as this one lets go may have found it taken: the
    files are looked at again once it is let go, and it is taken again for
    them if they are ahead.
    """
    while _hold_and_reload(profile, step, behind, look, clock, sleep):
        if not behind():
            return


def _hold_and_reload(
    profile: profiles.Profile,
    step: Callable[[], bool],
    behind: Callable[[], bool],
    look: Callable[[], str],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> bool:
    """
    Take the reloader lock, and reload for as long as there is something to
    reload; whether the proxy was caught up as the lock was let go. ``False``
    when another reloader holds the lock, the profile isn't running, or the
    reload failed (which is recorded).
    """
    with profiles.try_lock(profile, profiles.RELOADER_LOCK_FILE, LOCK_SECONDS, clock, sleep) as held:
        if not held:
            return False

        try:
            while True:
                wait_until_quiet(look, clock, sleep)

                if not step():
                    return False
                if not behind():
                    return True

        # Recorded under the lock, so that a reloader started after it, for
        # a later edit, finds the failure there and forgets it. Not for a
        # profile removed meanwhile: the file would make its folder, all
        # but its lock files gone, look like a profile again.
        except Exception as error:  # noqa: BLE001 - kept for ps and the app: nobody else would see it
            if _still_there(profile):
                profiles.record_proxy_reload_failure(profile, str(error) or type(error).__name__)
            return False


def _still_there(profile: profiles.Profile) -> bool:
    """
    Whether the profile hasn't been removed: its ``allowlist.txt`` is there.
    """
    return profile.allowlist_file.exists()
