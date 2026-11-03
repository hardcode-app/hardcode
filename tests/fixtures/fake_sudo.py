#!/usr/bin/env python3
"""
A stand-in for ``sudo``, for tests/images.py's check of the VS Code image's
package installer: it runs its command as it is, as the same user, with
FAKE_SUDO=1 in the environment, so that what it runs can tell it came
through sudo.
"""

import os
import sys


def main(arguments: list[str]) -> int:
    """
    Run *arguments* in place of this process; never returns when there are any.
    """
    if not arguments:
        print("usage: sudo COMMAND [ARGUMENT...]", file=sys.stderr)
        return 1

    os.environ["FAKE_SUDO"] = "1"
    os.execvp(arguments[0], arguments)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
