"""
A client that sends one request through the proxy and prints what came back.

Run by tests/proxy.py inside the test network:

    proxy_client.py GET http://origin.test/allowed/good
    proxy_client.py POST https://origin.test/upload/file
    proxy_client.py CONNECT sshonly.test:22
    proxy_client.py SSH pinned.test:22

It speaks HTTP by hand rather than through urllib, so the request line reaches
the proxy exactly as written: no client-side clean-up of dot segments,
backslashes or percent escapes, which are what the suite is testing. It prints
a single line:

    200 GET /allowed/good    the status, then the last line of the body
                             (what the origin saw)
    403                      the status alone, when the body is the
                             proxy's HTML error page
    reset / timeout          a tunnel the proxy dropped without answering
    tunnel 403               a CONNECT the proxy refused with a status
    tls-error                the TLS handshake inside a tunnel failed
    200 SSH-2.0-NAME         SSH: the tunnel opened, and that server greeted
    200 reset                SSH: the tunnel opened, and was reset as the
                             client greeted (nothing came from a server)
"""

import socket
import ssl
import sys

PROXY = ("squid", 3128)
TIMEOUT = 5  # seconds, for connecting and for each read

# The profile's CA, which the proxy signs inspected tunnels with: a VS Code container
# trusts it, and so does this client.
PROXY_CA = "/proxy-ca/ca.pem"


def status_code(response: bytes) -> str:
    """
    The three-digit status from the first line of an HTTP response.
    """
    status_line = response.split(b"\r\n")[0].decode(errors="replace")
    return status_line.split(" ", 1)[1][:3]


def read_all(connection: socket.socket) -> bytes:
    """
    Everything the other side sends until it closes the connection.

    A TLS connection the proxy closes without a clean shutdown raises instead
    of returning an empty read; what arrived until then is what counts.
    """
    data = b""

    try:
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            data += chunk
    except (ssl.SSLError, OSError):
        pass

    return data


def summarise(response: bytes) -> str:
    """
    The status code, and the last line of the body when it is plain text.
    """
    head, _, body = response.partition(b"\r\n\r\n")
    if not head:
        return "no response"

    text = body.decode(errors="replace").strip()
    if not text or text.startswith("<"):
        # The proxy's own error pages are HTML; the status says it all.
        return status_code(head)

    return f"{status_code(head)} {text.splitlines()[-1]}"


def send_request(connection: socket.socket, method: str, target: str, host: str) -> None:
    """
    Send a request with an empty body, asking the server to close afterwards.
    """
    lines = [
        f"{method} {target} HTTP/1.1",
        f"Host: {host}",
        "Content-Length: 0",
        "Connection: close",
        "",  # the blank line that ends the headers
        "",
    ]
    connection.sendall("\r\n".join(lines).encode())


def ask_for_tunnel(connection: socket.socket, destination: str) -> bytes:
    """
    Send a CONNECT for *destination* (``host:port``) and return the first
    bytes of the answer: empty when the proxy reset the connection instead.
    """
    connection.sendall(f"CONNECT {destination} HTTP/1.1\r\nHost: {destination}\r\n\r\n".encode())

    try:
        return connection.recv(65536)
    except ConnectionResetError:
        return b""


def plain_request(connection: socket.socket, method: str, url: str) -> str:
    """
    A plain HTTP request: the absolute URL goes to the proxy as written.
    """
    send_request(connection, method, url, host="origin.test")
    return summarise(read_all(connection))


def inspected_request(connection: socket.socket, method: str, url: str) -> str:
    """
    An HTTPS request: a tunnel the proxy inspects, TLS to the proxy's
    certificate for the host, then the request inside it.
    """
    host_and_port, _, path = url[len("https://") :].partition("/")
    host, _, port = host_and_port.partition(":")

    try:
        answer = ask_for_tunnel(connection, f"{host}:{port or 443}")
    except socket.timeout:
        answer = b""
    if not answer:
        return "tunnel reset"
    if not answer.startswith(b"HTTP/1.1 200"):
        return f"tunnel {status_code(answer)}"

    context = ssl.create_default_context(cafile=PROXY_CA)
    try:
        connection = context.wrap_socket(connection, server_hostname=host)
    except (ssl.SSLError, OSError):
        return "tls-error"

    send_request(connection, method, f"/{path}", host=host)
    return summarise(read_all(connection))


def tunnel(connection: socket.socket, destination: str) -> str:
    """
    A bare CONNECT: the answer is a status, or the proxy dropping the
    connection, which is how it refuses tunnels.
    """
    try:
        answer = ask_for_tunnel(connection, destination)
    except socket.timeout:
        return "timeout"

    return status_code(answer) if answer else "reset"


def ssh(connection: socket.socket, destination: str) -> str:
    """
    A tunnel the way ssh uses one: a CONNECT, then its version line at
    once, as ssh sends it. The answer is the server's greeting; or how the
    proxy refused, at the CONNECT or as the client greeted.
    """
    try:
        answer = ask_for_tunnel(connection, destination)
    except socket.timeout:
        return "timeout"

    if not answer:
        return "reset"
    if not answer.startswith(b"HTTP/1.1 200"):
        return f"tunnel {status_code(answer)}"

    try:
        connection.sendall(b"SSH-2.0-proxy-test\r\n")
        greeting = connection.recv(256)
    except OSError:
        greeting = b""

    line = greeting.split(b"\r\n")[0].decode(errors="replace")
    return f"200 {line}" if line else "200 reset"


def main() -> None:
    method, target = sys.argv[1], sys.argv[2]

    connection = socket.create_connection(PROXY, TIMEOUT)
    connection.settimeout(TIMEOUT)

    if method == "CONNECT":
        print(tunnel(connection, target))
    elif method == "SSH":
        print(ssh(connection, target))
    elif target.startswith("https://"):
        print(inspected_request(connection, method, target))
    else:
        print(plain_request(connection, method, target))


if __name__ == "__main__":
    main()
