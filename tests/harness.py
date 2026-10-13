"""
Shared plumbing for the test suites in this directory.

Every suite is a script (``tests/proxy.py``, ``tests/cli.py`` and so on) that
checks one part of HardCode for real and prints a line per check:

    ok   a last line without a newline is rendered
    FAIL dot segments can't escape the prefix
         wanted '403', got '200 GET /secret'

followed by a summary, exiting non-zero when anything failed.

Started on a workstation, a suite first re-runs itself inside the build image
(``images/build``), the Ubuntu 26.04 environment everything here is built and
tested in. That hop happens before anything else, on the workstation's own
Python, so this module imports nothing but the standard library.
"""

from __future__ import annotations

import contextlib
import http.client
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, NoReturn, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"

# The image the suites run in. Their helper containers (an origin server, a
# client, a clean-up job) use it too: it is Ubuntu 26.04 with Python, and it
# is already on the machine. images/build/run.sh passes the tag it started.
BUILD_IMAGE = os.environ.get("BUILD_IMAGE", "hardcode-build:local")


# ---------------------------------------------------------------------------
# Getting into the build image
# ---------------------------------------------------------------------------


def enter_build_image() -> None:
    """
    Re-run the calling script inside the build image, unless it already is.

    ``images/build/run.sh`` builds the image when it is missing, mounts the
    checkout at the same path, and caps the container's memory. The script's
    arguments are passed through. When the hop happens, this never returns.
    """
    if os.environ.get("HARDCODE_BUILD_IMAGE"):
        return

    script = Path(sys.argv[0]).resolve()
    run_sh = ROOT / "images" / "build" / "run.sh"
    os.execv(run_sh, [str(run_sh), str(script), *sys.argv[1:]])


# ---------------------------------------------------------------------------
# Running commands
# ---------------------------------------------------------------------------


@dataclass
class Output:
    """
    What a finished command printed, and how it exited.

    ``text`` holds stdout and stderr interleaved, the way a person at a
    terminal would have seen them, so a failed check can show everything.
    """

    code: int
    text: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    def tail(self, lines: int) -> str:
        """
        The last few lines of the output.
        """
        return "\n".join(self.text.rstrip().splitlines()[-lines:])


def run(
    command: Sequence[object],
    *,
    input: Optional[str] = None,
    cwd: Optional[Path] = None,
    env: Optional[dict] = None,
    timeout: float = 600,
) -> Output:
    """
    Run a command to completion and capture everything it prints.

    Arguments may be paths or numbers; they are turned into strings. A command
    that runs past *timeout* seconds is killed and reported with exit code 124,
    as coreutils' ``timeout`` does.
    """
    arguments = [str(part) for part in command]

    try:
        completed = subprocess.run(
            arguments,
            input=input,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        partial = error.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode(errors="replace")
        return Output(124, f"{partial}\n(killed after {timeout:.0f}s)")

    return Output(completed.returncode, completed.stdout)


def wait_until(condition: Callable[[], bool], timeout: float, interval: float = 1.0) -> bool:
    """
    Poll *condition* until it holds or *timeout* seconds have passed.

    Returns whether it held, so the caller decides what running out of time
    means.
    """
    deadline = time.monotonic() + timeout

    while True:
        if condition():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def current_user() -> str:
    """
    The ``uid:gid`` to run a container as, so the files it writes are ours.
    """
    return f"{os.getuid()}:{os.getgid()}"


def release_steps() -> types.ModuleType:
    """
    release/release.py, as a module. It can't be imported by its name:
    tests/release.py has the same one.
    """
    spec = importlib.util.spec_from_file_location("release_steps", ROOT / "release" / "release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


# ---------------------------------------------------------------------------
# Reporting checks
# ---------------------------------------------------------------------------


class SuiteAborted(Exception):
    """
    A step every later check depends on failed, so the suite stops.
    """

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


class Suite:
    """
    A run of named checks, reported as they happen and summarised at the end.
    """

    def __init__(self, name: str):
        self.name = name
        self.passed = 0
        self.failed = 0

    def section(self, title: str) -> None:
        """
        Print a heading for the checks that follow.
        """
        print(f"== {title}", flush=True)

    def note(self, text: str) -> None:
        """
        Print something worth seeing that isn't a check.
        """
        print(textwrap.indent(text.rstrip(), "   "), flush=True)

    def check(self, name: str, passed: bool, detail: str = "") -> bool:
        """
        Record one check. A failure prints *detail* indented beneath it.

        Returns *passed*, so a caller can skip checks that would only fail
        for the same reason.
        """
        if passed:
            self.passed += 1
            print(f"ok   {name}", flush=True)
        else:
            self.failed += 1
            print(f"FAIL {name}", flush=True)
            if detail.strip():
                print(textwrap.indent(detail.rstrip(), "     "), flush=True)

        return passed

    def expect(self, name: str, output: Output, pattern: Optional[str] = None) -> bool:
        """
        Check that a command succeeded and, when *pattern* is given, that its
        output matches it.

        The pattern is a regular expression searched for line by line, as
        ``grep -E`` would: ``^`` and ``$`` anchor to the start and end of a
        line.
        """
        matched = pattern is None or re.search(pattern, output.text, re.MULTILINE) is not None
        return self.check(name, output.ok and matched, f"exit {output.code}\n{output.text}")

    def expect_failure(self, name: str, output: Output, pattern: str) -> bool:
        """
        Check that a command failed, with output that matches *pattern*.
        """
        if output.ok:
            return self.check(f"{name} (it succeeded)", False, output.text)

        matched = re.search(pattern, output.text, re.MULTILINE) is not None
        return self.check(name, matched, output.text)

    def require(self, name: str, passed: bool, detail: str = "") -> None:
        """
        A check the rest of the suite depends on: when it fails, stop.
        """
        if not self.check(name, passed, detail):
            raise SuiteAborted(f"can't go on without: {name}")

    def abort(self, message: str, detail: str = "") -> NoReturn:
        """
        Stop the suite: something it needs didn't happen.
        """
        raise SuiteAborted(message, detail)

    def summary(self) -> int:
        """
        Print the totals and return the exit code for them.
        """
        print(f"\n{self.name}: {self.passed} passed, {self.failed} failed", flush=True)
        return 0 if self.failed == 0 else 1


def main(name: str, body: Callable[[Suite], None]) -> NoReturn:
    """
    Run a suite inside the build image and exit with its result.

    *body* receives the ``Suite`` to report its checks to. If it gives up
    with ``SuiteAborted``, the reason is printed and the suite fails; its
    ``with`` blocks have cleaned up by then.
    """
    enter_build_image()
    suite = Suite(name)

    try:
        body(suite)
    except SuiteAborted as error:
        suite.failed += 1
        print(f"\nABORTED: {error}", flush=True)
        if error.detail.strip():
            print(textwrap.indent(error.detail.rstrip(), "     "), flush=True)

    sys.exit(suite.summary())


def run_unittests(suite: Suite, name: str, start: Path, top: Path, cwd: Optional[Path] = None) -> bool:
    """
    Run a directory of unittest tests as one check, reporting how many ran.

    *start* and *top* are unittest's ``-s`` and ``-t``: where discovery
    begins, and the directory imports are relative to.
    """
    output = run(
        [sys.executable, "-m", "unittest", "discover", "-s", start, "-t", top],
        cwd=cwd,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )

    # unittest's last lines are "Ran N tests in Xs", a blank line, and "OK".
    ran = re.search(r"^Ran (\d+) tests?", output.text, re.MULTILINE)
    count = ran.group(1) if ran else "?"

    return suite.check(f"{name}: {count} tests", output.ok, output.tail(40))


# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------


class _UnixHTTPConnection(http.client.HTTPConnection):
    """
    HTTP over a Unix socket, which is where the Docker daemon listens.
    """

    def __init__(self, socket_path: str):
        super().__init__("docker")
        self._socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._socket_path)


class Docker:
    """
    The host's Docker daemon, as the suites drive it.

    Every container gets a hard memory cap: ``memory`` is a required argument.
    Containers started with ``detach=True`` and networks created here are
    removed again when the ``with`` block ends.
    """

    def __init__(self) -> None:
        self._containers: list[str] = []
        self._networks: list[str] = []

    def __enter__(self) -> "Docker":
        return self

    def __exit__(self, *exc_info) -> None:
        self.clean_up()

    def build(self, tag: str, context: Path, dockerfile: Optional[Path] = None) -> Output:
        """
        Build an image from a directory.
        """
        command = ["docker", "build", "--quiet", "--tag", tag]
        if dockerfile is not None:
            command += ["--file", dockerfile]

        return run([*command, context])

    def build_from_text(self, tag: str, dockerfile: str) -> Output:
        """
        Build an image from a Dockerfile given as text, with no build context.
        """
        return run(["docker", "build", "--quiet", "--tag", tag, "-"], input=dockerfile)

    def run(
        self,
        image: str,
        *command: object,
        memory: str,
        memory_swap: Optional[str] = None,
        name: Optional[str] = None,
        detach: bool = False,
        network: Optional[str] = None,
        aliases: Sequence[str] = (),
        hosts: Sequence[str] = (),
        user: Optional[str] = None,
        volumes: Sequence[str] = (),
        tmpfs: Sequence[str] = (),
        entrypoint: Optional[str] = None,
        workdir: Optional[str] = None,
        timeout: float = 600,
    ) -> Output:
        """
        Run a container: to completion and removed afterwards, or detached
        and named, to be removed when the suite cleans up.

        *volumes* are ``-v`` values (``host:container[:ro]``), *hosts* are
        ``--add-host`` values (``name:address``), *aliases* are the names the
        container answers to on *network*. Docker lets a container swap as
        much again as its *memory*; *memory_swap* equal to it allows none,
        as in a Kubernetes pod.
        """
        arguments = ["docker", "run", "--memory", memory]
        if memory_swap is not None:
            arguments += ["--memory-swap", memory_swap]

        if detach:
            arguments += ["--detach"]
        else:
            arguments += ["--rm"]

        if name is not None:
            arguments += ["--name", name]
            if detach:
                self._containers.append(name)

        if network is not None:
            arguments += ["--network", network]
        for alias in aliases:
            arguments += ["--network-alias", alias]
        for host in hosts:
            arguments += ["--add-host", host]

        if user is not None:
            arguments += ["--user", user]
        for volume in volumes:
            arguments += ["--volume", volume]
        for mount in tmpfs:
            arguments += ["--tmpfs", mount]
        if entrypoint is not None:
            arguments += ["--entrypoint", entrypoint]
        if workdir is not None:
            arguments += ["--workdir", workdir]

        return run([*arguments, image, *command], timeout=timeout)

    def remove(self, container: str) -> None:
        """
        Stop and remove a container now, rather than at clean-up.
        """
        run(["docker", "rm", "--force", container])
        if container in self._containers:
            self._containers.remove(container)

    def logs(self, container: str, lines: int = 20) -> str:
        """
        The last lines a container printed.
        """
        return run(["docker", "logs", "--tail", lines, container]).text

    def create_network(self, name: str, *options: str) -> Output:
        """
        Create a network for the suite, removed again at clean-up.
        """
        output = run(["docker", "network", "create", *options, name])
        if output.ok:
            self._networks.append(name)

        return output

    def inspect_network(self, name: str) -> dict:
        """
        A network as the daemon itself describes it.

        This asks the Docker API directly: the CLI's ``--format`` output for a
        field that isn't set differs between CLI versions (an empty string in
        some, "invalid IP" in others).
        """
        socket_path = os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")
        socket_path = socket_path.replace("unix://", "", 1)

        connection = _UnixHTTPConnection(socket_path)
        try:
            connection.request("GET", f"/networks/{name}")
            return json.loads(connection.getresponse().read())
        finally:
            connection.close()

    def clean_up(self) -> None:
        """
        Remove the containers and networks this suite created.
        """
        if self._containers:
            run(["docker", "rm", "--force", *self._containers])
        for network in self._networks:
            run(["docker", "network", "rm", network])

        self._containers.clear()
        self._networks.clear()


@contextlib.contextmanager
def scratch_directory(prefix: str) -> Iterator[Path]:
    """
    A temporary directory the suite and its containers can share.

    Containers may leave files in it that belong to another user (the
    proxy's CA belongs to squid's user, a volume root to root) and that the
    suite can't delete by itself, so a root container empties it first.
    """
    path = Path(tempfile.mkdtemp(prefix=prefix))

    try:
        yield path
    finally:
        # The build image runs as root when it isn't told otherwise.
        as_root = ["docker", "run", "--rm", "--memory", "64m", "--volume", f"{path}:/scratch", BUILD_IMAGE]
        run([*as_root, "find", "/scratch", "-mindepth", "1", "-delete"])
        shutil.rmtree(path, ignore_errors=True)
