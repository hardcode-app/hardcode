#!/bin/bash
# Starts squid, once the files it needs are there: the proxy CA (from the
# one-shot "pki" service) and the rendered policy (from the one-shot
# "allowlist" service). Without either, it refuses to start rather than run
# with no policy or an old one. It writes squid's hosts file first, with the
# names the policy pins to an address on your network, and the networks
# squid is attached to, which no request may reach.
set -euo pipefail

for file in /proxy-ca/ca.pem /proxy-ca/ca.key; do
  if [ ! -r "$file" ]; then
    echo "squid: $file missing or unreadable; did the pki service run?" >&2
    exit 1
  fi
done

for file in hosts.txt urls-ro.regex urls-rw.regex ssh.conf pins.conf pins.hosts sandbox.txt clients.txt internal.txt; do
  if [ ! -s "/etc/squid/generated/$file" ]; then
    echo "squid: /etc/squid/generated/$file missing; did the allowlist service run?" >&2
    exit 1
  fi
done

# Squid's hosts file: the names pinned to an address on your network, each
# with its one address, then the container's own /etc/hosts without any line
# naming a pinned name, so that nothing gives a pinned name another address.
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
' /etc/squid/generated/pins.hosts /etc/hosts > /var/spool/squid/hosts

# The networks squid is attached to (squid.conf's own_networks): each
# interface's subnet and addresses, and each route's gateway, the host or
# the node behind them. No request may reach them, and no pin exempts them.
# They are found again at every start, since only squid can see them; with
# none found, squid doesn't start.
squid-own-networks > /var/spool/squid/own-networks.txt
if [ ! -s /var/spool/squid/own-networks.txt ]; then
  echo "squid: found none of the networks it is attached to in /proc/net" >&2
  exit 1
fi

# The cache of per-host certificates squid makes while inspecting TLS.
certificates=/var/spool/squid/ssl_db
if [ ! -d "$certificates" ]; then
  /usr/lib/squid/security_file_certgen -c -s "$certificates" -M 16MB
fi

exec squid -N -f /etc/squid/squid.conf
