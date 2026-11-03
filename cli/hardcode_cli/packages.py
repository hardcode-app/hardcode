"""
A profile's own Ubuntu packages, beside what its languages and tools install.

They are ``VSCODE_PACKAGES`` in the profile's ``.env``, names separated by
spaces. On Compose the profile's VS Code image installs them, a build arg
of its own, and records them in a label, so that a changed list means a
rebuild is due. On Kubernetes, where the image is shared, the VS Code
container installs them as it starts (the Profile's ``spec.packages``).

A name is checked with Debian's rule for package names; whether the archive
has it is apt's to say, when the image is built.
"""

from __future__ import annotations

import re

# The .env key, and the build arg compose.yaml hands it to.
ENV_KEY = "VSCODE_PACKAGES"

# Debian's rule: lower-case letters, digits, "+", "-" and ".", at least two
# characters, starting with a letter or a digit. The image's Dockerfile, the
# VS Code container's installer and the CRD check the same.
PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
NAME_RULE = "lower-case letters, digits, +, - and ., at least two characters, starting with a letter or a digit"


class PackageError(ValueError):
    """
    Not the name of an Ubuntu package.
    """


def check(name: str) -> str:
    """
    *name* without the spaces around it; an error when it isn't a package
    name by Debian's rule.
    """
    package = name.strip()

    if not PACKAGE_NAME.match(package):
        raise PackageError(f"'{name}' is not an Ubuntu package name ({NAME_RULE})")

    return package


def parse(text: str) -> list[str]:
    """
    The package names in a ``VSCODE_PACKAGES`` value, in their order and
    without repeats. They are read, not checked, here: a name written by
    hand that isn't one fails the image's build, which says so.
    """
    return list(dict.fromkeys(text.split()))


def join(packages: list[str]) -> str:
    """
    The ``VSCODE_PACKAGES`` value for *packages*.
    """
    return " ".join(packages)


def same(first: list[str], second: list[str]) -> bool:
    """
    Whether two lists name the same packages, in whatever order: the order
    makes no difference to what the image has.
    """
    return sorted(set(first)) == sorted(set(second))
