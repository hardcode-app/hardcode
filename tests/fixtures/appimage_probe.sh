#!/usr/bin/env bash
# The program of the smallest AppImage, for tests/app.py: it copies this in
# as the AppImage's AppRun, builds the AppImage on the build image's runtime,
# and runs it (APPIMAGE_EXTRACT_AND_RUN=1, as a container has no FUSE).
#
#   AppRun   says it ran
set -euo pipefail

echo "the probe ran"
