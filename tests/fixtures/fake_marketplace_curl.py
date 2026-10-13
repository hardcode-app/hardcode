#!/usr/bin/env python3
"""
A stand-in for ``curl`` posting an extension query to the Marketplace, for
tests/images.py: what ``hardcode-install-extensions --verify`` asks to learn
whether each extension's publisher has a verified domain.

The query arrives on stdin (``--data-binary @-``), one criterion per id. The
answer lists every id the fake Marketplace ($FAKE_MARKETPLACE, the same
directory fake_code.py installs from) has, with its publisher verified unless
the id is in $FAKE_UNVERIFIED (comma separated). With $FAKE_MARKETPLACE_DOWN
set, it fails as curl does when the server can't be reached. With
$FAKE_CURL_LOG set, each call's arguments are added to that file, a line
per call.
"""

import json
import os
import sys
from pathlib import Path


def main() -> int:
    """
    Answer the query on stdin, or fail as an unreachable server.
    """
    calls = os.environ.get("FAKE_CURL_LOG")
    if calls:
        with open(calls, "a", encoding="utf-8") as handle:
            handle.write(" ".join(sys.argv[1:]) + "\n")

    if os.environ.get("FAKE_MARKETPLACE_DOWN"):
        print("curl: (7) Failed to connect to marketplace.visualstudio.com", file=sys.stderr)
        return 7

    query = json.loads(sys.stdin.read())
    wanted = [criterion["value"].lower() for criterion in query["filters"][0]["criteria"]]

    market = Path(os.environ["FAKE_MARKETPLACE"])
    unverified = {item.strip().lower() for item in os.environ.get("FAKE_UNVERIFIED", "").split(",") if item.strip()}

    extensions = []
    for extension_id in wanted:
        if not (market / f"{extension_id}.json").exists():
            continue

        publisher, name = extension_id.split(".", 1)
        extensions.append(
            {
                "extensionName": name,
                "publisher": {
                    "publisherName": publisher,
                    "displayName": publisher.title(),
                    "isDomainVerified": extension_id not in unverified,
                },
            }
        )

    print(json.dumps({"results": [{"extensions": extensions}]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
