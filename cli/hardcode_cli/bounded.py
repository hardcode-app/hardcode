"""
Running a command without holding more of its output than a limit.

Some of what the command line reads comes from inside the sandbox: a file the
VS Code container writes, the containers' logs. A limit asked of a command
in there (``head -c``) is kept by the sandbox's own programs, which its user
can replace, so the limit is kept here too, on this side: ``run`` reads a
command's output a piece at a time and stops the command once it has printed
more than the limit, before any more of it is held in memory.
"""

from __future__ import annotations

import subprocess
import threading
import time
from typing import Optional, Sequence

# What a command may print, on each of stdout and stderr, unless a call says
# otherwise. Rotated logs are at most 3 x 10 MB per service.
OUTPUT_LIMIT = 64 * 1024 * 1024

# How much is read from a pipe at a time.
CHUNK = 65536


class OutputTooLarge(RuntimeError):
    """
    A command printed more than its limit, and was stopped.
    """

    def __init__(self, argv: Sequence[str], limit: int):
        super().__init__(f"{argv[0]} printed more than {limit} bytes, and was stopped")
        self.limit = limit


def _read_up_to(pipe, limit: int, chunks: list[bytes], over: threading.Event) -> None:
    """
    Read *pipe* into *chunks* until it ends, or until it has given more than
    *limit* bytes; then *over* is set, and the rest is left unread.
    """
    total = 0

    while True:
        chunk = pipe.read1(CHUNK) if hasattr(pipe, "read1") else pipe.read(CHUNK)
        if not chunk:
            return

        total += len(chunk)
        if total > limit:
            over.set()
            return

        chunks.append(chunk)


def run(
    argv: Sequence[str],
    *,
    capture_output: bool = False,
    text: bool = False,
    timeout: Optional[float] = None,
    limit: int = OUTPUT_LIMIT,
    **options,
) -> subprocess.CompletedProcess:
    """
    ``subprocess.run`` for a command whose output can't be trusted to be small.

    With *capture_output*, stdout and stderr are each read up to *limit*
    bytes; a command that prints more is stopped, and ``OutputTooLarge``
    raised. Past *timeout* seconds the command is stopped and
    ``subprocess.TimeoutExpired`` raised, as ``subprocess.run`` does. Output
    that isn't captured goes where *options* send it, unread.
    """
    if not capture_output:
        return subprocess.run(argv, text=text, timeout=timeout, **options)

    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)

    over = threading.Event()
    stdout: list[bytes] = []
    stderr: list[bytes] = []
    readers = [
        threading.Thread(target=_read_up_to, args=(process.stdout, limit, stdout, over), daemon=True),
        threading.Thread(target=_read_up_to, args=(process.stderr, limit, stderr, over), daemon=True),
    ]
    for reader in readers:
        reader.start()

    # Wait for the command to end, stopping it when it has printed too much
    # or taken too long.
    deadline = None if timeout is None else time.monotonic() + timeout
    while process.poll() is None:
        if over.is_set():
            process.kill()
            process.wait()
            break

        if deadline is not None and time.monotonic() > deadline:
            process.kill()
            process.wait()
            raise subprocess.TimeoutExpired(argv, timeout)

        time.sleep(0.02)

    for reader in readers:
        reader.join(timeout=5)
    process.stdout.close()
    process.stderr.close()

    if over.is_set():
        raise OutputTooLarge(argv, limit)

    out = b"".join(stdout)
    err = b"".join(stderr)
    if text:
        return subprocess.CompletedProcess(
            argv,
            process.returncode,
            out.decode("utf-8", errors="replace"),
            err.decode("utf-8", errors="replace"),
        )

    return subprocess.CompletedProcess(argv, process.returncode, out, err)
