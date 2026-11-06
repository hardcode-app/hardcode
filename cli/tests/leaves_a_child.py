"""
A command that starts a process of its own, which inherits its output and
outlives it, as a parent that exits and leaves what it started running does
(docker, and the compose plugin it runs): for test_bounded.py.

    python3 leaves_a_child.py exit     print the child's pid, and end at once
    python3 leaves_a_child.py wait     print the child's pid, and wait for it
    python3 leaves_a_child.py loud     end at once; the child prints 1 MiB
    python3 leaves_a_child.py escape   as exit; the child leaves the command's
                                       session (POSIX), out of reach
    python3 leaves_a_child.py late PIDFILE
                                       write the child's pid to PIDFILE, and end
                                       at once; the child lets go of stderr, and
                                       a moment later prints 1 MiB

The child is this same file, run as ``leaves_a_child.py child MODE``: it
waits a minute, holding the command's stdout (and stderr, but in ``late``),
after printing 1 MiB when MODE is ``loud``, or after a moment in ``late``.
"""

import os
import subprocess
import sys
import time


def child(mode: str) -> int:
    """
    The child: print what *mode* asks, then outlive the command.
    """
    if mode == "late":
        # Its stderr goes nowhere, so that the command's ends with the
        # command; then, once the command has ended, too much on stdout.
        os.dup2(os.open(os.devnull, os.O_WRONLY), 2)
        time.sleep(0.2)

    if mode in ("loud", "late"):
        try:
            sys.stdout.buffer.write(b"x" * 1048576)
            sys.stdout.flush()
        except BrokenPipeError:
            pass

    if mode == "escape":
        os.setsid()

    time.sleep(60)
    return 0


def main(argv: list[str]) -> int:
    """
    Start the child, say who it is, and end or wait as *argv* says; or be
    the child.
    """
    if argv[0] == "child":
        return child(argv[1])

    mode = argv[0]
    started = subprocess.Popen([sys.executable, __file__, "child", mode])

    if mode == "late":
        with open(argv[1], "w", encoding="utf-8") as pid_file:
            pid_file.write(str(started.pid))
        return 0

    print(started.pid, flush=True)
    if mode == "wait":
        started.wait()

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
