"""
Tries a TCP connection to each target and says whether it got through.

tests/e2e_kind.py runs this in a pod labelled like a VS Code container, so the
NetworkPolicies treat it as one. The targets arrive in $TARGETS, as
space-separated ``name=address:port`` items, and the answer is one line each:

    reachable squid-svc
    unreachable internet TimeoutError

A refused connection and a timeout both count as unreachable: a policy may
drop packets or reject them.
"""

import os
import socket

TIMEOUT = 3  # seconds


def main() -> None:
    for target in os.environ["TARGETS"].split():
        name, address = target.split("=", 1)
        host, port = address.rsplit(":", 1)

        try:
            socket.create_connection((host, int(port)), TIMEOUT).close()
            print("reachable", name)
        except OSError as error:
            print("unreachable", name, type(error).__name__)


if __name__ == "__main__":
    main()
