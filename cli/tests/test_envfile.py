"""
Tests for reading and editing a profile's ``.env`` (hardcode_cli.envfile).
"""

import tempfile
import unittest
from pathlib import Path

from hardcode_cli import envfile


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".env"

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_missing_file(self):
        self.assertEqual(envfile.read(self.path), {})

    def test_read_skips_comments_blanks_and_junk(self):
        self.path.write_text("# c\n\nA=1\n B = two \nnot a pair\nB=3\nURL=http://x?y=1\n")

        self.assertEqual(envfile.read(self.path), {"A": "1", "B": "3", "URL": "http://x?y=1"})

    def test_read_strips_matching_quotes(self):
        self.path.write_text("A=\"8080\"\nB='two words'\nC=\"unbalanced\nD=''\n")

        self.assertEqual(envfile.read(self.path), {"A": "8080", "B": "two words", "C": '"unbalanced', "D": ""})

    def test_read_as_compose_does(self):
        # Inline comments after a space, `export`, a comment after a quoted
        # value, and the last of two lines for the same key.
        self.path.write_text(
            "VSCODE_PORT=8001  # moved\n"
            "export DEV_USER=alice\n"
            "TOKEN='a b' # quoted\n"
            "PASSWORD=pa#ss\n"
            "ENABLE_DOCKER=true\n"
            "ENABLE_DOCKER=false\n"
        )

        self.assertEqual(
            envfile.read(self.path),
            {"VSCODE_PORT": "8001", "DEV_USER": "alice", "TOKEN": "a b", "PASSWORD": "pa#ss", "ENABLE_DOCKER": "false"},
        )

    def test_set_changes_every_line_for_the_key(self):
        # Compose reads the last line; changing only the first would change nothing.
        self.path.write_text("ENABLE_DOCKER=true\n# ENABLE_DOCKER=x\nexport ENABLE_DOCKER=true\n")

        envfile.set_value(self.path, "ENABLE_DOCKER", "false")

        self.assertEqual(self.path.read_text(), "ENABLE_DOCKER=false\n# ENABLE_DOCKER=x\nENABLE_DOCKER=false\n")
        self.assertEqual(envfile.read(self.path), {"ENABLE_DOCKER": "false"})

    def test_get_defaults(self):
        # compose.yaml's defaults: this checkout's, here.
        self.assertEqual(envfile.get(self.path, "VSCODE_PORT"), "11111")
        self.assertEqual(envfile.get(self.path, "DEVNET_SUBNET"), "172.31.253.0/24")
        self.assertEqual(envfile.get(self.path, "DNS_IP"), "172.31.253.53")
        self.assertEqual(envfile.get(self.path, "OTHER"), "")

        self.path.write_text("VSCODE_PORT=8080\nDNS_IP=\n")
        self.assertEqual(envfile.get(self.path, "VSCODE_PORT"), "8080")
        self.assertEqual(envfile.get(self.path, "DNS_IP"), "172.31.253.53", "an empty value falls back to the default")

    def test_set_replaces_in_place_keeping_order_and_comments(self):
        self.path.write_text("# top\nA=1\nB=2\n")

        envfile.set_value(self.path, "A", "x")

        self.assertEqual(self.path.read_text(), "# top\nA=x\nB=2\n")

    def test_set_appends_and_fixes_missing_trailing_newline(self):
        self.path.write_text("VSCODE_PORT=8080")

        envfile.set_value(self.path, "INSTALL_DOTNET", "false")

        self.assertEqual(self.path.read_text(), "VSCODE_PORT=8080\nINSTALL_DOTNET=false\n")

    def test_set_creates_file(self):
        envfile.set_value(self.path, "K", "v")

        self.assertEqual(self.path.read_text(), "K=v\n")

    def test_set_does_not_match_prefixes(self):
        self.path.write_text("INSTALL_GO=true\n")

        envfile.set_value(self.path, "INSTALL_G", "x")

        self.assertEqual(envfile.read(self.path), {"INSTALL_GO": "true", "INSTALL_G": "x"})


if __name__ == "__main__":
    unittest.main()
