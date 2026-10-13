#!/usr/bin/env bash
# Runs a command in the build image (images/build/Dockerfile), the Ubuntu
# 26.04 environment everything here is built and tested in.
#
#   images/build/run.sh tests/run_all.py       every suite
#   images/build/run.sh app/build.sh bundle    the app's installers
#   images/build/run.sh                        a shell
#
# How the container is set up:
#
# - The checkout and a scratch directory are mounted at their own paths, so a
#   path a suite hands to Docker from inside resolves on the host as well.
# - The Docker socket is mounted: the CLI, proxy, image and kind suites drive
#   the host's Docker.
# - The container shares the host's network: kind's API port and the suites'
#   port-forwards are on the loopback.
# - It is memory-capped, like everything that runs here, and writes no core
#   dumps: a crashing process (WebKit's, under the app's headless launches)
#   would leave one the size of its memory in the checkout.
#
# Inside the image already, the command just runs.
#
# Settings:
#
#   BUILD_IMAGE            the image (hardcode-build:local, built when missing)
#   BUILD_IMAGE_REBUILD=1  build it again first, after editing the Dockerfile
#   BUILD_MEMORY           the memory cap (6g)
#   BUILD_CPUS             the CPU cap (4, or what the Docker daemon has
#                          if that is less: a CI runner may have 2)
#   HARDCODE_BUILD_CACHE   cargo's registry, helm's and Tauri's caches
#                          (~/.cache/hardcode-build)
#
# E2E, KEEP_CLUSTER, CLUSTER, KIND_NODE_MEMORY, SQUID_IMAGE and
# the variables named in BUILD_ENV are passed through.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

if [ -n "${HARDCODE_BUILD_IMAGE:-}" ]; then
  [ "$#" -gt 0 ] || set -- bash
  exec "$@"
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is needed: everything builds and tests in the build image" >&2
  exit 1
fi

image="${BUILD_IMAGE:-hardcode-build:local}"
if [ "${BUILD_IMAGE_REBUILD:-0}" = 1 ] || ! docker image inspect "$image" >/dev/null 2>&1; then
  echo "== building $image (images/build)" >&2
  docker build -t "$image" "$ROOT/images/build" >&2
fi

# The daemon's socket: DOCKER_HOST's, or the current Docker context's.
socket="$(docker context inspect --format '{{(index .Endpoints "docker").Host}}' 2>/dev/null || true)"
socket="${DOCKER_HOST:-$socket}"
socket="${socket#unix://}"
if [ ! -S "$socket" ]; then
  echo "no Docker socket at '$socket' (DOCKER_HOST=unix://... for a rootless daemon)" >&2
  exit 1
fi

cache="${HARDCODE_BUILD_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/hardcode-build}"
# The container's TMPDIR, at the same path on both sides.
scratch="${TMPDIR:-/tmp}/hardcode-build-$(id -u)"
mkdir -p "$cache" "$scratch"

# Start where the caller is, when that is inside the checkout.
case "$PWD/" in
  "$ROOT"/*) workdir="$PWD" ;;
  *) workdir="$ROOT" ;;
esac

# The CPU cap, never above what the daemon has: Docker refuses a larger one.
cpus="${BUILD_CPUS:-4}"
available="$(docker info --format '{{.NCPU}}' 2>/dev/null || true)"
cpus="$(awk -v want="$cpus" -v have="$available" 'BEGIN { print (have != "" && have + 0 < want + 0) ? have : want }')"

args=(
  --rm --init -i
  --network host
  --memory "${BUILD_MEMORY:-6g}"
  --cpus "$cpus"
  --ulimit core=0
  # The entrypoint runs the command as the caller, in the socket's group.
  -e BUILD_UID="$(id -u)"
  -e BUILD_GID="$(id -g)"
  -e BUILD_GROUPS="$(stat -c %g "$socket")"
  -e BUILD_IMAGE="$image"
  -v "$ROOT:$ROOT"
  -v "$cache:/cache"
  -v "$scratch:$scratch"
  -e TMPDIR="$scratch"
  -v "$socket:/var/run/docker.sock"
  -e DOCKER_HOST=unix:///var/run/docker.sock
  -w "$workdir"
)
[ -t 0 ] && args+=(-t)

# By name only: Docker reads the value from its environment, so a secret
# (the release's signing key) isn't on a command line.
for variable in E2E KEEP_CLUSTER CLUSTER KIND_NODE_MEMORY SQUID_IMAGE ${BUILD_ENV:-}; do
  [ -z "${!variable:-}" ] || args+=(-e "$variable")
done

[ "$#" -gt 0 ] || set -- bash
exec docker run "${args[@]}" "$image" "$@"
