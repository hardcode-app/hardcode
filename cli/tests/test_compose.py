"""
Tests for running ``docker compose`` for a profile (hardcode_cli.compose).
"""

import subprocess
import unittest
from unittest import mock

from hardcode_cli import compose, envfile, profiles, stack
from hardcode_cli.compose import Compose, ComposeError, Service

from .helpers import FakeRunner, HomeTestCase

TOKEN_COMMAND = "cat ~/.vscode-server/connection-token"


class ParseTests(unittest.TestCase):
    def test_array_and_ndjson(self):
        array = '[{"Service":"a","State":"running","Status":"Up","Health":"healthy"}]'
        self.assertEqual(compose.parse_ps(array), [Service("a", "running", "Up", "healthy")])

        one_per_line = (
            '{"Service":"a","State":"exited","Status":"Exited (0)"}\n'
            '{"Service":"b","State":"running","Status":"Up","Health":null}\n'
        )
        self.assertEqual(
            compose.parse_ps(one_per_line),
            [Service("a", "exited", "Exited (0)", ""), Service("b", "running", "Up", "")],
        )

        self.assertEqual(compose.parse_ps("  \n"), [])

    def test_docker_executable(self):
        self.assertEqual(compose.docker_executable(lambda _: "/usr/bin/docker"), "/usr/bin/docker")

        with self.assertRaises(ComposeError):
            compose.docker_executable(lambda _: None)


class ComposeTests(HomeTestCase):
    def make(self, runner=None, docker="docker"):
        """
        Compose for the default profile, through *runner* (the test's fake by default).
        """
        if not profiles.exists(self.root, "default"):
            profiles.create(self.root, "default", languages=[])

        profile = profiles.get(self.root, "default")
        return Compose(profile, runner=runner or self.runner, docker=docker)

    def test_argv_includes_project_env_file_and_override(self):
        project = self.make()
        folder = (self.root / "profiles" / "default").as_posix()

        self.assertEqual(
            project.argv("up", "-d"),
            [
                "docker",
                "compose",
                "--project-name",
                "hardcode-default",
                "--project-directory",
                self.stack.as_posix(),
                "--env-file",
                f"{folder}/.env",
                "-f",
                f"{self.stack.as_posix()}/compose.yaml",
                "-f",
                f"{folder}/compose.override.yaml",
                "up",
                "-d",
            ],
        )

        project.profile.override_file.unlink()
        self.assertNotIn("compose.override.yaml", " ".join(project.argv("ps")))

    def test_docker_is_read_as_the_entrypoint_reads_it(self):
        project = self.make()

        # On when unset or exactly "true"; anything else is off, as the
        # VS Code container's entrypoint has it.
        for value, expected in (("", True), ("true", True), ("false", False), ("False", False), ("TRUE", False)):
            envfile.set_value(project.profile.env_file, "ENABLE_DOCKER", value)
            self.assertEqual(project.docker_enabled(), expected, value)

    def test_environment_marks_the_profile(self):
        environment = self.make().environment()
        self.assertEqual(environment["HARDCODE_PROJECT"], "hardcode-default")
        self.assertEqual(environment["HARDCODE_PROFILE_DIR"], (self.root / "profiles" / "default").as_posix())
        self.assertEqual(environment["HARDCODE_VSCODE_TAG"], "default")
        self.assertEqual(environment["HARDCODE_SQUID_TAG"], stack.image_tag("squid"))
        self.assertEqual(environment["HARDCODE_GATEWAY_TAG"], stack.image_tag("gateway"))
        self.assertEqual(
            environment["COMPOSE_PROFILES"], "docker", "the daemon is on unless the profile says otherwise"
        )

        project = self.make()
        project.profile.env_file.write_text(project.profile.env_file.read_text() + "ENABLE_DOCKER=false\n")
        self.assertFalse(project.docker_enabled())
        self.assertEqual(project.environment()["COMPOSE_PROFILES"], "")

    def test_another_homes_profiles_have_names_of_their_own(self):
        self.make()
        profile = profiles.get(self.root, "default", home_id="0a1b2c")
        environment = Compose(profile, runner=self.runner, docker="docker").environment()

        self.assertEqual(profile.project_name, "hardcode-0a1b2c-default")
        self.assertEqual(environment["HARDCODE_PROJECT"], "hardcode-0a1b2c-default")
        self.assertEqual(environment["HARDCODE_VSCODE_TAG"], "0a1b2c-default")

    def test_dockers_subnets(self):
        runner = FakeRunner()
        runner.networks = {"bridge": "172.17.0.0/16", "mine": "10.9.0.0/24 fd00::/64"}
        self.assertEqual(compose.docker_subnets("docker", runner), ["172.17.0.0/16", "10.9.0.0/24", "fd00::/64"])

        runner.fail_on.add("network")
        self.assertEqual(compose.docker_subnets("docker", runner), [], "Docker can't say: nothing taken")

    def test_run_passes_cwd_and_check(self):
        project = self.make()
        self.assertEqual(project.run("ps", capture=True).returncode, 0)

        self.runner.fail_on.add("build")
        with self.assertRaisesRegex(ComposeError, "simulated failure"):
            project.run("build", capture=True, check=True)
        self.assertEqual(project.run("build").returncode, 1, "no check: the code is returned")

    def test_captured_commands_get_no_input(self):
        seen = []

        def record(argv, **options):
            seen.append(options.get("stdin"))
            return subprocess.CompletedProcess(argv, 0, "", "")

        project = self.make(runner=record)
        project.run("exec", "-T", "vscode", "true", capture=True)
        project.run("exec", "vscode", "bash")
        project.run_docker("image", "ls")

        # Captured: no input, so an `exec` can't be stopped reading a terminal.
        # Interactive: the terminal stays.
        self.assertEqual(seen, [subprocess.DEVNULL, None, subprocess.DEVNULL])

    def test_run_timeout_is_a_compose_error(self):
        def slow(argv, **_):
            raise subprocess.TimeoutExpired(argv, 5)

        project = self.make(runner=slow)

        with self.assertRaisesRegex(ComposeError, "timed out after 5s"):
            project.run("ps", timeout=5)

    def test_has_docker(self):
        self.assertTrue(self.make().has_docker())

        with mock.patch.object(compose, "docker_executable", side_effect=ComposeError("none")):
            self.assertFalse(self.make(docker=None).has_docker())

    def test_ps_running_health(self):
        stopped = self.make()
        self.assertEqual(stopped.ps(), [])
        self.assertFalse(stopped.running())
        self.assertEqual(stopped.health("squid"), "")

        running = self.make(FakeRunner(running=True))
        self.assertTrue(running.running())
        self.assertEqual(running.health("squid"), "healthy")
        self.assertEqual(running.health("nope"), "")

    def test_wait_healthy(self):
        slept = []

        healthy = self.make(FakeRunner(running=True))
        healthy.sleep = slept.append
        self.assertTrue(healthy.wait_healthy("squid", attempts=2))
        self.assertEqual(slept, [])

        sick = self.make(FakeRunner(running=True, healthy=False))
        sick.sleep = slept.append
        self.assertFalse(sick.wait_healthy("squid", attempts=3, delay=0.5))
        self.assertEqual(slept, [0.5, 0.5, 0.5])

    def test_config_check(self):
        project = self.make()
        self.assertEqual(project.config_check(), (True, ""))

        self.runner.fail_on.add("config --quiet")
        ok, complaint = project.config_check()
        self.assertFalse(ok)
        self.assertIn("simulated", complaint)

    def test_exec_and_token(self):
        self.assertEqual(self.make(FakeRunner(running=True)).vscode_token(), "tok3n")
        self.assertEqual(self.make().vscode_token(), "")

        with self.assertRaises(ComposeError):
            self.make().exec("vscode", TOKEN_COMMAND)

    def test_output_strips_nothing_and_fails_loudly(self):
        project = self.make(FakeRunner(running=True))

        self.assertEqual(project.output("exec", "-T", "vscode", "sh", "-lc", TOKEN_COMMAND), "tok3n\n")

    def test_status_carries_the_reason(self):
        project = self.make(FakeRunner(running=True))
        services, error = project.status()
        self.assertEqual((len(services), error), (2, ""))

        def daemon_down(argv, **_):
            return subprocess.CompletedProcess(argv, 1, "", "Cannot connect to the Docker daemon. Is it running?\n")

        self.assertEqual(self.make(daemon_down).status(), ([], "Cannot connect to the Docker daemon. Is it running?"))

        with mock.patch.object(compose, "docker_executable", side_effect=ComposeError("docker not found on the PATH")):
            self.assertEqual(self.make(docker=None).status(), ([], "docker not found on the PATH"))

    def test_published(self):
        runner = FakeRunner(running=True)
        project = self.make(runner)

        for printed, expected in (
            ("127.0.0.1:8080", "localhost:8080"),
            ("0.0.0.0:8080", "localhost:8080"),
            ("[::]:8080", "localhost:8080"),
            ("192.168.1.5:8080", "192.168.1.5:8080"),
            ("[fd00::5]:8080", "[fd00::5]:8080"),
        ):
            runner.published = printed
            self.assertEqual(project.published("gateway", 8000), expected, printed)

        self.assertEqual(self.make().published("gateway", 8000), "", "not running: not published")

    def test_ps_without_docker_is_empty(self):
        project = self.make(docker=None)

        with mock.patch.object(compose, "docker_executable", side_effect=ComposeError("docker not found on the PATH")):
            self.assertEqual(project.ps(), [])
            self.assertFalse(project.running())

    def test_docker_is_resolved_lazily(self):
        project = self.make(docker=None)
        project._docker = "/custom/docker"

        self.assertEqual(project.docker, "/custom/docker")


if __name__ == "__main__":
    unittest.main()
