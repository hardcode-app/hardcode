"""
Tests for a profile's own Dockerfile steps: the folder and its hash
(hardcode_cli.dockerfile), the stack's file that builds them
(compose.image.yaml), how Compose is run with it, whether the image has
them, ``hardcode image``, and that nothing in the sandbox can reach them.
"""

import json
import re
import tempfile
import unittest
from pathlib import Path

from hardcode_cli import dockerfile, langs, override, profiles, stack, yamlio
from hardcode_cli.compose import Compose
from hardcode_cli.dockerfile import DockerfileError
from hardcode_cli.ops import BASE_IMAGE, IMAGE_DOCKERFILE_LABEL, IMAGE_LANGUAGES_LABEL, Ops

from .helpers import REPO, FakeRunner, HomeTestCase, fake_host, yaml_available
from .test_cli import CliTestCase

# The languages label of an image built with every language and tool.
EVERY_LANGUAGE = " ".join(f"{key}=true" for key in langs.LANGS)


class FolderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hardcode-steps-")
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name) / "image"

    def test_no_dockerfile_is_no_steps(self):
        self.assertEqual(dockerfile.digest(self.folder), "")
        self.assertEqual(dockerfile.read(self.folder), "")

        # Files without a Dockerfile build nothing either.
        self.folder.mkdir()
        (self.folder / "tool.sh").write_text("echo\n", encoding="utf-8")
        self.assertEqual(dockerfile.digest(self.folder), "")

    def test_the_hash_follows_every_file_in_the_folder(self):
        dockerfile.save(self.folder, "FROM hardcode\n")
        first = dockerfile.digest(self.folder)
        self.assertRegex(first, r"^[0-9a-f]{12}$")
        self.assertEqual(dockerfile.digest(self.folder), first, "the same files, the same hash")

        # A file the steps copy counts, and so does its name.
        (self.folder / "files").mkdir()
        (self.folder / "files" / "tool.sh").write_text("echo one\n", encoding="utf-8")
        copied = dockerfile.digest(self.folder)
        self.assertNotEqual(copied, first)

        (self.folder / "files" / "tool.sh").write_text("echo two\n", encoding="utf-8")
        changed = dockerfile.digest(self.folder)
        self.assertNotEqual(changed, copied)

        (self.folder / "files" / "tool.sh").rename(self.folder / "files" / "other.sh")
        self.assertNotEqual(dockerfile.digest(self.folder), changed)

        dockerfile.save(self.folder, "FROM hardcode\nUSER root\n")
        self.assertNotEqual(dockerfile.digest(self.folder), changed)

    def test_save_makes_the_folder_and_ends_with_a_newline(self):
        path = dockerfile.save(self.folder, "FROM hardcode\r\nUSER dev")

        self.assertEqual(path, self.folder / "Dockerfile")
        self.assertEqual(path.read_bytes(), b"FROM hardcode\r\nUSER dev\n", "the lines end as they were given")

    def test_an_empty_dockerfile_is_refused(self):
        with self.assertRaisesRegex(DockerfileError, "an empty Dockerfile can't be built; to have no steps"):
            dockerfile.save(self.folder, " \n\n")

        self.assertFalse(self.folder.exists())

    def test_the_starter_says_what_the_steps_need(self):
        instructions = [line for line in dockerfile.STARTER.splitlines() if line and not line.startswith("#")]
        comments = " ".join(line for line in dockerfile.STARTER.splitlines() if line.startswith("#"))

        # It builds on HardCode's image, runs its steps as root, and gives
        # the VS Code container its user back.
        self.assertEqual(instructions, ["FROM hardcode", "USER root", "USER dev"])
        self.assertIn("run as root", comments)
        self.assertIn("apt-install", comments)
        self.assertIn("your computer's network", comments)


class StackFileTests(unittest.TestCase):
    """
    compose.image.yaml says what the CLI and the image's labels expect.
    """

    def setUp(self):
        if not yaml_available():
            self.skipTest("no YAML backend (yq, PyYAML or Docker)")

        self.document = yamlio.load((REPO / stack.IMAGE_STEPS).read_text(encoding="utf-8"))

    def test_it_ships_with_the_stack(self):
        self.assertIn(stack.IMAGE_STEPS, stack.PARTS)

    def test_hardcodes_image_is_built_under_a_name_of_its_own_and_never_run(self):
        base = self.document["services"]["vscode-base"]

        self.assertEqual(base["extends"], {"file": "compose.yaml", "service": "vscode"})
        self.assertEqual(base["image"].split(":")[0], BASE_IMAGE)
        self.assertEqual(base["scale"], 0)

    def test_the_profiles_steps_are_built_on_it_from_their_folder(self):
        build = self.document["services"]["vscode"]["build"]

        self.assertEqual(build["additional_contexts"], {dockerfile.BASE: "service:vscode-base"})
        self.assertTrue(build["context"].endswith(f"}}/{dockerfile.FOLDER}"), build["context"])
        self.assertEqual(list(build["labels"]), [IMAGE_DOCKERFILE_LABEL])
        self.assertIn("HARDCODE_DOCKERFILE_DIGEST", build["labels"][IMAGE_DOCKERFILE_LABEL])


class NothingInTheSandboxReachesTheStepsTests(unittest.TestCase):
    """
    The profile's folder is never mounted into a container, but for its
    extension list, alone and read-only, and its proxy folder, given only to
    the allowlist's renderer, which has no network and writes there what it
    did: a rebuild runs the steps in image/ on this computer, so nothing in
    the sandbox may write them.
    """

    # What compose.yaml mounts from the profile's folder, where, and whether
    # read-only.
    MOUNTED = [
        ("proxy", "allowlist", False),
        ("proxy", "allowlist-watch", False),
        ("vscode-extensions.txt", "vscode", True),
    ]

    # The services that may write in the profile's folder: none with a network.
    WRITERS = ("allowlist", "allowlist-watch")

    def setUp(self):
        if not yaml_available():
            self.skipTest("no YAML backend (yq, PyYAML or Docker)")

    def mounts(self, name):
        """
        Every bind mount from the profile's folder in the stack's file
        *name*: (service, the path under the profile's folder, read-only).
        """
        document = yamlio.load((REPO / name).read_text(encoding="utf-8"))
        found = []

        for service_name, service in document["services"].items():
            for volume in service.get("volumes") or []:
                if isinstance(volume, str):
                    parts = override.split_spec(volume)
                    source, read_only = parts[0], parts[-1] == "ro"
                else:
                    source, read_only = volume.get("source", ""), bool(volume.get("read_only"))

                if "HARDCODE_PROFILE_DIR" in source:
                    found.append((service_name, re.sub(r"^\$\{HARDCODE_PROFILE_DIR[^}]*\}/?", "", source), read_only))

        return found

    def test_compose_yaml_mounts_the_extension_list_and_the_proxy_folder_alone(self):
        mounts = self.mounts("compose.yaml")

        self.assertEqual(
            sorted((path, service, read_only) for service, path, read_only in mounts), sorted(self.MOUNTED)
        )
        self.assertFalse(any(path == "" or path.startswith(dockerfile.FOLDER) for _, path, _ in mounts), mounts)

        services = yamlio.load((REPO / "compose.yaml").read_text(encoding="utf-8"))["services"]
        for name in self.WRITERS:
            self.assertEqual(services[name]["network_mode"], "none", f"{name} writes there, so has no network")

    def test_the_steps_file_mounts_nothing_of_its_own(self):
        self.assertEqual(self.mounts(stack.IMAGE_STEPS), [])

        # The steps' folder is the context of a build on this computer:
        # sent to its Docker, never into a container.
        text = (REPO / stack.IMAGE_STEPS).read_text(encoding="utf-8")
        self.assertNotIn("volumes:", text)


class ComposeTests(HomeTestCase):
    """
    Compose run for a profile with its own steps, and without.
    """

    def compose(self):
        """
        The default profile's Compose, with the starter saved as its steps.
        """
        if not profiles.exists(self.root, "default"):
            profiles.create(self.root, "default", languages=[])
            dockerfile.save(profiles.get(self.root, "default").image_dir, dockerfile.STARTER)

        return Compose(profiles.get(self.root, "default"), runner=self.runner, docker="docker")

    def test_the_steps_get_the_stacks_file_and_their_hash(self):
        project = self.compose()
        argv = project.argv("build", "vscode")

        image_file = (self.stack / stack.IMAGE_STEPS).as_posix()
        self.assertEqual(argv[argv.index(image_file) - 1], "-f")
        self.assertLess(argv.index((self.stack / "compose.yaml").as_posix()), argv.index(image_file))
        self.assertLess(argv.index(image_file), argv.index(project.profile.override_file.as_posix()))

        environment = project.environment()
        self.assertEqual(environment["HARDCODE_DOCKERFILE_DIGEST"], dockerfile.digest(project.profile.image_dir))

    def test_the_hash_moves_with_the_file(self):
        project = self.compose()
        before = project.environment()["HARDCODE_DOCKERFILE_DIGEST"]

        dockerfile.save(project.profile.image_dir, "FROM hardcode\nUSER root\nRUN apt-install jq\nUSER dev\n")

        self.assertNotEqual(project.environment()["HARDCODE_DOCKERFILE_DIGEST"], before)


class OpsTests(HomeTestCase):
    """
    What the image was built with, the steps' state, saving them, and the
    folders a mount may not be.
    """

    def ops(self, name="default"):
        """
        The profile's operations, with the starter saved as its steps.
        """
        if not profiles.exists(self.root, name):
            profiles.create(self.root, name, languages=list(langs.LANGS))
            dockerfile.save(profiles.get(self.root, name).image_dir, dockerfile.STARTER)

        return Ops(self.root, profiles.get(self.root, name), runner=self.runner, docker="docker", host=fake_host())

    def built_from(self, ops, digest):
        """
        Have the fake Docker report an image built with every language and
        the steps whose hash is *digest*.
        """
        self.runner.image_labels = {IMAGE_LANGUAGES_LABEL: EVERY_LANGUAGE, IMAGE_DOCKERFILE_LABEL: digest}

    def test_a_changed_file_means_a_rebuild_is_due(self):
        ops = self.ops()
        self.built_from(ops, dockerfile.digest(ops.profile.image_dir))
        self.assertEqual(ops.state()["rebuildReasons"], [])

        ops.save_image_steps("FROM hardcode\nUSER root\nRUN apt-install jq\nUSER dev\n")
        state = ops.state()
        self.assertEqual((state["rebuildNeeded"], state["rebuildReasons"]), (True, ["dockerfile"]))

        # Deleted, the image is HardCode's alone again: one built from steps is behind.
        ops.profile.dockerfile.unlink()
        self.assertEqual(ops.state()["rebuildReasons"], ["dockerfile"])
        self.built_from(ops, "")
        self.assertEqual(ops.state()["rebuildReasons"], [])

    def test_the_steps_for_the_image_page(self):
        ops = self.ops()
        steps = ops.image_steps()

        self.assertEqual(steps["path"], str(ops.profile.dockerfile))
        self.assertTrue(steps["exists"])
        self.assertEqual(steps["dockerfile"], dockerfile.STARTER)
        self.assertEqual(steps["starter"], dockerfile.STARTER)
        self.assertEqual(steps["digest"], dockerfile.digest(ops.profile.image_dir))
        self.assertEqual(steps["base"], (self.stack / "images" / "vscode" / "Dockerfile").read_text(encoding="utf-8"))
        self.assertEqual(steps["image"]["built"], False)
        self.assertIs(steps["rebuildNeeded"], False, "the first start builds it")

        ops.profile.dockerfile.unlink()
        steps = ops.image_steps()
        self.assertEqual((steps["exists"], steps["dockerfile"], steps["digest"]), (False, "", ""))

    def test_saving_the_steps(self):
        ops = self.ops()

        result = ops.save_image_steps("FROM hardcode\nUSER root\nRUN apt-install jq\nUSER dev")

        self.assertEqual(
            result.message,
            f"saved {ops.profile.dockerfile} (built at the image's next rebuild: hardcode rebuild)",
        )
        self.assertEqual(ops.profile.dockerfile.read_text(encoding="utf-8").splitlines()[2], "RUN apt-install jq")
        with self.assertRaises(DockerfileError):
            ops.save_image_steps("")

    def test_the_profile_goes_with_both_its_images(self):
        ops = self.ops("gone")

        ops.destroy()

        self.assertEqual(
            self.runner.commands("image rm"),
            ["docker image rm hardcode-vscode:gone", f"docker image rm {BASE_IMAGE}:gone"],
        )

    def test_no_mount_holds_hardcodes_home_or_is_in_it(self):
        ops = self.ops()
        inside = ops.profile.image_dir
        outside = Path(self._tmp) / "src"
        outside.mkdir()

        # The root of the home's own drive: on Windows, "/" is the current
        # drive's, which needn't be the home's.
        drive_root = Path(self.root.anchor)

        for folder in (self.root, inside, ops.profile.dir, self.root.parent, drive_root):
            with self.subTest(folder=folder):
                with self.assertRaisesRegex(override.OverrideError, "HardCode's home .* or is in it"):
                    ops.mount(str(folder), "work")

        self.assertTrue(ops.mount(str(outside), "work").message.startswith("mounted "))
        self.assertNotIn(str(self.root), ops.profile.override_file.read_text(encoding="utf-8"))


class CommandTests(CliTestCase):
    """
    ``hardcode image`` and ``hardcode image save``, for a profile whose
    steps are the starter.
    """

    def setUp(self):
        super().setUp()
        dockerfile.save(profiles.get(self.root, "default").image_dir, dockerfile.STARTER)

    def test_image_says_where_the_steps_are(self):
        path = profiles.get(self.root, "default").dockerfile

        code, out, _ = self.run_cli("image")

        self.assertEqual(code, 0)
        self.assertIn(f"Dockerfile steps: {path}", out)
        self.assertIn("FROM hardcode", out)
        self.assertIn("no image yet", out)

    def test_image_says_when_a_rebuild_is_due(self):
        runner = FakeRunner()
        runner.image_labels = {IMAGE_LANGUAGES_LABEL: EVERY_LANGUAGE, IMAGE_DOCKERFILE_LABEL: "0123456789ab"}

        code, out, _ = self.run_cli("image", runner=runner)

        self.assertEqual(code, 0)
        self.assertIn("a rebuild is due (the Dockerfile steps changed): hardcode -p default rebuild", out)

    def test_without_steps_it_says_how_to_have_some(self):
        path = profiles.get(self.root, "default").dockerfile
        path.unlink()

        _, out, _ = self.run_cli("image")

        self.assertIn(f"none: write them in {path}", out)

    def test_json_and_save(self):
        code, out, _ = self.run_cli("--json", "image")
        shown = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(shown["dockerfile"], dockerfile.STARTER)
        self.assertEqual(shown["base"], (self.stack / "images" / "vscode" / "Dockerfile").read_text(encoding="utf-8"))

        text = "FROM hardcode\nUSER root\nRUN apt-install jq\nUSER dev\n"
        code, out, _ = self.run_cli("--json", "image", "save", "--", text)
        result = json.loads(out)["results"][0]
        self.assertEqual((code, result["changed"]), (0, True))
        self.assertEqual(profiles.get(self.root, "default").dockerfile.read_text(encoding="utf-8"), text)

        code, _, err = self.run_cli("image", "save", "--", "")
        self.assertEqual(code, 1)
        self.assertIn("an empty Dockerfile can't be built", err)
