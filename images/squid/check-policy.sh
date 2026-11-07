#!/bin/bash
# Checks a rendered policy with squid's own parser, before squid is given it:
# squid.conf as the image has it, pointed at the policy's files, parsed with
# `squid -k parse`. A policy squid would refuse (a rule it can't compile, a
# file it can't read) is refused here, rather than by a running squid,
# which stops on a configuration it can't load.
#
#   squid-check-policy POLICY SPOOL CA
#
#   POLICY  the policy's files (hosts.txt ... internal.txt, and the hosts
#           file squid reads, hosts), where squid.conf has
#           /var/spool/squid/policy/
#   SPOOL   a folder to write in, where squid.conf has /var/spool/squid/:
#           the networks squid is attached to (own-networks.txt) are read
#           from it, and the configuration checked is written to it
#   CA      the CA squid signs with (ca.pem, ca.key), where squid.conf has
#           /proxy-ca/: squid won't parse its configuration without one
#
# The renderer runs it on each policy it renders, with a CA of its own
# making and stand-ins for what only squid can see; squid's entrypoint, on
# each new policy before reloading squid with it, with its own. When squid
# refuses the policy, its complaint is printed and the check fails.
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: squid-check-policy POLICY SPOOL CA" >&2
  exit 2
fi

policy="$1"
spool="$2"
ca="$3"

# The paths go into a sed replacement: plain absolute paths only.
for path in "$policy" "$spool" "$ca"; do
  if ! [[ "$path" =~ ^/[A-Za-z0-9/._-]+$ ]]; then
    echo "squid-check-policy: '$path' is not a plain absolute path" >&2
    exit 2
  fi
done

# squid.conf with every path it reads moved: the policy's files first, then
# squid's own, then the CA. Each goes through a placeholder, so that a path
# moved once isn't moved again by the next rule.
conf="$spool/check.conf"
sed \
  -e 's#/var/spool/squid/policy/#@POLICY@/#g' \
  -e 's#/var/spool/squid/#@SPOOL@/#g' \
  -e 's#/proxy-ca/#@CA@/#g' \
  -e "s#@POLICY@#$policy#g" \
  -e "s#@SPOOL@#$spool#g" \
  -e "s#@CA@#$ca#g" \
  /etc/squid/squid.conf > "$conf"

if output="$(squid -k parse -f "$conf" 2>&1)"; then
  exit 0
fi

# Squid's complaint: its ERROR and FATAL lines without their timestamps,
# each with the indented lines that follow it, every line once.
awk '
  / (ERROR|FATAL): / {
    sub(/^.*\| /, "")
    print
    keep = 1
    next
  }
  keep && /^    / {
    print
    next
  }
  {
    keep = 0
  }
' <<<"$output" \
  | awk '!seen[$0]++' \
  | head -n 12 >&2

exit 1
