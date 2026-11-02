#!/usr/bin/env python3
"""
A stand-in for GitHub's gh, for the release workflow's nightly check
(tests/release.py runs the check's script with this as gh): it answers
``gh api repos/.../git/ref/tags/dev --jq .object.sha`` with the commit in
FAKE_DEV_COMMIT, or fails as gh does when there is no dev tag
(FAKE_DEV_COMMIT empty).
"""

import os
import sys


def main() -> int:
    """
    Print the dev tag's commit, or fail as gh does without one.
    """
    commit = os.environ.get("FAKE_DEV_COMMIT", "")
    if sys.argv[1:2] != ["api"] or not commit:
        print("gh: Not Found (HTTP 404)", file=sys.stderr)
        return 1

    print(commit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
