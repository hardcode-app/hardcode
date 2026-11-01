"""
A command that prints more than it was asked for, as a sandbox's own
``head``, replaced, would: for test_bounded.py and test_compose.py.

    python3 loud_command.py STDOUT_BYTES [STDERR_BYTES] [SECONDS]

Prints STDOUT_BYTES to stdout and STDERR_BYTES to stderr, then waits SECONDS,
so that a test can tell a command that was stopped from one that ended.
"""

import sys
import time

CHUNK = 65536


def write(stream, count: int) -> None:
    """
    Write *count* bytes to *stream*, a chunk at a time.
    """
    while count > 0:
        size = min(CHUNK, count)
        stream.write(b"x" * size)
        count -= size
    stream.flush()


def main(argv: list[str]) -> int:
    """
    Print what the arguments say, then wait.
    """
    stdout_bytes = int(argv[0])
    stderr_bytes = int(argv[1]) if len(argv) > 1 else 0
    seconds = float(argv[2]) if len(argv) > 2 else 0

    write(sys.stdout.buffer, stdout_bytes)
    write(sys.stderr.buffer, stderr_bytes)
    time.sleep(seconds)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
