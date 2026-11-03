#!/usr/bin/env python3
"""
Formatting and lint.

Python as pyproject.toml configures it (ruff's formatter and default rules,
then isort for the imports) and Rust with rustfmt; and every file the Python
reads or writes as text names its encoding.

    tests/lint.py          check
    tests/lint.py --fix    rewrite the files
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import harness
from harness import ROOT, Suite

# cli/hardcode is Python without a .py suffix: ruff finds it through
# extend-include in pyproject.toml, isort has to be told.
PYTHON = ["cli", "admin", "docs/tools", "release", "tests", "images/docker"]
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


def unnamed_encodings(paths: list[Path]) -> list[str]:
    """
    Every text file read or written without naming its encoding, as
    ``path:line: call``: ``read_text``, ``write_text`` and ``open`` in text
    mode.

    Without one, Python uses the platform's default, which on Windows isn't
    UTF-8: a file with a character outside ASCII (compose.yaml's diagram)
    can't be read there. ruff's own rule misses calls on paths it can't
    tell are paths, so the code is read here.
    """
    found = []

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or any(keyword.arg == "encoding" for keyword in node.keywords):
                continue

            function = node.func
            name = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", "")
            if name in ("read_text", "write_text"):
                found.append(f"{path.relative_to(ROOT)}:{node.lineno}: {name}")
                continue
            if name != "open" or isinstance(function, ast.Attribute):
                continue

            # open's mode: the second argument, or mode=; binary needs no encoding.
            mode = node.args[1] if len(node.args) > 1 else None
            mode = next((keyword.value for keyword in node.keywords if keyword.arg == "mode"), mode)
            if isinstance(mode, ast.Constant) and "b" in str(mode.value):
                continue
            found.append(f"{path.relative_to(ROOT)}:{node.lineno}: open")

    return found


def python_files() -> list[Path]:
    """
    The Python files lint covers.
    """
    files = [ROOT / script for script in PYTHON_SCRIPTS]
    for directory in PYTHON:
        files.extend(sorted((ROOT / directory).rglob("*.py")))

    return files


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

    unnamed = unnamed_encodings(python_files())
    suite.check(
        f"text files name their encoding ({len(unnamed)} don't)",
        not unnamed,
        "\n".join(unnamed[:40]),
    )


if __name__ == "__main__":
    if sys.argv[1:] == ["--fix"]:
        harness.enter_build_image()
        sys.exit(fix())

    harness.main("lint", run_checks)
