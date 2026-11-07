"""
Tests for the changes that recreate containers (hardcode_cli.applier), with
the clock and the recreation itself faked.
"""

import os
import subprocess
import sys
import unittest
from unittest import mock

from hardcode_cli import applier, devices, envfile, profiles

from .helpers import HomeTestCase


class FakeClock:
    """
    A clock that moves only when slept on, and runs *at* (a time: what to
    do) as it passes those times: the changes a test makes while an applier
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


class WaitingTests(HomeTestCase):
    """
    What a profile has waiting: recorded, read back, and put back.
    """

    def setUp(self):
        super().setUp()
        profiles.create(self.root, "default", languages=[])
        self.profile = profiles.get(self.root, "default")

    def test_changes_add_up_and_push_the_time_out(self):
        before = applier.snapshot(self.profile)

        first = applier.record(self.profile, "forward 3000", ["gateway"], before, clock=lambda: 100.0)
        self.assertEqual((first.services, first.due), (["gateway"], 100.0 + applier.QUIET_SECONDS))

        envfile.set_value(self.profile.env_file, "VSCODE_PORT", "12000")
        second = applier.record(
            self.profile,
            "new token",
            ["vscode", "gateway"],
            applier.snapshot(self.profile),
            token=True,
            clock=lambda: 103.0,
        )

        self.assertEqual(second.changes, ["forward 3000", "new token"])
        self.assertEqual(second.services, ["gateway", "vscode"], "each once, in the order named")
        self.assertTrue(second.token)
        self.assertEqual(second.due, 103.0 + applier.QUIET_SECONDS)
        self.assertEqual(second.before, before, "what goes back is the profile before the first")
        self.assertEqual(applier.load(self.profile), second)

    def test_a_change_forgets_what_was_undone(self):
        applier.save(self.profile, applier.Waiting(undone=["forward 3000"], error="port taken"))

        waiting = applier.record(self.profile, "forward 3001", ["gateway"], applier.snapshot(self.profile))

        self.assertEqual((waiting.undone, waiting.error), ([], ""))

    def test_nothing_waiting_is_no_file(self):
        applier.record(self.profile, "forward 3000", ["gateway"], applier.snapshot(self.profile))
        self.assertTrue(self.profile.changes_file.exists())

        applier.save(self.profile, applier.Waiting())
        self.assertFalse(self.profile.changes_file.exists())
        self.assertEqual(applier.load(self.profile), applier.Waiting())

    def test_a_file_that_isnt_ours_is_nothing_waiting(self):
        for text in ["not json", "[1, 2]", '{"changes": "x"}', '{"unknown": 1}', '{"changes": [1]}']:
            self.profile.changes_file.write_text(text, encoding="utf-8")
            self.assertEqual(applier.load(self.profile), applier.Waiting(), text)

    def test_the_report_leaves_the_profiles_files_out(self):
        waiting = applier.record(self.profile, "forward 3000", ["gateway"], applier.snapshot(self.profile))

        self.assertNotIn("before", waiting.report())
        self.assertEqual(waiting.report()["changes"], ["forward 3000"])

    def test_restore_puts_back_the_settings_and_files_it_names_and_nothing_else(self):
        envfile.set_value(self.profile.env_file, "VSCODE_PORT", "11111")
        before = applier.snapshot(self.profile)

        envfile.set_value(self.profile.env_file, "VSCODE_PORT", "12000")
        envfile.set_value(self.profile.env_file, "ENABLE_DOCKER", "false")
        envfile.set_value(self.profile.env_file, "INSTALL_GO", "true")
        self.profile.override_file.write_text("services: {}\n# changed\n", encoding="utf-8")
        self.profile.devices_file.write_text("services: {}\n", encoding="utf-8")

        applier.restore(self.profile, before)

        env = envfile.read(self.profile.env_file)
        self.assertEqual(env.get("VSCODE_PORT"), "11111")
        self.assertNotIn("ENABLE_DOCKER", env, "unset, as it was")
        self.assertEqual(env.get("INSTALL_GO"), "true", "a language switched meanwhile stays")
        self.assertEqual(
            self.profile.override_file.read_text(encoding="utf-8"), before["files"]["compose.override.yaml"]
        )
        self.assertFalse(self.profile.devices_file.exists(), "a file that wasn't there goes")
        self.assertIn(devices.ENV_KEY, before["env"])


class MakeTests(HomeTestCase):
    """
    Making what waits: the recreation, its failure, and what is kept.
    """

    def setUp(self):
        super().setUp()
        profiles.create(self.root, "default", languages=[])
        self.profile = profiles.get(self.root, "default")
        self.recreated = []
        self.running = True

    def recreate(self, waiting):
        self.recreated.append((list(waiting.services), waiting.token))

    def wait_for(self, change="forward 3000", services=("gateway",), clock=lambda: 100.0):
        applier.record(self.profile, change, list(services), applier.snapshot(self.profile), clock=clock)

    def test_not_before_it_is_due(self):
        self.wait_for()

        self.assertIsNone(applier.make(self.profile, self.recreate, lambda: True, clock=lambda: 101.0))
        self.assertEqual(self.recreated, [])

        made = applier.make(self.profile, self.recreate, lambda: True, clock=lambda: 106.0)
        self.assertEqual(made, applier.Waiting())
        self.assertEqual(self.recreated, [(["gateway"], False)])
        self.assertEqual(applier.load(self.profile), applier.Waiting(), "nothing waits any more")

    def test_now_doesnt_wait(self):
        self.wait_for()

        applier.make(self.profile, self.recreate, lambda: True, clock=lambda: 101.0, now=True)

        self.assertEqual(self.recreated, [(["gateway"], False)])

    def test_a_stopped_profile_leaves_them_to_its_next_start(self):
        self.wait_for()

        made = applier.make(self.profile, self.recreate, lambda: False, clock=lambda: 200.0)

        self.assertEqual((made, self.recreated), (applier.Waiting(), []))
        self.assertEqual(applier.load(self.profile), applier.Waiting())
        self.assertIn("at its next start", applier.log(self.profile))

    def test_what_cant_be_made_is_undone_and_kept(self):
        envfile.set_value(self.profile.env_file, "VSCODE_PORT", "11111")
        self.wait_for()
        envfile.set_value(self.profile.env_file, "VSCODE_PORT", "12000")
        ports = []

        def recreate(waiting):
            ports.append(envfile.read(self.profile.env_file)["VSCODE_PORT"])
            if len(ports) == 1:
                raise RuntimeError("port 12000 is already allocated")

        made = applier.make(self.profile, recreate, lambda: True, clock=lambda: 200.0)

        self.assertEqual((made.undone, made.error), (["forward 3000"], "port 12000 is already allocated"))
        self.assertEqual(ports, ["12000", "11111"], "tried with the change, then recreated as it was")
        self.assertEqual(applier.load(self.profile), made, "kept for ps and the app")
        self.assertIn("couldn't make the changes, so they were undone", applier.log(self.profile))

    def test_the_log_keeps_its_newer_half(self):
        with mock.patch.object(applier, "LOG_BYTES", 400):
            for number in range(40):
                applier.note(self.profile, f"line {number}")

        text = self.profile.applier_log.read_text(encoding="utf-8")
        self.assertLessEqual(len(text), 400)
        self.assertIn("line 39", text)
        self.assertNotIn("line 1\n", text)
        self.assertTrue(text.endswith("\n") and text.split("\n")[0].count(" line ") == 1, "whole lines only")


class RunTests(HomeTestCase):
    """
    The applier's loop: when it recreates, how often, and when it ends.
    """

    def setUp(self):
        super().setUp()
        profiles.create(self.root, "default", languages=[])
        self.profile = profiles.get(self.root, "default")
        self.made = []

    def wait_for(self, change, services=("gateway",)):
        return lambda: applier.record(
            self.profile,
            change,
            list(services),
            applier.snapshot(self.profile),
            clock=self.clock.time,
        )

    def recreate(self, waiting):
        self.made.append((self.clock.now, list(waiting.changes)))

    def run_applier(self, recreate=None, running=lambda: True):
        applier.run(
            self.profile,
            recreate or self.recreate,
            running,
            clock=self.clock.time,
            sleep=self.clock.sleep,
        )

    def test_a_burst_of_changes_is_one_recreation_after_the_last(self):
        self.clock = FakeClock({1.0: self.wait_for("b"), 2.0: self.wait_for("c"), 3.5: self.wait_for("d")})
        self.wait_for("a")()

        self.run_applier()

        self.assertEqual(self.made, [(3.5 + applier.QUIET_SECONDS, ["a", "b", "c", "d"])])

    def test_it_ends_when_nothing_waits(self):
        self.clock = FakeClock()

        self.run_applier()

        self.assertEqual((self.made, self.clock.now), ([], 0.0))

    def test_one_applier_at_a_time(self):
        self.clock = FakeClock()
        self.wait_for("a")()

        with profiles.try_lock(self.profile, profiles.APPLIER_LOCK_FILE):
            self.run_applier()

        self.assertEqual(self.made, [], "the one holding the lock does the work")
        self.assertTrue(applier.load(self.profile).changes)

    def test_a_failure_is_kept_and_the_lock_let_go(self):
        self.clock = FakeClock()
        self.wait_for("a")()

        def fail(waiting):
            raise RuntimeError("Compose said no")

        self.run_applier(recreate=fail)

        self.assertEqual(applier.load(self.profile).error, "Compose said no")
        self.assertFalse(applier.applying(self.profile), "and the lock is let go")

    def test_a_profile_removed_meanwhile_is_left_gone(self):
        self.clock = FakeClock()
        self.wait_for("a")()

        # Removed while its containers were being recreated (the applier
        # holds the profile's containers lock meanwhile, which a removal
        # takes too: here it is done without).
        def removed(waiting):
            profiles.remove(self.root, "default")
            raise RuntimeError("Compose can't find the profile's .env")

        self.run_applier(recreate=removed)

        self.assertEqual(profiles.list_profiles(self.root), [])


class StartTests(HomeTestCase):
    """
    Starting an applier, and the command it runs as.
    """

    def setUp(self):
        super().setUp()
        profiles.create(self.root, "default", languages=[])
        self.profile = profiles.get(self.root, "default")
        self.spawned = []

    def test_a_change_starts_one_unless_one_runs(self):
        applier.start(self.root, self.profile, spawn=self.spawned.append)
        self.assertEqual(self.spawned, [applier.command(self.root, self.profile)])

        with profiles.try_lock(self.profile, profiles.APPLIER_LOCK_FILE):
            self.assertTrue(applier.applying(self.profile))
            applier.start(self.root, self.profile, spawn=self.spawned.append)

        self.assertEqual(len(self.spawned), 1, "the running one picks the change up")
        self.assertFalse(applier.applying(self.profile))

    def test_the_command_is_this_command_for_the_profile(self):
        command = applier.command(self.root, self.profile)

        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[-5:], ["--config", str(self.root), "-p", "default", applier.COMMAND])

        with mock.patch.object(sys, "frozen", True, create=True):
            self.assertEqual(
                applier.command(self.root, self.profile),
                [sys.executable, "--config", str(self.root), "-p", "default", applier.COMMAND],
                "the app's executable is the command itself",
            )

    def test_the_command_runs(self):
        # The command line as an applier is started, with an argument
        # parser's answer in place of the work: it finds the package.
        command = applier.command(self.root, self.profile)
        result = subprocess.run(
            [*command[:-1], "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "HARDCODE_CONFIG": str(self.root)},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("hardcode", result.stdout)


class SpawnTests(unittest.TestCase):
    """
    How an applier is started: on its own, and, from the app's single-file
    command, as a program of its own. Nothing is started: Popen is faked.
    """

    def test_a_single_file_build_starts_a_copy_that_unpacks_itself(self):
        # The app's command runs from a folder it unpacks itself into and
        # deletes as it ends: an applier started with the environment it
        # inherits would run from that folder, and die with it.
        with mock.patch.object(subprocess, "Popen") as popen, mock.patch.object(sys, "frozen", True, create=True):
            applier._spawn(["hardcode", "_applier"])

        options = popen.call_args.kwargs
        self.assertEqual(options["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertEqual(options["env"]["PATH"], os.environ["PATH"], "and the rest of its environment as it is")
        self.assertEqual((options["stdin"], options["stdout"]), (subprocess.DEVNULL, subprocess.DEVNULL))

    def test_run_from_source_it_inherits_the_environment_as_it_is(self):
        with mock.patch.object(subprocess, "Popen") as popen:
            applier._spawn(["python3", "-c", "pass"])

        self.assertNotIn("env", popen.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
