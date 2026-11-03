#!/usr/bin/env python3
"""
The proxy, for real.

Builds the squid image from images/squid, so the configuration under test is
the one in the tree, and runs it in a throwaway isolated Docker network
against an origin server that normalises paths the way web servers do
(tests/fixtures/proxy_origin.py). It checks what the unit tests can't:

- a path entry can't be escaped with dot segments, encoded separators or
  backslashes, with any method, over plain HTTP or through an inspected HTTPS
  tunnel; read-only entries refuse uploads and ``rw`` entries allow them
- an upstream certificate nobody vouches for is refused
- tunnels to unlisted hosts, internal addresses or non-TLS ports are reset,
  while an ``ssh`` entry opens exactly its port
- a name pinned to an address on your network (a ``lan`` line) reaches
  exactly that address, over HTTP, HTTPS and SSH, whatever DNS says; no
  other name reaches it, an unpinned name inside is still refused (nothing
  passes through, even the first time squid looks it up), a pin with no
  entry allows nothing, and the log says why a name inside was refused
- no pin reaches squid's own networks: the gateway of the network it goes
  out on (the host, in Compose) or the sandbox's network, which squid finds
  for itself as it starts; and the renderer refuses a pin into the
  sandbox's network as the profile names it (DEVNET_SUBNET), or into
  Docker Desktop's (192.168.65.0/24)
- an empty allowlist renders a policy that allows nothing
- the renderer agrees with the CLI's grammar (tests/allowlist-corpus.txt) and
  renders a last line that has no newline
- an internal network in isolated gateway mode has no way to the host

Squid is on two networks, as in Compose: the internal one its clients are
on, the sandbox's, and one it goes out on. The origins are on that one, but
squid reaches each at an address of its own beyond both (``OUTSIDE``),
through a route, as it reaches the internet or your network: squid refuses
every address of the networks it is attached to.

Every container is memory-capped; nothing of your own stack is touched.

    tests/proxy.py                    build hardcode-squid:test and test it
    SQUID_IMAGE=name tests/proxy.py   test an image you built yourself
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Union

import harness
from harness import FIXTURES, ROOT, Docker, Suite

SQUID_IMAGE = os.environ.get("SQUID_IMAGE", "hardcode-squid:test")
NETWORK = f"hc-proxy-test-{os.getpid()}"
REFUSED = "403"

# The network squid goes out on, as Compose's egress is: a plain bridge,
# whose gateway is this machine. The origins are on it too.
EGRESS = f"{NETWORK}-egress"

# Where squid reaches each origin: an address beyond its own networks,
# which the origin has besides its address on EGRESS, and squid a route to.
# Private, so internal to the proxy's default ranges, and in no network of
# the suite's.
OUTSIDE = {
    "origin": "10.199.0.2",
    "badcert": "10.199.0.3",
    "inside-a": "10.199.0.10",
    "inside-b": "10.199.0.11",
}

# What the policy's squid resolves the origins' names to, from its hosts
# file. metadata.test is pointed at a link-local address, as a cloud
# metadata service would be.
POLICY_HOSTS = [
    f"origin.test:{OUTSIDE['origin']}",
    f"sshonly.test:{OUTSIDE['origin']}",
    f"badcert.test:{OUTSIDE['badcert']}",
    "metadata.test:169.254.169.254",
]

# What the proxy allows while the requests below are sent. metadata.test
# shows that an allowlisted name resolving inside is still refused.
ALLOWLIST = """\
origin.test/allowed
origin.test/upload   rw
metadata.test
badcert.test
sshonly.test:22 ssh
"""

# The test origins' addresses are private, so for these checks only
# loopback, link-local and shared address space count as internal.
INTERNAL_RANGES = """\
127.0.0.0/8
169.254.0.0/16
100.64.0.0/10
"""

# The names on your network: origins A and B, at their addresses beyond
# squid's networks ({a} is A's), which are private, so internal to the
# proxy's default ranges. Squid's hosts file says neighbour.test is A
# (PIN_HOSTS), and Docker's DNS says moved.test and inside.test are B, at
# its address on squid's own network; the pins send pinned.test, moved.test
# and entryless.test to A.
#
# gateway.test is pinned to the gateway of the network squid goes out on
# ({gateway}: the host, in Compose), and sandbox.test to origin D, on the
# sandbox's network ({sandbox}), where squid's clients are: allowed and
# pinned, neither may reach its address.
PIN_ALLOWLIST = """\
pinned.test
pinned.test:22 ssh
moved.test
moved.test:22 ssh
neighbour.test
neighbour.test:22 ssh
inside.test
inside.test:22 ssh
gateway.test
gateway.test:22 ssh
sandbox.test
sandbox.test:22 ssh
lan pinned.test {a}
lan moved.test {a}
lan entryless.test {a}
lan gateway.test {gateway}
lan sandbox.test {sandbox}
"""

# What the pins' squid resolves neighbour.test to: A's address, which only
# pinned names may reach.
PIN_HOSTS = [f"neighbour.test:{OUTSIDE['inside-a']}"]

# The renderer's devnet for the pins: one nowhere near the suite's networks,
# so that the pin into the sandbox's network is left for squid to refuse.
ELSEWHERE = "192.0.2.0/24"

# A refusal the proxy can't reset at once: the CONNECT is answered, and the
# tunnel reset as the client speaks. Either way nothing passes through.
TUNNEL_REFUSED = ("reset", "200 reset")


@dataclass(frozen=True)
class Request:
    """
    One request sent through the proxy, and what should come back.

    ``expect`` is compared with the start of what the client prints: the
    status code and, when the request reached the origin, the method and the
    path the origin saw (see tests/fixtures/proxy_client.py). A tuple lists
    outcomes that are all acceptable.
    """

    name: str
    method: str
    url: str
    expect: Union[str, tuple[str, ...]] = REFUSED


PLAIN_HTTP = [
    Request("GET under the allowed prefix passes", "GET", "http://origin.test/allowed/good", "200 GET /allowed/good"),
    Request("GET outside it is refused", "GET", "http://origin.test/secret"),
    Request("GET on an unlisted host is refused", "GET", "http://other.test/"),
    # Every way of spelling "go up a directory" is refused under a path entry.
    Request("dot segments can't escape the prefix", "GET", "http://origin.test/allowed/../secret"),
    Request("encoded dot segments can't either", "GET", "http://origin.test/allowed/%2e%2e/secret"),
    Request("nor a half-encoded one", "GET", "http://origin.test/allowed/.%2e/secret"),
    Request("nor an encoded slash", "GET", "http://origin.test/allowed/..%2Fsecret"),
    Request("nor a dot segment ended by a semicolon", "GET", "http://origin.test/allowed/..;/secret"),
    Request("a single-dot segment is refused too", "GET", "http://origin.test/allowed/./good"),
    Request("nor a fully encoded one", "GET", "http://origin.test/allowed/%2e%2e%2fsecret"),
    # ...while names that merely contain dots or encoded slashes still work.
    Request(
        "but a dotted file name is fine",
        "GET",
        "http://origin.test/allowed/.well-known/x",
        "200 GET /allowed/.well-known/x",
    ),
    Request(
        "and an encoded slash on its own (npm scopes) is fine",
        "GET",
        "http://origin.test/allowed/@scope%2fname",
        "200 GET /allowed/@scope/name",
    ),
    Request("an allowlisted name that resolves inside is refused", "GET", "http://metadata.test/latest/meta-data/"),
    # Uploads need an rw entry.
    Request("read-only refuses POST", "POST", "http://origin.test/allowed/good"),
    Request("rw allows POST", "POST", "http://origin.test/upload/file", "200 POST /upload/file"),
    Request("rw allows PUT", "PUT", "http://origin.test/upload/file", "200 PUT /upload/file"),
    Request("traversal is refused on rw entries as well", "PUT", "http://origin.test/upload/../allowed/x"),
    Request(
        "a query string doesn't change the path check",
        "GET",
        "http://origin.test/allowed/good?next=/../x",
        "200 GET /allowed/good",
    ),
    # A server that treats backslashes as separators must not be reachable
    # through them either.
    Request("a backslash can't escape the prefix either", "GET", "http://origin.test/allowed/x\\..\\..\\secret"),
    Request("nor an encoded backslash", "GET", "http://origin.test/allowed/x%5c..%5c..%5csecret"),
    Request("nor a backslash without dots", "GET", "http://origin.test/allowed\\secret"),
]

INSPECTED_HTTPS = [
    Request(
        "https: GET under the prefix passes the bumped tunnel",
        "GET",
        "https://origin.test/allowed/good",
        "200 GET /allowed/good",
    ),
    Request("https: GET outside it is refused", "GET", "https://origin.test/secret"),
    Request("https: dot segments are refused", "GET", "https://origin.test/allowed/../secret"),
    Request("https: encoded dot segments are refused", "GET", "https://origin.test/allowed/%2e%2e/secret"),
    Request("https: backslashes are refused", "GET", "https://origin.test/allowed/x\\..\\..\\secret"),
    Request("https: encoded backslashes are refused", "GET", "https://origin.test/allowed/x%5c..%5c..%5csecret"),
    Request("https: read-only refuses POST", "POST", "https://origin.test/allowed/good"),
    Request("https: rw allows POST", "POST", "https://origin.test/upload/file", "200 POST /upload/file"),
    Request("https: rw traversal is refused too", "PUT", "https://origin.test/upload/../allowed/x"),
    # The tunnel opens (the name is allowlisted), but the proxy's own check of
    # the origin's certificate fails, and it answers with its 503 page instead
    # of passing anything through. The certificate squid makes for that page
    # has no Authority Key Identifier, though, so a client that verifies
    # strictly (Python 3.13 and later do by default, and this one is 3.14)
    # rejects the page itself with a TLS error. Either way, nothing of the
    # origin's arrives.
    Request(
        "https: an upstream certificate nobody vouches for is refused, never reaching the origin",
        "GET",
        "https://badcert.test/",
        ("503", "tls-error"),
    ),
]

TUNNELS = [
    Request("an ssh entry opens a tunnel to its port", "CONNECT", "sshonly.test:22", "200"),
    Request("but not to another port of that host", "CONNECT", "sshonly.test:443", "reset"),
    Request("a tunnel to an allowlisted host is opened", "CONNECT", "origin.test:443", "200"),
    Request("a tunnel to an unlisted host is reset, not answered", "CONNECT", "other.test:443", "reset"),
    Request("a tunnel to a name that resolves inside is reset", "CONNECT", "metadata.test:443", "reset"),
    Request("a tunnel to a non-TLS port is reset", "CONNECT", "origin.test:22", "reset"),
]

PINNED = [
    # The first lookup of each name below is the request itself: squid has
    # cached nothing for them, so none of this depends on what it has.
    Request(
        "an ssh tunnel to an unpinned name inside is refused, the first time too",
        "SSH",
        "inside.test:22",
        TUNNEL_REFUSED,
    ),
    Request("and again, once squid has looked it up", "SSH", "inside.test:22", TUNNEL_REFUSED),
    Request("a pinned name reaches its address over plain HTTP", "GET", "http://pinned.test/x", "200 GET /x from A"),
    Request("through an inspected HTTPS tunnel", "GET", "https://pinned.test/x", "200 GET /x from A"),
    Request("and through its ssh tunnel", "SSH", "pinned.test:22", "200 SSH-2.0-A"),
    Request("what DNS says for a pinned name is ignored", "GET", "http://moved.test/x", "200 GET /x from A"),
    Request("for its ssh tunnel too", "SSH", "moved.test:22", "200 SSH-2.0-A"),
    Request("another name for the pinned address is still refused", "GET", "http://neighbour.test/x"),
    Request("its HTTPS tunnel too", "GET", "https://neighbour.test/x", ("tunnel reset", "tls-error")),
    Request("and its ssh tunnel", "SSH", "neighbour.test:22", TUNNEL_REFUSED),
    Request("an unpinned name inside is refused over HTTP", "GET", "http://inside.test/x"),
    Request("a pin without an entry allows nothing", "GET", "http://entryless.test/x"),
    Request("nor a tunnel", "SSH", "entryless.test:22", "reset"),
]

NEVER_PINNED = [
    # Without the refusal these would be answered: 503 by the gateway, which
    # serves nothing, and by origin D, which answers anything, "from D".
    Request("a name pinned to the gateway squid goes out by is refused", "GET", "http://gateway.test/x"),
    Request("its ssh tunnel too", "SSH", "gateway.test:22", TUNNEL_REFUSED),
    Request("a name pinned inside the sandbox's network is refused", "GET", "http://sandbox.test/x"),
    Request("its HTTPS tunnel too", "GET", "https://sandbox.test/x", ("tunnel reset", "tls-error")),
    Request("and its ssh tunnel", "SSH", "sandbox.test:22", TUNNEL_REFUSED),
]

NOTHING_ALLOWED = [
    Request("deny-all: the previously allowed URL is refused", "GET", "http://origin.test/allowed/good"),
    Request("deny-all: rw URL refused too", "POST", "http://origin.test/upload/file"),
]


def read_lines(path: Path) -> list[str]:
    """
    The lines of a file the renderer should have written; none if it didn't.
    """
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


class ProxyTest:
    """
    The squid image, its renderer, and the containers around them.
    """

    def __init__(self, suite: Suite, docker: Docker, scratch: Path):
        self.suite = suite
        self.docker = docker
        self.scratch = scratch

        # Each origin's address beyond squid's networks, and its address on
        # EGRESS, which squid's route to it goes through.
        self.routes: dict[str, str] = {}

    # -- the renderer --------------------------------------------------------

    def render(
        self,
        allowlist_text: str,
        name: str,
        environment: Optional[dict[str, str]] = None,
    ) -> tuple[bool, Path, str]:
        """
        Render an allowlist the way the one-shot ``allowlist`` service does,
        with the *environment* Compose would give it (none: the defaults).

        Returns whether the renderer accepted it, the directory it wrote to
        (``squid/`` and ``Corefile`` inside), and what it printed.
        """
        allowlist = self.scratch / f"{name}.txt"
        allowlist.write_text(allowlist_text, encoding="utf-8")

        output_dir = self.scratch / name
        output_dir.mkdir()

        output = self.docker.run(
            SQUID_IMAGE,
            memory="128m",
            network="none",
            user=harness.current_user(),
            environment=environment,
            volumes=[f"{allowlist}:/config/allowlist.txt:ro", f"{output_dir}:/out"],
            entrypoint="/usr/local/bin/allowlist-render",
        )
        return output.ok, output_dir, output.text

    def check_grammar_corpus(self) -> None:
        """
        Every line of the shared corpus is accepted or refused as it says.
        """
        corpus = (ROOT / "tests" / "allowlist-corpus.txt").read_text(encoding="utf-8")

        for number, line in enumerate(corpus.splitlines()):
            if not line or line.startswith("#"):
                continue

            verdict, entry = line.split("\t", 1)
            accepted, _, log = self.render(entry + "\n", f"corpus-{number}")

            got = "ok" if accepted else "bad"
            if got == verdict:
                self.suite.check(f"renderer says {verdict}: {entry}", True)
            else:
                self.suite.check(f"renderer should say {verdict}: {entry}", False, log)

    def check_rendering_edge_cases(self) -> None:
        """
        A last line without a newline, and an allowlist with nothing in it.
        """
        accepted, output_dir, log = self.render("pypi.org\nexample.com", "no-final-newline")
        rendered = read_lines(output_dir / "squid" / "hosts.txt")
        self.suite.check(
            "a last line without a newline is rendered",
            accepted and "example.com" in rendered,
            f"{log}\nhosts.txt: {rendered}",
        )

        accepted, output_dir, log = self.render("", "empty")
        squid_dir = output_dir / "squid"
        denies_everything = (
            accepted
            # Squid can't start with an empty host list, so the renderer
            # writes one name that can never resolve...
            and "deny-all.hardcode.invalid" in read_lines(squid_dir / "hosts.txt")
            # ...a URL pattern that matches nothing...
            and "^$" in read_lines(squid_dir / "urls-ro.regex")
            # ...and a DNS filter that forwards nothing.
            and not any("forward" in line for line in read_lines(output_dir / "Corefile"))
        )
        self.suite.check("an empty allowlist renders a deny-all policy", denies_everything, log)

        # The sandbox's own network, which squid refuses outright: Compose's
        # default devnet unless the profile says, and with none at all (as
        # on Kubernetes without the cluster's ranges) a stand-in squid won't
        # warn about.
        sandbox = read_lines(squid_dir / "sandbox.txt")
        self.suite.check("the default devnet is the sandbox's network", sandbox == ["172.31.253.0/24"], str(sandbox))

        environment = {"DEVNET_SUBNET": "", "CLUSTER_RANGES": "10.244.0.0/16 10.96.0.0/12"}
        accepted, output_dir, log = self.render("", "cluster", environment)
        sandbox = read_lines(output_dir / "squid" / "sandbox.txt")
        self.suite.check(
            "on a cluster, it is the cluster's own ranges",
            accepted and sandbox == ["10.244.0.0/16", "10.96.0.0/12"],
            f"{log}\nsandbox.txt: {sandbox}",
        )

        accepted, output_dir, log = self.render("", "nowhere", {"DEVNET_SUBNET": ""})
        sandbox = read_lines(output_dir / "squid" / "sandbox.txt")
        self.suite.check("and with none, a range nothing reaches anyway", sandbox == ["0.0.0.0/8"], str(sandbox))

        # Docker Desktop's own network, its way to the computer, is never pinned.
        accepted, _, log = self.render("example.com\nlan example.com 192.168.65.254\n", "docker-desktop")
        self.suite.check(
            "a pin into Docker Desktop's network is refused, saying so",
            not accepted and "is in Docker Desktop's own network (192.168.65.0/24)" in log,
            log,
        )

    # -- the network ---------------------------------------------------------

    def check_isolated_network(self) -> None:
        """
        The internal network has no address on the host and no way out.
        """
        isolated = "com.docker.network.bridge.gateway_mode_ipv4=isolated"
        created = self.docker.create_network(NETWORK, "--internal", "--opt", isolated)
        if not created.ok:
            self.suite.abort("could not create the test network (Docker 28 or later is needed)", created.text)

        # In isolated gateway mode the bridge gets no address on the host, so
        # a container has nothing on the host side to talk to.
        ipam = self.docker.inspect_network(NETWORK)["IPAM"]["Config"]
        gateways = [entry.get("Gateway", "") for entry in ipam if entry.get("Gateway")]
        self.suite.check("the bridge has no host-side address", not gateways, f"gateways: {gateways}")

        # /proc/net/route lists a default route with destination 00000000.
        routes = self.docker.run(harness.BUILD_IMAGE, "cat", "/proc/net/route", memory="64m", network=NETWORK)
        destinations = [line.split()[1] for line in routes.text.splitlines()[1:] if line.strip()]
        self.suite.check("no default route inside", "00000000" not in destinations, routes.text)

    def create_egress(self) -> str:
        """
        The network squid goes out on, with a gateway, as Compose's egress
        has; returns the gateway's address.
        """
        created = self.docker.create_network(EGRESS)
        if not created.ok:
            self.suite.abort("could not create the egress test network", created.text)

        ipam = self.docker.inspect_network(EGRESS)["IPAM"]["Config"]
        gateways = [entry["Gateway"] for entry in ipam if entry.get("Gateway") and ":" not in entry["Gateway"]]
        if not gateways:
            self.suite.abort("the egress test network has no IPv4 gateway", json.dumps(ipam))

        return gateways[0]

    def address_on(self, container: str, network: str) -> str:
        """
        A container's address on one of its networks.
        """
        found = harness.run(["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", container])
        networks = json.loads(found.text) if found.ok else {}

        address = (networks.get(network) or {}).get("IPAddress", "")
        if not address:
            self.suite.abort(f"could not tell {container}'s address on {network}", found.text)

        return address

    def in_network_of(self, container: str, *command: str) -> None:
        """
        Run ``ip`` with *command* in a container's network, from a helper
        container that shares it and may change it; the container itself
        gets no capability.
        """
        output = self.docker.run(
            harness.BUILD_IMAGE,
            "ip",
            *command,
            memory="64m",
            network=f"container:{container}",
            capabilities=["NET_ADMIN"],
        )
        if not output.ok:
            self.suite.abort(f"could not run 'ip {' '.join(command)}' for {container}", output.text)

    def start_origin(self, container: str, outside: str, arguments: list[str], aliases: Sequence[str] = ()) -> None:
        """
        Start an origin on EGRESS (answering to *aliases* there), and give it
        its address beyond squid's networks, *outside*, which squid's routes
        lead to (``self.routes``).
        """
        mounts = [f"{FIXTURES / 'proxy_origin.py'}:/origin.py:ro", f"{self.scratch / 'tls'}:/tls:ro"]
        self.docker.run(
            harness.BUILD_IMAGE,
            "python3",
            "/origin.py",
            *arguments,
            memory="128m",
            name=container,
            detach=True,
            network=EGRESS,
            aliases=aliases,
            volumes=mounts,
        )

        self.in_network_of(container, "address", "add", f"{outside}/32", "dev", "eth0")
        self.routes[outside] = self.address_on(container, EGRESS)

    # -- the proxy -----------------------------------------------------------

    def make_certificates(self) -> Path:
        """
        A test CA, a certificate from it for the origin, and a self-signed
        one for the origin nobody should trust. Made with the squid image's
        own openssl.
        """
        tls = self.scratch / "tls"
        tls.mkdir()

        script = r"""
            set -e
            openssl req -x509 -newkey rsa:2048 -sha256 -days 2 -nodes \
                -subj "/CN=proxy test CA" -addext "basicConstraints=critical,CA:TRUE" \
                -keyout ca.key -out ca.pem
            openssl req -newkey rsa:2048 -nodes \
                -subj "/CN=origin.test" \
                -addext "subjectAltName=DNS:origin.test,DNS:sshonly.test,DNS:pinned.test,DNS:moved.test,DNS:neighbour.test" \
                -keyout origin.key -out origin.csr
            openssl x509 -req -in origin.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
                -days 2 -copy_extensions copy -out origin.pem
            openssl req -x509 -newkey rsa:2048 -sha256 -days 2 -nodes \
                -subj "/CN=badcert.test" -addext "subjectAltName=DNS:badcert.test" \
                -keyout bad.key -out bad.pem

            # What the proxy trusts: the system's CAs plus the test CA.
            cat /etc/ssl/certs/ca-certificates.crt ca.pem > bundle.pem
        """
        output = self.docker.run(
            SQUID_IMAGE,
            "-c",
            script,
            memory="128m",
            network="none",
            user=harness.current_user(),
            volumes=[f"{tls}:/tls"],
            workdir="/tls",
            entrypoint="bash",
        )
        if not output.ok:
            self.suite.abort("could not make the test certificates", output.text)

        return tls

    def start_origins(self) -> None:
        """
        The origin (origin.test and sshonly.test) and the origin with the
        untrusted certificate (badcert.test), which squid's hosts file names
        (``POLICY_HOSTS``).
        """
        self.start_origin(f"{NETWORK}-origin", OUTSIDE["origin"], [])
        self.start_origin(f"{NETWORK}-badcert", OUTSIDE["badcert"], ["bad"])

    def make_proxy_ca(self) -> tuple[Path, Path]:
        """
        The profile CA the proxy signs inspected tunnels with: the private
        directory squid reads, and the public certificate clients trust.
        """
        private, public = self.scratch / "ca", self.scratch / "ca-public"
        private.mkdir()
        public.mkdir()

        output = self.docker.run(
            SQUID_IMAGE,
            memory="128m",
            network="none",
            user="root",
            volumes=[f"{private}:/proxy-ca", f"{public}:/proxy-ca-pub"],
            entrypoint="/usr/local/bin/squid-make-ca",
        )
        if not output.ok:
            self.suite.abort("could not make the proxy CA", output.text)

        return private, public

    def start_squid(self, name: str, generated: Path, ca: Path, tls: Path, hosts: Sequence[str]) -> None:
        """
        Start squid with a rendered policy and the *hosts* its hosts file
        names (``name:address``), on the sandbox's network and EGRESS, as in
        Compose; wait until it accepts connections, and route it to every
        origin's address beyond its networks.
        """
        self.docker.run(
            SQUID_IMAGE,
            memory="512m",
            name=name,
            detach=True,
            network=NETWORK,
            networks=[EGRESS],
            aliases=["squid"],
            hosts=hosts,
            volumes=[
                f"{generated}:/etc/squid/generated:ro",
                f"{ca}:/proxy-ca:ro",
                # Trust the test CA for upstream connections, as the system
                # CAs are trusted for real origins.
                f"{tls / 'bundle.pem'}:/etc/ssl/certs/ca-certificates.crt:ro",
            ],
            tmpfs=["/var/spool/squid:mode=01777"],
        )

        def accepts_connections() -> bool:
            probe = "import socket; socket.create_connection(('squid', 3128), 2)"
            return self.docker.run(harness.BUILD_IMAGE, "python3", "-c", probe, memory="64m", network=NETWORK).ok

        if not harness.wait_until(accepts_connections, timeout=30, interval=0.5):
            self.suite.abort("squid did not come up", self.docker.logs(name))

        # Added after squid started, as a route to your network would be
        # there from the start: squid takes neither for its own network.
        for outside, through in self.routes.items():
            self.in_network_of(name, "route", "add", f"{outside}/32", "via", through)

    def start_inside_origins(self) -> str:
        """
        Origins A and B, the two machines on "your network", and D on the
        sandbox's network, beside squid's clients: their names are in
        PIN_ALLOWLIST's comment. Returns D's address.
        """
        self.start_origin(f"{NETWORK}-inside-a", OUTSIDE["inside-a"], ["A"])
        self.start_origin(f"{NETWORK}-inside-b", OUTSIDE["inside-b"], ["B"], aliases=["moved.test", "inside.test"])

        sandbox = f"{NETWORK}-sandbox"
        self.docker.run(
            harness.BUILD_IMAGE,
            "python3",
            "/origin.py",
            "D",
            memory="128m",
            name=sandbox,
            detach=True,
            network=NETWORK,
            volumes=[f"{FIXTURES / 'proxy_origin.py'}:/origin.py:ro", f"{self.scratch / 'tls'}:/tls:ro"],
        )

        return self.address_on(sandbox, NETWORK)

    def check_own_networks(self, squid: str, gateway: str) -> None:
        """
        Squid found the networks it is attached to as it started: the
        sandbox's and EGRESS, whose subnet holds the gateway.
        """
        found = harness.run(["docker", "exec", squid, "cat", "/var/spool/squid/own-networks.txt"])
        listed = found.text.split()

        subnets = []
        for network in (NETWORK, EGRESS):
            subnets += [entry["Subnet"] for entry in self.docker.inspect_network(network)["IPAM"]["Config"]]

        self.suite.check(
            "squid lists the networks it is attached to as its own",
            found.ok and all(subnet in listed for subnet in subnets),
            f"listed: {listed}; the suite's networks: {subnets}",
        )
        self.suite.check(
            "and nothing twice: the gateway is inside its subnet",
            gateway not in listed and f"{gateway}/32" not in listed and len(listed) == len(set(listed)),
            str(listed),
        )

    def check_refusal_reasons(self, squid: str) -> None:
        """
        The access log says why a name inside was refused, and only then.
        """
        log = self.docker.logs(squid, lines=200)
        lines = [line for line in log.splitlines() if "TCP_DENIED" in line]

        def reasons(name: str) -> set[str]:
            return {line.split()[-1] for line in lines if f"{name}/" in line or f"{name}:" in line}

        self.suite.check(
            "the log says a name inside was refused for its address",
            reasons("neighbour.test") == {"internal-address"},
            log,
        )
        self.suite.check("and an ssh tunnel's", reasons("inside.test") == {"internal-address"}, log)
        self.suite.check("but gives no such reason for a name with no entry", reasons("entryless.test") == {"-"}, log)
        self.suite.check(
            "and the same reason for a pin squid refuses",
            reasons("gateway.test") == {"internal-address"} and reasons("sandbox.test") == {"internal-address"},
            log,
        )

    def send(self, request: Request, ca_public: Path) -> str:
        """
        Send one request through the proxy from a client container.
        """
        output = self.docker.run(
            harness.BUILD_IMAGE,
            "python3",
            "/client.py",
            request.method,
            request.url,
            memory="64m",
            network=NETWORK,
            volumes=[f"{FIXTURES / 'proxy_client.py'}:/client.py:ro", f"{ca_public}:/proxy-ca:ro"],
        )
        return output.text.strip()

    def check_requests(self, requests: list[Request], ca_public: Path) -> None:
        """
        Send each request and compare the reply with what it should be.
        """
        for request in requests:
            got = self.send(request, ca_public)

            wanted = request.expect if isinstance(request.expect, tuple) else (request.expect,)
            described = " or ".join(f"'{outcome}'" for outcome in wanted)
            self.suite.check(request.name, got.startswith(wanted), f"wanted {described}, got '{got}'")


def run_checks(suite: Suite) -> None:
    # Containers go before the scratch directory they mount.
    with harness.scratch_directory("hc-proxy-") as scratch, Docker() as docker:
        test = ProxyTest(suite, docker, scratch)

        suite.section(f"squid image ({SQUID_IMAGE}, built from images/squid)")
        built = docker.build(SQUID_IMAGE, ROOT / "images" / "squid")
        suite.require(f"{SQUID_IMAGE} builds", built.ok, built.text)

        suite.section("renderer: the grammar corpus")
        test.check_grammar_corpus()
        test.check_rendering_edge_cases()

        suite.section("network: internal, isolated gateway")
        test.check_isolated_network()
        gateway = test.create_egress()

        suite.section("the proxy against a normalising origin (HTTP and inspected HTTPS)")
        tls = test.make_certificates()
        test.start_origins()

        accepted, rendered, log = test.render(ALLOWLIST, "policy")
        suite.require("the test allowlist renders", accepted, log)

        internal = read_lines(rendered / "squid" / "internal.txt")
        suite.check(
            "the default internal ranges are rendered",
            "172.16.0.0/12" in internal and "169.254.0.0/16" in internal,
            "\n".join(internal),
        )
        (rendered / "squid" / "internal.txt").write_text(INTERNAL_RANGES, encoding="utf-8")

        ca_private, ca_public = test.make_proxy_ca()
        test.start_squid(f"{NETWORK}-squid", rendered / "squid", ca_private, tls, POLICY_HOSTS)

        test.check_requests(PLAIN_HTTP, ca_public)
        suite.note("-- the same through an inspected HTTPS tunnel")
        test.check_requests(INSPECTED_HTTPS, ca_public)
        test.check_requests(TUNNELS, ca_public)

        suite.section("names on your network")
        docker.remove(f"{NETWORK}-squid")
        sandbox = test.start_inside_origins()
        pins = PIN_ALLOWLIST.format(a=OUTSIDE["inside-a"], gateway=gateway, sandbox=sandbox)

        # The renderer refuses a pin into the sandbox's network as the
        # profile names it; squid, into the networks it finds itself on.
        devnet = docker.inspect_network(NETWORK)["IPAM"]["Config"][0]["Subnet"]
        accepted, _, log = test.render(pins, "pins-in-devnet", {"DEVNET_SUBNET": devnet})
        suite.check(
            "the renderer refuses a pin inside DEVNET_SUBNET",
            not accepted and f"{sandbox} is in the sandbox's own network ({devnet})" in log,
            log,
        )

        accepted, pinned, log = test.render(pins, "pins", {"DEVNET_SUBNET": ELSEWHERE})
        suite.require("an allowlist with names on your network renders", accepted, log)
        test.start_squid(f"{NETWORK}-pins", pinned / "squid", ca_private, tls, PIN_HOSTS)
        test.check_own_networks(f"{NETWORK}-pins", gateway)

        test.check_requests(PINNED, ca_public)
        suite.note("-- pins into squid's own networks, allowed and pinned")
        test.check_requests(NEVER_PINNED, ca_public)
        test.check_refusal_reasons(f"{NETWORK}-pins")

        suite.section("the proxy with nothing allowed")
        docker.remove(f"{NETWORK}-pins")
        empty = scratch / "empty" / "squid"
        test.start_squid(f"{NETWORK}-deny-all", empty, ca_private, tls, POLICY_HOSTS)
        test.check_requests(NOTHING_ALLOWED, ca_public)


if __name__ == "__main__":
    harness.main("proxy", run_checks)
