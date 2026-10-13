#!/usr/bin/env python3
"""
Formatting and lint.

Python as pyproject.toml configures it (ruff's formatter and default rules,
then isort for the imports) and Rust with rustfmt.

    tests/lint.py          check
    tests/lint.py --fix    rewrite the files
"""

from __future__ import annotations

import sys

import harness
from harness import ROOT, Suite

# cli/hardcode is Python without a .py suffix: ruff finds it through
# extend-include in pyproject.toml, isort has to be told.
PYTHON = ["cli", "admin", "docs/tools", "release", "tests"]
PYTHON_SCRIPTS = ["cli/hardcode"]
RUST = ROOT / "app" / "src-tauri"


def fix() -> int:
    """
    Format everything in place; returns the exit code.
    """
    steps = [
        (["ruff", "format", *PYTHON], ROOT),
        (["ruff", "check", "--fix", *PYTHON], ROOT),
        (["isort", *PYTHON, *PYTHON_SCRIPTS], ROOT),
        (["cargo", "fmt"], RUST),
    ]

    for command, directory in steps:
        output = harness.run(command, cwd=directory)
        print(output.text, end="", flush=True)
        if not output.ok:
            return output.code

    return 0


def run_checks(suite: Suite) -> None:
    hint = "tests/lint.py --fix rewrites"

    output = harness.run(["ruff", "format", "--check", *PYTHON], cwd=ROOT)
    suite.check("ruff format", output.ok, f"{hint}\n{output.text}")

    output = harness.run(["ruff", "check", *PYTHON], cwd=ROOT)
    suite.check("ruff check", output.ok, output.text)

    output = harness.run(["isort", "--check-only", "--diff", *PYTHON, *PYTHON_SCRIPTS], cwd=ROOT)
    suite.check("isort", output.ok, f"{hint}\n{output.text}")

    output = harness.run(["cargo", "fmt", "--check"], cwd=RUST)
    suite.check("rustfmt", output.ok, f"{hint}\n{output.text}")


if __name__ == "__main__":
    if sys.argv[1:] == ["--fix"]:
        harness.enter_build_image()
        sys.exit(fix())

    harness.main("lint", run_checks)
