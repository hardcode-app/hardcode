"""
Tests for where HardCode keeps profiles (hardcode_cli.home) and for the
stack it runs them with (hardcode_cli.stack).
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hardcode_cli import home, stack, yamlio
from hardcode_cli.home import HomeError

from .helpers import REPO, make_stack


class HomeTests(unittest.TestCase):
    def test_the_platforms_default(self):
        linux = home.default_home({"HOME": "/home/me"}, "linux")
        self.assertEqual(linux, Path("/home/me/.config/hardcode"))

        xdg = home.default_home({"HOME": "/home/me", "XDG_CONFIG_HOME": "/cfg"}, "linux")
        self.assertEqual(xdg, Path("/cfg/hardcode"))

        relative = home.default_home({"HOME": "/home/me", "XDG_CONFIG_HOME": "cfg"}, "linux")
        self.assertEqual(relative, Path("/home/me/.config/hardcode"), "a relative XDG path is ignored")

        mac = home.default_home({"HOME": "/Users/me"}, "darwin")
        self.assertEqual(mac, Path("/Users/me/Library/Application Support/HardCode"))

        windows = home.default_home({"LOCALAPPDATA": "C:/Users/me/AppData/Local"}, "win32")
        self.assertEqual(windows, Path("C:/Users/me/AppData/Local/HardCode"))

    def test_the_cache(self):
        self.assertEqual(home.cache_dir({"HOME": "/home/me"}, "linux"), Path("/home/me/.cache/hardcode"))
        self.assertEqual(home.cache_dir({"HOME": "/Users/me"}, "darwin"), Path("/Users/me/Library/Caches/HardCode"))
        self.assertEqual(
            home.cache_dir({"LOCALAPPDATA": "C:/Local"}, "win32"),
            Path("C:/Local/HardCode/Cache"),
        )

    def test_config_then_the_environment_then_the_default(self):
        with tempfile.TemporaryDirectory() as folder:
            given = Path(folder) / "apps" / "hardcode"
            environment = {"HOME": folder, "HARDCODE_CONFIG": str(Path(folder) / "from-env")}

            self.assertEqual(home.resolve(str(given), environment), given.resolve())
            self.assertEqual(home.resolve(None, environment), (Path(folder) / "from-env").resolve())
            self.assertEqual(home.resolve(None, {"HOME": folder}), home.default_home({"HOME": folder}))

    def test_a_file_is_not_a_home(self):
        with tempfile.NamedTemporaryFile() as file:
            with self.assertRaisesRegex(HomeError, "is a file"):
                home.resolve(file.name, {})

    def test_only_other_homes_have_an_id(self):
        environment = {"HOME": "/home/me"}

        self.assertEqual(home.home_id(Path("/home/me/.config/hardcode"), environment), "")

        other = home.home_id(Path("/Apps/hardcode"), environment)
        self.assertRegex(other, r"^[0-9a-f]{6}$")
        self.assertNotEqual(other, home.home_id(Path("/Apps/other"), environment))

    def test_sudo_is_refused(self):
        with self.assertRaisesRegex(HomeError, "doesn't need root: run it as me"):
            home.refuse_sudo({"SUDO_USER": "me"}, euid=0)

        home.refuse_sudo({}, euid=0)
        home.refuse_sudo({"SUDO_USER": "me"}, euid=1000)


class StackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(stack.use, None)

    def test_a_checkout_runs_with_its_own_files(self):
        self.assertEqual(stack.locate(Path(self.tmp.name)), REPO)

    def test_an_installed_one_writes_its_bundle_out_once_per_content(self):
        bundle = make_stack(Path(self.tmp.name) / "bundle")
        cache = Path(self.tmp.name) / "cache"

        with mock.patch.object(stack, "CHECKOUT", Path(self.tmp.name) / "nowhere"):
            with mock.patch.object(stack, "BUNDLED", bundle):
                first = stack.locate(cache)
                self.assertEqual(first, cache / "stack" / stack.digest(bundle))
                self.assertEqual((first / "compose.yaml").read_text(), (bundle / "compose.yaml").read_text())
                self.assertEqual(stack.locate(cache), first, "written once")

                # A newer stack gets a folder of its own; the old one is left as it is.
                (bundle / "compose.yaml").write_text("name: newer\n")
                second = stack.locate(cache)

        self.assertNotEqual(second, first)
        self.assertTrue((first / "compose.yaml").exists())
        self.assertEqual(sorted(path.name for path in (cache / "stack").iterdir()), sorted([first.name, second.name]))

    def test_without_a_stack_it_says_so(self):
        with (
            mock.patch.object(stack, "CHECKOUT", Path(self.tmp.name)),
            mock.patch.object(stack, "BUNDLED", Path(self.tmp.name)),
        ):
            with self.assertRaisesRegex(stack.StackError, "reinstall"):
                stack.locate(Path(self.tmp.name))

    def test_defaults_are_compose_yamls(self):
        stack.use(REPO)
        defaults = stack.defaults()

        self.assertEqual(defaults["VSCODE_PORT"], "11111")
        self.assertEqual(defaults["DEVNET_SUBNET"], "172.31.253.0/24")
        self.assertEqual(defaults["INSTALL_PYTHON"], "false")

    def test_every_service_rotates_its_logs(self):
        # The sandbox fills the proxy's log just by making requests. Rotated,
        # neither the disk nor a read of the logs (Blocked traffic, hardcode
        # logs) grows without bound.
        services = yamlio.load((REPO / "compose.yaml").read_text())["services"]

        for name, service in services.items():
            with self.subTest(service=name):
                logging = service.get("logging") or {}
                self.assertEqual(logging.get("driver"), "json-file")
                self.assertEqual(logging.get("options"), {"max-size": "10m", "max-file": "3"})

    def test_an_images_tag_follows_its_build_context(self):
        stack.use(make_stack(Path(self.tmp.name) / "stack"))
        squid = stack.image_tag("squid")
        gateway = stack.image_tag("gateway")

        self.assertNotEqual(squid, gateway)
        (stack.current() / "images" / "gateway" / "Dockerfile").write_text("FROM scratch  # changed\n")
        self.assertEqual(stack.image_tag("squid"), squid)
        self.assertNotEqual(stack.image_tag("gateway"), gateway)

    def test_staging_copies_every_part(self):
        destination = Path(self.tmp.name) / "staged"
        stack.stage(REPO, destination)

        for part in stack.PARTS:
            self.assertTrue((destination / part).exists(), part)
        self.assertEqual(stack.digest(destination), stack.digest(REPO))

    @unittest.skipIf(os.name == "nt", "file modes")
    def test_what_is_written_out_is_readable_by_everyone(self):
        # A binary's bundled files are unpacked readable by their owner only;
        # an image built from such a squid.conf had one squid couldn't read.
        bundle = make_stack(Path(self.tmp.name) / "bundle")
        for path in [bundle, *bundle.rglob("*")]:
            path.chmod(0o700 if path.is_dir() else 0o600)

        destination = Path(self.tmp.name) / "staged"
        stack.stage(bundle, destination)

        for path in [destination, *destination.rglob("*")]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o755 if path.is_dir() else 0o644, path)


class ImageTests(unittest.TestCase):
    """
    The images the stack builds don't depend on how their build files are
    stored: the same image from a checkout, a wheel or a binary's bundle.
    """

    def test_every_copied_file_gets_its_mode_in_the_dockerfile(self):
        for image in ("squid", "gateway", "vscode"):
            dockerfile = (REPO / "images" / image / "Dockerfile").read_text(encoding="utf-8")
            copies = [line for line in dockerfile.splitlines() if line.startswith(("COPY ", "ADD "))]

            for line in copies:
                if "--from=" in line:
                    continue
                self.assertIn("--chmod=", line, f"images/{image}/Dockerfile: {line}")


if __name__ == "__main__":
    unittest.main()
