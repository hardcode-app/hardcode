"""
Tests for reading and writing YAML through whatever backend the machine has
(hardcode_cli.yamlio).
"""

import shutil
import subprocess
import unittest

from hardcode_cli import yamlio
from hardcode_cli.yamlio import Backend, YamlError

# An override document with the values that trip YAML tools up: ${...}
# references, colons inside values, and long space-separated strings.
DOCUMENT = {
    "services": {
        "vscode": {
            "volumes": ["${HOME}/Git:/home/${DEV_USER:-dev}/git:ro"],
        },
        "gateway": {
            "environment": {"GATEWAY_FORWARDS": "8000:vscode:8000 3000:vscode:3000"},
            "ports": ["${VSCODE_BIND:-127.0.0.1}:3000:3000"],
        },
    }
}


class FakeProcess:
    """
    What ``subprocess.run`` returns, with just the fields yamlio reads.
    """

    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class FakeModule:
    """
    A stand-in for the PyYAML module.
    """

    @staticmethod
    def safe_load(text):
        return {"loaded": text}

    @staticmethod
    def safe_dump(document, sort_keys=False, width=1000):
        return "dumped\n"


def nothing_found(_name):
    return None


def nothing_run(*_args, **_options):
    return FakeProcess()


class SelectionTests(unittest.TestCase):
    def test_prefers_mikefarah_yq(self):
        calls = []

        def run(argv, **_):
            calls.append(argv)
            return FakeProcess("yq (https://github.com/mikefarah/yq/) version v4.44.1")

        backend = yamlio.select(
            which=lambda name: "/usr/bin/yq" if name == "yq" else None,
            run=run,
            import_pyyaml=lambda: object(),
        )

        self.assertEqual(backend.name, "yq")
        self.assertEqual(calls, [["/usr/bin/yq", "--version"]])

    def test_skips_the_python_yq_wrapper(self):
        backend = yamlio.select(
            which=lambda _: "/usr/bin/yq",
            run=lambda argv, **_: FakeProcess("yq 3.2.3"),
            import_pyyaml=FakeModule,
        )

        self.assertEqual(backend.name, "pyyaml")

    def test_yq_probe_failure_falls_through(self):
        def run(argv, **_):
            raise OSError("boom")

        backend = yamlio.select(which=lambda _: "/usr/bin/yq", run=run, import_pyyaml=FakeModule)

        self.assertEqual(backend.name, "pyyaml")

    def test_docker_image_is_the_last_resort(self):
        backend = yamlio.select(
            which=lambda name: "/usr/bin/docker" if name == "docker" else None,
            run=nothing_run,
            import_pyyaml=lambda: None,
        )

        self.assertEqual(backend.name, "docker-yq")
        self.assertIn("--memory", yamlio.YQ_DOCKER_ARGS, "the helper container is memory-capped")

    def test_nothing_available(self):
        with self.assertRaisesRegex(YamlError, "install yq"):
            yamlio.select(which=nothing_found, run=nothing_run, import_pyyaml=lambda: None)

    def test_use_and_backend_cache(self):
        chosen = Backend("fake", lambda text: {"k": text}, lambda document: "dumped")

        yamlio.use(chosen)
        try:
            self.assertIs(yamlio.backend(), chosen)
            self.assertEqual(yamlio.load("x"), {"k": "x"})
            self.assertEqual(yamlio.dump({}), "dumped")
        finally:
            yamlio.use(None)


class BackendTests(unittest.TestCase):
    def test_yq_backend_command_lines(self):
        calls = []

        def run(argv, input="", **_):
            calls.append((argv, input))
            wants_json = "-o" in argv and argv[argv.index("-o") + 1] == "json"
            return FakeProcess('{"a":1}' if wants_json else "a: 1\n")

        backend = yamlio.yq_backend(["yq"], run)

        self.assertEqual(backend.load("a: 1"), {"a": 1})
        self.assertEqual(backend.dump({"a": 1}), "a: 1\n")
        self.assertEqual(calls[0][0], ["yq", "-p", "yaml", "-o", "json", "-I", "0", "."])
        self.assertEqual(calls[1], (["yq", "-p", "json", "-o", "yaml", "."], '{"a": 1}'))

    def test_yq_backend_empty_and_errors(self):
        empty = yamlio.yq_backend(["yq"], lambda argv, **_: FakeProcess(""))
        self.assertIsNone(empty.load("# only a comment"))

        failing = yamlio.yq_backend(["yq"], lambda argv, **_: FakeProcess("", 1, "bad yaml"))
        with self.assertRaisesRegex(YamlError, "bad yaml"):
            failing.load("x: [")

        def timeout(argv, **_):
            raise subprocess.TimeoutExpired(argv, 60)

        with self.assertRaisesRegex(YamlError, "yq failed"):
            yamlio.yq_backend(["yq"], timeout).dump({})

    def test_pyyaml_backend(self):
        backend = yamlio.pyyaml_backend(FakeModule())

        self.assertEqual(backend.load("a: 1"), {"loaded": "a: 1"})
        self.assertEqual(backend.dump({}), "dumped\n")


class PyYamlErrorTests(unittest.TestCase):
    def test_parse_errors_are_yaml_errors(self):
        yaml = yamlio._import_pyyaml()
        if yaml is None:
            self.skipTest("PyYAML not installed")

        backend = yamlio.pyyaml_backend(yaml)

        with self.assertRaisesRegex(yamlio.YamlError, "invalid YAML"):
            backend.load("services:\n  vscode:\n    volumes: [\n")
        self.assertEqual(backend.load("a: 1"), {"a": 1})


@unittest.skipUnless(shutil.which("docker"), "needs docker")
class DockerYqRoundTrip(unittest.TestCase):
    """
    The real last-resort backend: yq's image. Pulls mikefarah/yq:4 if absent.
    """

    def test_round_trip(self):
        command = [shutil.which("docker"), *yamlio.YQ_DOCKER_ARGS, yamlio.YQ_IMAGE]
        backend = yamlio.yq_backend(command, name="docker-yq")

        text = backend.dump(DOCUMENT)

        self.assertIn("GATEWAY_FORWARDS: 8000:vscode:8000 3000:vscode:3000", text)
        self.assertEqual(backend.load(text), DOCUMENT)
        self.assertIsNone(backend.load("# nothing here\n"))


class LocalBackendRoundTrip(unittest.TestCase):
    """
    Whatever ``select()`` picks on this machine must round-trip the override document.
    """

    def test_round_trip(self):
        try:
            backend = yamlio.select()
        except YamlError as error:
            self.skipTest(str(error))

        text = backend.dump(DOCUMENT)

        self.assertEqual(backend.load(text), DOCUMENT)
        self.assertEqual(backend.load("services: {}\n"), {"services": {}})


if __name__ == "__main__":
    unittest.main()
