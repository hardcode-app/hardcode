#!/bin/bash
# Starts squid, once the files it needs are there: the proxy CA (from the
# one-shot "pki" service) and the rendered policy (from the one-shot
# "allowlist" service). Without either, it refuses to start rather than run
# with no policy or an old one.
set -euo pipefail

for file in /proxy-ca/ca.pem /proxy-ca/ca.key; do
  if [ ! -r "$file" ]; then
    echo "squid: $file missing or unreadable; did the pki service run?" >&2
    exit 1
  fi
done

for file in hosts.txt urls-ro.regex urls-rw.regex ssh.conf clients.txt internal.txt; do
  if [ ! -s "/etc/squid/generated/$file" ]; then
    echo "squid: /etc/squid/generated/$file missing; did the allowlist service run?" >&2
    exit 1
  fi
done

# The cache of per-host certificates squid makes while inspecting TLS.
certificates=/var/spool/squid/ssl_db
if [ ! -d "$certificates" ]; then
  /usr/lib/squid/security_file_certgen -c -s "$certificates" -M 16MB
fi

exec squid -N -f /etc/squid/squid.conf
