#!/usr/bin/env python3
"""
A stand-in for ``apt-get``, for tests/images.py's check of the VS Code
image's package installer.

Each call is written to $FAKE_APT_LOG as a JSON line: its arguments, whether
it came through the fake sudo, and the DEBIAN_FRONTEND it had. ``install``
of a package named in $FAKE_APT_MISSING (space-separated) fails as apt does
for a package the archive doesn't have, with apt's own words and exit code.
"""

import json
import os
import sys

# apt-get's exit code for an error, such as a package it can't find.
APT_ERROR = 100


def main(arguments: list[str]) -> int:
    """
    Record one apt-get call, and answer it.
    """
    call = {
        "args": arguments,
        "sudo": os.environ.get("FAKE_SUDO") == "1",
        "frontend": os.environ.get("DEBIAN_FRONTEND", ""),
    }
    with open(os.environ["FAKE_APT_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps(call) + "\n")

    missing = os.environ.get("FAKE_APT_MISSING", "").split()
    if arguments[:1] == ["install"]:
        for name in arguments[1:]:
            if name in missing:
                print(f"E: Unable to locate package {name}", file=sys.stderr)
                return APT_ERROR

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
