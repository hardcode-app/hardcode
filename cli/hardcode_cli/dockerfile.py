"""
A profile's own Dockerfile steps, built on HardCode's VS Code image.

They are ``image/Dockerfile`` in the profile's folder, with the files it
copies beside it: that folder is the build context. The file starts ``FROM
hardcode``, and the build supplies HardCode's VS Code image (the one built
with the profile's languages, tools and packages) under that name: the
stack's ``compose.image.yaml``, which ``hardcode`` merges over compose.yaml
when the profile has the file. Without it nothing changes.

HardCode's own Dockerfile is never copied here: a profile stores only what
differs. A hash of the folder goes into the image's labels, so that a
changed file means a rebuild is due.

Only on Compose: a cluster doesn't build images (a Profile names an image
of its own instead).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# The folder in a profile, and the file in it.
FOLDER = "image"
FILE = "Dockerfile"

# The name the steps build on: FROM hardcode.
BASE = "hardcode"

# How much of a file is read at a time for the hash.
CHUNK = 1 << 20

# What a new profile's file starts as, and what the app's Image page offers
# a profile that has none: it builds nothing of its own until it is edited.
STARTER = """\
# This profile's own steps for its VS Code image, built on top of HardCode's
# ("hardcode": the image HardCode builds with the profile's languages, tools
# and Ubuntu packages). hardcode rebuild, or Save & rebuild on the app's
# Image page, builds them; without this file the image is HardCode's alone.
#
# They run on your computer's Docker, with your computer's network: not in
# the sandbox, and not through its allowlist. The files you COPY go beside
# this one: this folder is what the build sees.
FROM hardcode

# The steps below run as root. apt-install NAME... installs packages from
# Ubuntu's archive (for packages alone the profile's list is simpler:
# hardcode package add NAME).
USER root

#RUN apt-install postgresql-client
#COPY my-script.sh /usr/local/bin/my-script

# Last, back to the VS Code container's user: dev, unless the profile's .env
# sets another DEV_USER.
USER dev
"""


class DockerfileError(ValueError):
    """
    Dockerfile steps that can't be saved.
    """


def digest(folder: Path) -> str:
    """
    A short hash of the folder of a profile's own steps: the path of every
    file in it, and its bytes. ``""`` when there is no Dockerfile in it,
    which is no steps at all.
    """
    if not (folder / FILE).is_file():
        return ""

    hasher = hashlib.sha256()
    files = sorted(path for path in folder.rglob("*") if path.is_file())

    for path in files:
        hasher.update(path.relative_to(folder).as_posix().encode("utf-8"))
        hasher.update(b"\0")

        # Read a piece at a time: a file the steps COPY may be large.
        with open(path, "rb") as handle:
            chunk = handle.read(CHUNK)
            while chunk:
                hasher.update(chunk)
                chunk = handle.read(CHUNK)

        hasher.update(b"\0")

    return hasher.hexdigest()[:12]


def read(folder: Path) -> str:
    """
    The text of the Dockerfile in *folder*; ``""`` when there is none.
    """
    path = folder / FILE
    if not path.is_file():
        return ""

    return path.read_text(encoding="utf-8")


def save(folder: Path, text: str) -> Path:
    """
    Write *text* as the Dockerfile in *folder*, making the folder when there
    is none, and return the file's path. Its lines end as *text*'s do, and
    the file ends with a newline.

    Empty text is refused: a Dockerfile without a FROM can't be built, and
    no steps at all is no file.
    """
    if not text.strip():
        raise DockerfileError(
            f"an empty Dockerfile can't be built; to have no steps of your own, delete {folder / FILE}"
        )

    folder.mkdir(parents=True, exist_ok=True)
    path = folder / FILE

    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text if text.endswith("\n") else text + "\n")

    return path
