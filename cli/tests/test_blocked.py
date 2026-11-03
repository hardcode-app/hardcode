"""
Tests for reading refusals out of squid's and CoreDNS's logs (hardcode_cli.blocked).
"""

import unittest

from hardcode_cli import blocked

# Two denials of one URL, an SSH tunnel and an HTTPS tunnel refused, one
# tunnel that was allowed, and a line that isn't a log entry at all.
SQUID = """\
1791293417.757 0 172.31.253.4 TCP_DENIED/403 3349 GET https://github.com/x/y?z=1 - HIER_NONE/- text/html
1791293427.814 0 172.31.253.4 TCP_DENIED/200 0 CONNECT gitlab.com:22 - HIER_NONE/- -
1791293427.814 0 172.31.253.4 TCP_DENIED/403 10 CONNECT example.com:443 - HIER_NONE/- -
1791287350.054 1 172.31.253.4 TCP_TUNNEL/200 27 CONNECT pypi.org:443 - HIER_DIRECT/1.2.3.4 -
1791293417.757 0 172.31.253.4 TCP_DENIED/403 3349 GET https://github.com/x/y?z=1 - HIER_NONE/- text/html
garbage line
"""

DNS = """\
[INFO] 172.31.253.4 BLOCKED A exfil.example.com.
[INFO] plugin/reload: Running configuration
[INFO] 172.31.253.4 BLOCKED AAAA exfil.example.com.
"""


class BlockedTests(unittest.TestCase):
    def test_parse_aggregates_by_target(self):
        items = blocked.parse(SQUID, DNS)

        # DNS refusals aggregate per name: the A and AAAA of one lookup are one row.
        self.assertEqual(
            [(item.kind, item.method, item.host, item.port, item.url, item.count) for item in items],
            [
                ("http", "GET", "github.com", 0, "https://github.com/x/y", 2),
                ("dns", "A", "exfil.example.com", 0, "", 2),
                ("http", "CONNECT", "gitlab.com", 22, "", 1),
                ("http", "CONNECT", "example.com", 0, "", 1),
            ],
        )
        self.assertEqual(items[0].client, "172.31.253.4")
        self.assertEqual(items[0].as_dict()["target"], "https://github.com/x/y?z=1")

    def test_parse_empty(self):
        self.assertEqual(blocked.parse("", ""), [])

    def test_since_arg(self):
        self.assertEqual(blocked.since_arg("2h"), "2h")
        self.assertEqual(blocked.since_arg("1y"), "30m")

    def test_aborted_and_reply_denials_count(self):
        log = """\
1 0 172.31.253.10 TCP_DENIED_ABORTED/200 0 CONNECT other.test:443 - HIER_NONE/- -
1 0 172.31.253.10 TCP_DENIED_REPLY/403 1 GET https://example.com/x - HIER_NONE/- -
"""

        items = {item.target: item for item in blocked.parse(log, "")}

        self.assertEqual(set(items), {"other.test:443", "https://example.com/x"})
        self.assertEqual(items["other.test:443"].method, "CONNECT")

    def test_the_reason_the_proxy_gives_is_kept(self):
        # squid.conf's format ends with why ("-" for no reason given), and a
        # target refused for a reason keeps it among later lines without one.
        log = """\
1 0 172.31.253.10 TCP_DENIED/200 0 CONNECT gitlab.example.com:22222 - HIER_NONE/- - internal-address
1 0 172.31.253.10 TCP_DENIED/000 0 CONNECT gitlab.example.com:22222 - HIER_NONE/- - -
1 0 172.31.253.10 TCP_DENIED/403 3347 GET http://registry.example.com/v2/ - HIER_NONE/- text/html internal-address
1 0 172.31.253.10 TCP_DENIED/403 3347 GET http://other.example.com/ - HIER_NONE/- text/html -
"""

        items = {item.target: item for item in blocked.parse(log, "")}

        ssh = items["gitlab.example.com:22222"]
        self.assertEqual(
            (ssh.host, ssh.port, ssh.reason, ssh.count), ("gitlab.example.com", 22222, "internal-address", 2)
        )
        self.assertEqual(items["http://registry.example.com/v2/"].reason, "internal-address")
        self.assertEqual(items["http://other.example.com/"].reason, "")
        self.assertEqual(blocked.REASONS["internal-address"], "resolves to an internal address")


if __name__ == "__main__":
    unittest.main()
