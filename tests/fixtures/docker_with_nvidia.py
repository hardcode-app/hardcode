#!/usr/bin/env python3
"""
A stand-in for the ``docker`` command whose Docker has NVIDIA's runtime,
for tests/cli.py's check of the nvidia device: ``docker info`` lists it
beside runc, and every other command is the real docker's
($REAL_DOCKER), so ``docker compose config`` resolves the profile's files
as it does anywhere. Nothing needs an NVIDIA GPU.
"""

import json
import os
import sys

# What `docker info --format '{{json .Runtimes}}'` prints for a Docker that
# NVIDIA's container toolkit has been set up in.
RUNTIMES = {
    "io.containerd.runc.v2": {"path": "runc"},
    "nvidia": {"path": "nvidia-container-runtime"},
    "runc": {"path": "runc"},
}


def main(arguments: list[str]) -> int:
    """
    Answer ``docker info``; hand anything else to the real docker, in
    place of this process.
    """
    if arguments[:1] == ["info"]:
        print(json.dumps(RUNTIMES))
        return 0

    real = os.environ["REAL_DOCKER"]
    os.execv(real, [real, *arguments])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
