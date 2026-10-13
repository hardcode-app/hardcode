#!/usr/bin/env python3
"""
The Kubernetes operator and the admin API, against an in-memory fake of the
Kubernetes API (admin/tests), then eight sign-ins at once in a container
capped at the chart's memory limit for the admin. Flask, qrcode and openssl
come from the build image.
"""

from __future__ import annotations

import yaml

import harness
from harness import FIXTURES, ROOT, Suite


def admin_memory_limit() -> str:
    """
    The chart's memory limit for the admin's container, as Docker writes it
    (``256Mi`` is ``256m``).
    """
    values = yaml.safe_load((ROOT / "helm" / "hardcode" / "values.yaml").read_text())
    limit = values["admin"]["resources"]["limits"]["memory"]

    return limit.replace("Mi", "m").replace("Gi", "g")


def check_login_burst(suite: Suite) -> None:
    """
    As many sign-ins at once as the admin's server has threads: each hashes
    a password (32 MB of scrypt), and they must fit in the container's limit
    together, or anyone who can reach the sign-in page can have the admin
    and its operator killed. Without swap, as in a pod.
    """
    limit = admin_memory_limit()

    with harness.Docker() as docker:
        output = docker.run(
            harness.BUILD_IMAGE,
            "python3",
            FIXTURES / "admin_login_burst.py",
            ROOT,
            memory=limit,
            memory_swap=limit,
            network="none",
            user=harness.current_user(),
            volumes=[f"{ROOT}:{ROOT}:ro"],
            timeout=300,
        )

    killed = " (killed: out of memory)" if output.code == 137 else ""
    suite.check(
        f"eight sign-ins at once fit in the admin's {limit}",
        output.ok,
        f"exit {output.code}{killed}\n{output.tail(20)}",
    )


def run_checks(suite: Suite) -> None:
    # Discovery runs from admin/ so that `tests` means the admin's tests: from
    # anywhere else, the repository's tests/ or cli/tests/ would be found
    # first. hardcode_admin finds the CLI package by itself.
    admin = ROOT / "admin"
    harness.run_unittests(suite, "admin/tests", start=admin / "tests", top=admin, cwd=admin)

    suite.section("sign-ins under the memory limit")
    check_login_burst(suite)


if __name__ == "__main__":
    harness.main("admin", run_checks)
