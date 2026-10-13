"""
A profile's ``.env``: ``KEY=VALUE`` lines, kept in order with their comments.

It holds only what differs from the defaults, which are compose.yaml's (see
``stack.defaults``), followed by a commented reference of every setting
(``template/.env``). A setting written here goes above that reference, with
the others the profile sets.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import stack

# The line the commented reference of every setting starts with.
REFERENCE = "# --- Every setting, with its default"


def read(path: Path) -> dict[str, str]:
    """
    Every ``KEY=VALUE`` pair in the file, as Compose reads it: comments and
    blank lines are skipped, an ``export`` in front of a key is ignored, and
    when a key appears twice the last one counts. A file that doesn't exist
    reads as empty.
    """
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, _, value = line.partition("=")
        values[_key(key)] = _value(value)

    return values


def _key(text: str) -> str:
    """
    The key of a line, without the ``export`` a shell-style file may put in front.
    """
    key = text.strip()
    if key.startswith("export "):
        key = key[len("export ") :].strip()

    return key


def _value(text: str) -> str:
    """
    The value of a line, as Compose reads it: what is inside matching quotes,
    or, unquoted, everything up to an inline comment (a ``#`` after a space).
    """
    value = text.strip()

    if value[:1] in ("'", '"'):
        closing = value.find(value[0], 1)
        if closing != -1:
            return value[1:closing]

    # "8001  # moved" is 8001; "pa#ss" keeps its "#".
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def get(path: Path, key: str) -> str:
    """
    One value: the file's, else compose.yaml's default, else ``""``.
    """
    return read(path).get(key) or stack.defaults().get(key, "")


def _sets(line: str, key: str) -> bool:
    """
    Whether *line* sets *key* (a commented-out one doesn't).
    """
    stripped = line.strip()
    return not stripped.startswith("#") and "=" in stripped and _key(stripped.partition("=")[0]) == key


def _insert_at(lines: list[str]) -> int:
    """
    Where a new setting goes: after the last line that sets one above the
    reference, or above the reference (and the blank line before it), or at
    the end of the file.
    """
    end = next((number for number, line in enumerate(lines) if line.startswith(REFERENCE)), len(lines))
    settings = [number for number in range(end) if "=" in lines[number] and not lines[number].strip().startswith("#")]

    if settings:
        return settings[-1] + 1

    while end > 0 and not lines[end - 1].strip():
        end -= 1
    return end


def set_value(path: Path, key: str, value: str) -> None:
    """
    Set ``KEY=VALUE``, replacing the existing line in place or appending one.

    Every line that sets the key is replaced: Compose reads the last one, so
    changing only the first would change nothing.
    """
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.split("\n")

    # Keep the file ending in a newline: the empty string after it goes.
    if lines and lines[-1] == "":
        lines.pop()

    found = False
    for index, line in enumerate(lines):
        if _sets(line, key):
            lines[index] = f"{key}={value}"
            found = True

    if not found:
        lines.insert(_insert_at(lines), f"{key}={value}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def unset(path: Path, key: str) -> None:
    """
    Remove every line that sets *key*, so compose.yaml's default applies again.
    """
    if not path.exists():
        return

    lines = path.read_text(encoding="utf-8").split("\n")
    kept = [line for line in lines if not _sets(line, key)]

    if kept != lines:
        path.write_text("\n".join(kept), encoding="utf-8")
