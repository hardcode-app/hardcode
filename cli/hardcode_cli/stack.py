"""
The stack: what HardCode runs every profile with.

``compose.yaml`` (and ``compose.image.yaml``, for a profile with Dockerfile
steps of its own), the build contexts of the images it builds (squid, the
gateway, the Docker daemon, the VS Code container) and the template a new profile's allowlist
and override start from. It belongs to the HardCode that is running, never
to the user: a newer HardCode brings its own, and a profile keeps only what
differs (its ``.env`` over compose.yaml's defaults, its
``compose.override.yaml`` over compose.yaml).

Run from a checkout, the stack is the checkout's own files, so a change to
them applies at once. Installed (pip, a standalone binary, the app's bundled
CLI), it ships inside this package, and is written out to the cache under
the hash of its content, since Compose needs real files: a newer stack gets
a folder of its own, and one in use is never changed.

``python -m hardcode_cli.stack DESTINATION`` copies a checkout's stack to
DESTINATION; the builds of the package and of the binaries use it.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Optional

from . import home

# The stack's files and folders, relative to the checkout (and to a copy).
# compose.image.yaml is merged over compose.yaml for a profile with
# Dockerfile steps of its own (see ``dockerfile``).
PARTS = (
    "compose.yaml",
    "compose.image.yaml",
    "images/squid",
    "images/gateway",
    "images/docker",
    "images/vscode",
    "template",
)

# The file of PARTS a profile's own Dockerfile steps are built with.
IMAGE_STEPS = "compose.image.yaml"

# The copy that ships inside the package.
BUNDLED = Path(__file__).resolve().parent / "stack"

# This package's checkout, when it is run from one: cli/hardcode_cli/../..
CHECKOUT = Path(__file__).resolve().parent.parent.parent

# A default in compose.yaml: ${NAME:-value}.
_DEFAULT = re.compile(r"\$\{([A-Z][A-Z0-9_]*):-([^}]*)\}")

# The stack this process uses, once found (``use`` sets it for tests).
_current: Optional[Path] = None


class StackError(Exception):
    """
    The stack can't be found or written out.
    """


def _complete(directory: Path) -> bool:
    """
    Whether *directory* has every part of a stack.
    """
    return all((directory / part).exists() for part in PARTS)


def _files(directory: Path, parts: Iterable[str]) -> list[Path]:
    """
    Every file of *parts* under *directory*, in a fixed order.
    """
    found: list[Path] = []

    for part in parts:
        path = directory / part
        if path.is_file():
            found.append(path)
        elif path.is_dir():
            found.extend(sorted(item for item in path.rglob("*") if item.is_file() and "__pycache__" not in item.parts))

    return found


def digest(directory: Path, parts: Iterable[str] = PARTS) -> str:
    """
    A short hash of *parts* under *directory*: their paths and their bytes.
    """
    hasher = hashlib.sha256()

    for path in _files(directory, parts):
        hasher.update(path.relative_to(directory).as_posix().encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")

    return hasher.hexdigest()[:12]


def stage(source: Path, destination: Path) -> None:
    """
    Copy the stack's parts from *source* to *destination*, replacing what
    was there.

    The copy is readable by everyone (folders 0755, files 0644), whatever
    the source's modes: a binary's bundled files are unpacked readable by
    their owner only, and an image built from them would have files its
    own users can't read. The Dockerfiles say which files are scripts.
    """
    if not _complete(source):
        raise StackError(f"{source} has no complete stack ({', '.join(PARTS)})")

    for part in PARTS:
        target = destination / part
        if target.is_dir():
            shutil.rmtree(target)

        target.parent.mkdir(parents=True, exist_ok=True)
        if (source / part).is_dir():
            shutil.copytree(source / part, target, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source / part, target)

    for path in [
        destination,
        *(item for part in PARTS for item in [destination / part, *(destination / part).rglob("*")]),
    ]:
        path.chmod(0o755 if path.is_dir() else 0o644)


def _written_out(bundle: Path, cache: Path) -> Path:
    """
    The cache's copy of the bundled stack, written the first time this
    content is used. It is written beside its place and then moved there, so
    a command never finds half of it.
    """
    destination = cache / "stack" / digest(bundle)
    if _complete(destination):
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="writing-", dir=destination.parent))

    try:
        stage(bundle, scratch)
        scratch.replace(destination)
    except OSError as error:
        shutil.rmtree(scratch, ignore_errors=True)

        # Another command wrote the same content at the same moment.
        if _complete(destination):
            return destination
        raise StackError(f"couldn't write the stack to {destination}: {error}") from error

    return destination


def locate(cache: Optional[Path] = None) -> Path:
    """
    The stack to run with: the checkout's, when this package runs from one;
    else the bundled one, written out to *cache* (the platform's cache
    folder by default).
    """
    if _complete(CHECKOUT):
        return CHECKOUT

    if _complete(BUNDLED):
        return _written_out(BUNDLED, cache or home.cache_dir())

    raise StackError("this hardcode has no stack (compose.yaml, images/, template/): reinstall it")


def use(directory: Optional[Path]) -> None:
    """
    Run with the stack in *directory* from now on; ``None`` finds it again.
    """
    global _current
    _current = directory


def current() -> Path:
    """
    The stack this process runs with, found the first time it is asked for.
    """
    global _current
    if _current is None:
        _current = locate()

    return _current


def template_dir() -> Path:
    """
    What a new profile's ``allowlist.txt`` and ``compose.override.yaml`` start from.
    """
    return current() / "template"


def defaults() -> dict[str, str]:
    """
    compose.yaml's defaults (``${NAME:-value}``): what a setting is when a
    profile's ``.env`` leaves it out.
    """
    text = (current() / "compose.yaml").read_text(encoding="utf-8")
    return {name: value for name, value in _DEFAULT.findall(text)}


def image_tag(image: str) -> str:
    """
    The tag of one of the images the stack builds that every profile shares
    (``squid``, ``gateway``, ``docker``): the hash of its build context, so that a stack
    with a changed image builds it again instead of running the old one.
    """
    return digest(current(), [f"images/{image}"])


def main(argv: list[str]) -> int:
    """
    ``python -m hardcode_cli.stack DESTINATION``: copy this checkout's stack.
    """
    if len(argv) != 1:
        print("usage: python -m hardcode_cli.stack DESTINATION", file=sys.stderr)
        return 2

    stage(CHECKOUT, Path(argv[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
