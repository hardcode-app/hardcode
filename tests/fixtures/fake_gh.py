#!/usr/bin/env python3
"""
A stand-in for GitHub's gh, for the release workflow's steps that talk to
GitHub (tests/release.py runs them with this as gh). It writes each call,
one per line, to FAKE_GH_LOG when that is set, and answers:

- ``gh release list ...``: FAKE_RELEASE_LIST, as the step's --jq would
  print it (the suite sets what the real filter would give);
- ``gh api repos/.../git/ref/tags/TAG``: the commit in FAKE_DEV_COMMIT, or
  fails as gh does when there is no such tag (FAKE_DEV_COMMIT empty);
- ``gh api repos/.../git/ref/heads/main``: the commit in FAKE_MAIN_COMMIT;
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

    if arguments[:2] == ["release", "list"]:
        listed = os.environ.get("FAKE_RELEASE_LIST", "")
        if listed:
            print(listed)
        return 0

    path = arguments[1] if arguments[:1] == ["api"] and len(arguments) > 1 else ""

    if "/git/ref/tags/" in path:
        commit = os.environ.get("FAKE_DEV_COMMIT", "")
        if not commit:
            print("gh: Not Found (HTTP 404)", file=sys.stderr)
            return 1
        print(commit)
        return 0

    if path.endswith("/git/ref/heads/main"):
        print(os.environ.get("FAKE_MAIN_COMMIT", ""))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
