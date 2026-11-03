"""
``docker compose`` for one profile.

Every call goes through an injectable *runner* with the signature of
:func:`subprocess.run`, so the module can be tested without Docker; by
default ``bounded.run``, which holds no more of a command's output than a
limit (some of it comes from inside the sandbox).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import bounded, envfile, stack
from .profiles import Profile

Runner = Callable[..., subprocess.CompletedProcess]

# Where Docker Desktop on macOS puts the CLI, for when an app started from
# the Dock doesn't have it on its PATH.
MAC_DOCKER_DIRS = ("/usr/local/bin", "/opt/homebrew/bin", "~/.docker/bin")


@dataclass(frozen=True)
class Service:
    """
    One of a profile's containers, as ``docker compose ps`` reports it.
    """

    service: str
    state: str
    status: str
    health: str


class ComposeError(RuntimeError):
    """
    A ``docker compose`` invocation that failed.
    """


def docker_executable(which: Callable[[str], Optional[str]] = shutil.which) -> str:
    """
    The ``docker`` executable, found on the PATH or where Docker Desktop
    usually installs it.
    """
    found = which("docker")
    if found:
        return found

    for directory in MAC_DOCKER_DIRS:
        candidate = Path(directory).expanduser() / "docker"
        if candidate.exists():
            return str(candidate)

    raise ComposeError("docker not found on the PATH")


def parse_ps(output: str) -> list[Service]:
    """
    The output of ``docker compose ps --format json``.

    Older Compose versions print a JSON array, newer ones one object per line;
    both are accepted.
    """
    text = output.strip()
    if not text:
        return []

    try:
        parsed = json.loads(text)
        items = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        items = [json.loads(line) for line in text.splitlines() if line.strip()]

    return [
        Service(
            service=item.get("Service", ""),
            state=item.get("State", ""),
            status=item.get("Status", ""),
            health=item.get("Health") or "",
        )
        for item in items
    ]


def docker_subnets(docker: str, runner: Runner = bounded.run) -> list[str]:
    """
    The subnets of Docker's networks, which a new profile's must not overlap;
    none when Docker can't say (no daemon, say): the check is a courtesy.
    """
    try:
        listed = runner(
            [docker, "network", "ls", "-q"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30
        )
        ids = (listed.stdout or "").split() if listed.returncode == 0 else []
        if not ids:
            return []

        template = "{{range .IPAM.Config}}{{.Subnet}} {{end}}"
        inspected = runner(
            [docker, "network", "inspect", "-f", template, *ids],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    return (inspected.stdout or "").split() if inspected.returncode == 0 else []


class Compose:
    """
    ``docker compose`` with one profile's project name, env file and
    override, over the stack this HardCode runs with.
    """

    def __init__(
        self,
        profile: Profile,
        runner: Runner = bounded.run,
        docker: Optional[str] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.profile = profile
        self._run = runner
        self._docker = docker
        self.sleep = sleep

    @property
    def docker(self) -> str:
        """
        The ``docker`` executable, looked up the first time it is needed.
        """
        if self._docker is None:
            self._docker = docker_executable()

        return self._docker

    def run_docker(self, *args: str, timeout: Optional[float] = 120) -> subprocess.CompletedProcess:
        """
        A plain ``docker <args>`` (not compose), with its output captured.
        """
        return self._run(
            [self.docker, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def has_docker(self) -> bool:
        """
        Whether a ``docker`` executable can be found at all.
        """
        try:
            return bool(self.docker)
        except ComposeError:
            return False

    def argv(self, *args: str) -> list[str]:
        """
        The full command line for ``docker compose <args>``: the stack's
        compose.yaml, with the profile's ``.env`` and override. Paths are
        absolute, with forward slashes, which Compose accepts on every
        platform; the stack is the project directory, where compose.yaml's
        build contexts are.
        """
        stack_dir = stack.current()

        command = [
            self.docker,
            "compose",
            "--project-name",
            self.profile.project_name,
            "--project-directory",
            stack_dir.as_posix(),
            "--env-file",
            self.profile.env_file.as_posix(),
            "-f",
            (stack_dir / "compose.yaml").as_posix(),
        ]
        if self.profile.override_file.exists():
            command += ["-f", self.profile.override_file.as_posix()]

        return command + list(args)

    def docker_enabled(self) -> bool:
        """
        Whether the profile runs the Docker daemon beside the VS Code container.

        Read as the VS Code container's entrypoint reads it: on when unset or exactly
        ``true``, off for anything else. Reading ``False`` or ``no`` as on here
        would start the daemon for a VS Code container that doesn't use it.
        """
        return (envfile.get(self.profile.env_file, "ENABLE_DOCKER") or "true") == "true"

    def environment(self) -> dict[str, str]:
        """
        The environment Compose runs with: compose.yaml reads the project's
        name, the profile's folder and the images' tags from it.
        """
        return {
            **os.environ,
            "HARDCODE_PROJECT": self.profile.project_name,
            "HARDCODE_PROFILE_DIR": self.profile.dir.as_posix(),
            "HARDCODE_VSCODE_TAG": self.profile.vscode_tag,
            "HARDCODE_SQUID_TAG": stack.image_tag("squid"),
            "HARDCODE_GATEWAY_TAG": stack.image_tag("gateway"),
            "HARDCODE_DOCKER_TAG": stack.image_tag("docker"),
            # The docker service sits behind a Compose profile of the same name,
            # so it only exists in the project while it is switched on.
            "COMPOSE_PROFILES": "docker" if self.docker_enabled() else "",
        }

    def run(
        self,
        *args: str,
        capture: bool = False,
        check: bool = False,
        timeout: Optional[float] = None,
        to_stderr: bool = False,
        limit: Optional[int] = None,
    ) -> subprocess.CompletedProcess:
        """
        Run ``docker compose <args>`` in the stack's folder.

        With *capture*, the output is returned instead of shown; with *check*,
        a non-zero exit raises ``ComposeError`` carrying what Compose said.

        With *to_stderr*, what Compose prints is shown on stderr. That is for
        a long step an operation takes behind the scenes (building the image),
        whose progress is worth watching but would otherwise land in stdout,
        in the middle of the JSON the app reads with ``--json``. Short steps
        are captured instead, so that their failure carries Compose's reason.

        A captured command gets no input. An interactive one (``hardcode
        shell``) keeps the terminal; but a captured ``exec`` reading a
        terminal it doesn't own would be stopped by it, and hang.

        Captured output is held up to the runner's limit, or *limit* bytes:
        a command that prints more is stopped, a ``ComposeError``.
        """
        options: dict = {}
        if to_stderr and not capture:
            options["stdout"] = sys.stderr
        if limit is not None:
            options["limit"] = limit

        try:
            result = self._run(
                self.argv(*args),
                cwd=str(stack.current()),
                env=self.environment(),
                stdin=subprocess.DEVNULL if capture else None,
                capture_output=capture,
                text=True,
                timeout=timeout,
                **options,
            )
        except subprocess.TimeoutExpired as error:
            raise ComposeError(f"docker compose {' '.join(args)} timed out after {error.timeout:g}s") from error
        except bounded.OutputTooLarge as error:
            raise ComposeError(f"docker compose {' '.join(args)} printed more than {error.limit} bytes") from error

        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() if capture else ""
            message = f"docker compose {' '.join(args)} failed"
            raise ComposeError(f"{message}: {detail}" if detail else message)

        return result

    def output(self, *args: str, timeout: Optional[float] = 60, limit: Optional[int] = None) -> str:
        """
        The stdout of a command that must succeed, no longer than *limit*.
        """
        return self.run(*args, capture=True, check=True, timeout=timeout, limit=limit).stdout

    def status(self) -> tuple[list[Service], str]:
        """
        The profile's containers, and why Compose couldn't list them when it
        couldn't (Docker is absent, its daemon isn't running, a file of the
        profile's can't be read); ``""`` when it could.
        """
        try:
            result = self.run("ps", "-a", "--format", "json", capture=True, timeout=60)
        except ComposeError as error:
            return [], str(error)

        if result.returncode != 0:
            complaint = (result.stderr or result.stdout or "").strip().splitlines()
            return [], complaint[-1] if complaint else "docker compose ps failed"

        return parse_ps(result.stdout), ""

    def ps(self) -> list[Service]:
        """
        The profile's containers; none when there are none or Compose can't say.
        """
        return self.status()[0]

    def running(self) -> bool:
        """
        Whether any of the profile's containers is running.
        """
        return any(service.state == "running" for service in self.ps())

    def health(self, service: str) -> str:
        """
        A service's health check status, or ``""`` when it has none.
        """
        for item in self.ps():
            if item.service == service:
                return item.health

        return ""

    def wait_healthy(self, service: str, attempts: int = 30, delay: float = 2.0) -> bool:
        """
        Wait for a service's health check to pass; whether it did in time.
        """
        for _ in range(attempts):
            if self.health(service) == "healthy":
                return True
            self.sleep(delay)

        return False

    def config_check(self) -> tuple[bool, str]:
        """
        Whether Compose accepts the profile's files, and its complaint if not.
        """
        result = self.run("config", "--quiet", capture=True, timeout=60)
        return result.returncode == 0, (result.stderr or result.stdout or "").strip()

    def exec(self, service: str, command: str, timeout: Optional[float] = 30, limit: Optional[int] = None) -> str:
        """
        Run a shell command in a running service and return its output, no
        longer than *limit*.
        """
        return self.output("exec", "-T", service, "sh", "-lc", command, timeout=timeout, limit=limit)

    def published(self, service: str, port: int) -> str:
        """
        Where the running *service* publishes its *port* on this machine, as
        the ``host:port`` of a URL; ``""`` when it doesn't (it isn't running).

        An address on every interface, or on the loopback one, is reached as
        localhost; any other is used as it is.
        """
        try:
            result = self.run("port", service, str(port), capture=True, timeout=30)
        except ComposeError:
            return ""

        lines = (result.stdout or "").strip().splitlines() if result.returncode == 0 else []
        if not lines:
            return ""

        # "127.0.0.1:8080", "0.0.0.0:8080" or "[::]:8080".
        host, _, published_port = lines[0].strip().rpartition(":")
        host = host.strip("[]")

        if host in ("", "0.0.0.0", "::", "127.0.0.1", "::1"):
            host = "localhost"
        elif ":" in host:
            host = f"[{host}]"

        return f"{host}:{published_port}"

    def vscode_token(self) -> str:
        """
        The VS Code server's connection token, or ``""`` when the VS Code container isn't
        running.
        """
        try:
            return self.exec("vscode", "cat ~/.vscode-server/connection-token").strip()
        except ComposeError:
            return ""
