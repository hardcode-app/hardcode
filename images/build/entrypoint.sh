#!/bin/bash
# The build image's entrypoint: runs the command as the user who invoked
# run.sh (BUILD_UID and BUILD_GID), so what it writes into the checkout and
# the caches belongs to them. That user gets a passwd entry (ssh and git
# want one) and the Docker socket's group (BUILD_GROUPS).
#
# Started without BUILD_UID, as the suites' helper containers are, the
# command runs as root.
set -euo pipefail

uid="${BUILD_UID:-0}"
gid="${BUILD_GID:-0}"
groups="${BUILD_GROUPS:-$gid}"

if [ "$uid" = 0 ]; then
  exec "$@"
fi

getent group "$gid" >/dev/null || groupadd -g "$gid" build
# The image has a "docker" group of its own, with another id.
getent group "$groups" >/dev/null || groupadd -g "$groups" hostdocker
getent passwd "$uid" >/dev/null || useradd -u "$uid" -g "$gid" -d /tmp -M -s /bin/bash build

exec setpriv --reuid="$uid" --regid="$gid" --groups="$groups" -- "$@"
