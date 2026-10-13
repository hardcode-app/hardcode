"""
Tests for a profile's ``compose.override.yaml``: forwards and mounts
(hardcode_cli.override).
"""

import tempfile
import unittest
from pathlib import Path

from hardcode_cli import override
from hardcode_cli.override import Forward, Mount, OverrideError

from .helpers import yaml_available

DEV_HOME = "/home/${DEV_USER:-dev}"


class SplitSpecTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(override.split_spec("a:b:c"), ["a", "b", "c"])

    def test_braces_are_opaque(self):
        self.assertEqual(
            override.split_spec("${HOME}/Git:/home/${DEV_USER:-dev}/git:ro"),
            ["${HOME}/Git", "/home/${DEV_USER:-dev}/git", "ro"],
        )
        self.assertEqual(
            override.split_spec("${VSCODE_BIND:-127.0.0.1}:3000:3000"),
            ["${VSCODE_BIND:-127.0.0.1}", "3000", "3000"],
        )

    def test_windows_drive_letters(self):
        self.assertEqual(
            override.split_spec("C:\\Users\\me\\proj:/home/dev/proj:ro"),
            ["C:\\Users\\me\\proj", "/home/dev/proj", "ro"],
        )
        self.assertEqual(override.split_spec("D:/work:/w"), ["D:/work", "/w"])


class ParseVolumeTests(unittest.TestCase):
    def test_modes_and_remote(self):
        self.assertEqual(override.parse_volume("/a:/b"), Mount("/a", "/b", ""))
        self.assertEqual(override.parse_volume("/a:/b:ro"), Mount("/a", "/b", "ro"))
        self.assertEqual(
            override.parse_volume("${HOME}/x:/home/${DEV_USER:-dev}/x:rw"),
            Mount("${HOME}/x", "/home/${DEV_USER:-dev}/x", "rw"),
        )

    def test_not_a_bind_spec(self):
        self.assertIsNone(override.parse_volume("justaname"))
        self.assertIsNone(override.parse_volume({"type": "volume"}))

    def test_spec_round_trip(self):
        self.assertEqual(Mount("/a", "/b", "ro").spec, "/a:/b:ro")
        self.assertEqual(Mount("/a", "/b").spec, "/a:/b")
        self.assertEqual(Forward(3000, "vscode", 3000).spec, "3000:vscode:3000")


class OverrideFileTestCase(unittest.TestCase):
    """
    A temporary directory with ``self.path`` for an override file in it.
    """

    def setUp(self):
        if not yaml_available():
            self.skipTest("no YAML backend (yq, PyYAML or Docker)")

        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.path = self.base / "compose.override.yaml"

    def tearDown(self):
        self.tmp.cleanup()


class FileTests(OverrideFileTestCase):
    def test_load_missing(self):
        header, document = override.load(self.path)

        self.assertEqual(header, override.HEADER)
        self.assertEqual(document, {"services": {}})

    def test_load_keeps_header_and_repairs_services(self):
        self.path.write_text("# keep me\n# and me\n\nservices: 5\n", encoding="utf-8")

        header, document = override.load(self.path)

        self.assertEqual(header, "# keep me\n# and me\n")
        self.assertEqual(document["services"], {})

        self.path.write_text("", encoding="utf-8")
        self.assertEqual(override.load(self.path)[1], {"services": {}})

    def test_get_forwards_skips_malformed_items(self):
        forwards = "8000:vscode:8000 junk 3000:vscode:3000 x:y:z 1:2"
        document = {"services": {"gateway": {"environment": {"GATEWAY_FORWARDS": forwards}}}}

        self.assertEqual(override.get_forwards(document), [Forward(3000, "vscode", 3000)])
        self.assertEqual(override.get_forwards({"services": {}}), [])

    def test_save_tidies_and_writes_empty_form(self):
        document = {"services": {"vscode": {"volumes": []}, "gateway": {"environment": {}, "ports": []}, "x": None}}

        override.save(self.path, "# h\n", document)

        self.assertEqual(self.path.read_text(encoding="utf-8"), "# h\nservices: {}\n")
        self.assertEqual(
            override.tidy({"services": {"a": {"volumes": ["v"]}, "b": {}}}),
            {"services": {"a": {"volumes": ["v"]}}},
        )


class ForwardTests(OverrideFileTestCase):
    def setUp(self):
        super().setUp()
        self.path.write_text(
            f"# keep me\nservices:\n  vscode:\n    volumes:\n      - ${{HOME}}/Git:{DEV_HOME}/git\n", encoding="utf-8"
        )

    def test_add_and_remove_round_trip(self):
        self.assertEqual(override.add_forward(self.path, 3000), Forward(3000, "vscode", 3000))
        override.add_forward(self.path, 8080, "docker", 80)

        header, document = override.load(self.path)
        gateway = document["services"]["gateway"]
        self.assertTrue(header.startswith("# keep me"))
        self.assertEqual(gateway["environment"]["GATEWAY_FORWARDS"], "8000:vscode:8000 3000:vscode:3000 8080:docker:80")
        self.assertEqual(
            gateway["ports"],
            ["${VSCODE_BIND:-127.0.0.1}:3000:3000", "${VSCODE_BIND:-127.0.0.1}:8080:8080"],
        )
        self.assertEqual(override.get_forwards(document), [Forward(3000, "vscode", 3000), Forward(8080, "docker", 80)])

        override.remove_forward(self.path, 3000)
        override.remove_forward(self.path, 8080)

        _, document = override.load(self.path)
        self.assertNotIn("gateway", document["services"])
        self.assertEqual(document["services"]["vscode"]["volumes"], [f"${{HOME}}/Git:{DEV_HOME}/git"])

    def test_validation(self):
        with self.assertRaisesRegex(OverrideError, "1-65535"):
            override.add_forward(self.path, 0)
        with self.assertRaisesRegex(OverrideError, "1-65535"):
            override.add_forward(self.path, 3000, "vscode", 70000)
        with self.assertRaisesRegex(OverrideError, "vscode or docker"):
            override.add_forward(self.path, 3000, "elsewhere")
        with self.assertRaisesRegex(OverrideError, "VS Code"):
            override.add_forward(self.path, 8000)

        override.add_forward(self.path, 3000)
        with self.assertRaisesRegex(OverrideError, "already"):
            override.add_forward(self.path, 3000)
        with self.assertRaisesRegex(OverrideError, "isn't forwarded"):
            override.remove_forward(self.path, 4444)

    def test_set_forwards_keeps_foreign_port_entries(self):
        _, document = override.load(self.path)
        long_syntax = {"target": 9, "published": 9}
        document["services"]["gateway"] = {"ports": [long_syntax, "127.0.0.1:5000:5000"]}

        override.set_forwards(document, [Forward(5000, "vscode", 5000)])
        self.assertEqual(document["services"]["gateway"]["ports"], [long_syntax, "127.0.0.1:5000:5000"])

        override.set_forwards(document, [])
        self.assertEqual(document["services"]["gateway"]["ports"], [long_syntax])


class MountTests(OverrideFileTestCase):
    def setUp(self):
        super().setUp()
        self.local = self.base / "src"
        self.local.mkdir()

    def test_normalise_remote(self):
        self.assertEqual(override.normalise_remote("git"), f"{DEV_HOME}/git")
        self.assertEqual(override.normalise_remote("./git/"), f"{DEV_HOME}/git")
        self.assertEqual(override.normalise_remote("/opt/data/", "dev"), "/opt/data")
        self.assertEqual(override.normalise_remote("x", "alice"), "/home/alice/x")

        with self.assertRaisesRegex(OverrideError, "required"):
            override.normalise_remote("  ")
        with self.assertRaisesRegex(OverrideError, "':'"):
            override.normalise_remote("a:b")

    def test_normalise_local(self):
        home = str(self.base)
        resolved = str(self.local.resolve())

        self.assertEqual(override.normalise_local(str(self.local), home=home, windows=False), "${HOME}/src")
        self.assertEqual(override.normalise_local("~/src", home=home, windows=False), "${HOME}/src")
        self.assertEqual(override.normalise_local(str(self.local), home=home, windows=True), resolved)
        self.assertEqual(override.normalise_local(str(self.local), home="/elsewhere", windows=False), resolved)

        with self.assertRaisesRegex(OverrideError, "not a folder"):
            override.normalise_local(str(self.base / "nope"), home=home)
        with self.assertRaisesRegex(OverrideError, "required"):
            override.normalise_local("")

    def test_a_home_reached_through_a_symlink(self):
        # macOS's /var is /private/var: the folder resolves, and so must the
        # home it is compared with.
        linked = self.base / "linked-home"
        try:
            linked.symlink_to(self.base, target_is_directory=True)
        except OSError:
            self.skipTest("no symlinks here (Windows without the privilege)")

        self.assertEqual(override.normalise_local(str(self.local), home=str(linked), windows=False), "${HOME}/src")

    def test_add_get_remove(self):
        mount = override.add_mount(self.path, str(self.local), "work")
        self.assertEqual(mount.remote, f"{DEV_HOME}/work")
        override.add_mount(self.path, str(self.local), "/opt/data", read_only=True)

        _, document = override.load(self.path)
        self.assertEqual(
            [(mount.remote, mount.mode) for mount in override.get_mounts(document)],
            [(f"{DEV_HOME}/work", ""), ("/opt/data", "ro")],
        )
        self.assertEqual(document["services"]["docker"]["volumes"], document["services"]["vscode"]["volumes"])

        with self.assertRaisesRegex(OverrideError, "already mounted"):
            override.add_mount(self.path, str(self.local), "work")

        self.assertEqual(override.remove_mount(self.path, "work"), f"{DEV_HOME}/work")
        override.remove_mount(self.path, "/opt/data/")

        _, document = override.load(self.path)
        self.assertEqual(document, {"services": {}})

        with self.assertRaisesRegex(OverrideError, "nothing is mounted"):
            override.remove_mount(self.path, "work")

    def test_dev_user_is_honoured(self):
        override.add_mount(self.path, str(self.local), "work", dev_user="alice")

        _, document = override.load(self.path)
        self.assertEqual(override.get_mounts(document)[0].remote, "/home/alice/work")

        override.remove_mount(self.path, "work", dev_user="alice")

    def test_a_mount_written_with_the_user_variable(self):
        # As the template's header shows people writing it by hand, and as the
        # app passes it back to unmount.
        override.add_mount(self.path, str(self.local), "/home/${DEV_USER:-dev}/git")

        with self.assertRaisesRegex(OverrideError, "already mounted"):
            override.add_mount(self.path, str(self.local), "git", dev_user="dev")

        self.assertEqual(override.remove_mount(self.path, "git", dev_user="dev"), "/home/dev/git")

        override.add_mount(self.path, str(self.local), "/home/${DEV_USER:-dev}/git")
        override.remove_mount(self.path, "/home/${DEV_USER:-dev}/git", dev_user="dev")

        _, document = override.load(self.path)
        self.assertEqual(override.get_mounts(document), [])

        # Any other ':' still splits the volume.
        with self.assertRaisesRegex(OverrideError, "':'"):
            override.normalise_remote("/home/${DEV_USER:-dev}/a:b")

    def test_get_mounts_ignores_non_bind_entries(self):
        document = {"services": {"vscode": {"volumes": ["named-volume", {"type": "volume"}, "/a:/b"]}}}

        self.assertEqual(override.get_mounts(document), [Mount("/a", "/b")])
        self.assertEqual(override.get_mounts({"services": {}}), [])


if __name__ == "__main__":
    unittest.main()
