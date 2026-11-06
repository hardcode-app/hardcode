"""
Tests for running a command without holding more of its output than a limit
(hardcode_cli.bounded), with real commands.
"""

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from hardcode_cli import bounded

LOUD = Path(__file__).resolve().parent / "loud_command.py"
LEAVES_A_CHILD = Path(__file__).resolve().parent / "leaves_a_child.py"


def loud(*arguments: object) -> list[str]:
    """
    The command line of loud_command.py with *arguments*.
    """
    return [sys.executable, str(LOUD), *(str(argument) for argument in arguments)]


class BoundedRunTests(unittest.TestCase):
    def test_what_fits_is_returned_as_it_was_printed(self):
        result = bounded.run(loud(1000, 10), capture_output=True, text=True, limit=4096, timeout=60)

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "x" * 1000, "x" * 10))

    def test_more_than_the_limit_stops_the_command(self):
        # It would print 1 MiB and then wait half a minute: stopped instead.
        started = time.monotonic()

        with self.assertRaisesRegex(bounded.OutputTooLarge, "more than 65536 bytes"):
            bounded.run(loud(1_048_576, 0, 30), capture_output=True, limit=65536, timeout=60)

        self.assertLess(time.monotonic() - started, 20, "stopped, not waited for")

    def test_stderr_has_the_limit_too(self):
        with self.assertRaises(bounded.OutputTooLarge):
            bounded.run(loud(0, 1_048_576, 30), capture_output=True, limit=65536, timeout=60)

    def test_a_command_past_its_time_is_stopped(self):
        started = time.monotonic()

        with self.assertRaises(subprocess.TimeoutExpired):
            bounded.run(loud(0, 0, 30), capture_output=True, timeout=1)

        self.assertLess(time.monotonic() - started, 20)

    def test_output_that_isnt_captured_isnt_held(self):
        result = bounded.run(loud(100_000), stdout=subprocess.DEVNULL, timeout=60)

        self.assertEqual(result.returncode, 0)

    def test_the_limit_is_the_commands_default(self):
        self.assertEqual(bounded.OUTPUT_LIMIT, 64 * 1024 * 1024)


def ended(pid: int) -> bool:
    """
    Whether the process *pid* has ended, asked without touching it: gone, or
    a zombie nobody has collected yet (as a container's first process may
    leave one). On Windows, asked of the process's handle, since os.kill
    there ends the process it is given rather than asking after it.
    """
    if os.name == "nt":
        import ctypes

        synchronize = 0x00100000
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)

        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return True

        finished = kernel32.WaitForSingleObject(handle, 0) == 0
        kernel32.CloseHandle(handle)
        return finished

    stat = Path(f"/proc/{pid}/stat")
    if Path("/proc/self").exists():
        return not stat.exists() or stat.read_text(encoding="utf-8").split(") ")[-1].startswith("Z")

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True

    return False


def gone(pid: int) -> bool:
    """
    Whether the process *pid* has ended, waiting a few seconds for it.
    """
    for _ in range(50):
        if ended(pid):
            return True
        time.sleep(0.1)

    return False


def open_descriptors():
    """
    How many files this process has open, where the system says (Linux);
    None elsewhere.
    """
    if not Path("/proc/self/fd").exists():
        return None

    return len(os.listdir("/proc/self/fd"))


def kill(pid: int) -> None:
    """
    End the process *pid*, which a test left running on purpose.
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, check=False)
        return

    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


class UnheldJob:
    """
    A Windows job the command couldn't be put in, as when one can't be made.
    """

    held = False

    def __init__(self, pid: int):
        pass

    def stop(self) -> bool:
        return False

    def close(self) -> None:
        pass


@mock.patch.object(bounded, "GRACE_SECONDS", 0.5)
class LeftBehindTests(unittest.TestCase):
    """
    A command that leaves a process running that holds its output: run
    still returns or raises within a bounded time, and stops that process
    too, however the command ended: on POSIX through the command's session,
    on Windows through its job.
    """

    def run_leaving_a_child(self, mode: str, **options):
        """
        Run leaves_a_child.py in *mode*; what run gave or raised, how long
        it took, and the child's pid when it printed one.
        """
        started = time.monotonic()
        try:
            outcome = bounded.run(
                [sys.executable, str(LEAVES_A_CHILD), mode], capture_output=True, text=True, **options
            )
        except (subprocess.TimeoutExpired, bounded.OutputTooLarge) as error:
            outcome = error

        return outcome, time.monotonic() - started

    def test_a_command_that_ends_and_leaves_a_child(self):
        result, took = self.run_leaving_a_child("exit", timeout=60)

        self.assertEqual(result.returncode, 0)
        self.assertLess(took, 10, "not held up by the child")
        self.assertTrue(gone(int(result.stdout.split()[0])), "and the child was stopped, though the command had ended")

    def test_a_command_past_its_time_with_a_child(self):
        error, took = self.run_leaving_a_child("wait", timeout=1)

        self.assertIsInstance(error, subprocess.TimeoutExpired)
        self.assertLess(took, 10)
        self.assertTrue(error.output.strip(), "what it printed comes with it")
        self.assertTrue(gone(int(error.output.split()[0])), "the command and its child were both stopped")

    def test_a_child_out_of_reach_leaves_no_reader_or_pipe_behind(self):
        # On POSIX the child leaves the command's session, which is how run
        # stops what a command started; on Windows the command isn't put in
        # a job, so once it has ended its child can't be found. Either way
        # the child can't be stopped, and run must still let go of it.
        if os.name == "nt":
            out_of_reach = mock.patch.object(bounded, "_WindowsJob", UnheldJob)
            mode = "exit"
        else:
            out_of_reach = contextlib.nullcontext()
            mode = "escape"

        before = open_descriptors()
        with out_of_reach:
            result, took = self.run_leaving_a_child(mode, timeout=60)

        child = int(result.stdout.split()[0])
        self.addCleanup(kill, child)

        self.assertLess(took, 10)
        self.assertFalse(ended(child), "the child was out of reach, and is still running")
        readers = [thread for thread in threading.enumerate() if thread.name == bounded.READER_NAME]
        self.assertEqual(readers, [], "no reader is left reading its pipe")
        if before is not None:
            self.assertEqual(open_descriptors(), before, "and no pipe is left open")

    def test_too_much_printed_after_the_command_ended_stops_what_it_left(self):
        # The command ends; what it left lets go of stderr, so both pipes
        # can end without a reader left waiting, then prints too much. run
        # raises, and stops it, as when the command prints too much itself.
        with tempfile.TemporaryDirectory(prefix="hc-bounded-") as scratch:
            pid_file = Path(scratch) / "child"
            started = time.monotonic()

            with self.assertRaises(bounded.OutputTooLarge):
                bounded.run(
                    [sys.executable, str(LEAVES_A_CHILD), "late", str(pid_file)],
                    capture_output=True,
                    limit=65536,
                    timeout=60,
                )

            child = int(pid_file.read_text(encoding="utf-8"))

        self.addCleanup(kill, child)
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(gone(child), "what it left was stopped")

    def test_a_child_that_prints_too_much(self):
        error, took = self.run_leaving_a_child("loud", timeout=60, limit=65536)

        self.assertIsInstance(error, bounded.OutputTooLarge)
        self.assertLess(took, 10)


class FakeKernel32:
    """
    Windows' kernel32 as a job uses it, recording each call: a job is handle
    7, the command's process handle 9. *made* and *assigned* say whether
    making the job and putting the command in it work.
    """

    def __init__(self, made: bool = True, assigned: bool = True):
        self.made = made
        self.assigned = assigned
        self.calls: list[tuple] = []

    def CreateJobObjectW(self, attributes, name):  # noqa: N802 - kernel32's names
        self.calls.append(("CreateJobObjectW",))
        return 7 if self.made else 0

    def OpenProcess(self, access, inherit, pid):  # noqa: N802
        self.calls.append(("OpenProcess", pid))
        return 9

    def AssignProcessToJobObject(self, job, process):  # noqa: N802
        self.calls.append(("AssignProcessToJobObject", job, process))
        return 1 if self.assigned else 0

    def TerminateJobObject(self, job, code):  # noqa: N802
        self.calls.append(("TerminateJobObject", job))
        return 1

    def CloseHandle(self, handle):  # noqa: N802
        self.calls.append(("CloseHandle", handle))
        return 1


class FakeProcess:
    """
    A Popen that has already ended, for _stop.
    """

    pid = 4242

    def poll(self):
        return 0

    def kill(self):
        raise AssertionError("an ended command isn't killed")

    def wait(self):
        return 0


class WindowsJobTests(unittest.TestCase):
    """
    The Windows job a command is held in, with kernel32 faked: the calls it
    makes, and what happens when they don't work. (Windows itself runs the
    LeftBehindTests above.)
    """

    def test_a_command_held_is_stopped_with_all_it_started(self):
        kernel32 = FakeKernel32()
        job = bounded._WindowsJob(4242, kernel32=kernel32)

        self.assertTrue(job.held)
        self.assertTrue(bounded._stop(FakeProcess(), job), "reached, though the command had ended")
        job.close()

        self.assertEqual(
            kernel32.calls,
            [
                ("CreateJobObjectW",),
                ("OpenProcess", 4242),
                ("AssignProcessToJobObject", 7, 9),
                ("CloseHandle", 9),
                ("TerminateJobObject", 7),
                ("CloseHandle", 7),
            ],
        )

    def test_a_command_that_couldnt_be_put_in_the_job(self):
        kernel32 = FakeKernel32(assigned=False)
        job = bounded._WindowsJob(4242, kernel32=kernel32)

        self.assertFalse(job.held)
        self.assertFalse(job.stop(), "nothing to stop it with")
        self.assertNotIn(("TerminateJobObject", 7), kernel32.calls)
        self.assertIn(("CloseHandle", 9), kernel32.calls, "the process handle let go of all the same")

    def test_a_job_that_couldnt_be_made(self):
        kernel32 = FakeKernel32(made=False)
        job = bounded._WindowsJob(4242, kernel32=kernel32)
        job.close()

        self.assertFalse(job.held)
        self.assertEqual(kernel32.calls, [("CreateJobObjectW",)], "nothing else asked of it")


if __name__ == "__main__":
    unittest.main()
