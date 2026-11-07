"""
Tests for the command line (hardcode_cli.cli), end to end with
``docker compose`` faked.
"""

import hashlib
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

import hardcode_cli
from hardcode_cli import allowlist, applier, cli, compose, envfile, langs, profiles

from .helpers import FakeRunner, HomeTestCase, fake_host, updated_catalogue
from .resolver import FakeResolver

NOT_RUNNING = "(profile 'default' isn't running; applies when it starts)"
RELOADS_LATER = "(the proxy takes it in 5 seconds; hardcode apply: now)"


class CliTestCase(HomeTestCase):
    """
    A home with a default profile, and the command line run in it with
    docker compose and name lookups faked.
    """

    def setUp(self):
        super().setUp()

        # A default profile with every language and tool on, as most tests
        # here want one; the first-run tests use a home of their own.
        profiles.create(self.root, "default", languages=list(langs.LANGS))

        # Name lookups: a GitLab on your network, and nothing else resolves;
        # this computer's own addresses (none, so that nothing depends on
        # the machine's); and the questions the command asked.
        self.resolver = FakeResolver({"gitlab.example.com": ["192.168.71.200"]})
        self.this_computer = set()
        self.asked = []

        # A Linux computer without devices: this machine's /dev is never read.
        self.host = fake_host()

        # The profiles whose applier was started: none is really started here.
        self.appliers = []

    def run_cli(self, *argv, running=False, runner=None, config=None, answers=None, docker="docker"):
        """
        Run ``hardcode --config <home> <argv>``; returns the exit code and
        what was printed to stdout and stderr. With *answers*, the command
        runs on a terminal and they answer its questions, in order. The
        computer is ``self.host``, and its ``docker`` command *docker*; the
        start of an applier is only noted in ``self.appliers``.
        """
        self.runner = runner or FakeRunner(running=running)
        out, err = [], []
        replies = list(answers or [])

        code = cli.main(
            ["--config", str(config or self.root), *argv],
            runner=self.runner,
            docker=docker,
            out=out.append,
            err=err.append,
            sleep=lambda _: None,
            ask=lambda question: self.asked.append(question) or replies.pop(0),
            terminal=answers is not None,
            resolve=self.resolver,
            this_computer=lambda address: address in self.this_computer,
            host=self.host,
            start_applier=lambda root, profile: self.appliers.append(profile.name),
        )
        return code, "\n".join(out), "\n".join(err)

    def allowlist_of(self, profile):
        return profiles.get(self.root, profile).allowlist_file.read_text(encoding="utf-8")


class CliTests(CliTestCase):
    """
    The command line end to end, with docker compose faked.
    """

    # -- general ---------------------------------------------------------------

    def test_no_command_prints_help(self):
        with redirect_stdout(io.StringIO()) as buffer:
            self.assertEqual(cli.main(["--config", str(self.root)]), 2)

        self.assertIn("hardcode [-p NAME | --all]", buffer.getvalue())

    def test_version(self):
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()) as buffer:
            cli.main(["--version"])

        self.assertEqual(buffer.getvalue(), f"hardcode {hardcode_cli.__version__}\n")

    def test_a_development_build_says_so_after_its_version(self):
        with mock.patch.object(hardcode_cli, "__build__", "dev, 2c419ae"):
            with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()) as buffer:
                cli.main(["--version"])

            code, out, _ = self.run_cli("--json", "profiles")

        self.assertEqual(buffer.getvalue(), f"hardcode {hardcode_cli.__version__} (dev, 2c419ae)\n")
        self.assertEqual((code, json.loads(out)["version"]), (0, f"{hardcode_cli.__version__} (dev, 2c419ae)"))

    def test_unknown_profile_and_home(self):
        code, _, err = self.run_cli("-p", "nope", "allow", "x.org")
        self.assertEqual(code, 1)
        self.assertIn("no profile 'nope'", err)

        empty = self.root.parent / "empty-home"
        code, out, _ = self.run_cli("profiles", config=empty)
        self.assertEqual(
            (code, out), (0, f"no profiles yet in {empty}: hardcode profile add default --lang python,docker")
        )

        code, _, err = self.run_cli("allow", "x.org", config=empty)
        self.assertEqual(code, 1)
        self.assertIn("no profiles yet", err)

        (self.root.parent / "a-file").write_text("", encoding="utf-8")
        code, _, err = self.run_cli("profiles", config=self.root.parent / "a-file")
        self.assertEqual(code, 1)
        self.assertIn("is a file", err)

    def test_profile_flag_positions_and_env(self):
        self.run_cli("profile", "add", "p2", "--lang", "")

        # -p works before the command and after it.
        _, out, _ = self.run_cli("-p", "p2", "allow", "x.org")
        self.assertIn("allowed x.org", out)
        _, out, _ = self.run_cli("allow", "y.org", "-p", "p2")
        self.assertIn("allowed y.org", out)
        self.assertIn("y.org", self.allowlist_of("p2"))

        _, out, _ = self.run_cli("allow", "z.org", "--all", "--json")
        self.assertEqual(len(json.loads(out)["results"]), 2)
        self.assertIn("x.org", self.allowlist_of("p2"))
        self.assertNotIn("x.org", self.allowlist_of("default"))

        with mock.patch.dict("os.environ", {"HARDCODE_PROFILE": "p2"}):
            code, _, _ = self.run_cli("allow", "com")
            self.assertEqual(code, 1)

    # -- profiles --------------------------------------------------------------

    def test_profiles_lists_them_and_says_where_they_are(self):
        code, out, _ = self.run_cli("profiles")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"default\s+11111\s+172\.31\.253\.0/24\s+stopped")

        _, out, _ = self.run_cli("--json", "status", running=True)
        listed = json.loads(out)
        self.assertEqual(listed["profiles"][0]["state"], "running (3 containers)")
        self.assertEqual(listed["home"], str(self.root))
        self.assertEqual(listed["version"], hardcode_cli.__version__)

    def test_profile_add_copy_and_remove(self):
        code, _, err = self.run_cli("profile", "add", "old-node")
        self.assertEqual(code, 1)
        self.assertIn("needs its languages and tools: --lang", err)

        code, out, _ = self.run_cli("profile", "add", "old-node", "--lang", "node,docker")
        self.assertEqual(code, 0)
        self.assertIn("created from the template (VS Code on port 11112", out)
        self.assertIn("languages: node, docker", out)

        _, out, _ = self.run_cli("profile", "add", "copy", "--from", "old-node")
        self.assertIn("created from old-node (VS Code on port 11113", out)

        code, _, err = self.run_cli("profile", "add", "Bad_Name")
        self.assertEqual(code, 1)
        self.assertIn("not a valid profile name", err)

        _, _, err = self.run_cli("profile", "add", "copy", "--lang", "")
        self.assertIn("already exists", err)

        code, _, _ = self.run_cli("profile", "remove", "copy", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(profiles.list_profiles(self.root), ["default", "old-node"])

        # Nothing makes default again: it can go like any other.
        code, _, _ = self.run_cli("profile", "remove", "default", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(profiles.list_profiles(self.root), ["old-node"])

        self.run_cli("profile", "remove", "old-node", "--yes")
        self.assertEqual(profiles.list_profiles(self.root), [])

        _, _, err = self.run_cli("profile", "remove", "nope", "--yes")
        self.assertIn("no profile", err)

    def test_profile_remove_asks(self):
        self.run_cli("profile", "add", "x", "--lang", "")

        with mock.patch("builtins.input", return_value="n"):
            code, out, _ = self.run_cli("profile", "remove", "x")
        self.assertEqual((code, out), (1, "cancelled"))

        with mock.patch("builtins.input", return_value="y"):
            self.assertEqual(self.run_cli("profile", "remove", "x")[0], 0)

    def test_profile_remove_without_a_terminal_is_cancelled(self):
        self.run_cli("profile", "add", "x", "--lang", "")

        with mock.patch("builtins.input", side_effect=EOFError):
            code, out, _ = self.run_cli("profile", "remove", "x")

        self.assertEqual((code, out), (1, "cancelled"))
        self.assertTrue(profiles.exists(self.root, "x"))

    def test_profile_without_action(self):
        code, _, err = self.run_cli("profile")

        self.assertEqual(code, 2)
        self.assertIn("usage", err)

    # -- allowlist -------------------------------------------------------------

    def test_allow_deny_single_profile(self):
        code, out, _ = self.run_cli("allow", "GitHub.com/Me")
        self.assertEqual(code, 0)
        self.assertIn("allowed github.com/me", out)
        self.assertIn("isn't running", out)

        _, out, _ = self.run_cli("allow", "github.com/me", "rw", running=True)
        self.assertIn(f"allowed github.com/me (rw) {RELOADS_LATER}", out)
        self.assertEqual(self.appliers, [], "the running proxy takes it by itself")
        self.assertFalse(self.runner.commands(" rm "), "nothing is recreated")

        _, out, _ = self.run_cli("deny", "github.com/me")
        self.assertIn("denied github.com/me", out)

        code, _, err = self.run_cli("deny", "github.com/me")
        self.assertEqual(code, 1)
        self.assertIn("not an active entry", err)

        code, _, err = self.run_cli("allow", "com")
        self.assertEqual(code, 1)
        self.assertIn("not a valid hostname", err)

    def test_all_profiles(self):
        self.run_cli("profile", "add", "a", "--lang", "")
        self.run_cli("profile", "add", "b", "--lang", "")

        code, out, _ = self.run_cli("--all", "allow", "github.com/ourcompany")
        self.assertEqual(code, 0)
        self.assertEqual(out.count("allowed github.com/ourcompany"), 3)
        self.assertTrue(out.startswith("[default] "))

        # One profile without the entry doesn't make --all fail.
        self.run_cli("deny", "-p", "b", "github.com/ourcompany")
        code, out, _ = self.run_cli("--all", "deny", "github.com/ourcompany")
        self.assertEqual(code, 0)
        self.assertEqual(out.count("denied"), 2)
        self.assertIn("[b] 'github.com/ourcompany' is not an active entry", out)

        _, out, _ = self.run_cli("--all", "--json", "allow", "x.org")
        self.assertEqual([result["profile"] for result in json.loads(out)["results"]], ["default", "a", "b"])

    def test_all_fails_when_every_profile_fails(self):
        self.run_cli("profile", "add", "a", "--lang", "")

        code, out, _ = self.run_cli("--all", "mount", "/nonexistent-xyz", "x")

        self.assertEqual(code, 1)
        self.assertEqual(out.count("skipped: "), 2)

    def test_all_is_refused_where_it_makes_no_sense(self):
        code, _, err = self.run_cli("--all", "forward", "3000")

        self.assertEqual(code, 2)
        self.assertIn("--all isn't supported", err)

    def test_allowlist_subcommands_and_apply(self):
        self.run_cli("allow", "x.org")

        _, out, _ = self.run_cli("allowlist", "disable", "x.org")
        self.assertIn("disabled x.org", out)
        _, out, _ = self.run_cli("allowlist", "enable", "x.org", running=True)
        self.assertIn(f"enabled x.org {RELOADS_LATER}", out)
        _, out, _ = self.run_cli("--all", "allowlist", "remove", "x.org")
        self.assertIn("[default] removed x.org", out)

        _, out, _ = self.run_cli("apply")
        self.assertIn("isn't running", out)
        _, out, _ = self.run_cli("apply", running=True)
        self.assertIn("the proxy has the allowlist (applied)", out)
        self.assertTrue(self.runner.commands(" run --rm --no-deps allowlist"), "the renderer runs at once")
        self.assertEqual(self.runner.commands(" rm "), [], "and nothing is recreated")

        refusing = FakeRunner(running=True)
        refusing.fail_on.add("run --rm --no-deps allowlist")
        code, out, err = self.run_cli("apply", runner=refusing)
        self.assertEqual(code, 1, "an allowlist the renderer refuses is a failure, not a stopped profile")
        self.assertIn("the proxy keeps the allowlist it has", err)
        self.assertNotIn("isn't running", out)

        _, out, _ = self.run_cli("rebuild", running=True)
        self.assertIn("VS Code image rebuilt (applied)", out)

    def test_ssh_entries_are_addressed_with_the_flag(self):
        self.run_cli("allow", "github.com")
        self.run_cli("allow", "github.com", "ssh")

        code, out, _ = self.run_cli("deny", "github.com", "ssh")
        self.assertEqual((code, out), (0, f"denied github.com (ssh) {NOT_RUNNING}"))

        _, out, _ = self.run_cli("allowlist", "remove", "github.com", "ssh")
        self.assertIn("removed github.com (ssh)", out)

        _, out, _ = self.run_cli("--json", "show")
        flags = [entry["flag"] for entry in json.loads(out)["allowlist"] if entry["entry"] == "github.com"]
        self.assertEqual(flags, [""])

    # -- forwards and mounts ---------------------------------------------------

    def test_forward_and_unforward(self):
        _, out, _ = self.run_cli("forward", "3000")
        self.assertIn("forwarding localhost:3000 -> vscode:3000", out)

        _, out, _ = self.run_cli("forward", "8080", "docker", "80", running=True)
        self.assertIn("-> docker:80 (gateway restarts in 5 seconds; hardcode apply: now)", out)
        self.assertEqual(self.runner.commands(" up "), [], "the gateway waits for the applier")
        self.assertEqual(self.appliers, ["default"])

        _, _, err = self.run_cli("forward", "8000")
        self.assertIn("VS Code port", err)

        _, out, _ = self.run_cli("unforward", "3000")
        self.assertIn("removed", out)
        _, _, err = self.run_cli("unforward", "4444")
        self.assertIn("isn't forwarded", err)

    def test_mount_and_unmount(self):
        source = self.root.parent / "src"  # beside the home: never one in it
        source.mkdir()

        code, out, _ = self.run_cli("mount", str(source), "work", "--ro")
        self.assertEqual(code, 0)
        self.assertIn("(read-only)", out)

        _, _, err = self.run_cli("mount", str(source), "work")
        self.assertIn("already mounted", err)

        _, out, _ = self.run_cli("unmount", "work")
        self.assertIn("removed", out)

        _, _, err = self.run_cli("mount", "/nonexistent-xyz", "x")
        self.assertIn("not a folder", err)

    def test_a_broken_override_is_an_error_not_a_traceback(self):
        self.run_cli("profile", "add", "b", "--lang", "")
        profiles.get(self.root, "b").override_file.write_text("services:\n  gateway:\n    ports: [\n", encoding="utf-8")

        code, _, err = self.run_cli("forward", "3000")
        self.assertEqual(code, 0, "the other profile's damaged file is a warning here, not a stop")
        self.assertRegex(err, r"warning: .*profiles[/\\]b[/\\]compose.override.yaml: invalid YAML")

        code, _, err = self.run_cli("-p", "b", "forward", "3001")
        self.assertEqual(code, 1, "the damaged profile itself can't be edited")
        self.assertRegex(err, r"error: .*profiles[/\\]b[/\\]compose.override.yaml: invalid YAML")

        code, _, _ = self.run_cli("--all", "allow", "x.org")
        self.assertEqual(code, 0, "an allowlist edit doesn't read overrides")

        _, out, _ = self.run_cli("--all", "unmount", "nothing")
        self.assertIn("[b] skipped:", out)
        self.assertIn("invalid YAML", out)

    # -- the image and the Docker daemon ---------------------------------------

    def test_enable_disable(self):
        _, out, _ = self.run_cli("disable", "c#", "--no-build")
        self.assertIn("dotnet off (the image is rebuilt with: hardcode rebuild)", out)

        _, out, _ = self.run_cli("enable", "dotnet", running=True)
        self.assertIn(f"dotnet on {RELOADS_LATER}", out)
        self.assertTrue(self.runner.commands(" build vscode"))

        _, _, err = self.run_cli("enable", "cobol")
        self.assertIn("unknown language", err)

    def test_languages_and_extensions_commands(self):
        code, out, _ = self.run_cli("profile", "add", "py", "--lang", "python,docker")
        self.assertEqual(code, 0)
        self.assertIn("languages: python, docker", out)

        listed = json.loads(self.run_cli("--json", "-p", "py", "languages")[1])
        enabled = [lang["key"] for lang in listed["languages"] if lang["enabled"]]
        self.assertEqual(enabled, ["python", "docker"])

        code, out, _ = self.run_cli("-p", "py", "enable", "go", "rust", "--no-build")
        self.assertEqual(code, 0)
        self.assertIn("go on, rust on", out)

        self.assertEqual(self.run_cli("-p", "py", "extension", "add", "me.mine")[0], 0)
        rows = json.loads(self.run_cli("--json", "-p", "py", "extensions")[1])
        self.assertIn({"id": "me.mine", "source": "custom", "enabled": True, "duplicate": False}, rows["extensions"])
        self.assertEqual(rows["refused"], [])

        self.assertIn("me.mine", self.run_cli("-p", "py", "extensions")[1])

        code, _, err = self.run_cli("-p", "py", "extension", "add", "not-an-id")
        self.assertEqual(code, 1)
        self.assertIn("not an extension id", err)

    def test_docker_command(self):
        code, out, _ = self.run_cli("docker", "off")
        self.assertEqual((code, out), (0, f"docker off {NOT_RUNNING}"))
        self.assertFalse(json.loads(self.run_cli("--json", "show")[1])["docker"])
        self.assertIn("docker:      off", self.run_cli("show")[1])

        self.run_cli("profile", "add", "b", "--lang", "")
        code, out, _ = self.run_cli("--all", "docker", "on")
        self.assertEqual(code, 0)
        self.assertIn("[default] docker on", out)
        self.assertIn("[b] docker is already on", out)

    # -- lifecycle -------------------------------------------------------------

    def test_lifecycle_passthrough(self):
        commands = [
            (["up"], "up -d"),
            (["up", "--build"], "up -d --build"),
            (["down"], " down"),
            (["down", "-v"], "down -v"),
            (["start"], " start"),
            (["stop"], " stop"),
            (["restart"], " restart"),
            (["build"], " build"),
            (["logs", "squid"], "logs squid"),
            (["logs", "-f"], "logs -f"),
            (["logs", "--tail", "20", "squid"], "logs --tail 20 squid"),
            (["ps"], "ps -a"),
            (["compose", "exec", "vscode", "ls"], "exec vscode ls"),
        ]

        for argv, expected in commands:
            code, _, _ = self.run_cli(*argv)
            self.assertEqual(code, 0, argv)
            self.assertIn(expected, " ".join(self.runner.calls[-1]), argv)

        self.assertEqual(self.run_cli("shell")[0], 0)
        self.assertIn("exec vscode bash -l", " ".join(self.runner.calls[-1]))

    def test_a_start_gives_the_proxy_what_an_update_provides(self):
        given = lambda: profiles.get(self.root, "default").proxy_allowlist_file.read_text(encoding="utf-8")  # noqa: E731
        newer = mock.patch.object(langs, "CATALOGUE", updated_catalogue(added=["newrustsite.com"]))

        code, _, err = self.run_cli("up")
        self.assertEqual((code, err), (0, ""))

        # After an update, the file is written before the restart, which runs
        # the renderer again as it starts the profile; what changed is said.
        with newer:
            code, _, err = self.run_cli("restart", running=True)
        self.assertEqual((code, err), (0, "allowlist: Rust: + newrustsite.com"))
        self.assertIn("\nnewrustsite.com\n", given())
        self.assertEqual(self.runner.commands(" allowlist"), [], "nothing more than the restart")

        # Nothing changed, nothing said.
        with newer:
            code, _, err = self.run_cli("restart", running=True)
        self.assertEqual((code, err), (0, ""))

        # A stopped proxy reads the file as it starts; stop, down and build
        # leave it alone.
        newest = mock.patch.object(langs, "CATALOGUE", updated_catalogue(added=["newest.example"]))
        for command in ["stop", "down", "build"]:
            with newest:
                self.assertEqual(self.run_cli(command)[0], 0, command)
            self.assertNotIn("newest.example", given(), command)

        with newest:
            code, _, err = self.run_cli("start")
        self.assertEqual(err, "allowlist: Rust: + newest.example\nallowlist: Rust: - newrustsite.com")

    def test_an_edit_does_not_wait_for_containers_being_recreated(self):
        profile = profiles.get(self.root, "default")

        # The applier holds the containers' lock while it recreates them;
        # edits of the allowlist go on meanwhile, and write the file the
        # proxy watches, running or not.
        with profiles.containers_lock(profile):
            code, out, _ = self.run_cli("allow", "x.org", running=True)
            self.assertEqual((code, out), (0, f"allowed x.org {RELOADS_LATER}"))

            code, out, _ = self.run_cli("lan", "add", "gitlab.example.com", running=True)
            self.assertEqual(code, 0, out)

            code, out, _ = self.run_cli("allow", "y.org")
            self.assertEqual((code, out), (0, f"allowed y.org {NOT_RUNNING}"))

        given = profile.proxy_allowlist_file.read_text(encoding="utf-8")
        self.assertIn("\nx.org\n", given)
        self.assertIn("\ny.org\n", given)
        self.assertEqual(self.appliers, [], "the proxy takes them by itself")

    def test_ps_says_how_the_proxy_stands(self):
        self.assertEqual(self.run_cli("up")[0], 0)
        profile = profiles.get(self.root, "default")

        def proxy():
            code, out, _ = self.run_cli("--json", "ps", running=True)
            self.assertEqual(code, 0)
            return json.loads(out)["proxy"]

        def renderer_says(name, *lines):
            digest = hashlib.sha256(profile.proxy_allowlist_file.read_bytes()).hexdigest()
            (profile.proxy_dir / name).write_text("\n".join([digest, *lines]) + "\n", encoding="utf-8")

        renderer_says("applied")
        quiet = {"reloading": False, "due": None, "behind": False, "error": ""}
        self.assertEqual(proxy(), quiet)

        # An edit waits for its quiet seconds, while the watch runs.
        self.run_cli("allow", "x.org", running=True)
        waiting = proxy()
        self.assertEqual((waiting["reloading"], waiting["behind"], waiting["error"]), (True, True, ""))
        self.assertAlmostEqual(
            waiting["due"],
            profile.proxy_allowlist_file.stat().st_mtime + applier.QUIET_SECONDS,
            places=3,
        )

        # One the renderer refused says why.
        renderer_says("failed", "line 7: not a name")
        self.assertEqual(proxy(), {"reloading": False, "due": None, "behind": True, "error": "line 7: not a name"})

        # Published, it is caught up.
        renderer_says("applied")
        profile.proxy_failed_file.unlink()
        self.assertEqual(proxy(), quiet)

        # A stopped proxy isn't behind anything.
        self.run_cli("allow", "y.org")
        code, out, _ = self.run_cli("--json", "ps")
        self.assertFalse(json.loads(out)["proxy"]["behind"])

    def test_ps_says_what_waits_to_be_recreated(self):
        self.assertEqual(self.run_cli("up")[0], 0)

        code, out, _ = self.run_cli("--json", "ps", running=True)
        self.assertEqual(json.loads(out)["changes"], applier.Waiting().report(), "nothing waits")

        self.run_cli("forward", "3000", running=True)
        code, out, _ = self.run_cli("--json", "ps", running=True)
        changes = json.loads(out)["changes"]
        self.assertEqual(changes["changes"], ["forwarding localhost:3000 -> vscode:3000"])
        self.assertEqual(changes["services"], ["gateway"])
        self.assertNotIn("before", changes, "the profile's own files stay out of it")

    def test_the_applier_recreates_what_waits(self):
        self.assertEqual(self.run_cli("up")[0], 0)
        self.run_cli("forward", "3000", running=True)
        profile = profiles.get(self.root, "default")

        with mock.patch.object(applier, "QUIET_SECONDS", 0):
            self.run_cli("forward", "3001", running=True)
            code, _, _ = self.run_cli(applier.COMMAND, running=True)

        self.assertEqual(code, 0)
        self.assertEqual(len(self.runner.commands("up -d --no-deps gateway")), 1, "once for both")
        self.assertEqual(applier.load(profile), applier.Waiting())

        code, out, _ = self.run_cli("logs", "applier")
        self.assertEqual(code, 0)
        self.assertIn("waiting: forwarding localhost:3000 -> vscode:3000", out)
        self.assertIn("done: gateway recreated", out)

        # Nothing waiting, it does nothing; a profile that isn't running
        # takes the change at its next start.
        with mock.patch.object(applier, "QUIET_SECONDS", 0):
            self.run_cli(applier.COMMAND, running=True)
            self.assertEqual(self.runner.commands(" up "), [])

            self.run_cli("forward", "3002", running=True)
            self.run_cli(applier.COMMAND)
            self.assertEqual(self.runner.commands(" up "), [])
            self.assertEqual(applier.load(profile), applier.Waiting())

    def test_what_the_applier_undid_is_kept_for_ps(self):
        self.assertEqual(self.run_cli("up")[0], 0)
        self.run_cli("forward", "3000", running=True)
        failing = FakeRunner(running=True)
        failing.fail_on.add("up -d --no-deps gateway")

        with mock.patch.object(applier, "QUIET_SECONDS", 0):
            code, _, _ = self.run_cli(applier.COMMAND, runner=failing)
        self.assertEqual(code, 0, "nobody reads its exit code")

        code, out, _ = self.run_cli("--json", "ps", running=True)
        changes = json.loads(out)["changes"]
        self.assertEqual(changes["undone"], ["forwarding localhost:3000 -> vscode:3000"])
        self.assertIn("simulated failure", changes["error"])
        self.assertNotIn("3000", self.run_cli("show")[1], "the forward is gone")

        # The next change forgets it.
        self.run_cli("forward", "3001", running=True)
        code, out, _ = self.run_cli("--json", "ps", running=True)
        self.assertEqual(json.loads(out)["changes"]["undone"], [])

    def test_a_start_or_a_stop_leaves_nothing_waiting(self):
        self.assertEqual(self.run_cli("up")[0], 0)
        profile = profiles.get(self.root, "default")

        for command in ["stop", "start", "down", "restart"]:
            self.run_cli("forward", "3000", running=True)
            self.run_cli("unforward", "3000", running=True)
            self.assertTrue(applier.load(profile).changes, command)

            self.assertEqual(self.run_cli(command, running=True)[0], 0, command)
            self.assertEqual(applier.load(profile), applier.Waiting(), command)

        self.assertIn("left to the next start", self.run_cli("logs", "applier")[1])

    def test_allowlist_lists_everything(self):
        self.run_cli("allow", "github.com/me", "rw")
        self.run_cli("deny", "crates.io")

        code, out, _ = self.run_cli("allowlist")
        self.assertEqual(code, 0)

        # The profile's own first, so that one just added is in sight.
        lines = out.splitlines()
        self.assertTrue(lines[0].endswith("(this profile's own, in allowlist.txt)"), lines[0])
        self.assertIn("VS Code (every profile)", lines)
        self.assertIn("Rust (comes with the language)", lines)
        self.assertIn("Codex CLI (comes with the tool)", lines)
        self.assertIn("  off  crates.io", lines)
        self.assertIn("  on   static.crates.io", lines)
        self.assertIn("  on   github.com/me               rw", lines)
        self.assertIn("Custom (this profile's own, in allowlist.txt)", lines)
        self.assertLess(lines.index("  on   github.com/me               rw"), lines.index("VS Code (every profile)"))

        code, out, _ = self.run_cli("--json", "allowlist")
        rows = json.loads(out)["allowlist"]
        crates = next(row for row in rows if row["entry"] == "crates.io")
        self.assertEqual((crates["enabled"], crates["provided"], crates["source"]), (False, True, "rust"))
        self.assertEqual(rows, json.loads(self.run_cli("--json", "show")[1])["allowlist"], "what the app shows")

        code, _, err = self.run_cli("--all", "allowlist")
        self.assertEqual((code, err), (1, "error: the list is one profile's: -p NAME picks which"))
        code, _, err = self.run_cli("allowlist", "disable")
        self.assertEqual(code, 1)
        self.assertIn("allowlist disable needs an entry", err)

    # -- the first profile, and the VS Code port --------------------------------

    def test_the_first_up_on_a_terminal_asks_for_the_languages(self):
        empty = self.root.parent / "first"

        code, _, err = self.run_cli("up", config=empty, answers=["cobol", "python, docker"])
        self.assertEqual(code, 0)
        self.assertIn("No profile 'default' yet", err)
        self.assertIn("unknown language 'cobol'", err, "a wrong answer is asked again")
        self.assertIn("profile 'default' made (VS Code on port 11111)", err)

        made = profiles.get(empty, "default")
        self.assertEqual(langs.enabled_keys(envfile.read(made.env_file)), ["python", "docker"])
        self.assertIn("up -d", " ".join(self.runner.calls[-1]))

    def test_an_empty_answer_is_the_tools(self):
        empty = self.root.parent / "first"

        self.assertEqual(self.run_cli("up", config=empty, answers=[""])[0], 0)

        made = profiles.get(empty, "default")
        tools = [lang.key for lang in langs.CATALOGUE if lang.kind == "tool"]
        self.assertEqual(langs.enabled_keys(envfile.read(made.env_file)), tools)

    def test_without_a_terminal_up_says_how_to_make_one(self):
        code, _, err = self.run_cli("up", config=self.root.parent / "first")

        self.assertEqual(code, 1)
        self.assertIn("hardcode profile add default --lang python,docker", err)
        self.assertEqual(self.runner.commands(" up "), [])

    def test_languages_lists_the_catalogue_before_any_profile(self):
        code, out, _ = self.run_cli("--json", "languages", config=self.root.parent / "first")

        self.assertEqual(code, 0)
        listed = json.loads(out)["languages"]
        self.assertEqual([lang["key"] for lang in listed], list(langs.LANGS))
        self.assertFalse(any(lang["enabled"] for lang in listed))

    def test_port_moves_vscode(self):
        self.run_cli("profile", "add", "b", "--lang", "")

        code, out, _ = self.run_cli("port", "12000")
        self.assertEqual(
            (code, out), (0, "VS Code on port 12000 (profile 'default' isn't running; applies when it starts)")
        )
        self.assertEqual(envfile.read(profiles.get(self.root, "default").env_file)["VSCODE_PORT"], "12000")

        code, _, err = self.run_cli("port", "11112")
        self.assertEqual(code, 1)
        self.assertIn("port 11112 is used by profile 'b'", err)

        # Running: only the gateway is recreated, a few seconds later; VS
        # Code carries on.
        code, out, _ = self.run_cli("port", "12002", running=True)
        self.assertEqual(code, 0)
        self.assertIn("(gateway restarts in 5 seconds; hardcode apply: now)", out)
        self.assertEqual(applier.load(profiles.get(self.root, "default")).services, ["gateway"])

    def test_a_port_the_gateway_cannot_take_is_undone(self):
        # Another program holds it: Docker refuses to publish it.
        runner = FakeRunner(running=True)
        runner.fail_on.add("--no-deps gateway")

        self.assertEqual(self.run_cli("port", "12003", runner=runner)[0], 0, "the change is fine itself")
        code, _, err = self.run_cli("apply", runner=runner)

        self.assertEqual(code, 1)
        self.assertIn("VS Code on port 12003: undone, as", err)
        self.assertEqual(envfile.read(profiles.get(self.root, "default").env_file)["VSCODE_PORT"], "11111")

    def test_open(self):
        with mock.patch("webbrowser.open") as opened:
            code, out, _ = self.run_cli("open", running=True)
        self.assertEqual(code, 0)
        self.assertEqual(out, "http://localhost:8000/?tkn=tok3n")
        opened.assert_called_once_with("http://localhost:8000/?tkn=tok3n")

        with mock.patch("webbrowser.open") as opened:
            self.run_cli("open", "--no-browser", running=True)
        opened.assert_not_called()

        # The app reads the URL from the JSON result.
        code, out, _ = self.run_cli("--json", "open", "--no-browser", running=True)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["results"][0]["data"]["url"], "http://localhost:8000/?tkn=tok3n")

        code, _, err = self.run_cli("open")
        self.assertEqual(code, 1)
        self.assertIn("isn't running", err)

    def test_open_uses_where_the_gateway_publishes(self):
        # Not the .env's port: the gateway may have started before an edit,
        # or on another address (VSCODE_BIND).
        runner = FakeRunner(running=True)
        runner.published = "192.168.1.5:8123"

        code, out, _ = self.run_cli("open", "--no-browser", runner=runner)

        self.assertEqual((code, out), (0, "http://192.168.1.5:8123/?tkn=tok3n"))

    def test_open_says_what_is_missing(self):
        # Running, but VS Code hasn't written its token yet.
        runner = FakeRunner(running=True)
        runner.fail_on.add("connection-token")
        code, _, err = self.run_cli("open", "--no-browser", runner=runner)
        self.assertEqual(code, 1)
        self.assertIn("still starting", err)

        # Running, but the gateway isn't (it couldn't publish its ports).
        runner = FakeRunner(running=True)
        runner.fail_on.add(" port gateway")
        code, _, err = self.run_cli("open", "--no-browser", runner=runner)
        self.assertEqual(code, 1)
        self.assertIn("the gateway isn't running", err)

    def test_logs_as_json(self):
        runner = FakeRunner(running=True)
        runner.log_output = "Squid Cache: started\nAccepting HTTP connections\n"

        code, out, _ = self.run_cli("--json", "logs", "--tail", "50", "squid", runner=runner)

        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(out),
            {"services": ["squid"], "logs": "Squid Cache: started\nAccepting HTTP connections\n"},
        )
        self.assertIn("logs --no-color --tail 50 --no-log-prefix squid", " ".join(self.runner.calls[-1]))

        # Every service's, prefixed with its name, 200 lines unless told otherwise.
        self.run_cli("--json", "logs")
        self.assertTrue(" ".join(self.runner.calls[-1]).endswith("logs --no-color --tail 200"))

        code, _, err = self.run_cli("--json", "logs", "-f", "squid")
        self.assertEqual(code, 1)
        self.assertIn("can't be followed with --json", err)

        code, _, err = self.run_cli("logs", "--tail", "-1")
        self.assertEqual(code, 1)
        self.assertIn("--tail", err)

    def test_show_blocked_ps_json(self):
        self.run_cli("allow", "x.org")

        _, out, _ = self.run_cli("--json", "show")
        state = json.loads(out)
        self.assertEqual(state["profile"], "default")
        self.assertEqual(state["profiles"], [{"name": "default", "port": "11111"}])
        self.assertTrue(any(entry["entry"] == "x.org" for entry in state["allowlist"]))
        self.assertEqual([lang["key"] for lang in state["langs"]][:2], ["python", "node"])

        _, out, _ = self.run_cli("show")
        self.assertIn("profile default", out)
        self.assertIn("languages:", out)

        _, out, _ = self.run_cli("--json", "blocked", "--since", "2h", running=True)
        self.assertEqual(json.loads(out), {"blocked": []})
        _, out, _ = self.run_cli("blocked")
        self.assertIn("nothing blocked", out)

        _, out, _ = self.run_cli("--json", "ps", running=True)
        status = json.loads(out)
        self.assertEqual([service["service"] for service in status["services"]], ["squid", "allowlist-watch", "vscode"])
        self.assertIs(status["docker"], True)
        self.assertEqual(status["error"], "")

        # Compose couldn't list them: the app shows why, not "not created".
        runner = FakeRunner(running=True)
        runner.fail_on.add("ps -a")
        _, out, _ = self.run_cli("--json", "ps", runner=runner)
        self.assertIn("simulated failure", json.loads(out)["error"])

    def test_the_docker_snap_is_refused_with_the_docs_install_section(self):
        code, _, err = self.run_cli("up", docker="/snap/bin/docker")
        self.assertNotEqual(code, 0)
        self.assertIn("Docker on this computer is the snap, which can't run HardCode", err)
        self.assertIn(compose.INSTALL_DOCS, err)

        # What the app reads: Docker is there, and the refusal is the reason.
        _, out, _ = self.run_cli("--json", "ps", docker="/snap/bin/docker")
        status = json.loads(out)
        self.assertEqual((status["docker"], status["error"]), (True, compose.SNAP_REFUSAL))


class GithubRepositoryCliTests(CliTestCase):
    """
    ``github:OWNER/REPO`` on the command line: allow, deny, enable, disable
    and remove all take it.
    """

    def test_allowlist_remove_takes_a_repository(self):
        self.run_cli("allow", "github:me/app", "rw")
        self.assertEqual(len(allowlist.find(self.allowlist_of("default"), "github.com/me/app.git")), 1)

        code, out, _ = self.run_cli("allowlist", "remove", "github:me/app")

        self.assertEqual(code, 0)
        self.assertIn("removed github.com/me/app.git; removed api.github.com/repos/me/app", out)
        self.assertNotIn("github.com/me/app", self.allowlist_of("default"))


class LanCliTests(CliTestCase):
    """
    Names on your network, from the command line: asked about on a terminal,
    said without one, and listed, added, moved and removed with lan.
    """

    QUESTION = "gitlab.example.com resolves to 192.168.71.200, on your network. Let it reach that address? [y/N] "

    def pins_of(self, profile="default"):
        return [(pin.name, pin.address) for pin in allowlist.pins(self.allowlist_of(profile))]

    def test_allow_asks_on_a_terminal(self):
        code, out, _ = self.run_cli("allow", "gitlab.example.com:22222", "ssh", answers=["y"])

        self.assertEqual((code, self.asked), (0, [self.QUESTION]))
        self.assertIn("gitlab.example.com may reach 192.168.71.200 on your network", out)
        self.assertEqual(self.pins_of(), [("gitlab.example.com", "192.168.71.200")])

    def test_a_no_adds_the_entry_and_says_how_to_pin(self):
        code, out, _ = self.run_cli("allow", "gitlab.example.com", answers=["n"])

        self.assertEqual(code, 0)
        self.assertIn("allowed gitlab.example.com", out)
        self.assertIn("hardcode lan add gitlab.example.com 192.168.71.200", out)
        self.assertEqual(self.pins_of(), [])

    def test_lan_says_yes_without_asking(self):
        code, out, _ = self.run_cli("allow", "gitlab.example.com", "--lan", answers=[])

        self.assertEqual((code, self.asked), (0, []))
        self.assertEqual(self.pins_of(), [("gitlab.example.com", "192.168.71.200")])

    def test_without_a_terminal_json_says_what_the_app_should_ask(self):
        code, out, _ = self.run_cli("--json", "allow", "gitlab.example.com/group")

        result = json.loads(out)["results"][0]
        self.assertEqual(code, 0)
        self.assertEqual(
            result["data"]["lan"], {"name": "gitlab.example.com", "address": "192.168.71.200", "pinned": False}
        )
        self.assertEqual(self.pins_of(), [])

    def test_lan_add_list_check_and_remove(self):
        self.run_cli("allow", "gitlab.example.com:22222", "ssh")

        code, out, _ = self.run_cli("lan", "add", "gitlab.example.com:22222")
        self.assertEqual(code, 0)
        self.assertIn("gitlab.example.com may reach 192.168.71.200 on your network", out)

        code, out, _ = self.run_cli("--json", "lan")
        listed = json.loads(out)["lan"]
        self.assertEqual(
            [(item["name"], item["address"], item["state"]) for item in listed],
            [("gitlab.example.com", "192.168.71.200", "ok")],
        )

        code, out, _ = self.run_cli("lan")
        self.assertIn("gitlab.example.com", out)
        self.assertIn("192.168.71.200", out)

        code, out, _ = self.run_cli("--json", "lan", "check", "gitlab.example.com")
        self.assertEqual(json.loads(out)["pinned"], "192.168.71.200")

        code, out, _ = self.run_cli("--json", "allowlist")
        self.assertEqual([pin["name"] for pin in json.loads(out)["lan"]], ["gitlab.example.com"])
        code, out, _ = self.run_cli("allowlist")
        self.assertIn("On your network", out)

        code, out, _ = self.run_cli("lan", "remove", "gitlab.example.com")
        self.assertEqual(code, 0)
        self.assertIn("gitlab.example.com no longer reaches your network", out)
        self.assertEqual(self.pins_of(), [])

    def test_lan_add_takes_an_address_and_refuses_bad_ones(self):
        code, _, _ = self.run_cli("lan", "add", "registry.example.com", "fd00::10")
        self.assertEqual(code, 0)
        self.assertEqual(self.pins_of(), [("registry.example.com", "fd00::10")])

        code, _, err = self.run_cli("lan", "add", "metadata.example.com", "169.254.169.254")
        self.assertEqual(code, 1)
        self.assertIn("can't be pinned", err)

        code, _, err = self.run_cli("lan", "add", "nowhere.example.com")
        self.assertEqual(code, 1)
        self.assertIn("doesn't resolve to an address on your network from here", err)

        code, _, err = self.run_cli("lan", "remove", "registry.example.com", "fd00::10")
        self.assertEqual(code, 1)
        self.assertIn("only lan add takes an address", err)

    def test_this_computer_is_refused_before_anything_is_asked(self):
        self.this_computer = {"192.168.71.200"}
        refused = "192.168.71.200 is this computer: HardCode never lets a sandbox reach it"

        code, out, _ = self.run_cli("allow", "gitlab.example.com", answers=[])
        self.assertEqual((code, self.asked), (0, []), "no question")
        self.assertIn(f"gitlab.example.com resolves to 192.168.71.200, and {refused}", out)
        self.assertNotIn("on your network", out)

        code, _, err = self.run_cli("lan", "add", "gitlab.example.com", "192.168.71.200")
        self.assertEqual((code, err), (1, f"error: {refused}"))

        code, out, _ = self.run_cli("lan", "check", "gitlab.example.com")
        self.assertEqual(out, f"gitlab.example.com resolves to 192.168.71.200, and {refused}")
        self.assertEqual(self.pins_of(), [])

    def test_a_start_leaves_out_a_hand_written_pin_to_this_computer(self):
        self.this_computer = {"192.168.1.5"}
        path = profiles.get(self.root, "default").allowlist_file
        path.write_text(path.read_text(encoding="utf-8") + "lan nas.example.com 192.168.1.5\n", encoding="utf-8")

        code, _, err = self.run_cli("up")

        self.assertEqual(code, 0)
        self.assertIn(
            "on your network: allowlist.txt pins nas.example.com to 192.168.1.5, which is this computer: left out", err
        )
        given = profiles.get(self.root, "default").proxy_allowlist_file.read_text(encoding="utf-8")
        self.assertNotIn("\nlan nas.example.com", given)

    def test_a_start_warns_about_a_name_that_moved_and_keeps_its_pin(self):
        self.run_cli("lan", "add", "gitlab.example.com", "192.168.71.199")

        code, _, err = self.run_cli("up")

        self.assertEqual(code, 0)
        self.assertIn("on your network: gitlab.example.com now resolves to 192.168.71.200, not 192.168.71.199", err)
        self.assertEqual(self.pins_of(), [("gitlab.example.com", "192.168.71.199")])


if __name__ == "__main__":
    unittest.main()
