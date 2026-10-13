"""
Tests for creating, finding and removing profiles (hardcode_cli.profiles).
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

from hardcode_cli import envfile, override, profiles, yamlio
from hardcode_cli.profiles import Profile, ProfileError

from .helpers import REPO, HomeTestCase

BROKEN_OVERRIDE = "services:\n  gateway:\n    ports: [\n"


class NamingTests(unittest.TestCase):
    def test_valid_names(self):
        # The admin's and the CRD's rule, except that a leading digit is
        # fine here: Compose has no Service names to satisfy.
        for name in ["default", "old-node", "a", "x1-2-3", "a" * 32, "2fa"]:
            self.assertTrue(profiles.valid_name(name), name)

        for name in ["Bad_Name", "-x", "x-", "", "a" * 33, "with space", "UPPER", "_template"]:
            self.assertFalse(profiles.valid_name(name), name)

    def test_profile_paths(self):
        profile = Profile("x", Path("/r/profiles/x"))

        self.assertEqual(profile.env_file, Path("/r/profiles/x/.env"))
        self.assertEqual(profile.allowlist_file, Path("/r/profiles/x/allowlist.txt"))
        self.assertEqual(profile.proxy_allowlist_file, Path("/r/profiles/x/proxy-allowlist.txt"))
        self.assertEqual(profile.override_file, Path("/r/profiles/x/compose.override.yaml"))
        self.assertEqual(profile.project_name, "hardcode-x")
        self.assertEqual(profile.vscode_tag, "x")

        elsewhere = Profile("x", Path("/r/profiles/x"), "0a1b2c")
        self.assertEqual(elsewhere.project_name, "hardcode-0a1b2c-x")
        self.assertEqual(elsewhere.vscode_tag, "0a1b2c-x")


class ProfileTests(HomeTestCase):
    def profile(self, name):
        return profiles.get(self.root, name)

    def make(self, name, source="", languages=None):
        """
        Create profile *name*: from the template with *languages* (none by
        default), or as a copy of *source*.
        """
        if not source and languages is None:
            languages = []
        return profiles.create(self.root, name, source, languages=languages)

    def copy_of(self, source, name="copy"):
        """
        Create profile *name* from *source* and return it.
        """
        return self.profile(self.make(name, source).name)

    # -- finding --------------------------------------------------------------

    def test_a_home_starts_empty_and_nothing_is_made_by_itself(self):
        self.assertEqual(profiles.list_profiles(self.root), [])
        self.assertEqual(profiles.list_profiles(self.root / "nowhere"), [])

        with self.assertRaisesRegex(ProfileError, "no profiles yet .*hardcode profile add default --lang"):
            self.profile("default")
        self.assertEqual(profiles.list_profiles(self.root), [], "asking for one doesn't make it")

        self.make("default")
        self.assertEqual(profiles.list_profiles(self.root), ["default"])

    def test_get_rejects_unknown_and_bad_names(self):
        self.make("default")
        default = self.profile("default")
        self.assertTrue(default.allowlist_file.exists())
        self.assertEqual(envfile.get(default.env_file, "VSCODE_PORT"), "11111")

        with self.assertRaisesRegex(ProfileError, "no profile 'missing'"):
            self.profile("missing")
        with self.assertRaisesRegex(ProfileError, "not a valid"):
            self.profile("Bad Name")

    def test_get_restores_missing_files_from_template(self):
        self.make("default")
        self.profile("default").allowlist_file.unlink()

        self.assertTrue(self.profile("default").allowlist_file.exists())

    def test_the_proxys_allowlist_is_there_but_written_only_where_it_is_given(self):
        self.make("default")
        default = self.profile("default")
        given = default.proxy_allowlist_file.read_text(encoding="utf-8")
        self.assertIn("update.code.visualstudio.com", given, "every profile's entries")

        # Compose mounts it, so a missing one comes back...
        default.proxy_allowlist_file.unlink()
        self.assertEqual(self.profile("default").proxy_allowlist_file.read_text(encoding="utf-8"), given)

        # ...but one that is there stays what the running proxy was given,
        # until something gives the proxy the new one too.
        default.allowlist_file.write_text(
            default.allowlist_file.read_text(encoding="utf-8") + "hand.example\n", encoding="utf-8"
        )
        self.assertEqual(self.profile("default").proxy_allowlist_file.read_text(encoding="utf-8"), given)

        before, after = profiles.write_proxy_allowlist(default)
        self.assertEqual(before, given)
        self.assertIn("\nhand.example\n", after)
        if os.name != "nt":
            self.assertEqual(default.proxy_allowlist_file.stat().st_mode & 0o777, 0o644)

    def test_a_restored_env_gets_a_port_and_subnet_of_its_own(self):
        self.make("default")
        self.make("work")
        self.profile("work").env_file.unlink()

        env = envfile.read(self.profile("work").env_file)

        self.assertEqual(env["VSCODE_PORT"], "11112")
        self.assertEqual(env["DEVNET_SUBNET"], "172.31.252.0/24")
        self.assertEqual(env["DNS_IP"], "172.31.252.53")

    # -- the .env: only what differs ------------------------------------------

    def test_a_new_env_sets_only_the_port_the_subnet_and_the_languages(self):
        self.make("default", languages=["python", "docker"])
        text = self.profile("default").env_file.read_text(encoding="utf-8")

        settings = [line for line in text.splitlines() if line and not line.startswith("#")]
        self.assertEqual(
            settings,
            [
                "VSCODE_PORT=11111",
                "DEVNET_SUBNET=172.31.253.0/24",
                "DNS_IP=172.31.253.53",
                "INSTALL_PYTHON=true",
                "INSTALL_DOCKER=true",
            ],
        )
        self.assertTrue(text.startswith(profiles.ENV_HEADER))
        self.assertTrue(text.rstrip().endswith("#DEV_USER=dev"), "then the reference of every setting")
        self.assertLess(text.index("INSTALL_DOCKER=true"), text.index(envfile.REFERENCE))

        # A setting written later goes with the others, above the reference.
        envfile.set_value(self.profile("default").env_file, "VSCODE_MEMORY", "12g")
        text = self.profile("default").env_file.read_text(encoding="utf-8")
        self.assertLess(text.index("VSCODE_MEMORY=12g"), text.index(envfile.REFERENCE))

        envfile.unset(self.profile("default").env_file, "VSCODE_MEMORY")
        self.assertNotIn("VSCODE_MEMORY=", self.profile("default").env_file.read_text(encoding="utf-8"))

    # -- ports and subnets ----------------------------------------------------

    def test_create_allocates_port_and_subnet(self):
        created = self.make("default")
        self.assertEqual((created.port, created.subnet, created.source), (11111, "172.31.253.0/24", ""))

        second = self.make("old-node", "default")
        self.assertEqual((second.port, second.subnet, second.source), (11112, "172.31.252.0/24", "default"))

        env = envfile.read(self.profile("old-node").env_file)
        self.assertEqual(
            (env["VSCODE_PORT"], env["DEVNET_SUBNET"], env["DNS_IP"]),
            ("11112", "172.31.252.0/24", "172.31.252.53"),
        )
        self.assertEqual(profiles.list_profiles(self.root), ["default", "old-node"])

    def test_unset_keys_occupy_the_defaults(self):
        self.make("default")
        self.profile("default").env_file.write_text("VSCODE_PORT=8080\n", encoding="utf-8")

        created = self.make("next")

        self.assertEqual((created.port, created.subnet), (11111, "172.31.252.0/24"))

    def test_a_subnet_dockers_networks_use_is_skipped(self):
        created = profiles.create(
            self.root,
            "default",
            languages=[],
            taken_subnets=["172.31.253.128/25", "172.31.0.0/23", "fd00::/64", "not a subnet"],
        )
        self.assertEqual(created.subnet, "172.31.252.0/24", "one that overlaps any part of it is taken")

        with self.assertRaisesRegex(ProfileError, "no free internal subnet"):
            profiles.create(self.root, "full", languages=[], taken_subnets=["172.31.0.0/16"])

    def test_allocate_avoids_other_profiles_forwards(self):
        self.make("default")
        override.add_forward(self.profile("default").override_file, 11112)
        override.add_forward(self.profile("default").override_file, 11113, "docker", 80)

        self.assertEqual(profiles.reserved_ports(self.root), {11111, 11112, 11113})
        self.assertEqual(profiles.reserved_ports(self.root, exclude="default"), set())
        self.assertEqual(
            self.make("next").port,
            11114,
            "a forward's host port is as taken as a VS Code port",
        )

    def test_ports_by_profile_names_each_owner_and_survives_a_broken_override(self):
        for name in ("default", "a", "b"):
            self.make(name)
        override.add_forward(self.profile("a").override_file, 3000)
        self.profile("b").override_file.write_text(BROKEN_OVERRIDE, encoding="utf-8")
        self.profile("b").env_file.write_text("VSCODE_PORT=11113\n", encoding="utf-8")

        warnings: list[str] = []
        ports = profiles.ports_by_profile(self.root, exclude="default", warn=warnings.append)

        self.assertEqual(
            ports,
            {"a": {11112, 3000}, "b": {11113}},
            "the broken override's forwards are unknown; its VS Code port still counts",
        )
        self.assertEqual(len(warnings), 1)
        self.assertIn(str(Path("profiles") / "b" / "compose.override.yaml"), warnings[0])

        with self.assertRaises(yamlio.YamlError):
            profiles.ports_by_profile(self.root, exclude="default")

        self.assertEqual(
            profiles.create(self.root, "c", languages=[], warn=warnings.append).port,
            11114,
            "profile add still works around it",
        )

    def test_allocate_runs_out(self):
        self.make("default")

        with self.assertRaisesRegex(ProfileError, "no free port"):
            profiles.allocate(self.root, last=11111)
        self.assertEqual(profiles.allocate(self.root, last=11112)[0], 11112)

    def test_allocate_skips_gaps(self):
        self.make("default")
        self.make("b")
        profiles.remove(self.root, "default")

        self.assertEqual(profiles.allocate(self.root), (11111, "172.31.253.0/24"))

    # -- one change at a time -------------------------------------------------

    def test_a_second_change_waits_for_the_first(self):
        self.make("default")
        profile = self.profile("default")
        said: list[str] = []

        with profiles.lock(profile):
            # Another command, while this one holds the lock: it says it is
            # waiting, and gives up after its timeout.
            with self.assertRaisesRegex(ProfileError, "still being changed by another command"):
                with profiles.lock(profile, waiting=said.append, timeout=0.5):
                    self.fail("got the lock while it was held")

        self.assertEqual(said, ["waiting for another change to profile 'default' to finish..."])

        # Once the first lets go, the next gets it straight away.
        with profiles.lock(profile, timeout=0.5):
            pass

    def test_the_lock_follows_the_process_that_holds_it(self):
        self.make("default")
        profile = self.profile("default")
        holder = (
            "import sys, time; sys.path.insert(0, sys.argv[1]);"
            "from pathlib import Path; from hardcode_cli import profiles;"
            "p = profiles.Profile('default', Path(sys.argv[2]));"
            "ctx = profiles.lock(p); ctx.__enter__(); print('held', flush=True); time.sleep(30)"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", holder, str(REPO / "cli"), str(profile.dir)],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "held")
            with self.assertRaises(ProfileError):
                with profiles.lock(profile, timeout=0.3):
                    pass
        finally:
            process.kill()
            process.wait()
            process.stdout.close()

        # Killed while holding it: the lock went with it.
        with profiles.lock(profile, timeout=2):
            pass

    # -- the extension list ---------------------------------------------------

    def test_every_profile_has_a_current_extension_list(self):
        self.make("default", languages=["claude_cli"])
        profile = self.profile("default")
        self.assertIn("anthropic.claude-code", profile.extensions_file.read_text(encoding="utf-8"))

        # Edited behind the CLI's back (by hand, or by a catalogue that changed
        # with an upgrade): the next command writes it again.
        envfile.set_value(profile.env_file, "VSCODE_EXTENSIONS", "me.mine")
        self.assertIn("me.mine", self.profile("default").extensions_file.read_text(encoding="utf-8"))

        # Unchanged, it isn't rewritten: the container's watcher sees no change.
        before = profile.extensions_file.stat().st_mtime_ns
        self.profile("default")
        self.assertEqual(profile.extensions_file.stat().st_mtime_ns, before)

    # -- languages ------------------------------------------------------------

    def test_a_new_profile_needs_its_languages(self):
        with self.assertRaisesRegex(ProfileError, "needs its languages and tools: --lang"):
            profiles.create(self.root, "default")
        self.assertFalse(profiles.exists(self.root, "default"))

    def test_create_around_chosen_languages(self):
        created = self.make("py", languages=["python", "docker"])

        self.assertEqual(created.languages, ("python", "docker"))

        env = envfile.read(self.profile("py").env_file)
        self.assertEqual((env["INSTALL_PYTHON"], env["INSTALL_DOCKER"]), ("true", "true"))
        self.assertNotIn("INSTALL_GO", env, "off is unset, compose.yaml's default")

        # The proxy gets their entries; the profile's own allowlist is the
        # template's, untouched.
        given = self.profile("py").proxy_allowlist_file.read_text(encoding="utf-8")
        self.assertIn("\npypi.org\n", given)
        self.assertNotIn("proxy.golang.org", given)

        template = (self.stack / "template" / "allowlist.txt").read_text(encoding="utf-8")
        self.assertEqual(self.profile("py").allowlist_file.read_text(encoding="utf-8"), template)

    def test_create_without_languages_keeps_the_sources(self):
        self.make("go", languages=["go"])

        copy = profiles.create(self.root, "copy", source="go")

        self.assertEqual(copy.languages, ("go",))

    # -- copying --------------------------------------------------------------

    def test_create_copies_the_source_files(self):
        self.make("default")
        source = self.profile("default")
        source.allowlist_file.write_text(
            source.allowlist_file.read_text(encoding="utf-8") + "github.com/x\n", encoding="utf-8"
        )
        source.override_file.write_text("services:\n  vscode:\n    volumes:\n      - /a:/b\n", encoding="utf-8")

        copy = self.copy_of("default")

        self.assertIn("github.com/x", copy.allowlist_file.read_text(encoding="utf-8"))
        self.assertIn("/a:/b", copy.override_file.read_text(encoding="utf-8"))
        if os.name != "nt":
            self.assertEqual(copy.allowlist_file.stat().st_mode & 0o777, 0o644)

    def test_create_copies_settings_but_not_the_token(self):
        self.make("default")
        envfile.set_value(self.profile("default").env_file, "VSCODE_CONNECTION_TOKEN", "fixed-secret")
        envfile.set_value(self.profile("default").env_file, "VSCODE_MEMORY", "12g")

        copy = self.copy_of("default")

        env = envfile.read(copy.env_file)
        self.assertNotIn("VSCODE_CONNECTION_TOKEN", env, "a copy gets its own token")
        self.assertEqual(env["VSCODE_MEMORY"], "12g")
        if os.name != "nt":
            self.assertEqual(copy.env_file.stat().st_mode & 0o777, 0o600, ".env can hold a token")
            self.assertEqual(copy.allowlist_file.stat().st_mode & 0o777, 0o644)

    def test_create_copies_mounts_but_not_forwards(self):
        self.make("default")
        source = self.profile("default")
        source.override_file.write_text(
            "# header\n"
            "services:\n"
            "  vscode:\n"
            "    volumes:\n"
            "      - /a:/b\n"
            "  gateway:\n"
            "    environment:\n"
            "      GATEWAY_FORWARDS: 8000:vscode:8000 3000:vscode:3000\n"
            "    ports:\n"
            "      - 127.0.0.1:3000:3000\n",
            encoding="utf-8",
        )

        copy = self.copy_of("default")

        _, document = override.load(copy.override_file)
        self.assertEqual(override.get_forwards(document), [])
        self.assertNotIn("gateway", document["services"])
        self.assertEqual([mount.local for mount in override.get_mounts(document)], ["/a"])
        self.assertTrue(copy.override_file.read_text(encoding="utf-8").startswith("# header\n"))

        _, source_document = override.load(source.override_file)
        self.assertEqual(len(override.get_forwards(source_document)), 1, "the source keeps its forward")

    def test_create_falls_back_to_template_for_missing_source_files(self):
        self.make("default")
        self.profile("default").override_file.unlink()

        copy = self.copy_of("default")

        self.assertTrue(copy.override_file.exists())

    def test_create_errors(self):
        with self.assertRaisesRegex(ProfileError, "not a valid"):
            self.make("Bad_Name")

        self.make("x")
        with self.assertRaisesRegex(ProfileError, "already exists"):
            self.make("x")
        with self.assertRaisesRegex(ProfileError, "copy from"):
            self.make("y", "nope")

    # -- removing -------------------------------------------------------------

    def test_remove(self):
        self.make("x")

        profiles.remove(self.root, "x")

        self.assertFalse(profiles.exists(self.root, "x"))
        with self.assertRaisesRegex(ProfileError, "no profile"):
            profiles.remove(self.root, "x")

    def test_a_profile_removed_under_its_lock_keeps_the_lock_until_it_is_released(self):
        # `hardcode profile remove` holds the profile's lock while it removes
        # the profile. Windows can't delete a file that is open, so the lock
        # file stays until the lock is let go, and goes with the folder then.
        self.make("x")
        profile = self.profile("x")
        lock_file = profile.dir / ".lock"

        with profiles.lock(profile):
            profiles.remove(self.root, "x")

            self.assertFalse(profiles.exists(self.root, "x"))
            self.assertTrue(lock_file.exists(), "the lock file is open: not deleted")
            self.assertEqual([path.name for path in profile.dir.iterdir()], [".lock"])

        self.assertFalse(profile.dir.exists(), "released, the lock file goes, and the folder with it")


if __name__ == "__main__":
    unittest.main()
