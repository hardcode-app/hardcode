#!/bin/bash
# Renders a profile's allowlist.txt into the files CoreDNS and Squid read.
# It runs as the one-shot "allowlist" service, before either of them starts
# (on Kubernetes, as their init container).
#
# The entry format, one per line ("#" starts a comment):
#
#   host                  that host, any path
#   .host                 that host and every subdomain, any path
#   host/path/prefix      only URLs under that prefix (/prefix and /prefix/...)
#   <entry> rw            uploads too (POST/PUT/PATCH/DELETE, git push)
#   host[:port] ssh       an SSH tunnel to host (port 22 by default); SSH can't
#                         be inspected, so this is whole-host and read-write
#
# Without "rw" an entry is read-only: GET, HEAD, OPTIONS, and the POST git
# uses for clone, fetch and pull. Hosts are names, never addresses. An empty
# list renders a policy that allows nothing.
#
# The same grammar lives in cli/hardcode_cli/allowlist.py; both are run over
# tests/allowlist-corpus.txt.
#
# Output:
#
#   /out/Corefile             CoreDNS: allowlisted hosts resolve, the rest NXDOMAIN
#   /out/squid/hosts.txt      dstdomain list, for CONNECT and the TLS server name
#   /out/squid/urls-ro.regex  url_regex list for read-only entries
#   /out/squid/urls-rw.regex  url_regex list for rw entries
#   /out/squid/ssh.conf       squid rules for the ssh entries (included by squid.conf)
#   /out/squid/clients.txt    who may use the proxy (CLIENT_RANGES)
#   /out/squid/internal.txt   where an allowlisted name must never point (INTERNAL_RANGES)
set -euo pipefail

input=/config/allowlist.txt
out=/out
upstream="${UPSTREAM_DNS:-/etc/resolv.conf}"

# Space-separated extra zones to resolve upstream unchecked, e.g. the squid
# service's own name in the cluster on Kubernetes.
extra_zones="${EXTRA_ZONES:-}"

# The sandbox's address ranges (the proxy's clients), and the ranges an
# allowlisted name must never resolve to: loopback, link-local (cloud
# metadata), private and shared address space. Both can be set per profile,
# e.g. for a registry on the LAN.
client_ranges="${CLIENT_RANGES:-10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 fc00::/7 fe80::/10}"
internal_ranges="${INTERNAL_RANGES:-0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.168.0.0/16 ::1 fc00::/7 fe80::/10}"

die() {
  echo "allowlist: $*" >&2
  exit 1
}

# One DNS label: letters, digits and inner hyphens.
label='[a-z0-9]([a-z0-9-]*[a-z0-9])?'

# A name, not an address: at least two labels (a bare TLD would open a whole
# TLD), and a last label that isn't all digits.
valid_host() {
  [[ "$1" =~ ^${label}(\.${label})+$ ]] && ! [[ "${1##*.}" =~ ^[0-9]+$ ]]
}

# What the entries add up to, filled in as the file is read.
hosts=()      # exact hosts
wildcards=()  # .host entries, without the dot
ro=()         # URL regexes for read-only entries
rw=()         # URL regexes for rw entries
ssh_hosts=()
ssh_rules=()  # squid.conf lines for the ssh entries

# ---------------------------------------------------------------------------
# Reading the entries
# ---------------------------------------------------------------------------

# add_ssh_entry <line number> <entry>: an SSH tunnel to host[:port].
add_ssh_entry() {
  local number="$1" entry="$2"
  local host="${entry%%:*}" port=22
  [[ "$entry" == *:* ]] && port="${entry#*:}"

  [[ "$host" != .* && "$host" != */* ]] \
    || die "line $number: '$entry': ssh entries are host[:port], no wildcard or path"
  valid_host "$host" \
    || die "line $number: '$entry' is not a valid hostname"
  [[ "$port" =~ ^[0-9]+$ ]] && [ "$port" -ge 1 ] && [ "$port" -le 65535 ] \
    || die "line $number: '$entry': bad port"

  local n=$(( ${#ssh_hosts[@]} + 1 ))
  ssh_hosts+=("$host")

  # A tunnel to exactly this host and port, never to an internal address,
  # passed through without inspection (it isn't TLS).
  ssh_rules+=(
    "# $host:$port"
    "acl ssh_${n} dstdomain -n ${host}"
    "acl ssh_${n}_port port ${port}"
    "http_access allow CONNECT ssh_${n} ssh_${n}_port !to_internal"
    "on_unsupported_protocol tunnel ssh_${n} ssh_${n}_port"
  )
}

# add_http_entry <line number> <entry> <mode>: a host, .host or host/path,
# read-only (mode ro) or read-write (mode rw).
add_http_entry() {
  local number="$1" entry="$2" mode="$3"

  [[ "$entry" != *:* ]] || die "line $number: '$entry': only ssh entries take a port"

  local host="${entry%%/*}" path=""
  [[ "$entry" == */* ]] && path="/${entry#*/}"

  local wildcard=0
  if [[ "$host" == .* ]]; then
    wildcard=1
    host="${host#.}"
  fi

  # One trailing dot is the fully qualified spelling of the same name.
  host="${host%.}"

  valid_host "$host" \
    || die "line $number: '$entry' is not a valid hostname (IP addresses can't be allowlisted; the proxy matches names)"
  [ "$wildcard" = 0 ] || [ -z "$path" ] \
    || die "line $number: '$entry': a .wildcard can't have a path"
  [[ "$path" != *'?'* && "$path" != *'#'* ]] \
    || die "line $number: '$entry': paths can't contain ? or #"
  ! [[ "$path" =~ (^|/)\.\.?(/|$) ]] \
    || die "line $number: '$entry': paths can't contain . or .. segments"

  if [ "$wildcard" = 1 ]; then
    wildcards+=("$host")
  else
    hosts+=("$host")
  fi

  # The URL regex: the scheme, the host (and its subdomains, for a
  # wildcard), an optional port, the path prefix with its special characters
  # escaped, and then the end of the URL, a slash or a query. "/team" covers
  # /team and /team/x, but not /teammates.
  local host_re="${host//./\\.}"
  [ "$wildcard" = 0 ] || host_re="([^/]+\\.)?${host_re}"

  local path_re=""
  if [ -n "$path" ]; then
    path="${path%/}"
    path_re="$(sed -E 's/[][\\.^$*+?(){}|]/\\&/g' <<<"$path")"
  fi

  local regex="^https?://${host_re}(:[0-9]+)?${path_re}(/|\\?|\$)"
  if [ "$mode" = rw ]; then
    rw+=("$regex")
  else
    ro+=("$regex")
  fi
}

number=0
# `|| [ -n "$line" ]`: a last line without a newline is a line too.
while IFS= read -r line || [ -n "$line" ]; do
  number=$((number + 1))
  line="${line%%#*}"

  read -r entry flag extra <<<"$line" || true
  [ -n "${entry:-}" ] || continue
  [ -z "${extra:-}" ] || die "line $number: unexpected '$extra' (format: <entry> [rw|ssh])"

  case "${flag:-}" in
    "") mode=ro ;;
    rw | ssh) mode="$flag" ;;
    *) die "line $number: unknown flag '$flag' (rw or ssh; read-only is the default)" ;;
  esac

  entry="${entry,,}"
  if [ "$mode" = ssh ]; then
    add_ssh_entry "$number" "$entry"
  else
    add_http_entry "$number" "$entry" "$mode"
  fi
done < <(sed -e 's/\r$//' "$input")

# ---------------------------------------------------------------------------
# Hosts and DNS zones
# ---------------------------------------------------------------------------

# covered <host>: whether a wildcard entry already covers it. Squid warns
# about overlapping dstdomain entries, so those exact hosts are dropped.
covered() {
  local host="$1" wildcard
  for wildcard in "${wildcards[@]}"; do
    [[ "$host" == "$wildcard" || "$host" == *".$wildcard" ]] && return 0
  done
  return 1
}

# sorted_unique <name of array>: sort the array in place, dropping repeats.
sorted_unique() {
  local -n items="$1"
  [ "${#items[@]}" -eq 0 ] || mapfile -t items < <(printf '%s\n' "${items[@]}" | sort -u)
}

# Squid's dstdomain list: ".host" matches the host and every subdomain.
domains=()
for wildcard in "${wildcards[@]}"; do domains+=(".$wildcard"); done
for host in "${hosts[@]}"; do covered "$host" || domains+=("$host"); done
sorted_unique domains

# The zones CoreDNS answers for; a zone covers its subdomains too.
zones=()
for wildcard in "${wildcards[@]}"; do zones+=("$wildcard"); done
for host in "${hosts[@]}" "${ssh_hosts[@]}"; do covered "$host" || zones+=("$host"); done
for zone in $extra_zones; do zones+=("${zone,,}"); done
sorted_unique zones

# ---------------------------------------------------------------------------
# Writing the files
# ---------------------------------------------------------------------------

# Everything is written next to its final name first and moved into place
# at the end, so a failure halfway never leaves a half-written policy.
mkdir -p "$out/squid"

# print_or <placeholder> <items...>: the items one per line, or the
# placeholder when there are none. Squid rejects an empty ACL file, so with
# nothing allowed a name that can't exist stands in for the hosts (no tunnel
# matches it), and "^$" for the URLs (it can never match a URL).
print_or() {
  local placeholder="$1"
  shift
  if [ "$#" -gt 0 ]; then
    printf '%s\n' "$@"
  else
    echo "$placeholder"
  fi
}

print_or 'deny-all.hardcode.invalid' "${domains[@]}" > "$out/squid/hosts.txt.tmp"
print_or '^$' "${ro[@]}" > "$out/squid/urls-ro.regex.tmp"
print_or '^$' "${rw[@]}" > "$out/squid/urls-rw.regex.tmp"

{
  echo "# Generated from allowlist.txt by allowlist-render. Do not edit."
  echo "# SSH tunnels (not inspectable), one host:port each."
  [ "${#ssh_rules[@]}" -eq 0 ] || printf '%s\n' "${ssh_rules[@]}"
} > "$out/squid/ssh.conf.tmp"

# The ranges are space-separated words: one per line.
# shellcheck disable=SC2086
printf '%s\n' $client_ranges > "$out/squid/clients.txt.tmp"
# shellcheck disable=SC2086
printf '%s\n' $internal_ranges > "$out/squid/internal.txt.tmp"

{
  echo "# Generated from allowlist.txt by allowlist-render. Do not edit."
  echo

  if [ "${#zones[@]}" -gt 0 ]; then
    echo "# Allowlisted hosts (and their subdomains) are resolved upstream."
    printf '%s ' "${zones[@]}"
    cat <<COREFILE
{
    forward . ${upstream}
    cache 300
    errors
}

COREFILE
  else
    echo "# Nothing is allowlisted: every name gets NXDOMAIN."
    echo
  fi

  cat <<COREFILE
# Everything else gets NXDOMAIN here and never leaves the host.
. {
    log . "{remote} BLOCKED {type} {name}"
    template ANY ANY {
        rcode NXDOMAIN
    }
    errors
    reload 10s
}
COREFILE
} > "$out/Corefile.tmp"

for file in squid/hosts.txt squid/urls-ro.regex squid/urls-rw.regex squid/ssh.conf \
            squid/clients.txt squid/internal.txt Corefile; do
  mv "$out/$file.tmp" "$out/$file"
done

# Squid and CoreDNS run as users of their own; they read, nothing more.
chmod -R a+rX "$out"

summary="${#domains[@]} hosts, ${#ro[@]} read-only and ${#rw[@]} read-write URL rules, ${#ssh_hosts[@]} ssh tunnels"
if [ "${#domains[@]}" -eq 0 ] && [ "${#ssh_hosts[@]}" -eq 0 ]; then
  summary="$summary (nothing allowed: deny-all)"
fi
echo "allowlist: $summary"
