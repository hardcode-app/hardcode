"""
Tests for the language catalogue (hardcode_cli.langs), and for the files
that repeat parts of it.
"""

import re
import unittest

from hardcode_cli import langs, ops
from hardcode_cli.langs import LangError

from .helpers import REPO


class LangTests(unittest.TestCase):
    def test_canonical_and_aliases(self):
        self.assertEqual(langs.normalise("python"), "python")
        self.assertEqual(langs.normalise("C++"), "cpp")
        self.assertEqual(langs.normalise("c#"), "dotnet")
        self.assertEqual(langs.normalise("JS"), "node")
        self.assertEqual(langs.normalise("golang"), "go")
        self.assertEqual(langs.normalise("claude"), "claude_cli")
        self.assertEqual(langs.normalise("build-essentials"), "build_essentials")

    def test_env_key(self):
        self.assertEqual(langs.env_key("c#"), "INSTALL_DOTNET")
        self.assertEqual(langs.env_key("claude_cli"), "INSTALL_CLAUDE_CLI")
        self.assertEqual(langs.CATALOGUE[0].env_key, "INSTALL_PYTHON")

    def test_enabled_in(self):
        state = langs.enabled_in({"INSTALL_DOTNET": "false", "INSTALL_GO": "true"})

        enabled = {item["key"]: item["enabled"] for item in state}
        self.assertFalse(enabled["dotnet"])
        self.assertTrue(enabled["go"])
        self.assertFalse(enabled["python"], "unset is off, as compose.yaml's default")

        self.assertEqual([item["key"] for item in state], list(langs.LANGS))
        self.assertEqual(state[0]["name"], "Python")
        self.assertEqual(state[0]["domains"], ["pypi.org", "files.pythonhosted.org"])
        self.assertIn("ms-python.python", state[0]["extensions"])

        # The Dockerfile installs a language only for exactly "true".
        state = langs.enabled_in({"INSTALL_JAVA": "TRUE", "INSTALL_RUST": "False", "INSTALL_GO": ""})
        enabled = {item["key"]: item["enabled"] for item in state}
        self.assertFalse(enabled["java"])
        self.assertFalse(enabled["rust"])
        self.assertFalse(enabled["go"], "empty is off too")

    def test_parse_list(self):
        self.assertEqual(langs.parse_list("Go, python ts"), ["python", "node", "go"])
        self.assertEqual(langs.parse_list(""), [])

        with self.assertRaises(LangError):
            langs.parse_list("python,cobol")

    def test_both_coding_agents_bring_their_hosts_and_extension(self):
        for key, host, extension in (
            ("claude_cli", "api.anthropic.com", "anthropic.claude-code"),
            ("codex_cli", "api.openai.com", "openai.chatgpt"),
        ):
            agent = langs.BY_KEY[key]
            self.assertEqual(agent.kind, "tool")
            self.assertIn(host, agent.rw)
            self.assertEqual(agent.extensions, (extension,))
            self.assertNotIn(extension, langs.CORE_EXTENSIONS)

    def test_uploads_only_where_a_language_says_so(self):
        for lang in langs.CATALOGUE:
            self.assertLessEqual(set(lang.rw), set(lang.domains), lang.key)

        codex = langs.BY_KEY["codex_cli"]
        self.assertEqual(codex.kind, "tool")
        self.assertIn("api.openai.com", codex.rw)
        self.assertEqual(langs.describe(codex, True)["rw"], list(codex.rw))

    def test_the_extension_list(self):
        text = langs.extension_list(["rust", "codex_cli"], ["me.mine", "rust-lang.rust-analyzer"])
        lines = text.splitlines()
        ids = [line for line in lines if not line.startswith("#")]

        # Every container's first, then the languages' and tools', then the
        # profile's own, each once; the reviewed exception marked; the end
        # marker last, so a list caught half-written is never taken for a
        # shorter one.
        self.assertEqual(
            ids,
            [
                "redhat.vscode-yaml",
                "rust-lang.rust-analyzer",
                "vadimcn.vscode-lldb unverified-ok",
                "openai.chatgpt",
                "me.mine",
            ],
        )
        self.assertEqual(lines[-1], langs.EXTENSION_LIST_END)

    def test_extension_ids(self):
        self.assertEqual(langs.normalise_extension(" Ms-Python.Python "), "ms-python.python")
        for bad in ("python", "a.b.c", ".x", "a b.c"):
            with self.assertRaises(LangError, msg=bad):
                langs.normalise_extension(bad)

        self.assertEqual(langs.parse_extensions("a.b, C.D  junk a.b"), ["a.b", "c.d"])

    def test_extension_sources(self):
        rows = langs.extension_sources(["go"], ["golang.go", "me.mine"])
        by_id = {(row["id"], row["source"]): row for row in rows}

        self.assertTrue(by_id[("redhat.vscode-yaml", "core")]["enabled"])
        self.assertFalse(by_id[("anthropic.claude-code", "claude_cli")]["enabled"], "Claude Code's comes with its tool")
        self.assertTrue(by_id[("golang.go", "go")]["enabled"])
        self.assertFalse(by_id[("ms-python.python", "python")]["enabled"], "listed, but its language is off")
        self.assertFalse(by_id[("me.mine", "custom")]["duplicate"])
        self.assertTrue(by_id[("golang.go", "custom")]["duplicate"], "a custom one a language brings anyway")

    def test_unknown(self):
        with self.assertRaisesRegex(LangError, "unknown language 'cobol'"):
            langs.normalise("cobol")


class CatalogueAgreementTests(unittest.TestCase):
    """
    The files that repeat parts of the catalogue say the same as it does.
    """

    def test_the_vscode_container_is_handed_the_extension_list(self):
        compose = (REPO / "compose.yaml").read_text(encoding="utf-8")

        self.assertIn("/vscode-extensions.txt:/etc/hardcode/extensions/wanted:ro", compose)
        self.assertFalse((REPO / "images" / "vscode" / "extensions.txt").exists(), "no extensions in the image")

    def test_the_provided_entries_are_only_in_the_catalogue(self):
        # The template's allowlist is a profile's own part: nothing in it is
        # allowed, so every entry allowed comes from the catalogue, where an
        # update reaches every profile.
        from hardcode_cli import allowlist

        for path in (
            REPO / "template" / "allowlist.txt",
            REPO / "helm" / "hardcode" / "files" / "allowlist.txt",
        ):
            text = path.read_text(encoding="utf-8")

            self.assertEqual([entry.entry for entry in allowlist.parse(text) if entry.enabled], [], path)
            self.assertIn(allowlist.CUSTOM_HEADER, text)
            self.assertIn(f"{allowlist.OFF_HEADER}\n{allowlist.OFF_NOTE}\n", text)

    def test_every_provided_entry_is_valid_and_narrow(self):
        from hardcode_cli import allowlist

        blocks = langs.blocks(langs.LANGS)
        keys = [block.key for block in blocks]
        self.assertEqual(len(keys), len(set(keys)), "a block's key is its own")

        for block in blocks:
            self.assertEqual(len(set(block.domains)), len(block.domains), block.key)
            self.assertEqual(set(block.rw) - set(block.domains), set(), f"{block.key}: rw names its own entries")

            for domain in block.domains:
                self.assertIsNone(allowlist.validate(domain), domain)
                self.assertEqual(allowlist.canonical(domain), domain, "written as the CLI writes entries")

                # Never a whole git host: a path on it, at most.
                host = domain.partition("/")[0].lstrip(".")
                if host in ("github.com", "gitlab.com", "bitbucket.org"):
                    self.assertIn("/", domain, domain)

        provided = {domain for block in blocks for domain in block.domains}
        self.assertEqual(set(langs.NOTES) - provided, set(), "notes are for provided entries")

    def test_the_proxy_is_handed_the_rendered_allowlist(self):
        compose = (REPO / "compose.yaml").read_text(encoding="utf-8")

        self.assertIn("/proxy-allowlist.txt:/config/allowlist.txt:ro", compose)

    def test_the_charts_allowlist_is_the_templates(self):
        template = REPO / "template" / "allowlist.txt"
        chart = REPO / "helm" / "hardcode" / "files" / "allowlist.txt"

        self.assertEqual(chart.read_text(encoding="utf-8"), template.read_text(encoding="utf-8"))

    def test_every_language_is_a_build_arg_and_reaches_the_vscode(self):
        # The image built on its own has every toolchain (Kubernetes' default
        # image); a profile's languages are off unless its .env says so.
        dockerfile = (REPO / "images" / "vscode" / "Dockerfile").read_text(encoding="utf-8")
        compose = (REPO / "compose.yaml").read_text(encoding="utf-8")
        env = (REPO / "template" / ".env").read_text(encoding="utf-8")

        for lang in langs.CATALOGUE:
            self.assertIn(f"ARG {lang.env_key}=true", dockerfile)
            self.assertIn(f"{lang.key}=${{{lang.env_key}}}", dockerfile, "the image's languages label")
            self.assertIn(f"{lang.env_key}: ${{{lang.env_key}:-false}}", compose)
            self.assertIn(lang.env_key.removeprefix("INSTALL_"), env, "the template's reference names it")

        self.assertIn(f"LABEL {ops.IMAGE_LANGUAGES_LABEL}=", dockerfile, "the label the CLI reads")

    def test_the_templates_env_is_a_reference_of_compose_yamls_defaults(self):
        # A profile's .env holds only what it changes: the template's is all
        # comments, each setting with compose.yaml's default.
        compose = (REPO / "compose.yaml").read_text(encoding="utf-8")
        env = (REPO / "template" / ".env").read_text(encoding="utf-8")

        settings = [line for line in env.splitlines() if line and not line.startswith("#")]
        self.assertEqual(settings, [], "nothing set")

        defaults = dict(re.findall(r"\$\{([A-Z][A-Z0-9_]*):-([^}]*)\}", compose))
        for key, value in defaults.items():
            if key.startswith(("INSTALL_", "HARDCODE_")):
                continue
            self.assertIn(f"\n#{key}={value}\n", env, key)

        self.assertTrue(env.startswith("# --- Every setting, with its default"))


if __name__ == "__main__":
    unittest.main()
