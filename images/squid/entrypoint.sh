#!/bin/bash
# Starts squid, once the files it needs are there: the proxy CA (from the
# one-shot "pki" service) and the rendered policy (from the one-shot
# "allowlist" service). Without either, it refuses to start rather than run
# with no policy or an old one. It writes the networks squid is attached to,
# which no request may reach, and takes its own copy of the policy, with
# squid's hosts file beside it: the names the policy pins to an address on
# your network.
#
# Then it gives squid each new policy the renderer publishes (Compose's
# allowlist-watch), in place: it takes a copy, checks it with squid's own
# parser (squid-check-policy), and only then puts it where squid reads it
# and tells squid to reload (SIGHUP), without a restart and without
# dropping a connection. A policy squid would refuse is left out, and said
# so: squid keeps the one it has, and its healthcheck fails until a policy
# it takes is published.
#
# It runs as the container's first process, squid beside it, and ends with
# squid: Docker's TERM is passed on to squid.
set -euo pipefail

generated=/etc/squid/generated
spool=/var/spool/squid
policy="$spool/policy"
next="$spool/next"

# A policy squid would refuse, left out: the healthcheck fails while it is
# there.
refused_mark="$spool/refused"

# Squid's files, as the renderer publishes them, with their manifest.
POLICY_FILES=(
  hosts.txt
  urls-ro.regex
  urls-rw.regex
  ssh.conf
  pins.conf
  pins.hosts
  sandbox.txt
  clients.txt
  internal.txt
)

for file in /proxy-ca/ca.pem /proxy-ca/ca.key; do
  if [ ! -r "$file" ]; then
    echo "squid: $file missing or unreadable; did the pki service run?" >&2
    exit 1
  fi
done

# take_policy <folder>: copy the published policy into the folder, and
# write squid's hosts file beside it: the names pinned to an address on
# your network, each with its one address, then the container's own
# /etc/hosts without any line naming a pinned name, so that nothing gives a
# pinned name another address. Fails (2) with a policy caught halfway
# through publishing, whose files don't match its manifest; (1) with none.
take_policy() {
  local into="$1"
  local file

  rm -rf "$into"
  mkdir "$into"

  for file in "${POLICY_FILES[@]}" manifest; do
    if [ ! -s "$generated/$file" ]; then
      echo "squid: $generated/$file missing; did the allowlist service run?" >&2
      return 1
    fi

    cp "$generated/$file" "$into/$file"
  done

  if ! (cd "$into" && sha256sum --check --status manifest); then
    return 2
  fi

  awk '
    FNR == NR {
      if ($1 !~ /^#/ && NF >= 2) pinned[$2] = 1
      print
      next
    }
    {
      for (field = 2; field <= NF; field++) if ($field in pinned) next
      print
    }
  ' "$into/pins.hosts" /etc/hosts > "$into/hosts"
}

# The policy squid starts with. One being published this moment is taken a
# moment later.
attempt=0
until take_policy "$policy"; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 10 ]; then
    echo "squid: the policy in $generated doesn't match its manifest; did the allowlist service finish?" >&2
    exit 1
  fi

  sleep 1
done

# The networks squid is attached to (squid.conf's own_networks): each
# interface's subnet and addresses, and each route's gateway, the host or
# the node behind them. No request may reach them, and no pin exempts them.
# They are found again at every start, since only squid can see them; with
# none found, squid doesn't start.
squid-own-networks > "$spool/own-networks.txt"
if [ ! -s "$spool/own-networks.txt" ]; then
  echo "squid: found none of the networks it is attached to in /proc/net" >&2
  exit 1
fi

# The cache of per-host certificates squid makes while inspecting TLS.
certificates="$spool/ssl_db"
if [ ! -d "$certificates" ]; then
  /usr/lib/squid/security_file_certgen -c -s "$certificates" -M 16MB
fi

rm -f "$refused_mark"

squid -N -f /etc/squid/squid.conf &
squid_pid="$!"

# Docker stops the container with TERM: squid is told, and ends by itself.
trap 'kill -TERM "$squid_pid" 2>/dev/null || true' TERM INT

taken="$(cat "$policy/manifest")"
refused=""

# Each policy published after the one squid has: taken whole, checked, put
# in place and loaded. While squid runs; its exit status is the container's.
while kill -0 "$squid_pid" 2>/dev/null; do
  # In the background, so that TERM is handled at once rather than after it.
  sleep 1 &
  wait "$!" || true

  published="$(cat "$generated/manifest" 2>/dev/null || true)"
  if [ -z "$published" ] || [ "$published" = "$taken" ] || [ "$published" = "$refused" ]; then
    continue
  fi

  # Caught halfway through publishing: the next look takes it whole.
  if ! take_policy "$next"; then
    continue
  fi

  if ! complaint="$(squid-check-policy "$next" "$spool" /proxy-ca 2>&1)"; then
    echo "squid: kept the policy it has, as it refuses the new one: $complaint" >&2
    refused="$published"
    touch "$refused_mark"
    continue
  fi

  # Squid reads its files only as it reloads, so the swap is safe; the old
  # policy goes once the new one is in place.
  rm -rf "$spool/previous"
  mv "$policy" "$spool/previous"
  mv "$next" "$policy"
  rm -rf "$spool/previous"

  kill -HUP "$squid_pid"
  taken="$published"
  refused=""
  rm -f "$refused_mark"
  echo "squid: reloading with the allowlist's new policy"
done

wait "$squid_pid"
