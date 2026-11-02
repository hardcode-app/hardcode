#!/usr/bin/env python3
"""
A stand-in for GitHub's gh, for the release workflow's steps that talk to
GitHub (tests/release.py runs them with this as gh). It writes each call,
one per line, to FAKE_GH_LOG when that is set, and answers:

- ``gh api repos/.../git/ref/tags/dev``: the commit in FAKE_DEV_COMMIT, or
  fails as gh does when there is no dev tag (FAKE_DEV_COMMIT empty);
- ``gh api repos/.../git/ref/heads/main``: the commit in FAKE_MAIN_COMMIT;
- ``gh release view dev``: succeeds when there is a dev tag;
- anything else: succeeds, saying nothing.
"""

import os
import sys


def main() -> int:
    """
    Record the call, then answer it.
    """
    arguments = sys.argv[1:]
    log = os.environ.get("FAKE_GH_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as file:
            file.write(" ".join(arguments) + "\n")

    dev = os.environ.get("FAKE_DEV_COMMIT", "")
    path = arguments[1] if arguments[:1] == ["api"] and len(arguments) > 1 else ""

    if path.endswith("/git/ref/tags/dev") or arguments[:3] == ["release", "view", "dev"]:
        if not dev:
            print("gh: Not Found (HTTP 404)", file=sys.stderr)
            return 1
        if path:
            print(dev)
        return 0

    if path.endswith("/git/ref/heads/main"):
        print(os.environ.get("FAKE_MAIN_COMMIT", ""))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
