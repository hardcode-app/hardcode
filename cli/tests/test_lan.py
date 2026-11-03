"""
Tests for addresses on your network (hardcode_cli.lan): which ones a name
may be pinned to, and what a name resolves to.
"""

import re
import socket
import unittest
from unittest import mock

from hardcode_cli import lan

from .helpers import REPO

RENDERER = REPO / "images" / "squid" / "allowlist-render.sh"
COMPOSE = REPO / "compose.yaml"


class AddressTests(unittest.TestCase):
    def test_addresses_inside_the_default_ranges_can_be_pinned(self):
        for address in ("192.168.71.200", "10.0.0.1", "172.31.255.254", "100.64.0.1", "fd00::10", "FD00::10"):
            self.assertIsNone(lan.address_problem(address), address)

    def test_some_addresses_never_can(self):
        for address in (
            "127.0.0.1",
            "::1",
            "169.254.169.254",
            "fe80::1",
            "0.0.0.0",
            "0.1.2.3",
            "::",
            "224.0.0.1",
            "ff02::1",
            "::ffff:c0a8:101",
        ):
            self.assertIn("can't be pinned", lan.address_problem(address) or "", address)

    def test_an_address_outside_the_ranges_is_not_on_your_network(self):
        self.assertIn("isn't on your network", lan.address_problem("8.8.8.8"))
        self.assertIn("isn't on your network", lan.address_problem("2001:db8::1"))

        # A profile's own ranges decide: narrowed, 192.168/16 is outside.
        self.assertIn("isn't on your network", lan.address_problem("192.168.1.1", ["10.0.0.0/8"]))
        self.assertIsNone(lan.address_problem("10.1.1.1", ["10.0.0.0/8", "garbage"]))

        # ...but nothing makes loopback pinnable.
        self.assertIn("can't be pinned", lan.address_problem("127.0.0.1", ["0.0.0.0/0"]))

    def test_only_plain_addresses_are_read(self):
        for text in (
            "192.168.01.1",
            "192.168.1.256",
            "192.168.1.0/24",
            "::ffff:192.168.1.1",
            "fd00::10%eth0",
            "fd00:::10",
            "fd00::1::2",
            "1:2:3:4:5:6:7:8:9",
            "gitlab.example.com",
            "",
        ):
            self.assertIsNone(lan.parse_address(text), text)
            self.assertIn("is not an IP address", lan.address_problem(text), text)

    def test_canonical_address(self):
        self.assertEqual(lan.canonical_address("FD00:0:0:0:0:0:0:10"), "fd00::10")
        self.assertEqual(lan.canonical_address("192.168.71.200"), "192.168.71.200")

    def test_in_ranges(self):
        self.assertTrue(lan.in_ranges("10.96.0.1", ["10.96.0.0/12"]))
        self.assertFalse(lan.in_ranges("10.96.0.1", ["fd00::/8"]))
        self.assertFalse(lan.in_ranges("not an address", ["0.0.0.0/0"]))

    def test_pinnable_takes_the_first_address_on_your_network(self):
        self.assertEqual(
            lan.pinnable(["203.0.113.5", "192.168.71.200", "10.0.0.1"], lan.internal_ranges()), "192.168.71.200"
        )
        self.assertIsNone(lan.pinnable(["203.0.113.5", "127.0.0.1"], lan.internal_ranges()))
        self.assertEqual(lan.internal_ranges(" 10.0.0.0/8  fd00::/8 "), ["10.0.0.0/8", "fd00::/8"])

    def test_the_sandboxs_own_network_never_can(self):
        refused = lan.address_problem("172.31.253.10")
        self.assertEqual(
            refused,
            "172.31.253.10 is in the sandbox's own network (172.31.253.0/24): HardCode never lets a sandbox reach it",
        )
        self.assertIsNone(lan.address_problem("172.31.252.10"), "next door is fine")

        # A profile's own DEVNET_SUBNET decides, and a cluster has none.
        self.assertIsNone(lan.address_problem("172.31.253.10", devnet=["172.31.250.0/24"]))
        self.assertIn("sandbox's own network", lan.address_problem("172.31.250.9", devnet=["172.31.250.0/24"]))
        self.assertIsNone(lan.address_problem("172.31.253.10", devnet=[]))

        # ...however wide the internal ranges are.
        self.assertIn("sandbox's own network", lan.address_problem("172.31.253.10", ["0.0.0.0/0"]))

    def test_docker_desktops_network_never_can(self):
        self.assertEqual(
            lan.address_problem("192.168.65.254"),
            "192.168.65.254 is in Docker Desktop's own network (192.168.65.0/24), its way to this computer: "
            "HardCode never lets a sandbox reach it",
        )
        self.assertIn("Docker Desktop's own network", lan.address_problem("192.168.65.2", devnet=[]))
        self.assertIsNone(lan.address_problem("192.168.66.1"))

    def test_pinnable_skips_this_computer_and_the_sandbox(self):
        def this_computer(address):
            return address == "192.168.1.5"

        addresses = ["192.168.1.5", "172.31.253.4", "192.168.71.200"]
        self.assertEqual(lan.pinnable(addresses, lan.internal_ranges(), None, this_computer), "192.168.71.200")
        self.assertIsNone(lan.pinnable(addresses[:2], lan.internal_ranges(), None, this_computer))


class ThisComputerTests(unittest.TestCase):
    """
    This computer's own addresses: those a socket can be bound to here.
    The socket is faked, so nothing depends on the machine's addresses.
    """

    def test_an_address_a_socket_binds_to_is_this_computer(self):
        with mock.patch("socket.socket") as made:
            self.assertTrue(lan.is_this_computer("192.168.1.5"))

        made.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM)
        probe = made.return_value.__enter__.return_value
        probe.bind.assert_called_once_with(("192.168.1.5", 0))
        made.return_value.__exit__.assert_called_once()

    def test_one_it_cant_bind_to_isnt(self):
        with mock.patch("socket.socket") as made:
            made.return_value.__enter__.return_value.bind.side_effect = OSError(99, "Cannot assign requested address")
            self.assertFalse(lan.is_this_computer("192.168.71.200"))

        made.return_value.__exit__.assert_called_once()

    def test_ipv6_is_bound_as_ipv6_and_text_that_isnt_an_address_never(self):
        with mock.patch("socket.socket") as made:
            self.assertTrue(lan.is_this_computer("FD00:0:0:0:0:0:0:10"))
            made.assert_called_once_with(socket.AF_INET6, socket.SOCK_STREAM)
            made.return_value.__enter__.return_value.bind.assert_called_once_with(("fd00::10", 0))

            made.reset_mock()
            self.assertFalse(lan.is_this_computer("gitlab.example.com"))
            made.assert_not_called()

    def test_loopback_is_this_computer_for_real(self):
        self.assertTrue(lan.is_this_computer("127.0.0.1"))

    def test_the_refusal_says_so_plainly(self):
        self.assertEqual(
            lan.this_computer_problem("192.168.1.5", lambda address: True),
            "192.168.1.5 is this computer: HardCode never lets a sandbox reach it",
        )
        self.assertIsNone(lan.this_computer_problem("192.168.71.200", lambda address: False))

        # The address rules come first, and this computer only when asked.
        self.assertIn("isn't on your network", lan.refusal("8.8.8.8", lan.internal_ranges(), None, lambda a: True))
        self.assertIsNone(lan.refusal("192.168.1.5", lan.internal_ranges()))
        self.assertIn("is this computer", lan.refusal("192.168.1.5", lan.internal_ranges(), None, lambda a: True))


class RendererAgreementTests(unittest.TestCase):
    """
    The ranges written in both places: here, and in the renderer.
    """

    def setUp(self):
        self.script = RENDERER.read_text(encoding="utf-8")

    def test_the_default_internal_ranges_are_the_renderers(self):
        found = re.search(r'internal_ranges="\$\{INTERNAL_RANGES:-([^}]*)\}"', self.script)
        self.assertEqual(found.group(1).split(), lan.DEFAULT_INTERNAL_RANGES.split())

    def test_the_refused_ranges_are_the_renderers(self):
        found = re.search(r'^refused_ranges="([^"]*)"', self.script, re.MULTILINE)
        self.assertEqual(found.group(1).split(), list(lan.REFUSED_RANGES))

    def test_docker_desktops_network_is_the_renderers(self):
        found = re.search(r'^docker_desktop_subnet="([^"]*)"', self.script, re.MULTILINE)
        self.assertEqual(found.group(1), lan.DOCKER_DESKTOP_SUBNET)

    def test_the_default_devnet_is_the_renderers_and_composes(self):
        found = re.search(r'devnet_subnet="\$\{DEVNET_SUBNET-([^}]*)\}"', self.script)
        self.assertEqual(found.group(1), lan.DEFAULT_DEVNET_SUBNET)

        # compose.yaml names it for the network and for the renderer: both alike.
        compose = COMPOSE.read_text(encoding="utf-8")
        defaults = re.findall(r"\$\{DEVNET_SUBNET:-([^}]*)\}", compose)
        self.assertEqual(len(defaults), 2, "devnet's subnet, and the allowlist service's DEVNET_SUBNET")
        self.assertEqual(set(defaults), {lan.DEFAULT_DEVNET_SUBNET})


class ResolveTests(unittest.TestCase):
    def test_each_address_once_in_order(self):
        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.71.200", 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd00:0::10", 0, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.71.200", 0)),
        ]
        with mock.patch("socket.getaddrinfo", return_value=answers):
            self.assertEqual(lan.resolve("gitlab.example.com"), ["192.168.71.200", "fd00::10"])

    def test_a_name_that_doesnt_resolve_has_no_addresses(self):
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror("no such name")):
            self.assertEqual(lan.resolve("nowhere.invalid"), [])


if __name__ == "__main__":
    unittest.main()
