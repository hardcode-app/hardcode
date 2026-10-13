"""
The origin server behind the proxy in tests/proxy.py.

It answers every GET, POST and PUT with "<METHOD> <path>", where the path is
what a careless web server would make of the request: percent-decoded,
backslashes taken for slashes (as a Windows-minded server does), dot segments
resolved. If the proxy ever lets an escaping path through, the reply shows
where the request really ended up.

    proxy_origin.py        plain HTTP on 80, and TLS on 443 with a
                           certificate from the suite's test CA
    proxy_origin.py bad    TLS on 443 with a self-signed certificate
                           that nobody vouches for

The certificates are made by the suite and mounted at /tls.
"""

import posixpath
import ssl
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer


class EchoPath(BaseHTTPRequestHandler):
    """
    Replies with the method and the normalised path of each request.
    """

    def reply(self) -> None:
        path_only = self.path.split("?")[0]
        decoded = urllib.parse.unquote(path_only).replace("\\", "/")
        resolved = posixpath.normpath(decoded)

        body = f"{self.command} {resolved}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = reply
    do_POST = reply
    do_PUT = reply

    def log_message(self, *args) -> None:
        """
        Stay quiet: the suite reads what the client prints, not the server.
        """


def serve(port: int, certificate: str = "", key: str = "") -> None:
    """
    Serve forever on *port*, over TLS when a certificate is given.
    """
    server = HTTPServer(("0.0.0.0", port), EchoPath)

    if certificate:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)

    server.serve_forever()


def main() -> None:
    if sys.argv[1:] == ["bad"]:
        serve(443, "/tls/bad.pem", "/tls/bad.key")
        return

    tls = threading.Thread(target=serve, args=(443, "/tls/origin.pem", "/tls/origin.key"), daemon=True)
    tls.start()
    serve(80)


if __name__ == "__main__":
    main()
