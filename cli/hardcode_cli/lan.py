"""
Addresses on your network: which ones a name may be pinned to, and what a
name resolves to.

A pin (a ``lan <name> <address>`` line of a profile's allowlist) lets exactly
that name reach exactly that address, which the proxy refuses otherwise:
every allowlisted name that resolves inside the profile's internal ranges
is refused. The rules here are the same as in
``images/squid/allowlist-render.sh``, which reads the same lines;
``tests/allowlist-corpus.txt`` is run through both.

Three kinds of address are never pinned, whatever the ranges say: the
sandbox's own network (the profile's ``DEVNET_SUBNET``) and Docker
Desktop's (``DOCKER_DESKTOP_SUBNET``), which the renderer refuses too, and
this computer's own addresses, which only the command can tell
(``is_this_computer``): it refuses them as a pin is added, and leaves out
of the proxy's file one written by hand. The proxy refuses the networks it
is attached to as well, at every start, pinned or not.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import Callable, Iterable, Optional, Union

Address = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

# The sandbox's own network when the profile doesn't say (DEVNET_SUBNET):
# compose.yaml's default, and the renderer's, which a test keeps in step.
DEFAULT_DEVNET_SUBNET = "172.31.253.0/24"

# Where an allowlisted name must never point, when the profile doesn't say
# (EGRESS_INTERNAL_RANGES, or internalRanges on Kubernetes): the renderer's
# default, which a test keeps in step with it.
DEFAULT_INTERNAL_RANGES = (
    "0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.168.0.0/16 ::1 fc00::/7 fe80::/10"
)

# Addresses no name may ever be pinned to, whatever the internal ranges say:
# "this network" (0.0.0.0/8, the unspecified address with it), loopback,
# link-local (where cloud metadata services answer), multicast, and IPv4
# addresses written as IPv6, which would get round the IPv4 ones.
REFUSED_RANGES = (
    "0.0.0.0/8",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "224.0.0.0/4",
    "::/128",
    "::1/128",
    "fe80::/10",
    "ff00::/8",
    "::ffff:0:0/96",
)

# Docker Desktop's own network on macOS and Windows, its documented default
# ("Docker subnet" in its Network settings): its DNS server and proxy are
# there, and host.docker.internal, the way from a container to the computer
# itself. No name may be pinned into it; the renderer has the same range.
DOCKER_DESKTOP_SUBNET = "192.168.65.0/24"

# What an address may look like before Python reads it: dotted IPv4 with no
# leading zeros (010 is octal to some readers, decimal to others), or IPv6 in
# hex, without an IPv4 part or a zone ("%eth0"). The renderer takes the same.
_IPV4 = re.compile(r"^(0|[1-9][0-9]{0,2})(\.(0|[1-9][0-9]{0,2})){3}$")
_IPV6 = re.compile(r"^[0-9a-f:]+$")

# A resolver: the addresses a name has, as text, in the order given.
Resolver = Callable[[str], list[str]]

# Whether an address, as text, is one of this computer's own.
ThisComputer = Callable[[str], bool]


def parse_address(text: str) -> Optional[Address]:
    """
    *text* as an IPv4 or IPv6 address, or ``None`` when it isn't one the
    renderer reads too.
    """
    text = text.strip().lower()

    shaped = _IPV4.match(text) or (":" in text and _IPV6.match(text))
    if not shaped:
        return None

    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def parse_ranges(ranges: Iterable[str]) -> list[Network]:
    """
    The networks in *ranges* (``address`` or ``address/prefix``), leaving
    out anything that isn't one, as the renderer does.
    """
    networks = []

    for text in ranges:
        try:
            networks.append(ipaddress.ip_network(text.strip(), strict=False))
        except ValueError:
            continue

    return networks


def internal_ranges(setting: str = "") -> list[str]:
    """
    A profile's internal ranges, from its setting (space-separated); the
    default ones when it has none.
    """
    return (setting.strip() or DEFAULT_INTERNAL_RANGES).split()


def _inside(address: Address, networks: Iterable[Network]) -> bool:
    """
    Whether *address* is in one of *networks* of its own family.
    """
    return any(address.version == network.version and address in network for network in networks)


def in_ranges(text: str, ranges: Iterable[str]) -> bool:
    """
    Whether the address *text* is inside one of *ranges*; never for
    something that isn't an address.
    """
    address = parse_address(text)
    return address is not None and _inside(address, parse_ranges(ranges))


def address_problem(
    text: str,
    ranges: Optional[Iterable[str]] = None,
    devnet: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """
    Why a name can't be pinned to the address *text*, in a profile with the
    internal *ranges* and the sandbox's own network *devnet* (the default
    ones when ``None``; on Kubernetes, which has no devnet, none); ``None``
    when it can.

    The renderer refuses a line for the same reasons, in the same order.
    """
    address = parse_address(text)
    if address is None:
        return f"'{text}' is not an IP address"

    if _inside(address, parse_ranges(REFUSED_RANGES)):
        return f"{text} can't be pinned: loopback, link-local, multicast and 0.0.0.0/8 addresses never can"

    if _inside(address, parse_ranges([DOCKER_DESKTOP_SUBNET])):
        return (
            f"{text} is in Docker Desktop's own network ({DOCKER_DESKTOP_SUBNET}), its way to this computer: "
            "HardCode never lets a sandbox reach it"
        )

    for network in parse_ranges([DEFAULT_DEVNET_SUBNET] if devnet is None else devnet):
        if _inside(address, [network]):
            return f"{text} is in the sandbox's own network ({network}): HardCode never lets a sandbox reach it"

    if not _inside(address, parse_ranges(internal_ranges() if ranges is None else ranges)):
        return f"{text} isn't on your network (outside the profile's internal ranges), so there is nothing to pin"

    return None


def is_this_computer(text: str) -> bool:
    """
    Whether the address *text* is one of this computer's own: one a socket
    can be bound to here, which an address of another machine can't. The
    addresses Docker gives its bridges on this computer, the default
    bridge's gateway among them, are this computer's too.
    """
    address = parse_address(text)
    if address is None:
        return False

    family = socket.AF_INET if address.version == 4 else socket.AF_INET6

    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((str(address), 0))
    except OSError:
        return False

    return True


def this_computer_problem(text: str, this_computer: ThisComputer = is_this_computer) -> Optional[str]:
    """
    Why a name can't be pinned to the address *text* because it is this
    computer's own (as *this_computer* tells), or ``None`` when it isn't.
    """
    if not this_computer(text):
        return None

    return f"{canonical_address(text)} is this computer: HardCode never lets a sandbox reach it"


def refusal(
    text: str,
    ranges: Iterable[str],
    devnet: Optional[Iterable[str]] = None,
    this_computer: Optional[ThisComputer] = None,
) -> Optional[str]:
    """
    Why a name can't be pinned to the address *text* here: the rules of
    ``address_problem``, and, given *this_computer*, that it is this
    computer's own. ``None`` when it can.
    """
    problem = address_problem(text, ranges, devnet)
    if problem is None and this_computer is not None:
        problem = this_computer_problem(text, this_computer)

    return problem


def canonical_address(text: str) -> str:
    """
    The way an address is written into the allowlist: Python's short form
    (``fd00::10``, not ``fd00:0:0:0:0:0:0:10``).
    """
    address = parse_address(text)
    return str(address) if address is not None else text.strip().lower()


def pinnable(
    addresses: Iterable[str],
    ranges: Iterable[str],
    devnet: Optional[Iterable[str]] = None,
    this_computer: Optional[ThisComputer] = None,
) -> Optional[str]:
    """
    The first of *addresses* that a name could be pinned to in a profile with
    the internal *ranges* and the sandbox's own network *devnet*: one on
    your network that no rule of ``refusal`` refuses. ``None`` when there is
    none.
    """
    ranges = list(ranges)
    devnet = None if devnet is None else list(devnet)

    for text in addresses:
        if refusal(text, ranges, devnet, this_computer) is None:
            return canonical_address(text)

    return None


def resolve(name: str) -> list[str]:
    """
    The addresses *name* resolves to on this machine, IPv4 and IPv6, each
    once and in the resolver's order; none when it doesn't resolve.
    """
    try:
        answers = socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return []

    found: list[str] = []
    for *_, sockaddr in answers:
        address = canonical_address(str(sockaddr[0]))
        if address not in found:
            found.append(address)

    return found
