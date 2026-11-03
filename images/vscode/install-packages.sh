#!/usr/bin/env bash
# hardcode-install-packages NAME...: install Ubuntu packages from the
# archive, as root through sudo, with the proxy settings the entrypoint
# gave apt. The VS Code container's entrypoint runs it on Kubernetes, where
# the image is shared, for the profile's own packages (HARDCODE_PACKAGES);
# on Compose the profile's image has them already.
#
# Every name is checked with Debian's rule before apt runs, so nothing but
# package names reaches it. A name the archive doesn't have fails, with
# apt's own message.
set -euo pipefail

# Debian's rule: lower-case letters, digits, +, - and ., at least two
# characters, starting with a letter or a digit.
PACKAGE_NAME='^[a-z0-9][a-z0-9+.-]+$'

if [ "$#" -eq 0 ]; then
  echo "usage: hardcode-install-packages NAME..." >&2
  exit 2
fi

for package in "$@"; do
  if ! [[ "$package" =~ $PACKAGE_NAME ]]; then
    echo "not an Ubuntu package name: $package" >&2
    exit 2
  fi
done

# sudo doesn't pass our environment on; env sets what apt needs for root.
sudo env DEBIAN_FRONTEND=noninteractive apt-get update -q
sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "$@"
