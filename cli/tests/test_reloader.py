"""
Tests for the proxy's background reload (hardcode_cli.reloader), with the
clock and the reload itself faked.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from hardcode_cli import langs, profiles, reloader

from .helpers import HomeTestCase


class FakeClock:
    """
    A clock that moves only when slept on, and runs *at* (a time: what to
    do) as it passes those times: the edits a test makes while a reloader
    waits.
    """

    def __init__(self, at=None):
        self.now = 0.0
        self.at = dict(at or {})

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

        for when in sorted(self.at):
            if when <= self.now:
                self.at.pop(when)()


class QuietTests(unittest.TestCase):
    """
    The wait for the files to go quiet.
    """

    def test_it_waits_for_the_quiet_seconds(self):
        clock = FakeClock()

        reloader.wait_until_quiet(lambda: "same", clock.time, clock.sleep)

        self.assertEqual(clock.now, reloader.QUIET_SECONDS)

    def test_every_change_starts_the_wait_again(self):
        files = {"text": "a"}

        def edit(text):
            return lambda: files.update(text=text)

        clock = FakeClock({2.0: edit("b"), 6.0: edit("c")})

        reloader.wait_until_quiet(lambda: files["text"], clock.time, clock.sleep)

        self.assertEqual(clock.now, 6.0 + reloader.QUIET_SECONDS, "five quiet seconds after the last edit")


class RunTests(HomeTestCase):
    """
    The reloader's loop: when it reloads, how often, and when it ends.
    """

    def setUp(self):
        super().setUp()
        profiles.create(self.root, "default", languages=list(langs.LANGS))
        self.profile = profiles.get(self.root, "default")
        self.files = {"text": "a"}
        self.loaded = {"text": "a"}
        self.steps = []

    def look(self):
        return self.files["text"]

    def behind(self):
        return self.loaded["text"] != self.files["text"]

    def step(self, during=None):
        """
        A reload: it gives the proxy the files as they are, then runs
        *during* (an edit made while it ran).
        """
        self.steps.append(self.clock.now)
        self.loaded["text"] = self.files["text"]
        if during:
            during()
        return True

    def run_reloader(self, step=None):
        reloader.run(
            self.profile,
            step=step or self.step,
            behind=self.behind,
            look=self.look,
            clock=self.clock.time,
            sleep=self.clock.sleep,
        )

    def test_a_burst_of_edits_is_one_reload_after_the_last(self):
        def edit(text):
            return lambda: self.files.update(text=text)

        self.files["text"] = "b"
        self.clock = FakeClock({1.0: edit("c"), 2.0: edit("d"), 3.5: edit("e")})

        self.run_reloader()

        self.assertEqual(self.steps, [3.5 + reloader.QUIET_SECONDS])
        self.assertEqual(self.loaded["text"], "e")

    def test_an_edit_during_the_reload_is_another_reload_after_it(self):
        self.files["text"] = "b"
        self.clock = FakeClock()
        edits = [lambda: self.files.update(text="c")]

        self.run_reloader(step=lambda: self.step(edits.pop() if edits else None))

        self.assertEqual(self.steps, [5.0, 10.0], "a new wait starts when the reload ends")
        self.assertEqual(self.loaded["text"], "c")

    def test_it_ends_when_the_profile_isnt_running(self):
        self.files["text"] = "b"
        self.clock = FakeClock()

        def stopped():
            self.steps.append(self.clock.now)
            return False

        self.run_reloader(step=stopped)

        self.assertEqual(len(self.steps), 1)
        self.assertTrue(self.behind(), "its next start gives the proxy the allowlist")

    def test_one_reloader_at_a_time(self):
        self.files["text"] = "b"
        self.clock = FakeClock()

        with profiles.try_lock(self.profile, profiles.RELOADER_LOCK_FILE):
            self.run_reloader()

        self.assertEqual(self.steps, [], "the one holding the lock does the work")
        self.assertGreaterEqual(self.clock.now, reloader.LOCK_SECONDS, "after trying for a moment")

    def test_a_failed_reload_is_kept(self):
        self.files["text"] = "b"
        self.clock = FakeClock()

        def fail():
            raise RuntimeError("squid didn't come back healthy")

        self.run_reloader(step=fail)

        self.assertEqual(profiles.proxy_reload_failure(self.profile), "squid didn't come back healthy")
        self.assertFalse(reloader.running(self.profile), "and the lock is let go")

    def test_a_profile_removed_meanwhile_is_left_gone(self):
        self.files["text"] = "b"
        self.clock = FakeClock()

        def removed_then_failed():
            with profiles.lock(self.profile):
                profiles.remove(self.root, "default")
            raise RuntimeError("Compose can't find the profile's .env")

        self.run_reloader(step=removed_then_failed)

        self.assertFalse(self.profile.dir.exists(), "no failure is written into the folder it leaves")
        self.assertEqual(profiles.list_profiles(self.root), [])

    def test_a_step_does_nothing_for_a_removed_profile(self):
        ops = mock.Mock(profile=self.profile)
        profiles.remove(self.root, "default")

        self.assertFalse(reloader.reload_step(ops))
        ops.compose.running.assert_not_called()
        ops.reload_allowlist.assert_not_called()


class StartTests(HomeTestCase):
    """
    Starting a reloader, and the command it runs as.
    """

    def setUp(self):
        super().setUp()
        profiles.create(self.root, "default", languages=[])
        self.profile = profiles.get(self.root, "default")
        self.spawned = []

    def test_an_edit_starts_one_unless_one_runs(self):
        profiles.record_proxy_reload_failure(self.profile, "an old failure")

        reloader.start(self.root, self.profile, spawn=self.spawned.append)

        self.assertEqual(self.spawned, [reloader.command(self.root, self.profile)])
        self.assertEqual(profiles.proxy_reload_failure(self.profile), "", "another reload is on its way")

        with profiles.try_lock(self.profile, profiles.RELOADER_LOCK_FILE):
            self.assertTrue(reloader.running(self.profile))
            reloader.start(self.root, self.profile, spawn=self.spawned.append)

        self.assertEqual(len(self.spawned), 1, "the running one picks the edit up")
        self.assertFalse(reloader.running(self.profile))

    def test_the_command_is_this_command_for_the_profile(self):
        command = reloader.command(self.root, self.profile)

        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[-5:], ["--config", str(self.root), "-p", "default", reloader.COMMAND])

        with mock.patch.object(sys, "frozen", True, create=True):
            self.assertEqual(
                reloader.command(self.root, self.profile),
                [sys.executable, "--config", str(self.root), "-p", "default", reloader.COMMAND],
                "the app's executable is the command itself",
            )

    def test_the_command_runs(self):
        # The command line as a reloader is started, with an argument
        # parser's answer in place of the reload: it finds the package.
        command = reloader.command(self.root, self.profile)
        result = subprocess.run(
            [*command[:-1], "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "HARDCODE_CONFIG": str(self.root)},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("hardcode", result.stdout)

    def test_the_fingerprint_follows_the_files_an_edit_writes(self):
        before = reloader.fingerprint(self.profile)

        with open(self.profile.allowlist_file, "a", encoding="utf-8") as handle:
            handle.write("x.org\n")
        after_edit = reloader.fingerprint(self.profile)

        with open(self.profile.env_file, "a", encoding="utf-8") as handle:
            handle.write("INSTALL_GO=true\n")
        after_language = reloader.fingerprint(self.profile)

        self.assertEqual(len({before, after_edit, after_language}), 3)
        self.assertEqual(reloader.fingerprint(profiles.Profile("gone", Path(self.root) / "nowhere")), "")


class SpawnTests(unittest.TestCase):
    """
    How a reloader is started: on its own, and, from the app's single-file
    command, as a program of its own. Nothing is started: Popen is faked.
    """

    def test_a_single_file_build_starts_a_copy_that_unpacks_itself(self):
        # The app's command runs from a folder it unpacks itself into and
        # deletes as it ends: a reloader started with the environment it
        # inherits would run from that folder, and die with it.
        with mock.patch.object(subprocess, "Popen") as popen, mock.patch.object(sys, "frozen", True, create=True):
            reloader._spawn(["hardcode", "_reloader"])

        options = popen.call_args.kwargs
        self.assertEqual(options["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertEqual(options["env"]["PATH"], os.environ["PATH"], "and the rest of its environment as it is")
        self.assertEqual((options["stdin"], options["stdout"]), (subprocess.DEVNULL, subprocess.DEVNULL))

    def test_run_from_source_it_inherits_the_environment_as_it_is(self):
        with mock.patch.object(subprocess, "Popen") as popen:
            reloader._spawn(["python3", "-c", "pass"])

        self.assertNotIn("env", popen.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
