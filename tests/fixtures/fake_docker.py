#!/usr/bin/env python3
"""
A stand-in for the ``docker`` command, for tests/images.py's check of
images/build/run.sh: it answers what run.sh asks before it starts the build
image, and prints what it would have started.

- ``docker image inspect``: the image is there.
- ``docker info``: the daemon has $FAKE_DOCKER_CPUS CPUs; with that unset,
  it fails, as when the daemon can't say.
- ``docker context inspect``: no context, so run.sh goes by $DOCKER_HOST.
- ``docker run``: its arguments, one per line, and nothing is run.
"""

import os
import sys


def main(arguments: list[str]) -> int:
    """
    Answer one docker command.
    """
    if arguments[:2] == ["image", "inspect"]:
        return 0

    if arguments[:1] == ["info"]:
        cpus = os.environ.get("FAKE_DOCKER_CPUS")
        if not cpus:
            print("Cannot connect to the Docker daemon", file=sys.stderr)
            return 1
        print(cpus)
        return 0

    if arguments[:1] == ["run"]:
        print("\n".join(arguments))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
