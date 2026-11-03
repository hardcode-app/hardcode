"""
Tests for a profile's own Ubuntu packages: the names (hardcode_cli.packages),
the operations that change them, the commands, whether the image has them,
and the files that repeat the rule a name follows.
"""

import json
import unittest

from hardcode_cli import envfile, langs, packages, profiles
from hardcode_cli.ops import IMAGE_LANGUAGES_LABEL, IMAGE_PACKAGES_LABEL, Ops
from hardcode_cli.packages import PackageError

from .helpers import REPO, FakeRunner, HomeTestCase
from .test_cli import CliTestCase

# The languages label of an image built with every language and tool.
EVERY_LANGUAGE = " ".join(f"{key}=true" for key in langs.LANGS)


class NameTests(unittest.TestCase):
    def test_debians_rule(self):
        for name in ("jq", "postgresql-client", "libstdc++6", "g++", "python3.14", "0ad"):
            self.assertEqual(packages.check(f" {name} "), name, name)

        for name in ("", "a", "-jq", ".jq", "+jq", "Jq", "jq;rm", "jq bash", "jq/x", "$(id)", "jq_1"):
            with self.assertRaises(PackageError, msg=name):
                packages.check(name)

    def test_the_error_says_the_rule(self):
        with self.assertRaisesRegex(PackageError, r"'Jq' is not an Ubuntu package name \(lower-case letters"):
            packages.check("Jq")

    def test_a_value_is_read_in_its_order_without_repeats(self):
        self.assertEqual(packages.parse("  jq postgresql-client\tjq  "), ["jq", "postgresql-client"])
        self.assertEqual(packages.parse(""), [])
        self.assertEqual(packages.join(["jq", "tree"]), "jq tree")

    def test_the_order_makes_no_difference_to_the_image(self):
        self.assertTrue(packages.same(["jq", "tree"], ["tree", "jq"]))
        self.assertFalse(packages.same(["jq"], ["jq", "tree"]))


class AgreementTests(unittest.TestCase):
    """
    The files that build and install the packages say what the CLI says.
    """

    def test_the_packages_are_a_build_arg_and_a_label(self):
        dockerfile = (REPO / "images" / "vscode" / "Dockerfile").read_text(encoding="utf-8")
        compose = (REPO / "compose.yaml").read_text(encoding="utf-8")
        env = (REPO / "template" / ".env").read_text(encoding="utf-8")

        self.assertIn(f"{packages.ENV_KEY}: ${{{packages.ENV_KEY}:-}}", compose)
        self.assertIn(f'ARG {packages.ENV_KEY}=""', dockerfile)
        self.assertIn(f'LABEL {IMAGE_PACKAGES_LABEL}="${{{packages.ENV_KEY}}}"', dockerfile)
        self.assertIn(f"\n#{packages.ENV_KEY}=\n", env, "the template's reference names it")

        # Declared just before its step, after the languages and tools: a
        # change of the list doesn't rebuild what comes before.
        declared = dockerfile.index(f"ARG {packages.ENV_KEY}")
        last_tool = dockerfile.index('"${INSTALL_CODEX_CLI}" = "true"')
        self.assertGreater(declared, last_tool)

    def test_the_image_and_its_installer_check_names_as_the_cli_does(self):
        dockerfile = (REPO / "images" / "vscode" / "Dockerfile").read_text(encoding="utf-8")
        installer = (REPO / "images" / "vscode" / "install-packages.sh").read_text(encoding="utf-8")

        self.assertIn(f"=~ {packages.PACKAGE_NAME.pattern} ]]", dockerfile)
        self.assertIn(f"PACKAGE_NAME='{packages.PACKAGE_NAME.pattern}'", installer)

    def test_the_app_checks_names_as_the_cli_does(self):
        # The Ubuntu packages card says at once that a name isn't one; the
        # CLI checks again.
        page = (REPO / "app" / "src" / "app.js").read_text(encoding="utf-8")

        self.assertIn(f"const PACKAGE_NAME = /{packages.PACKAGE_NAME.pattern}/;", page)

    def test_the_entrypoint_installs_the_operators_list_with_the_installer(self):
        dockerfile = (REPO / "images" / "vscode" / "Dockerfile").read_text(encoding="utf-8")
        entrypoint = (REPO / "images" / "vscode" / "entrypoint.sh").read_text(encoding="utf-8")

        self.assertIn("COPY --chmod=0755 install-packages.sh /usr/local/bin/hardcode-install-packages", dockerfile)
        self.assertIn('read -ra wanted_packages <<<"${HARDCODE_PACKAGES:-}"', entrypoint)
        self.assertIn('hardcode-install-packages "${wanted_packages[@]}"', entrypoint)

        # After apt is given the proxy, which it needs to reach the archive.
        self.assertGreater(entrypoint.index("hardcode-install-packages"), entrypoint.index("90hardcode-proxy"))


class OpsTests(HomeTestCase):
    """
    Adding and removing packages, and whether the image has them.
    """

    def ops(self):
        if not profiles.exists(self.root, "default"):
            profiles.create(self.root, "default", languages=list(langs.LANGS))

        return Ops(self.root, profiles.get(self.root, "default"), runner=self.runner, docker="docker")

    def stored(self, ops):
        return envfile.read(ops.profile.env_file).get(packages.ENV_KEY)

    def test_add_and_remove(self):
        ops = self.ops()
        self.assertEqual(ops.own_packages(), [])

        added = ops.add_packages(["jq", "postgresql-client", "jq"])
        self.assertEqual(
            added.message,
            "added packages jq postgresql-client (installed at the image's next rebuild: hardcode rebuild)",
        )
        self.assertTrue(added.changed)
        self.assertIsNone(added.applied, "nothing runs differently until the rebuild")
        self.assertEqual(self.stored(ops), "jq postgresql-client")

        again = ops.add_packages(["jq"])
        self.assertEqual((again.changed, again.message), (False, "already added: jq"))

        ops.add_packages(["tree"])
        self.assertEqual(ops.own_packages(), ["jq", "postgresql-client", "tree"], "in the order added")

        removed = ops.remove_packages(["postgresql-client"])
        self.assertEqual(
            removed.message,
            "removed package postgresql-client (gone from the image at its next rebuild: hardcode rebuild)",
        )
        self.assertEqual(self.stored(ops), "jq tree")

        # None left is unset, compose.yaml's default: a profile stores only what differs.
        ops.remove_packages(["jq", "tree"])
        self.assertNotIn(packages.ENV_KEY, envfile.read(ops.profile.env_file))

    def test_nothing_is_written_for_a_bad_name_or_one_not_there(self):
        ops = self.ops()
        ops.add_packages(["jq"])

        with self.assertRaisesRegex(PackageError, "not an Ubuntu package name"):
            ops.add_packages(["tree", "Bad;Name"])
        with self.assertRaisesRegex(PackageError, "not one of this profile's packages: tree"):
            ops.remove_packages(["jq", "tree"])

        self.assertEqual(self.stored(ops), "jq")

    def test_a_change_means_a_rebuild_is_due(self):
        ops = self.ops()

        self.runner.image_labels = {IMAGE_LANGUAGES_LABEL: EVERY_LANGUAGE, IMAGE_PACKAGES_LABEL: ""}
        state = ops.state()
        self.assertEqual((state["rebuildNeeded"], state["rebuildReasons"]), (False, []))
        self.assertEqual(state["packages"], [])

        ops.add_packages(["jq", "tree"])
        state = ops.state()
        self.assertEqual((state["rebuildNeeded"], state["rebuildReasons"]), (True, ["packages"]))
        self.assertEqual(state["packages"], ["jq", "tree"])
        self.assertEqual(state["image"]["packages"], [])

        # Built with them, in another order: what it has is the same.
        self.runner.image_labels[IMAGE_PACKAGES_LABEL] = "tree jq"
        self.assertEqual(ops.state()["rebuildReasons"], [])

        # An image without the label was built without any.
        del self.runner.image_labels[IMAGE_PACKAGES_LABEL]
        self.assertEqual(ops.state()["rebuildReasons"], ["packages"])


class CommandTests(CliTestCase):
    """
    ``hardcode packages`` and ``hardcode package add|remove``.
    """

    def test_add_list_and_remove(self):
        code, out, _ = self.run_cli("package", "add", "jq", "postgresql-client")
        self.assertEqual(code, 0)
        self.assertIn("added packages jq postgresql-client", out)

        code, out, _ = self.run_cli("packages")
        self.assertEqual((code, out.splitlines()), (0, ["jq", "postgresql-client"]))

        code, out, _ = self.run_cli("package", "remove", "jq")
        self.assertEqual(code, 0)
        self.assertIn("removed package jq", out)
        self.assertEqual(self.run_cli("packages")[1], "postgresql-client")

    def test_none_yet(self):
        code, out, _ = self.run_cli("packages")

        self.assertEqual(code, 0)
        self.assertIn("no Ubuntu packages of its own", out)

    def test_json(self):
        self.run_cli("package", "add", "jq")

        code, out, _ = self.run_cli("--json", "packages")
        listed = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(listed["packages"], ["jq"])
        self.assertEqual(listed["image"], [], "no image yet")
        self.assertIs(listed["rebuildNeeded"], False, "the first start builds it")

        code, out, _ = self.run_cli("--json", "package", "add", "tree")
        result = json.loads(out)["results"][0]
        self.assertEqual((code, result["profile"], result["changed"]), (0, "default", True))

    def test_a_built_image_with_other_packages_says_a_rebuild_is_due(self):
        runner = FakeRunner()
        runner.image_labels = {IMAGE_LANGUAGES_LABEL: EVERY_LANGUAGE, IMAGE_PACKAGES_LABEL: "jq"}

        self.run_cli("package", "add", "tree")
        code, out, _ = self.run_cli("packages", runner=runner)

        self.assertEqual(code, 0)
        self.assertIn("a rebuild is due (the Ubuntu packages changed): hardcode -p default rebuild", out)

        _, out, _ = self.run_cli("languages", runner=runner)
        self.assertIn("rebuild", out, "languages says so too")
        _, out, _ = self.run_cli("show", runner=runner)
        self.assertIn("packages:  tree", out)
        self.assertIn("image:     a rebuild is due (the Ubuntu packages changed)", out)

    def test_a_bad_name_is_an_error(self):
        code, _, err = self.run_cli("package", "add", "Not_A_Package")

        self.assertEqual(code, 1)
        self.assertIn("not an Ubuntu package name", err)
        self.assertIn("no Ubuntu packages of its own", self.run_cli("packages")[1], "nothing was written")

    def test_every_profile_at_once(self):
        self.run_cli("profile", "add", "other", "--lang", "")

        code, out, _ = self.run_cli("--all", "package", "add", "jq")

        self.assertEqual(code, 0)
        self.assertIn("[default] added package jq", out)
        self.assertIn("[other] added package jq", out)
