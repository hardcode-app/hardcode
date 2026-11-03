"""
Tests for the allowlist grammar and edits (hardcode_cli.allowlist).
"""

import unittest
from unittest import mock

from hardcode_cli import allowlist, langs
from hardcode_cli.allowlist import AllowlistError, Entry

from .helpers import REPO, updated_catalogue

TEMPLATE = REPO / "template" / "allowlist.txt"

# Sections, an entry with a flag and a comment, a commented-out entry, a
# plain comment, an indented entry and an invalid line, numbered from 0.
SAMPLE = """\
# header
# --- Anthropic ----
api.anthropic.com   rw   # the API
claude.com
# --- Git ----
#github.com/myuser  rw
# plain comment that is not an entry
  indented.example.com
bad entry here
# --- Custom ----
"""


class ValidateTests(unittest.TestCase):
    def test_accepts_hosts_wildcards_paths_and_ssh(self):
        acceptable = [
            ("github.com/myuser", ""),
            (".vscode-cdn.net", ""),
            ("ssh.github.com:443", "ssh"),
            ("github.com", "ssh"),
            ("Example.COM", "rw"),
            ("a.b.c.d/x/y-z", ""),
        ]

        for entry, flag in acceptable:
            self.assertIsNone(allowlist.validate(entry, flag), entry)

    def test_rejects_bad_entries(self):
        reasons = {
            "": "enter a host",
            "com": "not a valid hostname",
            ".github.com/x": "can't have a path",
            "github.com/x?y": "can't contain ? or #",
            "git hub.com": "spaces",
            "a.b:443": "only ssh entries take a port",
            "-bad.com": "not a valid hostname",
        }
        for entry, reason in reasons.items():
            self.assertIn(reason, allowlist.validate(entry) or "", entry)

        self.assertIn("unknown flag", allowlist.validate("a.b", "bogus"))

        self.assertIn("bad port", allowlist.validate("github.com:99999", "ssh"))
        self.assertIn("bad port", allowlist.validate("github.com:abc", "ssh"))
        self.assertIn("host[:port]", allowlist.validate(".github.com", "ssh"))
        self.assertIn("host[:port]", allowlist.validate("github.com/x", "ssh"))
        self.assertIn("host[:port]", allowlist.validate("a.b:1:2", "ssh"))


class KindTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(allowlist.kind_of("github.com", "ssh"), "ssh")
        self.assertEqual(allowlist.kind_of(".x.com"), "subdomains")
        self.assertEqual(allowlist.kind_of("x.com/p"), "path")
        self.assertEqual(allowlist.kind_of("x.com"), "host")
        self.assertEqual(Entry("x.com/p").kind, "path")


class ParseLineTests(unittest.TestCase):
    def test_entry_with_flag_and_comment(self):
        self.assertEqual(
            allowlist.parse_line("api.anthropic.com   rw   # the API"),
            Entry("api.anthropic.com", "rw", "the API", enabled=True, invalid=False),
        )

    def test_lowercases(self):
        self.assertEqual(allowlist.parse_line("GitHub.com/X").entry, "github.com/x")

    def test_disabled_entry(self):
        self.assertEqual(
            allowlist.parse_line("#github.com/myuser  rw"),
            Entry("github.com/myuser", "rw", "", enabled=False, invalid=False),
        )
        self.assertEqual(allowlist.parse_line("  #  a.b  "), Entry("a.b", "", "", enabled=False, invalid=False))

    def test_plain_comments_and_blanks_are_not_entries(self):
        for line in ["", "   ", "# a comment", "# --- Section ---", "#bad entry here", "# key: value"]:
            self.assertIsNone(allowlist.parse_line(line), repr(line))

    def test_invalid_active_lines_are_flagged(self):
        self.assertTrue(allowlist.parse_line("bad entry here").invalid)
        self.assertTrue(allowlist.parse_line("com").invalid)


class ParseTests(unittest.TestCase):
    def test_sections_lines_and_entries(self):
        entries = allowlist.parse(SAMPLE)

        self.assertEqual(
            [(entry.entry, entry.flag, entry.enabled, entry.section, entry.line) for entry in entries],
            [
                ("api.anthropic.com", "rw", True, "Anthropic", 2),
                ("claude.com", "", True, "Anthropic", 3),
                ("github.com/myuser", "rw", False, "Git", 5),
                ("indented.example.com", "", True, "Git", 7),
                ("bad", "entry", True, "Git", 8),
            ],
        )
        self.assertTrue(entries[-1].invalid)
        self.assertEqual(entries[0].comment, "the API")

    def test_find_is_case_insensitive(self):
        self.assertEqual([entry.line for entry in allowlist.find(SAMPLE, "GitHub.com/MyUser")], [5])
        self.assertEqual(allowlist.find(SAMPLE, "nope.com"), [])


class FormatTests(unittest.TestCase):
    def test_columns(self):
        self.assertEqual(allowlist.format_entry("a.b"), "a.b")
        self.assertEqual(allowlist.format_entry("a.b", "rw"), "a.b                         rw")
        self.assertEqual(allowlist.format_entry("a.b", "", "note"), "a.b                                # note")
        self.assertEqual(allowlist.format_entry("a.b", "ssh", "note"), "a.b                         ssh    # note")


class EditTests(unittest.TestCase):
    def test_add_appends_under_custom_header(self):
        text = "# --- Defaults ----\npypi.org\n"

        added = allowlist.add(text, "GitHub.com/MyUser")

        self.assertIn(allowlist.CUSTOM_HEADER, added)
        self.assertTrue(added.endswith("github.com/myuser\n"))
        self.assertEqual(added.count(allowlist.CUSTOM_HEADER), 1)
        self.assertEqual(allowlist.add(added, "other.org").count(allowlist.CUSTOM_HEADER), 1)

    def test_add_reenables_and_updates_flag_keeping_comment(self):
        text = allowlist.add(SAMPLE, "github.com/myuser", "")

        found = allowlist.find(text, "github.com/myuser")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].enabled)
        self.assertEqual(found[0].flag, "")

        text = allowlist.add(text, "api.anthropic.com", "")
        self.assertEqual(allowlist.find(text, "api.anthropic.com")[0].comment, "the API")

    def test_add_is_idempotent(self):
        once = allowlist.add(SAMPLE, "new.example.com")
        self.assertEqual(allowlist.add(once, "new.example.com"), once)

    def test_add_rejects_invalid(self):
        with self.assertRaises(AllowlistError):
            allowlist.add(SAMPLE, "com")
        with self.assertRaises(AllowlistError):
            allowlist.add(SAMPLE, "a.b", "bogus")

    def test_set_enabled_round_trip(self):
        off = allowlist.set_enabled(SAMPLE, 2, False)
        self.assertIn("#api.anthropic.com   rw   # the API", off)
        self.assertEqual(allowlist.set_enabled(off, 2, False), off)
        self.assertEqual(allowlist.set_enabled(off, 2, True), SAMPLE)

        # Indentation survives a round trip too.
        indented_off = allowlist.set_enabled(SAMPLE, 7, False)
        self.assertEqual(allowlist.set_enabled(indented_off, 7, True), SAMPLE)

        # Enabling removes only the "#": a space after it stays.
        self.assertEqual(allowlist.set_enabled("# spaced.example.com\n", 0, True), " spaced.example.com\n")
        self.assertTrue(allowlist.parse_line(" spaced.example.com").enabled)

    def test_set_enabled_errors(self):
        with self.assertRaises(AllowlistError):
            allowlist.set_enabled(SAMPLE, 99, True)
        with self.assertRaises(AllowlistError):
            allowlist.set_enabled(SAMPLE, 0, True)

    def test_remove(self):
        text = allowlist.remove(SAMPLE, 3)

        self.assertNotIn("claude.com", text)
        self.assertEqual(len(text.splitlines()), len(SAMPLE.splitlines()) - 1)
        with self.assertRaises(AllowlistError):
            allowlist.remove(SAMPLE, 99)

    def test_entry_level_helpers(self):
        twice = SAMPLE + "claude.com\n"

        disabled = allowlist.set_entry_enabled(twice, "claude.com", False)
        self.assertTrue(all(not entry.enabled for entry in allowlist.find(disabled, "claude.com")))
        self.assertEqual(allowlist.set_entry_enabled(SAMPLE, "absent.com", False), SAMPLE)

        removed = allowlist.remove_entry(twice, "claude.com")
        self.assertEqual(allowlist.find(removed, "claude.com"), [])
        self.assertEqual(allowlist.remove_entry(SAMPLE, "absent.com"), SAMPLE)

    def test_output_always_ends_with_newline(self):
        self.assertTrue(allowlist.add("pypi.org", "a.b").endswith("\n"))
        self.assertTrue(allowlist.remove("a.b\nc.d\n", 0).endswith("\n"))


class IdentityTests(unittest.TestCase):
    """
    An SSH entry and an HTTPS entry for one host are different things.
    """

    TEXT = "#github.com ssh   # port 22\n#github.com/myuser rw\nexample.com\n"

    def test_allow_does_not_resurrect_the_ssh_example(self):
        text = allowlist.add(self.TEXT, "github.com")

        self.assertIn("#github.com ssh   # port 22\n", text, "the commented SSH example is untouched")
        self.assertTrue(text.endswith("github.com\n"), "a new HTTPS line is appended instead")
        self.assertEqual(len(allowlist.find(text, "github.com")), 2)
        self.assertEqual([entry.flag for entry in allowlist.find(text, "github.com", ssh=True)], ["ssh"])
        self.assertEqual([entry.flag for entry in allowlist.find(text, "github.com", ssh=False)], [""])

    def test_ssh_form_is_addressed_explicitly(self):
        text = allowlist.add(self.TEXT, "github.com")

        both = allowlist.add(text, "github.com", "ssh")
        self.assertTrue(
            allowlist.find(both, "github.com", ssh=True)[0].enabled,
            "allow … ssh uncomments the SSH example",
        )

        off = allowlist.set_entry_enabled(both, "github.com", False)
        self.assertFalse(allowlist.find(off, "github.com", ssh=False)[0].enabled)
        self.assertTrue(
            allowlist.find(off, "github.com", ssh=True)[0].enabled,
            "the HTTPS toggle leaves the SSH entry alone",
        )

        gone = allowlist.remove_entry(both, "github.com", ssh=True)
        self.assertEqual([entry.flag for entry in allowlist.find(gone, "github.com")], [""])

    def test_lines_in_any_valid_spelling_are_found_by_their_text(self):
        text = "#github.com/ourcompany/   rw\nExample.com.\n"

        self.assertEqual([entry.line for entry in allowlist.find(text, "github.com/ourcompany")], [0])
        self.assertEqual([entry.line for entry in allowlist.find(text, "example.com")], [1])

        enabled = allowlist.add(text, "github.com/ourcompany", "rw")
        self.assertEqual(enabled.count("ourcompany"), 1, "re-enabled in place, not appended again")
        self.assertTrue(allowlist.find(enabled, "github.com/ourcompany")[0].enabled)

        disabled = allowlist.set_entry_enabled(text, "example.com", False)
        self.assertFalse(allowlist.find(disabled, "example.com")[0].enabled)

    def test_entries_are_written_in_canonical_form(self):
        self.assertEqual(allowlist.canonical(" Example.COM. "), "example.com")
        self.assertEqual(allowlist.canonical("github.com/team/"), "github.com/team")
        self.assertEqual(allowlist.canonical("example.com/"), "example.com")
        self.assertEqual(allowlist.canonical("ssh.github.com:443", "ssh"), "ssh.github.com:443")

        self.assertTrue(allowlist.add("", "GitHub.com/Team/").endswith("github.com/team\n"))
        self.assertEqual(allowlist.kind_of("example.com/"), "host")
        self.assertEqual(allowlist.kind_of("example.com/x"), "path")

        # Typed input is normalised, but a line in the file is held to the
        # grammar the renderer enforces.
        self.assertTrue(allowlist.add("", "example.com..").endswith("example.com\n"))
        self.assertIsNotNone(allowlist.validate("example.com.."))
        self.assertIsNone(allowlist.validate("example.com."), "one trailing dot is the renderer's tolerance too")

        self.assertEqual(allowlist.validate("example.com/a/../b"), "paths can't contain . or .. segments")
        self.assertEqual(allowlist.validate("example.com/./a"), "paths can't contain . or .. segments")
        self.assertIsNone(allowlist.validate("example.com/.well-known/x"))


class OffLineTests(unittest.TestCase):
    """
    The lines that switch a provided entry off: "off <entry> [ssh]".
    """

    def test_an_off_line(self):
        item = allowlist.parse_line("off Crates.io   # we use a mirror")

        self.assertEqual(
            (item.entry, item.flag, item.comment, item.off_line), ("crates.io", "", "we use a mirror", True)
        )
        self.assertFalse(item.invalid)

    def test_an_off_line_for_an_ssh_entry(self):
        item = allowlist.parse_line("off ssh.github.com:443 ssh")

        self.assertEqual((item.entry, item.flag, item.off_line), ("ssh.github.com:443", "ssh", True))

    def test_off_lines_the_proxy_could_not_read_are_invalid(self):
        for line in ["off crates.io rw", "off not_a_host", "off crates.io ssh extra"]:
            self.assertTrue(allowlist.parse_line(line).invalid, line)

    def test_commented_out_it_is_just_a_comment(self):
        self.assertIsNone(allowlist.parse_line("#off crates.io"))

    def test_find_leaves_off_lines_out(self):
        text = "crates.io\noff crates.io\n"

        self.assertEqual([item.line for item in allowlist.find(text, "crates.io")], [0])
        self.assertEqual([item.line for item in allowlist.off_lines(text, "crates.io")], [1])


class EffectiveTests(unittest.TestCase):
    """
    The full list: the profile's own, then the provided entries, block by block.
    """

    def test_every_profiles_blocks_then_the_languages_in_catalogue_order(self):
        entries = allowlist.effective("", ["rust", "python", "cpp"])
        blocks = list(dict.fromkeys(item.source for item in entries))

        self.assertEqual(blocks, ["vscode", "os_packages", "registries", "github_raw", "python", "rust"])
        self.assertTrue(all(item.provided and item.enabled for item in entries))

    def test_a_provided_entry_says_where_it_comes_from(self):
        entries = {item.entry: item for item in allowlist.effective("", ["rust"])}

        crates = entries["crates.io"]
        self.assertEqual((crates.section, crates.language, crates.source), ("Rust", "rust", "rust"))

        gallery = entries["marketplace.visualstudio.com/_apis/public/gallery"]
        self.assertEqual((gallery.flag, gallery.comment), ("rw", "extension search is a POST"))
        self.assertEqual((gallery.section, gallery.language), ("VS Code", ""))

    def test_an_off_line_switches_a_provided_entry_off_and_the_profiles_own_come_first(self):
        text = "mine.example rw\n#paused.example\noff crates.io\n"
        entries = allowlist.effective(text, ["rust"])
        by_entry = {item.entry: item for item in entries}

        self.assertFalse(by_entry["crates.io"].enabled)
        self.assertTrue(by_entry["static.crates.io"].enabled)

        own = [item for item in entries if not item.provided]
        self.assertEqual(
            [(item.entry, item.enabled, item.source) for item in own],
            [
                ("mine.example", True, "custom"),
                ("paused.example", False, "custom"),
            ],
        )
        self.assertEqual(entries[:2], own)

    def test_an_off_line_the_proxy_could_not_read_is_listed_with_the_profiles_own(self):
        entries = allowlist.effective("off crates.io rw\n", ["rust"])

        self.assertTrue(next(item for item in entries if item.entry == "crates.io" and item.provided).enabled)
        self.assertTrue(entries[0].invalid and entries[0].off_line)

    def test_an_update_adds_entries_switched_on_and_drops_others(self):
        text = "off static.crates.io\n"

        with mock.patch.object(langs, "CATALOGUE", updated_catalogue(added=["newrustsite.com"], dropped=["crates.io"])):
            entries = {item.entry: item.enabled for item in allowlist.effective(text, ["rust"])}

        self.assertTrue(entries["newrustsite.com"])
        self.assertNotIn("crates.io", entries)
        self.assertFalse(entries["static.crates.io"], "switched off, it stays off")

    def test_an_update_can_change_an_entrys_access(self):
        with mock.patch.object(langs, "CATALOGUE", updated_catalogue(rw=("crates.io",))):
            entries = {item.entry: item.flag for item in allowlist.effective("", ["rust"])}

        self.assertEqual(entries["crates.io"], "rw")

    def test_switched_off_stays_off_through_a_language_switch(self):
        text, _ = allowlist.switch("", "crates.io", False, False, ["rust"])

        self.assertNotIn("crates.io", [item.entry for item in allowlist.effective(text, [])])
        entries = {item.entry: item.enabled for item in allowlist.effective(text, ["rust"])}
        self.assertFalse(entries["crates.io"])

    def test_as_dicts_numbers_the_entries(self):
        rows = allowlist.as_dicts(allowlist.effective("mine.example\n", []))

        self.assertEqual([row["id"] for row in rows], list(range(len(rows))))
        self.assertEqual(rows[0]["entry"], "mine.example")
        self.assertEqual(rows[0]["kind"], "host")


class RenderTests(unittest.TestCase):
    """
    The file the proxy is given.
    """

    def test_provided_entries_then_the_profiles_own_and_no_off_lines(self):
        text = "# --- Custom ----\nmine.example rw   # ours\n#paused.example\noff crates.io\n"
        rendered = allowlist.render(text, ["rust"])
        entries = allowlist.parse(rendered)

        self.assertFalse(any(item.off_line for item in entries))
        self.assertIn("#crates.io", rendered)
        self.assertIn("\nstatic.crates.io\n", rendered)
        self.assertIn("\nmine.example rw   # ours\n", rendered)
        self.assertIn("\n#paused.example\n", rendered)
        self.assertLess(rendered.index("static.crates.io"), rendered.index("mine.example"))

    def test_what_the_proxy_gets_is_all_valid(self):
        rendered = allowlist.render(TEMPLATE.read_text(encoding="utf-8"), langs.LANGS)

        self.assertFalse([item for item in allowlist.parse(rendered) if item.invalid])

    def test_an_entry_already_written_is_not_written_again(self):
        rendered = allowlist.render("crates.io\ncrates.io rw\n", ["rust"])
        active = [item for item in allowlist.parse(rendered) if item.enabled and item.entry == "crates.io"]

        self.assertEqual([item.flag for item in active], ["", "rw"])

    def test_lines_the_proxy_cannot_read_are_passed_on_for_it_to_refuse(self):
        rendered = allowlist.render("bad entry here\noff crates.io rw\n", ["rust"])

        self.assertIn("\nbad entry here\n", rendered)
        self.assertIn("\noff crates.io rw\n", rendered)

    def test_changes_say_what_an_update_brings_and_takes(self):
        before = allowlist.render("mine.example\n", ["rust"])
        with mock.patch.object(langs, "CATALOGUE", updated_catalogue(added=["newrustsite.com"], dropped=["crates.io"])):
            after = allowlist.render("mine.example\nother.example\n", ["rust"])

        self.assertEqual(
            allowlist.changes(before, after),
            ["Rust: + newrustsite.com", "Custom: + other.example", "Rust: - crates.io"],
        )
        self.assertEqual(allowlist.changes(after, after), [])


class ProvidedEditTests(unittest.TestCase):
    """
    The edits the CLI and the admin share, with a provided entry involved.
    """

    def test_allowing_a_provided_entry_changes_nothing(self):
        text, message = allowlist.allow("", "crates.io", "", ["rust"])

        self.assertEqual(text, "")
        self.assertEqual(message, "crates.io is already allowed (it comes with Rust)")

    def test_allowing_a_switched_off_one_switches_it_back_on(self):
        text, message = allowlist.allow("off crates.io\n", "crates.io", "", ["rust"])

        self.assertEqual(allowlist.off_lines(text, "crates.io"), [])
        self.assertEqual(message, "switched crates.io back on (it comes with Rust)")

    def test_more_access_than_provided_makes_an_entry_of_the_profiles_own(self):
        text, message = allowlist.allow("", "crates.io", "rw", ["rust"])

        self.assertEqual(message, "allowed crates.io (rw)")
        self.assertEqual([item.flag for item in allowlist.find(text, "crates.io")], ["rw"])

    def test_switching_a_provided_entry_off_and_on(self):
        text = "# --- Custom ----\nmine.example\n"

        off, message = allowlist.switch(text, "Crates.io", False, False, ["rust"])
        self.assertEqual(message, "disabled crates.io")
        self.assertTrue(off.endswith(f"{allowlist.OFF_HEADER}\n{allowlist.OFF_NOTE}\noff crates.io\n"))
        self.assertEqual(allowlist.switch(off, "crates.io", False, False, ["rust"])[1], "crates.io is already disabled")

        on, message = allowlist.switch(off, "crates.io", True, False, ["rust"])
        self.assertEqual(message, "enabled crates.io")
        self.assertEqual(allowlist.off_lines(on, "crates.io"), [])

    def test_switching_the_profiles_own_comments_it_out(self):
        text, message = allowlist.switch("mine.example\n", "mine.example", False, False, [])

        self.assertEqual((text, message), ("#mine.example\n", "disabled mine.example"))

    def test_switching_what_is_nowhere(self):
        self.assertEqual(allowlist.switch("", "x.org", True, False, [])[1], "'x.org' is not in the allowlist")

    def test_a_language_that_is_off_keeps_its_off_lines_switchable(self):
        text, message = allowlist.switch("off crates.io\n", "crates.io", True, False, [])

        self.assertEqual((allowlist.off_lines(text, "crates.io"), message), ([], "enabled crates.io"))

    def test_a_provided_entry_cannot_be_deleted(self):
        with self.assertRaisesRegex(AllowlistError, "comes with Rust and can't be deleted: switch it off instead"):
            allowlist.delete("", "crates.io", False, ["rust"])

    def test_deleting_the_profiles_own_copy_of_a_provided_entry(self):
        text, message = allowlist.delete("crates.io rw\n", "crates.io", False, ["rust"])

        self.assertEqual(allowlist.find(text, "crates.io"), [])
        self.assertEqual(message, "removed crates.io; it still comes with Rust")

    def test_own_entries_go_at_the_end_of_custom_before_switched_off(self):
        text = allowlist.switch_off(f"{allowlist.CUSTOM_HEADER}\nmine.example\n", "crates.io")
        text = allowlist.add(text, "other.example")

        lines = text.splitlines()
        self.assertLess(lines.index("other.example"), lines.index(allowlist.OFF_HEADER))
        self.assertEqual(lines.index("other.example"), lines.index("mine.example") + 1)


class CorpusTests(unittest.TestCase):
    """
    The shared acceptance corpus; tests/proxy.py runs the renderer over it.
    """

    def test_corpus(self):
        corpus = (REPO / "tests" / "allowlist-corpus.txt").read_text(encoding="utf-8")
        cases = [line.split("\t", 1) for line in corpus.splitlines() if line and not line.startswith("#")]
        self.assertGreater(len(cases), 20)

        for verdict, line in cases:
            parsed = allowlist.parse_line(line)
            self.assertIsNotNone(parsed, line)

            problem = allowlist.validate(parsed.entry, parsed.flag)
            self.assertEqual(not parsed.invalid, verdict == "ok", f"{verdict}: {line!r} -> {problem}")


if __name__ == "__main__":
    unittest.main()
