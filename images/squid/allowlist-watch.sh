#!/bin/bash
# Gives the running proxy each change of the profile's allowlist, in
# Compose: it runs as the "allowlist-watch" service beside squid and CoreDNS,
# with the profile's proxy folder (STATUS_DIR) and their policy volume, and
# no network.
#
#   allowlist-watch
#
# Every second it looks at the allowlist the hardcode command writes. Once
# the allowlist has gone QUIET_SECONDS without a change, and is neither the
# one published nor one refused, allowlist-render renders it again, checks
# it with squid's own parser and publishes it, or refuses it and says why
# in the proxy folder. Squid takes each policy published within a second or
# so, and reloads with it in place; CoreDNS within two. A burst of changes
# (a script making a hundred edits) is one reload, after the last.
#
# On Kubernetes, the operator restarts squid and CoreDNS with each change
# instead; this doesn't run there.
set -euo pipefail

input=/config/allowlist.txt
published=/out/allowlist.sha256
failed="${STATUS_DIR:?the proxy folder, where the command reads how the proxy stands}/failed"
quiet="${QUIET_SECONDS:-5}"

# sha256_of <file>: the file's sha256; nothing when it can't be read.
sha256_of() {
  sha256sum < "$1" 2>/dev/null | cut -d ' ' -f 1
}

# The allowlist's last sha256 seen, and since when (bash's SECONDS) it has
# been that.
seen=""
since="$SECONDS"

# Docker stops the service with TERM, which bash, as the container's first
# process, would otherwise ignore.
trap 'exit 0' TERM INT

echo "allowlist-watch: the proxy is given each change of the allowlist, ${quiet} seconds after the last"

while true; do
  current="$(sha256_of "$input" || true)"
  if [ "$current" != "$seen" ]; then
    seen="$current"
    since="$SECONDS"
  fi

  quiet_for=$((SECONDS - since))
  given="$(cat "$published" 2>/dev/null || true)"
  refused="$(head -n 1 "$failed" 2>/dev/null || true)"

  if [ -n "$current" ] && [ "$quiet_for" -ge "$quiet" ] && [ "$current" != "$given" ] && [ "$current" != "$refused" ]; then
    # It says what it published, or what it refused and why (and keeps
    # that for the command): either way, the watch goes on.
    allowlist-render || true
  fi

  # In the background, so that TERM is handled at once rather than after it.
  sleep 1 &
  wait "$!" || true
done
