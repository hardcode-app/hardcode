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
- an empty allowlist renders a policy that allows nothing
- the renderer agrees with the CLI's grammar (tests/allowlist-corpus.txt) and
  renders a last line that has no newline
- an internal network in isolated gateway mode has no way to the host

Every container is memory-capped; nothing of your own stack is touched.

    tests/proxy.py                    build hardcode-squid:test and test it
    SQUID_IMAGE=name tests/proxy.py   test an image you built yourself
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Union

import harness
from harness import FIXTURES, ROOT, Docker, Suite

SQUID_IMAGE = os.environ.get("SQUID_IMAGE", "hardcode-squid:test")
NETWORK = f"hc-proxy-test-{os.getpid()}"
REFUSED = "403"

# What the proxy allows while the requests below are sent. metadata.test is
# pointed at a link-local address, as a cloud metadata service would be, to
# show that an allowlisted name resolving inside is still refused.
ALLOWLIST = """\
origin.test/allowed
origin.test/upload   rw
metadata.test
badcert.test
sshonly.test:22 ssh
"""

# The test origin lives on a private Docker network, so for these checks only
# loopback, link-local and shared address space count as internal.
INTERNAL_RANGES = """\
127.0.0.0/8
169.254.0.0/16
100.64.0.0/10
"""

# The names on your network: two origins on the test network, which is
# private, so internal to the proxy's default ranges. Docker's DNS answers
# pinned.test and neighbour.test with origin A, and moved.test and
# inside.test with origin B; the pins send pinned.test, moved.test and
# entryless.test to origin A ({inside} is its address).
PIN_ALLOWLIST = """\
pinned.test
pinned.test:22 ssh
moved.test
moved.test:22 ssh
neighbour.test
inside.test
inside.test:22 ssh
lan pinned.test {inside}
lan moved.test {inside}
lan entryless.test {inside}
"""

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
    Request("an unpinned name inside is refused over HTTP", "GET", "http://inside.test/x"),
    Request("a pin without an entry allows nothing", "GET", "http://entryless.test/x"),
    Request("nor a tunnel", "SSH", "entryless.test:22", "reset"),
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

    # -- the renderer --------------------------------------------------------

    def render(self, allowlist_text: str, name: str) -> tuple[bool, Path, str]:
        """
        Render an allowlist the way the one-shot ``allowlist`` service does.

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

    def start_origins(self, tls: Path) -> None:
        """
        The origin (origin.test and sshonly.test) and the origin with the
        untrusted certificate (badcert.test).
        """
        script = FIXTURES / "proxy_origin.py"
        mounts = [f"{script}:/origin.py:ro", f"{tls}:/tls:ro"]

        for name, aliases, arguments in (
            ("origin", ["origin.test", "sshonly.test"], []),
            ("badcert", ["badcert.test"], ["bad"]),
        ):
            self.docker.run(
                harness.BUILD_IMAGE,
                "python3",
                "/origin.py",
                *arguments,
                memory="128m",
                name=f"{NETWORK}-{name}",
                detach=True,
                network=NETWORK,
                aliases=aliases,
                volumes=mounts,
            )

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

    def start_squid(self, name: str, generated: Path, ca: Path, tls: Path) -> None:
        """
        Start squid with a rendered policy and wait until it accepts connections.
        """
        self.docker.run(
            SQUID_IMAGE,
            memory="512m",
            name=name,
            detach=True,
            network=NETWORK,
            aliases=["squid"],
            hosts=["metadata.test:169.254.169.254"],
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

    def start_inside_origins(self, tls: Path) -> str:
        """
        Origins A and B, the two machines on "your network" (the test
        network is private): their names, as Docker's DNS answers them, are
        in PIN_ALLOWLIST's comment. Returns A's address.
        """
        mounts = [f"{FIXTURES / 'proxy_origin.py'}:/origin.py:ro", f"{tls}:/tls:ro"]

        for name, aliases in (("A", ["pinned.test", "neighbour.test"]), ("B", ["moved.test", "inside.test"])):
            self.docker.run(
                harness.BUILD_IMAGE,
                "python3",
                "/origin.py",
                name,
                memory="128m",
                name=f"{NETWORK}-inside-{name.lower()}",
                detach=True,
                network=NETWORK,
                aliases=aliases,
                volumes=mounts,
            )

        found = harness.run(
            ["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", f"{NETWORK}-inside-a"]
        )
        networks = json.loads(found.text) if found.ok else {}
        address = (networks.get(NETWORK) or {}).get("IPAddress", "")
        if not address:
            self.suite.abort("could not tell origin A's address", found.text)

        return address

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

        suite.section("the proxy against a normalising origin (HTTP and inspected HTTPS)")
        tls = test.make_certificates()
        test.start_origins(tls)

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
        test.start_squid(f"{NETWORK}-squid", rendered / "squid", ca_private, tls)

        test.check_requests(PLAIN_HTTP, ca_public)
        suite.note("-- the same through an inspected HTTPS tunnel")
        test.check_requests(INSPECTED_HTTPS, ca_public)
        test.check_requests(TUNNELS, ca_public)

        suite.section("names on your network")
        docker.remove(f"{NETWORK}-squid")
        inside = test.start_inside_origins(tls)

        accepted, pinned, log = test.render(PIN_ALLOWLIST.format(inside=inside), "pins")
        suite.require("an allowlist with names on your network renders", accepted, log)
        test.start_squid(f"{NETWORK}-pins", pinned / "squid", ca_private, tls)

        test.check_requests(PINNED, ca_public)
        test.check_refusal_reasons(f"{NETWORK}-pins")

        suite.section("the proxy with nothing allowed")
        docker.remove(f"{NETWORK}-pins")
        empty = scratch / "empty" / "squid"
        test.start_squid(f"{NETWORK}-deny-all", empty, ca_private, tls)
        test.check_requests(NOTHING_ALLOWED, ca_public)


if __name__ == "__main__":
    harness.main("proxy", run_checks)
