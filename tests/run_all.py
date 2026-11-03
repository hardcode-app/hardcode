#!/usr/bin/env python3
"""
Every test suite, one after the other, in the build image.

    tests/commits.py   the commit messages: Conventional Commits subjects, no attribution
    tests/lint.py      formatting and lint: Python (ruff, then isort) and Rust (rustfmt)
    tests/cli.py       the hardcode command: unit tests, then a scenario against a throwaway copy
    tests/app.py       the desktop app: unit tests, a build, and a headless launch
    tests/admin.py     the Kubernetes operator and admin API (fake API server)
    tests/proxy.py     the squid image for real: path rules, read-only, deny-all, the renderer's grammar
    tests/images.py    the VS Code image's start-up scripts: the SSH proxy stanza, home seeding
    tests/docker.py    the Docker sidecar: its runc wrapper with the real runc on real images, then the daemon
    tests/helm.py      the Helm chart: lint and the rendered objects
    tests/release.py   the release's steps: the version, the updater's configuration, latest.json, checksums
    tests/maintenance.py  the dependency scripts: pins and their ages, vendor releases, which updates merge
    tests/e2e_kind.py  the operator and admin on a real local kind cluster (opt in with E2E=1)
"""

from __future__ import annotations

import os
import subprocess
import sys

import harness
from harness import ROOT

SUITES = ["commits", "lint", "cli", "app", "admin", "proxy", "images", "docker", "helm", "release", "maintenance"]


def main() -> int:
    harness.enter_build_image()

    suites = list(SUITES)
    if os.environ.get("E2E") == "1":
        suites.append("e2e_kind")

    failed = []
    for name in suites:
        print(f"\n########## {name}", flush=True)
        if subprocess.call([sys.executable, ROOT / "tests" / f"{name}.py"]) != 0:
            failed.append(name)

    if "e2e_kind" not in suites:
        print("\n(skipping tests/e2e_kind.py; set E2E=1 to run it on a kind cluster)")

    print()
    if failed:
        print(f"FAILED: {' '.join(failed)}")
        return 1

    print("all suites passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
