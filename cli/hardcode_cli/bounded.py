"""
Running a command without holding more of its output than a limit.

Some of what the command line reads comes from inside the sandbox: a file the
VS Code container writes, the containers' logs. A limit asked of a command
in there (``head -c``) is kept by the sandbox's own programs, which its user
can replace, so the limit is kept here too, on this side: ``run`` reads a
command's output a piece at a time and stops the command once it has printed
more than the limit, before any more of it is held in memory.

A command it reads from runs in a session of its own (on Windows, a
process group, held in a job object), so that what it started can be
stopped with it, however it ends: a process it leaves behind holding its
output would otherwise keep ``run`` waiting. Where that can't be done (a
process that left the session; on Windows, one started before the command
was put in its job, in the moment after it starts, or a job that couldn't
be made), ``run`` still returns within its grace periods: its readers are
told to stop, and its pipes closed after them, leaving that process to end
on its own.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import threading
import time
from typing import Optional, Sequence

# What a command may print, on each of stdout and stderr, unless a call says
# otherwise. Rotated logs are at most 3 x 10 MB per service.
OUTPUT_LIMIT = 64 * 1024 * 1024

# How much is read from a pipe at a time.
CHUNK = 65536

# How long the output is read for once the command has ended or been
# stopped: what it started may still be printing. Past that, what still holds
# its output is stopped too, and given as long again to let go; then the
# readers are told to stop.
GRACE_SECONDS = 5.0

# How long a reader waits for output at a time, before it looks whether it
# has been told to stop.
POLL_SECONDS = 0.05

# What the readers' threads are called.
READER_NAME = "bounded-reader"


class OutputTooLarge(RuntimeError):
    """
    A command printed more than its limit, and was stopped.
    """

    def __init__(self, argv: Sequence[str], limit: int):
        super().__init__(f"{argv[0]} printed more than {limit} bytes, and was stopped")
        self.limit = limit


class _PipeWait:
    """
    A wait for a pipe to have something to read, or to have ended, on POSIX:
    the system says (selectors).
    """

    def __init__(self, fd: int):
        self._selector = selectors.DefaultSelector()
        self._selector.register(fd, selectors.EVENT_READ)

    def ready(self, seconds: float) -> bool:
        """
        Whether a read won't wait, asked for up to *seconds*.
        """
        return bool(self._selector.select(seconds))

    def close(self) -> None:
        self._selector.close()


class _WindowsPipeWait:
    """
    The same on Windows, whose pipes the system can't be asked to wait on:
    the pipe is looked into (kernel32's PeekNamedPipe) instead, and when
    nothing is there, waited for a moment. *kernel32* stands in for
    Windows' own, in tests.
    """

    def __init__(self, fd: int, kernel32=None):
        import ctypes
        import msvcrt

        if kernel32 is None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.PeekNamedPipe.restype = ctypes.c_int
            kernel32.PeekNamedPipe.argtypes = (
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_void_p,
            )

        self._ctypes = ctypes
        self._kernel32 = kernel32
        self._handle = msvcrt.get_osfhandle(fd)

    def ready(self, seconds: float) -> bool:
        """
        Whether a read won't wait: something is there, or the pipe has
        ended (peeking fails), which the read that follows says.
        """
        available = self._ctypes.c_uint32(0)
        if not self._kernel32.PeekNamedPipe(self._handle, None, 0, None, self._ctypes.byref(available), None):
            return True
        if available.value:
            return True

        time.sleep(seconds)
        return False

    def close(self) -> None:
        pass


def _read_up_to(pipe, limit: int, chunks: list[bytes], over: threading.Event, stop: threading.Event) -> None:
    """
    Read *pipe* into *chunks* until it ends, until it has given more than
    *limit* bytes (then *over* is set, and the rest is left unread), or
    until *stop* is set.

    It reads only when a read won't wait, so it never blocks for longer
    than POLL_SECONDS, and a reader told to stop does so: a process that
    holds the pipe open can't keep it, or its pipe, alive. It reads the
    pipe's file descriptor itself, past the pipe's buffer, which is never
    used.
    """
    fd = pipe.fileno()
    wait = _WindowsPipeWait(fd) if os.name == "nt" else _PipeWait(fd)
    total = 0

    try:
        while not stop.is_set():
            if not wait.ready(POLL_SECONDS):
                continue

            chunk = os.read(fd, CHUNK)
            if not chunk:
                return

            total += len(chunk)
            if total > limit:
                over.set()
                return

            chunks.append(chunk)
    finally:
        wait.close()


def _own_group() -> dict:
    """
    ``Popen``'s options that start a command in a session (on Windows, a
    process group) of its own: what it starts is in it too.
    """
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}

    return {"start_new_session": True}


def _kernel32():
    """
    Windows' kernel32, through ctypes, with the signatures of the calls a
    job needs.
    """
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel32.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    kernel32.TerminateJobObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)

    return kernel32


class _WindowsJob:
    """
    A Windows job object holding a command: the processes it starts from
    then on join it too, and stopping the job stops all of them, even once
    the command itself has ended and they can't be found under it.

    The command is put in the job just after it has started: anything it
    starts before that (in that moment) isn't in the job, and is left to end
    on its own. ``held`` is false when the job couldn't be made, or the
    command not put in it; then the command is stopped as before (its tree,
    while it runs). *kernel32* stands in for Windows' own, in tests.
    """

    # The rights to put a process in a job, and to end it there.
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001

    def __init__(self, pid: int, kernel32=None):
        kernel32 = kernel32 or _kernel32()
        self._kernel32 = kernel32

        self._job = kernel32.CreateJobObjectW(None, None)
        self.held = False
        if not self._job:
            return

        process = kernel32.OpenProcess(self.PROCESS_SET_QUOTA | self.PROCESS_TERMINATE, False, pid)
        if process:
            self.held = bool(kernel32.AssignProcessToJobObject(self._job, process))
            kernel32.CloseHandle(process)

    def stop(self) -> bool:
        """
        Stop every process in the job; whether that could be done.
        """
        return self.held and bool(self._kernel32.TerminateJobObject(self._job, 1))

    def close(self) -> None:
        """
        Let go of the job (what is still in it carries on, outside it).
        """
        if self._job:
            self._kernel32.CloseHandle(self._job)
            self._job = None


def _stop(process: subprocess.Popen, job: Optional[_WindowsJob] = None) -> bool:
    """
    Stop the command and what it started, and wait for the command; whether
    what it started could be reached.

    On POSIX, its session's process group, which lives on while any of what
    it started does. On Windows, its job (*job*), or, without one, the tree
    of processes under it, which can only be found while the command still
    runs.
    """
    reached = False

    if job is not None and job.held:
        reached = job.stop()
        if process.poll() is None:
            process.kill()
    elif os.name == "nt":
        if process.poll() is None:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            reached = True
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            reached = True
        except (ProcessLookupError, PermissionError):
            # The group has gone: the command and all it started had ended.
            pass

    process.wait()
    return reached


def _finish(
    process: subprocess.Popen,
    readers: list[threading.Thread],
    stop: threading.Event,
    over: threading.Event,
    job: Optional[_WindowsJob] = None,
) -> None:
    """
    Read what is left of the output, for a while, then let go of the pipes.

    A reader still waiting after ``GRACE_SECONDS`` waits on a process the
    command left behind, which holds the pipe open: that is stopped too,
    where it can be reached, and the reader given as long again. Then the
    readers are told to stop (*stop*), which each does within POLL_SECONDS,
    whatever still holds its pipe, and the pipes are closed after them: no
    reader or pipe outlives the call. The job, on Windows, is let go of last.

    Too much printed (*over*) stops what the command started too, though no
    reader is left waiting: what it left behind may have printed it after
    the command ended, and let go of both pipes.
    """
    for reader in readers:
        reader.join(timeout=GRACE_SECONDS)

    waiting = any(reader.is_alive() for reader in readers)
    if (waiting or over.is_set()) and _stop(process, job) and waiting:
        for reader in readers:
            reader.join(timeout=GRACE_SECONDS)

    stop.set()
    for reader in readers:
        reader.join(timeout=GRACE_SECONDS)

    # A pipe is closed only once its reader has let go of it: closing one a
    # reader still reads would wait for that read. With the stop above,
    # that is every one.
    for reader, pipe in zip(readers, (process.stdout, process.stderr)):
        if not reader.is_alive():
            pipe.close()

    if job is not None:
        job.close()


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
    ``subprocess.TimeoutExpired`` raised, with what it printed until then,
    as ``subprocess.run`` does. Either way, and when it ends by itself,
    what it started and left holding its output is stopped too (``_finish``).
    Output that isn't captured goes where *options* send it, unread.
    """
    if not capture_output:
        return subprocess.run(argv, text=text, timeout=timeout, **options)

    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **_own_group(),
        **options,
    )

    # On Windows, put in a job as soon as it has started: what it starts
    # from then on joins the job.
    job = _WindowsJob(process.pid) if os.name == "nt" else None

    over = threading.Event()
    stop = threading.Event()
    stdout: list[bytes] = []
    stderr: list[bytes] = []
    readers = [
        threading.Thread(
            target=_read_up_to,
            args=(pipe, limit, chunks, over, stop),
            name=READER_NAME,
            daemon=True,
        )
        for pipe, chunks in ((process.stdout, stdout), (process.stderr, stderr))
    ]
    for reader in readers:
        reader.start()

    # Wait for the command to end, stopping it when it has printed too much
    # or taken too long. Whatever happens, even an interruption here, what it
    # started is cleaned up before this returns or raises.
    deadline = None if timeout is None else time.monotonic() + timeout
    timed_out = False
    try:
        while process.poll() is None:
            if over.is_set():
                _stop(process, job)
                break

            if deadline is not None and time.monotonic() > deadline:
                _stop(process, job)
                timed_out = True
                break

            time.sleep(0.02)
    except BaseException:
        _stop(process, job)
        _finish(process, readers, stop, over, job)
        raise

    _finish(process, readers, stop, over, job)

    if over.is_set():
        raise OutputTooLarge(argv, limit)

    out = b"".join(stdout)
    err = b"".join(stderr)
    if text:
        out = out.decode("utf-8", errors="replace")
        err = err.decode("utf-8", errors="replace")

    # What it printed before it was stopped, as subprocess.run gives it.
    if timed_out:
        raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err)

    return subprocess.CompletedProcess(argv, process.returncode, out, err)
