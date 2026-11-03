"""
Tests for the operations behind the commands (hardcode_cli.ops).
"""

import os
import sys
import unittest
from unittest import mock

from hardcode_cli import allowlist, envfile, langs, override, profiles
from hardcode_cli.compose import ComposeError
from hardcode_cli.ops import STATUS_BYTES, Image, Ops

from .helpers import FakeRunner, HomeTestCase, updated_catalogue
from .resolver import FakeResolver


def python_in_image(state: dict):
    """
    Whether the state says Python is in the image: True, False or None.
    """
    return next(lang for lang in state["langs"] if lang["key"] == "python")["inImage"]


class OpsTests(HomeTestCase):
    """
    The operations, with ``docker compose`` faked: what they write, and what
    they ask Compose to do.
    """

    def ops(self, name="default", running=False, healthy=True, answers=None):
        """
        Operations on profile *name* (created when missing), with a fresh fake
        runner that reports the profile as running or not, and name lookups
        that give *answers* (``{name: [address, ...]}``).
        """
        if not profiles.exists(self.root, name):
            profiles.create(self.root, name, languages=list(langs.LANGS))

        self.runner = FakeRunner(running=running, healthy=healthy)
        self.resolver = FakeResolver(answers)
        return Ops(
            self.root,
            profiles.get(self.root, name),
            runner=self.runner,
            docker="docker",
            sleep=lambda _: None,
            resolve=self.resolver,
        )

    def make_folder(self, name="src"):
        # Beside the home, not in it: a folder in HardCode's home is never mounted.
        folder = self.root.parent / name
        folder.mkdir()
        return folder

    # -- allowlist ------------------------------------------------------------

    def test_allow_appends_and_reports_not_running(self):
        ops = self.ops()

        result = ops.allow("github.com/me")

        self.assertEqual((result.message, result.changed, result.applied), ("allowed github.com/me", True, False))
        self.assertIn("github.com/me", ops.profile.allowlist_file.read_text(encoding="utf-8"))
        if os.name != "nt":
            self.assertEqual(ops.profile.allowlist_file.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.runner.commands("allowlist squid"), [], "nothing reloaded when stopped")

    def test_provided_entries_are_switched_not_deleted(self):
        ops = self.ops(running=True)

        allowed = ops.allow("crates.io")
        self.assertEqual(
            (allowed.message, allowed.changed), ("crates.io is already allowed (it comes with Rust)", False)
        )

        denied = ops.deny("crates.io")
        self.assertEqual((denied.message, denied.applied), ("denied crates.io", True))
        self.assertIn("\noff crates.io\n", ops.profile.allowlist_file.read_text(encoding="utf-8"))
        self.assertIn("\n#crates.io ", ops.profile.proxy_allowlist_file.read_text(encoding="utf-8"))
        self.assertFalse(next(item for item in ops.allowlist_entries() if item.entry == "crates.io").enabled)

        with self.assertRaisesRegex(allowlist.AllowlistError, "comes with Rust and can't be deleted"):
            ops.remove_entry("crates.io")

        enabled = ops.set_entry_enabled("crates.io", True)
        self.assertEqual(enabled.message, "enabled crates.io")
        self.assertNotIn("off crates.io", ops.profile.allowlist_file.read_text(encoding="utf-8"))
        self.assertEqual(len(self.runner.commands("up -d allowlist squid")), 2, "each change reloaded the proxy")

    def test_allow_reloads_a_running_profile(self):
        ops = self.ops(running=True)

        result = ops.allow("github.com/me", "rw")

        self.assertEqual(result.message, "allowed github.com/me (rw)")
        self.assertTrue(result.applied)
        self.assertEqual(len(self.runner.commands("up -d allowlist squid")), 1)

    def test_allow_reports_unhealthy_reload(self):
        ops = self.ops(running=True, healthy=False)

        with self.assertRaisesRegex(ComposeError, "written, but squid didn't come back healthy"):
            ops.allow("x.org")
        self.assertIn(
            "x.org",
            ops.profile.allowlist_file.read_text(encoding="utf-8"),
            "the edit is kept; what failed is the reload",
        )

        with self.assertRaisesRegex(ComposeError, "didn't come back healthy"):
            ops.reload()

    def test_allow_idempotent(self):
        ops = self.ops()
        ops.allow("x.org")

        again = ops.allow("x.org")

        self.assertFalse(again.changed)
        self.assertIn("already allowed", again.message)

    def test_allow_invalid(self):
        with self.assertRaises(allowlist.AllowlistError):
            self.ops().allow("com")

    def test_deny_and_reenable(self):
        ops = self.ops()
        ops.allow("x.org")

        def entry():
            return allowlist.find(ops.profile.allowlist_file.read_text(encoding="utf-8"), "x.org")[0]

        self.assertEqual(ops.deny("x.org").message, "denied x.org")
        self.assertFalse(entry().enabled)
        self.assertFalse(ops.deny("x.org").changed, "already inactive")
        self.assertFalse(ops.deny("absent.org").changed)

        ops.allow("x.org")
        self.assertTrue(entry().enabled)

    def test_deny_targets_the_https_or_the_ssh_entry(self):
        ops = self.ops()
        ops.allow("github.com")
        ops.allow("github.com", "ssh")

        self.assertEqual(ops.deny("github.com").message, "denied github.com")
        text = ops.profile.allowlist_file.read_text(encoding="utf-8")
        self.assertTrue(allowlist.find(text, "github.com", ssh=True)[0].enabled, "the SSH entry is still on")
        self.assertFalse(ops.deny("github.com").changed, "already denied")

        self.assertEqual(ops.deny("github.com", ssh=True).message, "denied github.com (ssh)")
        self.assertEqual(ops.remove_entry("github.com", ssh=True).message, "removed github.com (ssh)")
        self.assertEqual(ops.set_entry_enabled("github.com", True).message, "enabled github.com")

    def test_entry_enable_disable_remove(self):
        ops = self.ops()
        ops.allow("x.org")

        self.assertEqual(ops.set_entry_enabled("x.org", False).message, "disabled x.org")
        self.assertFalse(ops.set_entry_enabled("x.org", False).changed)
        self.assertEqual(ops.set_entry_enabled("x.org", True).message, "enabled x.org")
        self.assertFalse(ops.set_entry_enabled("absent.org", True).changed)

        self.assertEqual(ops.remove_entry("x.org").message, "removed x.org")
        self.assertFalse(ops.remove_entry("x.org").changed)
        self.assertEqual(allowlist.find(ops.profile.allowlist_file.read_text(encoding="utf-8"), "x.org"), [])

    def test_reload(self):
        ops = self.ops()
        quiet = ops.reload()
        self.assertEqual((quiet.message, quiet.changed, quiet.applied), ("profile isn't running", False, False))

        # A hand edit reaches the proxy's file, and the reload says what it changes...
        with open(ops.profile.allowlist_file, "a", encoding="utf-8") as handle:
            handle.write("hand.example\n")

        edited = ops.reload()
        self.assertEqual(edited.message, "allowlist: Custom: + hand.example; profile isn't running")
        self.assertEqual(edited.data["changes"], ["Custom: + hand.example"])
        self.assertIn("\nhand.example\n", ops.profile.proxy_allowlist_file.read_text(encoding="utf-8"))

        # ...as does an update of HardCode's provided entries.
        with mock.patch.object(langs, "CATALOGUE", updated_catalogue(added=["newrustsite.com"])):
            updated = ops.reload()
        self.assertEqual(updated.data["changes"], ["Rust: + newrustsite.com"])

        running = self.ops(running=True).reload()
        self.assertTrue(running.applied)

        # Every container of the two services goes first, a leftover from a
        # recreate that was cut short included, then both start fresh.
        reload_steps = [command for command in map(" ".join, self.runner.calls) if "allowlist squid" in command]
        self.assertEqual(len(reload_steps), 2)
        self.assertIn("rm --stop --force allowlist squid", reload_steps[0])
        self.assertIn("up -d allowlist squid", reload_steps[1])

    def test_rebuild(self):
        ops = self.ops(running=True)
        profiles.mark_proxy_allowlist_applied(ops.profile)  # the proxy has the file, as after a start
        first = ops.rebuild()

        self.assertEqual(first.message, "VS Code image rebuilt")
        self.assertTrue(first.applied)
        self.assertEqual(len(self.runner.commands(" build vscode")), 1)
        self.assertEqual(self.runner.commands("up -d allowlist squid"), [], "nothing new for the proxy")

        # A running proxy nobody saw load the file is given it.
        ops.profile.proxy_applied_file.unlink()
        ops.rebuild()
        self.assertEqual(len(self.runner.commands("up -d allowlist squid")), 1)
        self.runner.calls.clear()

        # The proxy's allowlist is brought up to date too, as at every start,
        # and the running proxy given it.
        with mock.patch.object(langs, "CATALOGUE", updated_catalogue(added=["newrustsite.com"])):
            updated = ops.rebuild()

        self.assertEqual(updated.message, "VS Code image rebuilt; allowlist: Rust: + newrustsite.com")
        self.assertEqual(len(self.runner.commands("up -d allowlist squid")), 1)

        self.assertIsNotNone(self.ops().rebuild())

    def test_state(self):
        local = self.make_folder()
        ops = self.ops()
        ops.allow("x.org", "rw")
        ops.forward(3000)
        ops.mount(str(local), "work")
        ops.set_language("dotnet", False, build=False)

        state = ops.state()

        self.assertEqual(state["profile"], "default")
        self.assertEqual(state["vscodePort"], "11111")

        entry = next(item for item in state["allowlist"] if item["entry"] == "x.org")
        self.assertEqual(
            (entry["flag"], entry["enabled"], entry["kind"], entry["section"]),
            ("rw", True, "host", "Custom"),
        )

        self.assertEqual(state["forwards"], [{"port": 3000, "host": "vscode", "target": 3000}])
        self.assertEqual(state["mounts"][0]["remote"], "/home/dev/work")

        dotnet = next(lang for lang in state["langs"] if lang["key"] == "dotnet")
        self.assertFalse(dotnet["enabled"])

    def test_services_and_blocked(self):
        ops = self.ops(running=True)

        self.assertEqual([service["service"] for service in ops.services()], ["squid", "vscode"])

        self.assertEqual(ops.blocked("2h"), [])
        logs = self.runner.commands("logs")
        self.assertTrue(any("logs --since 2h --no-log-prefix squid" in command for command in logs))

        # A window the app doesn't offer falls back to the default.
        ops.blocked("bogus")
        logs = self.runner.commands("logs")
        self.assertTrue(any("--since 30m" in command for command in logs))

    def test_status_says_why_compose_could_not_list_the_containers(self):
        ops = self.ops(running=True)
        self.assertEqual(ops.status()["error"], "")
        self.assertTrue(ops.status()["docker"])

        # The daemon isn't running: not the same as "never created".
        self.runner.fail_on.add("ps -a")
        status = ops.status()

        self.assertEqual(status["services"], [])
        self.assertIn("simulated failure", status["error"])

    def test_logs(self):
        ops = self.ops(running=True)
        self.runner.log_output = "a line\n"

        self.assertEqual(ops.logs(["vscode"], 30), "a line\n")
        self.assertIn("logs --no-color --tail 30 --no-log-prefix vscode", self.runner.commands("logs")[-1])

        ops.logs(["squid", "coredns"])
        self.assertTrue(self.runner.commands("logs")[-1].endswith("logs --no-color --tail 200 squid coredns"))

        self.runner.fail_on.add("logs")
        with self.assertRaisesRegex(ComposeError, "logs .* failed"):
            ops.logs(["vscode"])

    # -- forwards -------------------------------------------------------------

    def test_forward_writes_override_and_recreates_gateway_when_running(self):
        ops = self.ops(running=True)

        result = ops.forward(3000)

        self.assertEqual(result.message, "forwarding localhost:3000 -> vscode:3000")
        self.assertTrue(result.applied)
        self.assertEqual(len(self.runner.commands("up -d gateway")), 1)
        self.assertTrue(self.runner.commands("config --quiet"), "the file is validated with compose")

        _, document = override.load(ops.profile.override_file)
        self.assertEqual(override.get_forwards(document)[0].port, 3000)

        self.assertFalse(ops.forward(3000).changed)

    def test_forward_refuses_vscode_port_and_other_profiles_ports(self):
        ops = self.ops()
        with self.assertRaisesRegex(override.OverrideError, "VS Code port"):
            ops.forward(8000)

        other = self.ops("other")
        other.forward(3000)
        envfile.set_value(other.profile.env_file, "VSCODE_PORT", "8081")

        ops = self.ops()
        with self.assertRaisesRegex(override.OverrideError, "used by profile 'other'"):
            ops.forward(3000)
        with self.assertRaisesRegex(override.OverrideError, "used by profile 'other'"):
            ops.forward(8081)

    def test_forward_names_every_profile_holding_the_port(self):
        profiles.create(self.root, "x", languages=[])
        profiles.create(self.root, "y", languages=[])

        # With VSCODE_PORT unset, both use compose.yaml's default of 11111.
        profiles.get(self.root, "x").env_file.write_text("DEV_USER=dev\n", encoding="utf-8")
        profiles.get(self.root, "y").env_file.write_text("DEV_USER=dev\n", encoding="utf-8")

        ops = self.ops("z")
        with self.assertRaisesRegex(override.OverrideError, "port 11111 is used by profile 'x', 'y'"):
            ops.forward(11111)

    def test_forward_warns_about_an_unreadable_override_and_goes_on(self):
        ops = self.ops()
        profiles.create(self.root, "broken", languages=[])
        profiles.get(self.root, "broken").override_file.write_text(
            "services:\n  gateway:\n    ports: [\n", encoding="utf-8"
        )

        warnings: list[str] = []
        ops.warn = warnings.append

        self.assertEqual(ops.forward(3000).message, "forwarding localhost:3000 -> vscode:3000")
        self.assertEqual(len(warnings), 1)
        self.assertIn("compose.override.yaml", warnings[0])

    def test_forward_restores_file_when_compose_rejects_it(self):
        ops = self.ops()
        ops.forward(3000)
        before = ops.profile.override_file.read_text(encoding="utf-8")

        self.runner.fail_on.add("config --quiet")
        with self.assertRaisesRegex(override.OverrideError, "restored"):
            ops.forward(3001)

        self.assertEqual(ops.profile.override_file.read_text(encoding="utf-8"), before)

    def test_a_forward_the_gateway_cannot_take_is_undone(self):
        # Compose accepts the file, but the gateway can't start with it (the
        # port is taken on this machine). The old file goes back and the
        # gateway is started again from it, VS Code's port with it.
        ops = self.ops(running=True)
        before = ops.profile.override_file.read_text(encoding="utf-8")
        self.runner.fail_on.add("up -d gateway")

        warnings: list[str] = []
        ops.warn = warnings.append

        with self.assertRaisesRegex(ComposeError, "simulated failure for up -d gateway; the change was undone"):
            ops.forward(3000)

        self.assertEqual(ops.profile.override_file.read_text(encoding="utf-8"), before)
        self.assertEqual(len(self.runner.commands("up -d gateway")), 2, "with the change, then as before")
        self.assertIn("couldn't be started again as before", warnings[0], "the fake fails both times")

    def test_an_override_edit_is_undone_when_it_cannot_be_checked(self):
        local = self.make_folder()
        ops = self.ops()
        before = ops.profile.override_file.read_text(encoding="utf-8")

        with mock.patch.object(ops.compose, "config_check", side_effect=ComposeError("docker not found on the PATH")):
            with self.assertRaisesRegex(ComposeError, "docker not found"):
                ops.mount(str(local), "work")

        self.assertEqual(ops.profile.override_file.read_text(encoding="utf-8"), before)

    def test_unforward(self):
        ops = self.ops()
        ops.forward(3000)

        self.assertEqual(ops.unforward(3000).message, "forward for port 3000 removed")
        with self.assertRaises(override.OverrideError):
            ops.unforward(3000)

    # -- mounts ---------------------------------------------------------------

    def test_mount_and_unmount(self):
        local = self.make_folder()
        ops = self.ops(running=True)
        envfile.set_value(ops.profile.env_file, "DEV_USER", "dev")

        result = ops.mount(str(local), "work", read_only=True)

        self.assertIn("at /home/dev/work (read-only)", result.message, "DEV_USER from .env is used")
        self.assertEqual(len(self.runner.commands("up -d vscode docker")), 1)

        _, document = override.load(ops.profile.override_file)
        self.assertEqual(document["services"]["docker"]["volumes"], document["services"]["vscode"]["volumes"])

        self.assertEqual(ops.unmount("work").message, "mount at /home/dev/work removed")
        with self.assertRaises(override.OverrideError):
            ops.unmount("work")

    def test_mount_without_dev_user_uses_compose_yamls_default(self):
        local = self.make_folder()
        ops = self.ops()
        ops.profile.env_file.write_text("VSCODE_PORT=11111\n", encoding="utf-8")

        self.assertIn("at /home/dev/work", ops.mount(str(local), "work").message)

    def test_mount_creates_the_override_from_the_template(self):
        local = self.make_folder()
        ops = self.ops()
        ops.profile.override_file.unlink()

        ops.mount(str(local), "work")

        self.assertTrue(ops.profile.override_file.read_text(encoding="utf-8").startswith("# This profile's mounts"))

    # -- languages ------------------------------------------------------------

    def test_set_language_edits_env_builds_and_recreates(self):
        ops = self.ops(running=True)

        result = ops.set_language("c#", False)

        self.assertEqual(result.message, "dotnet off")
        self.assertNotIn("INSTALL_DOTNET", envfile.read(ops.profile.env_file), "off is unset")
        self.assertEqual(len(self.runner.commands(" build vscode")), 1)
        self.assertEqual(len(self.runner.commands("up -d vscode")), 1)
        self.assertTrue(result.applied)

    def test_set_language_without_build(self):
        ops = self.ops()

        result = ops.set_language("python", True, build=False)

        # Python's allowlist block was added; it applies when the profile starts.
        self.assertFalse(result.applied)
        self.assertEqual(self.runner.commands(" build"), [])
        self.assertEqual(envfile.read(ops.profile.env_file)["INSTALL_PYTHON"], "true")

    def test_languages_switch_the_proxys_entries_and_reload(self):
        ops = self.ops(running=True)
        own = ops.profile.allowlist_file.read_text(encoding="utf-8")

        result = ops.set_languages({"python": True, "go": False}, build=False)

        self.assertEqual(result.message, "python on, go off (the image is rebuilt with: hardcode rebuild)")
        self.assertTrue(result.applied, "the proxy reloaded with the new entries")
        self.assertEqual(len(self.runner.commands("up -d allowlist squid")), 1)
        self.assertEqual(self.runner.commands(" build"), [])

        given = ops.profile.proxy_allowlist_file.read_text(encoding="utf-8")
        self.assertIn("\npypi.org\n", given)
        self.assertNotIn("proxy.golang.org", given)
        self.assertEqual(
            ops.profile.allowlist_file.read_text(encoding="utf-8"), own, "the profile's own allowlist isn't touched"
        )

        again = ops.set_languages({"python": True, "go": False}, build=False)
        self.assertFalse(again.changed)

    def test_extensions_of_the_profiles_own(self):
        ops = self.ops(running=True)

        added = ops.add_extension("Me.Mine")
        self.assertEqual((added.message, added.applied), ("added extension me.mine", True))
        self.assertEqual(envfile.read(ops.profile.env_file)["VSCODE_EXTENSIONS"], "me.mine")

        # The running container installs it from its list, by itself.
        self.assertIn("me.mine\n", ops.profile.extensions_file.read_text(encoding="utf-8"))
        self.assertEqual(self.runner.commands(" up -d"), [], "nothing is recreated")

        self.assertFalse(ops.add_extension("me.mine").changed)
        self.assertEqual(ops.add_extension("ms-python.python").message, "ms-python.python already comes with Python")
        self.assertIn({"id": "me.mine", "source": "custom", "enabled": True, "duplicate": False}, ops.extensions())

        with self.assertRaisesRegex(langs.LangError, "comes with Python; switch that off in Languages / Tools"):
            ops.remove_extension("ms-python.python")
        with self.assertRaisesRegex(langs.LangError, "comes with every VS Code container and can't be removed"):
            ops.remove_extension("redhat.vscode-yaml")
        with self.assertRaisesRegex(langs.LangError, "comes with Claude Code CLI; switch that off"):
            ops.remove_extension("anthropic.claude-code")
        with self.assertRaisesRegex(langs.LangError, "isn't one of this profile's"):
            ops.remove_extension("other.one")

        self.assertEqual(ops.remove_extension("me.mine").message, "removed extension me.mine")
        self.assertEqual(envfile.read(ops.profile.env_file)["VSCODE_EXTENSIONS"], "")

    def test_switching_a_tool_rewrites_the_extension_list_in_place(self):
        ops = self.ops(running=True)
        path = ops.profile.extensions_file
        inode = path.stat().st_ino
        self.assertIn("openai.chatgpt", path.read_text(encoding="utf-8"), "Codex is on in the template")

        result = ops.set_languages({"codex_cli": False}, build=False)

        self.assertNotIn("openai.chatgpt", path.read_text(encoding="utf-8"))
        self.assertEqual(path.stat().st_ino, inode, "the container's bind mount follows the same file")
        self.assertTrue(result.applied)

    def test_the_extension_status_of_the_running_container(self):
        ops = self.ops(running=True)
        self.runner.extension_status = {"state": "installing", "installed": 3, "total": 12, "pending": ["a.b"]}

        self.assertEqual(ops.extension_status()["pending"], ["a.b"])
        self.assertEqual(ops.status()["extensions"], {"state": "installing", "installed": 3, "total": 12})

        stopped = self.ops()
        self.assertIsNone(stopped.extension_status())
        self.assertIsNone(stopped.status()["extensions"])

    def test_the_extension_status_is_read_within_a_limit_and_checked(self):
        # Anything in the VS Code container can write that file. It is read
        # up to a limit, in the container, so a huge one never reaches this
        # process; and taken only when it is what the extension sync writes.
        ops = self.ops(running=True)
        good = {"state": "done", "total": 2, "installed": 1, "pending": [], "refused": ["x.y"], "failed": []}
        self.runner.extension_status = {**good, "updated": "2026-10-13T10:00:00Z", "extra": "dropped"}

        self.assertEqual(ops.extension_status(), {**good, "updated": "2026-10-13T10:00:00Z"})
        read = self.runner.commands("hardcode-extensions-status")[-1]
        self.assertIn(f"head -c {STATUS_BYTES} ~/.vscode-server/.hardcode-extensions-status.json", read)

        # The lists the page counts are there even when the file leaves them out.
        self.runner.extension_status = {"state": "installing", "total": 3, "installed": 0}
        self.assertEqual(ops.extension_status()["failed"], [])

        refused = [
            {**good, "pending": ["x" * 1_048_576]},
            {**good, "pending": [f"a.b{index}" for index in range(5000)]},
            {**good, "installed": "1"},
            {**good, "total": -1},
            {**good, "state": "anything"},
            {**good, "refused": "x.y"},
            {**good, "failed": [7]},
        ]
        for status in refused:
            self.runner.extension_status = status
            self.assertIsNone(ops.extension_status(), str(status)[:120])

    def test_a_new_vscode_token(self):
        # Running: the token file goes, and VS Code restarts to write a new one.
        ops = self.ops(running=True)
        result = ops.rotate_token()

        self.assertTrue(result.applied)
        calls = [" ".join(call) for call in self.runner.calls]
        removed = next(
            index
            for index, call in enumerate(calls)
            if "exec -T vscode sh -c rm -f ~/.vscode-server/connection-token" in call
        )
        restarted = next(index for index, call in enumerate(calls) if call.endswith("restart vscode"))
        self.assertLess(removed, restarted)

        # Stopped: a container of its own removes it, and nothing starts.
        stopped = self.ops()
        result = stopped.rotate_token()
        self.assertFalse(result.applied)
        self.assertTrue(self.runner.commands("run --rm --no-deps --entrypoint sh vscode -c rm -f"))
        self.assertEqual(self.runner.commands(" restart"), [])

        # A token the .env fixes is changed there.
        envfile.set_value(stopped.profile.env_file, "VSCODE_CONNECTION_TOKEN", "fixed")
        with self.assertRaisesRegex(profiles.ProfileError, "fixed"):
            stopped.rotate_token()

    def test_the_images_languages_and_whether_to_rebuild(self):
        ops = self.ops()
        no_image = Image(built=False, languages=[], packages=[], dockerfile="")
        self.assertEqual(ops.image(), no_image, "no image yet")
        self.assertIs(ops.state()["rebuildNeeded"], False, "the first start builds it")

        every = " ".join(f"{key}=true" for key in langs.LANGS)
        self.runner.image_labels = {"io.github.hardcode-app.languages": every}
        self.assertEqual(ops.image(), Image(built=True, languages=list(langs.LANGS), packages=[], dockerfile=""))
        self.assertIs(ops.state()["rebuildNeeded"], False)

        ops.set_languages({"go": False}, build=False)
        state = ops.state()
        self.assertIs(state["rebuildNeeded"], True)
        go = next(lang for lang in state["langs"] if lang["key"] == "go")
        self.assertEqual((go["enabled"], go["inImage"]), (False, True))

        # No Docker to ask: nobody can say.
        with mock.patch.object(ops.compose, "run_docker", side_effect=ComposeError("docker not found on the PATH")):
            self.assertEqual(ops.image(), Image(built=None, languages=None, packages=None, dockerfile=None))
            self.assertIsNone(ops.state()["rebuildNeeded"])

    def test_an_image_without_languages_is_an_image(self):
        # Built with no language or tool at all, it is still there: one
        # switched on since needs a rebuild, not a first build.
        ops = self.ops()
        ops.set_languages({key: False for key in langs.LANGS}, build=False)
        self.runner.image_labels = {"io.github.hardcode-app.languages": " ".join(f"{key}=false" for key in langs.LANGS)}

        state = ops.state()
        self.assertEqual(state["image"], {"built": True, "languages": [], "packages": [], "dockerfile": ""})
        self.assertIs(state["rebuildNeeded"], False)

        ops.set_languages({"python": True}, build=False)
        state = ops.state()
        python = next(lang for lang in state["langs"] if lang["key"] == "python")
        self.assertEqual((state["image"]["built"], state["rebuildNeeded"]), (True, True))
        self.assertEqual((python["enabled"], python["inImage"]), (True, False))

        # An image without the label is there, with languages nobody can tell.
        self.runner.image_labels = {}
        state = ops.state()
        self.assertEqual(state["image"], {"built": True, "languages": None, "packages": [], "dockerfile": ""})
        self.assertIsNone(state["rebuildNeeded"])
        self.assertIsNone(python_in_image(state))

    # -- the Docker daemon ----------------------------------------------------

    def test_docker_switch(self):
        ops = self.ops()
        self.assertTrue(ops.state()["docker"])
        self.assertFalse(ops.set_docker(True).changed)

        result = ops.set_docker(False)

        self.assertEqual((result.message, result.applied), ("docker off", False))
        self.assertEqual(envfile.read(ops.profile.env_file)["ENABLE_DOCKER"], "false")
        self.assertFalse(ops.state()["docker"])

        with self.assertRaisesRegex(override.OverrideError, "without the Docker daemon"):
            ops.forward(8080, "docker", 80)
        ops.forward(3000)

        running = self.ops(running=True)
        result = running.set_docker(True)

        self.assertEqual((result.message, result.applied), ("docker on", True))
        self.assertEqual(
            len(self.runner.commands(" up -d")), 1, "the VS Code container is recreated with the daemon beside it"
        )
        self.assertEqual(self.runner.commands("rm --stop"), [])

    def test_a_docker_switch_that_cannot_be_applied_is_undone(self):
        ops = self.ops(running=True)
        self.runner.fail_on.add("rm --stop")

        with self.assertRaisesRegex(ComposeError, "the change was undone"):
            ops.set_docker(False)

        self.assertNotIn("ENABLE_DOCKER", envfile.read(ops.profile.env_file), "back on: unset, the default")
        self.assertTrue(ops.compose.docker_enabled())
        self.assertTrue(self.runner.commands("--progress quiet up -d"), "started again as it was, daemon included")

    def test_docker_off_stops_the_daemon_and_nothing_restarts_it(self):
        ops = self.ops(running=True)

        result = ops.set_docker(False)

        self.assertEqual((result.message, result.applied), ("docker off", True))
        stopped = self.runner.commands("--profile docker --progress quiet rm --stop --force docker")
        self.assertEqual(
            len(stopped),
            1,
            "a disabled Compose profile's container isn't an orphan: it is removed by name",
        )
        self.assertEqual(
            ops.compose.environment()["COMPOSE_PROFILES"],
            "",
            "and the VS Code container is recreated without the docker profile active",
        )

        # Mounts recreate the VS Code container, but must never name docker while it is off:
        # naming a service starts it.
        local = self.make_folder()
        ops.mount(str(local), "work")
        ops.unmount("work")

        recreates = [command for command in self.runner.commands(" up -d ") if "rm --stop" not in command]
        self.assertTrue(recreates, "mounts still recreate the VS Code container")
        vscode_recreates = [command for command in recreates if "vscode" in command]
        self.assertTrue(
            all(command.endswith(" up -d vscode") for command in vscode_recreates),
            f"never naming docker while it is off: {recreates}",
        )

        ops.set_docker(True)
        ops.mount(str(local), "again")
        self.assertTrue(
            any(command.endswith(" up -d vscode docker") for command in self.runner.commands(" up -d ")),
            "on again, both are recreated",
        )

    def test_steps_behind_the_scenes_keep_stdout_for_json(self):
        # What Compose prints while recreating or removing a service mustn't
        # reach stdout, where --json output goes ("Going to remove ..." once
        # broke the app's JSON for docker off). Short steps are captured, so
        # that a failure carries Compose's reason; builds show on stderr.
        ops = self.ops(running=True)
        local = self.make_folder()

        ops.allow("x.org")
        ops.forward(3000)
        ops.mount(str(local), "work")
        ops.set_docker(False)
        ops.set_docker(True)
        ops.set_language("go", False)
        ops.rebuild()

        changing = [
            (" ".join(call), target)
            for call, target in zip(self.runner.calls, self.runner.stdout_targets)
            if any(word in call for word in ("up", "rm", "build"))
        ]
        self.assertTrue(changing)
        for command, target in changing:
            expected = sys.stderr if " build " in f"{command} " else "captured"
            self.assertEqual(target, expected, command)

    def test_destroy_takes_the_daemon_with_it_even_when_off(self):
        ops = self.ops()
        ops.set_docker(False)

        ops.destroy()

        self.assertEqual(len(self.runner.commands("--profile docker --progress quiet down -v --remove-orphans")), 1)

    # -- lifecycle ------------------------------------------------------------

    def test_destroy_removes_everything(self):
        ops = self.ops("gone")

        result = ops.destroy()

        self.assertEqual(result.message, "profile 'gone' removed")
        self.assertFalse(profiles.exists(self.root, "gone"))
        self.assertEqual(len(self.runner.commands("down -v --remove-orphans")), 1)
        self.assertEqual(len(self.runner.commands("image rm hardcode-vscode:gone")), 1)

    def test_destroy_keeps_the_profile_when_teardown_fails(self):
        ops = self.ops("stuck")
        self.runner.fail_on.add("down -v")

        with self.assertRaisesRegex(ComposeError, "profiles/stuck is kept.*simulated failure"):
            ops.destroy()

        self.assertTrue(profiles.exists(self.root, "stuck"), "the files to inspect and retry are still there")
        self.assertEqual(self.runner.commands("image rm"), [], "nothing after the failed step")

    def test_destroy_reports_an_image_it_could_not_remove(self):
        ops = self.ops("img")
        self.runner.fail_on.add("image rm")

        result = ops.destroy()

        self.assertIn("image hardcode-vscode:img not removed", result.message)
        self.assertFalse(profiles.exists(self.root, "img"))


class LanTests(HomeTestCase):
    """
    Names on your network: a name whose address is inside the profile's
    internal ranges is refused there by the proxy, unless the profile lets
    that one name reach that one address.
    """

    GITLAB = {"gitlab.example.com": ["203.0.113.9", "192.168.71.200"]}

    def ops(self, answers=None, running=False):
        """
        Operations on the default profile, with name lookups giving *answers*.
        """
        if not profiles.exists(self.root, "default"):
            profiles.create(self.root, "default", languages=[])

        self.runner = FakeRunner(running=running)
        self.resolver = FakeResolver(self.GITLAB if answers is None else answers)
        return Ops(
            self.root,
            profiles.get(self.root, "default"),
            runner=self.runner,
            docker="docker",
            sleep=lambda _: None,
            resolve=self.resolver,
        )

    def pins(self, ops):
        return [(pin.name, pin.address) for pin in ops.pins()]

    def test_allow_says_when_the_name_resolves_inside_and_pins_nothing(self):
        ops = self.ops()

        result = ops.allow("gitlab.example.com:22222", "ssh")

        self.assertEqual(
            result.data["lan"], {"name": "gitlab.example.com", "address": "192.168.71.200", "pinned": False}
        )
        self.assertIn("gitlab.example.com resolves to 192.168.71.200, on your network", result.message)
        self.assertIn("hardcode lan add gitlab.example.com 192.168.71.200", result.message)
        self.assertEqual(self.pins(ops), [])

    def test_a_yes_pins_it_in_the_same_change(self):
        ops = self.ops(running=True)
        asked = []

        def confirm(name, address):
            asked.append((name, address))
            return True

        result = ops.allow("gitlab.example.com/group", confirm=confirm)

        self.assertEqual(asked, [("gitlab.example.com", "192.168.71.200")])
        self.assertTrue(result.data["lan"]["pinned"])
        self.assertIn("gitlab.example.com may reach 192.168.71.200 on your network", result.message)
        self.assertEqual(self.pins(ops), [("gitlab.example.com", "192.168.71.200")])
        self.assertEqual(len(self.runner.commands("up -d allowlist squid")), 1, "one reload for both")
        self.assertIn(
            "lan gitlab.example.com 192.168.71.200", ops.profile.proxy_allowlist_file.read_text(encoding="utf-8")
        )

    def test_a_no_pins_nothing_and_lan_ok_doesnt_ask(self):
        ops = self.ops()

        refused = ops.allow("gitlab.example.com", confirm=lambda name, address: False)
        self.assertFalse(refused.data["lan"]["pinned"])
        self.assertEqual(self.pins(ops), [])

        def never(name, address):
            raise AssertionError("asked")

        allowed = ops.allow("gitlab.example.com", lan_ok=True, confirm=never)
        self.assertTrue(allowed.data["lan"]["pinned"])
        self.assertEqual(self.pins(ops), [("gitlab.example.com", "192.168.71.200")])

    def test_no_offer_for_public_names_wildcards_or_names_on_it_already(self):
        ops = self.ops({"pypi.org": ["151.101.0.223"], "example.com": ["192.168.1.1"], **self.GITLAB})

        self.assertEqual(ops.allow("pypi.org").data, {})
        self.assertEqual(ops.allow(".example.com").data, {})
        self.assertNotIn("example.com", self.resolver.asked, "a wildcard's name isn't looked up")

        ops.add_pin("gitlab.example.com")
        self.assertEqual(ops.allow("gitlab.example.com").data, {})

        nothing = ops.allow("pypi.org/simple", lan_ok=True)
        self.assertIn("pypi.org doesn't resolve to an address on it from here", nothing.message)

    def test_an_entry_the_allowlist_refuses_is_not_looked_up(self):
        ops = self.ops()

        with self.assertRaises(allowlist.AllowlistError):
            ops.allow("gitlab.example.com/a/../b")

        self.assertEqual(self.resolver.asked, [])

    def test_add_pin_finds_the_address_or_takes_the_one_given(self):
        ops = self.ops({**self.GITLAB, "public.example.com": ["203.0.113.10"]})

        found = ops.add_pin("gitlab.example.com:22222")
        self.assertIn("gitlab.example.com may reach 192.168.71.200 on your network", found.message)
        self.assertIn("it allows nothing until gitlab.example.com has an allowlist entry", found.message)

        given = ops.add_pin("registry.example.com", "fd00::10")
        self.assertEqual(
            given.message,
            "registry.example.com may reach fd00::10 on your network; it allows nothing until registry.example.com has an allowlist entry",
        )

        with self.assertRaisesRegex(
            allowlist.AllowlistError, "doesn't resolve to an address on your network from here"
        ):
            ops.add_pin("public.example.com")
        with self.assertRaisesRegex(allowlist.AllowlistError, "only an exact name"):
            ops.add_pin(".example.com", "10.0.0.1")

        self.assertEqual(
            self.pins(ops),
            [("gitlab.example.com", "192.168.71.200"), ("registry.example.com", "fd00::10")],
        )

    def test_the_profiles_own_ranges_decide(self):
        ops = self.ops()
        envfile.set_value(ops.profile.env_file, "EGRESS_INTERNAL_RANGES", "10.0.0.0/8")

        self.assertEqual(ops.allow("gitlab.example.com").data, {}, "192.168/16 isn't internal to this profile")
        with self.assertRaisesRegex(allowlist.AllowlistError, "isn't on your network"):
            ops.add_pin("gitlab.example.com", "192.168.71.200")

    def test_remove_pin(self):
        ops = self.ops()
        ops.add_pin("gitlab.example.com")

        removed = ops.remove_pin("gitlab.example.com:22222")

        self.assertEqual(removed.message, "gitlab.example.com no longer reaches your network")
        self.assertEqual(self.pins(ops), [])

    def test_a_pin_never_follows_its_name(self):
        ops = self.ops({**self.GITLAB, "moved.example.com": ["10.0.0.9"]})
        ops.add_pin("gitlab.example.com")
        ops.add_pin("moved.example.com", "10.0.0.8")
        ops.add_pin("gone.example.com", "10.0.0.7")

        states = {item["name"]: (item["state"], item["resolves"]) for item in ops.lan_states()}
        self.assertEqual(
            states,
            {
                "gitlab.example.com": ("ok", ["203.0.113.9", "192.168.71.200"]),
                "moved.example.com": ("moved", ["10.0.0.9"]),
                "gone.example.com": ("unresolved", []),
            },
        )

        self.assertEqual(
            ops.lan_warnings(),
            [
                "moved.example.com now resolves to 10.0.0.9, not 10.0.0.8: it still reaches only 10.0.0.8; "
                "if the move is right, hardcode lan add moved.example.com 10.0.0.9"
            ],
        )
        self.assertIn(("moved.example.com", "10.0.0.8"), self.pins(ops), "the pin stays where it was")

    def test_reload_looks_the_names_up_again(self):
        ops = self.ops({"moved.example.com": ["10.0.0.9"]}, running=True)
        ops.add_pin("moved.example.com", "10.0.0.8")

        result = ops.reload()

        self.assertIn("moved.example.com now resolves to 10.0.0.9, not 10.0.0.8", result.message)
        self.assertEqual(result.data["lan"][0]["state"], "moved")

    def test_check_name(self):
        ops = self.ops()

        self.assertEqual(
            ops.check_name("gitlab.example.com:22222"),
            {
                "name": "gitlab.example.com",
                "resolves": ["203.0.113.9", "192.168.71.200"],
                "address": "192.168.71.200",
                "pinned": "",
            },
        )

        ops.add_pin("gitlab.example.com")
        self.assertEqual(ops.check_name("gitlab.example.com")["pinned"], "192.168.71.200")

    def test_the_state_lists_the_pins_without_looking_them_up(self):
        ops = self.ops()
        ops.add_pin("gitlab.example.com")
        self.resolver.asked.clear()

        self.assertEqual([pin["name"] for pin in ops.state()["lan"]], ["gitlab.example.com"])
        self.assertEqual(self.resolver.asked, [])


if __name__ == "__main__":
    unittest.main()
