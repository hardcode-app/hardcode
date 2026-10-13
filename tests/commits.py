#!/usr/bin/env python3
"""
The commit messages on the branch: each subject in the Conventional Commits
form, ``type(scope)!: summary``, and no attribution lines (AGENTS.md, Git).

    tests/commits.py    every commit reachable from HEAD, merges left out

A commit that bundles several changes lists them in its body, a line each in
the same form, so that changelog tools can split it; only the subject is
checked here.
"""

from __future__ import annotations

import re

import harness
from harness import ROOT, Suite

# What a change is. The scope, when there is one, names the part of the
# repository: cli, app, admin, chart, images, proxy, release, ci, deps...
TYPES = ("feat", "fix", "docs", "test", "ci", "build", "refactor", "perf", "chore", "revert", "style")

# type(scope)!: summary. The ! marks a breaking change, explained in a
# "BREAKING CHANGE:" footer.
SUBJECT = re.compile(rf"^(?:{'|'.join(TYPES)})(?:\([a-z0-9][a-z0-9-]*\))?!?: \S")

# The lines tools add to say who or what wrote a commit; the commits are the
# owner's.
ATTRIBUTION = re.compile(r"^\s*(?:co-authored-by:|.*generated with\b)", re.IGNORECASE | re.MULTILINE)

# Between git log's records: what no commit message contains.
SEPARATOR = "\x1e"


def commits() -> list[tuple[str, str]]:
    """
    Every commit reachable from HEAD but merges, as (short hash, message).
    """
    output = harness.run(["git", "log", "--no-merges", f"--format=%h%n%B{SEPARATOR}"], cwd=ROOT)
    if not output.ok:
        raise harness.SuiteAborted("git log failed", output.text)

    found = []
    for record in output.text.split(SEPARATOR):
        record = record.strip("\n")
        if not record:
            continue

        short, _, message = record.partition("\n")
        found.append((short, message))

    return found


def run_checks(suite: Suite) -> None:
    history = commits()

    badly_named = [
        f"{short} {message.splitlines()[0] if message else '(no message)'}"
        for short, message in history
        if not SUBJECT.match(message)
    ]
    suite.check(
        f"{len(history)} commits: every subject is type(scope): summary",
        not badly_named,
        "types: " + ", ".join(TYPES) + "\n" + "\n".join(badly_named),
    )

    attributed = [
        f"{short}: {match.group(0).strip()}" for short, message in history for match in ATTRIBUTION.finditer(message)
    ]
    suite.check("no attribution lines", not attributed, "\n".join(attributed))


if __name__ == "__main__":
    harness.main("commits", run_checks)
