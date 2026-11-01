"""
Tests for running a command without holding more of its output than a limit
(hardcode_cli.bounded), with real commands.
"""

import subprocess
import sys
import time
import unittest
from pathlib import Path

from hardcode_cli import bounded

LOUD = Path(__file__).resolve().parent / "loud_command.py"


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


if __name__ == "__main__":
    unittest.main()
