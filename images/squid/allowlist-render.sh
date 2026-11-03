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
#   lan name address      lets exactly that name reach exactly that address
#                         on your network, which is refused otherwise; it
#                         allows nothing by itself (the name's entries do)
#
# Without "rw" an entry is read-only: GET, HEAD, OPTIONS, and the POST git
# uses for clone, fetch and pull. Hosts are names, never addresses. An empty
# list renders a policy that allows nothing.
#
# A "lan" line pins an exact name (no wildcard, path or port) to one IPv4 or
# IPv6 address inside INTERNAL_RANGES, never a loopback, link-local,
# multicast or 0.0.0.0/8 one (refused_ranges, below), and a name only once.
# Squid then resolves that name to that address alone, from its hosts file,
# without asking DNS.
#
# The same grammar lives in cli/hardcode_cli/allowlist.py, and the rules for a
# lan line's address in cli/hardcode_cli/lan.py; both are run over
# tests/allowlist-corpus.txt.
#
# Output:
#
#   /out/Corefile             CoreDNS: allowlisted hosts resolve, the rest NXDOMAIN
#   /out/squid/hosts.txt      dstdomain list, for CONNECT and the TLS server name
#   /out/squid/urls-ro.regex  url_regex list for read-only entries
#   /out/squid/urls-rw.regex  url_regex list for rw entries
#   /out/squid/ssh.conf       squid rules for the ssh entries (included by squid.conf)
#   /out/squid/pins.conf      the "pinned" ACL, from the lan lines (included by squid.conf)
#   /out/squid/pins.hosts     the pinned names' addresses, hosts-file style
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

# Addresses no name may ever be pinned to, whatever INTERNAL_RANGES says:
# "this network" (0.0.0.0/8, the unspecified address with it), loopback,
# link-local (where cloud metadata services answer), multicast, and IPv4
# addresses written as IPv6, which would get round the IPv4 ones. The same
# list is in cli/hardcode_cli/lan.py.
refused_ranges="0.0.0.0/8 127.0.0.0/8 169.254.0.0/16 224.0.0.0/4 ::/128 ::1/128 fe80::/10 ff00::/8 ::ffff:0:0/96"

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
pin_names=()      # names pinned to an address on your network
pin_addresses=()  # and their addresses, in the same order

# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------

# binary <number> <width>: the number in base 2, <width> digits.
binary() {
  local value="$1" width="$2" bits="" position
  for (( position = width - 1; position >= 0; position-- )); do
    bits+=$(( (value >> position) & 1 ))
  done
  echo "$bits"
}

# address_bits <address>: "4:" or "6:" and then the address in base 2 (32 or
# 128 digits); fails for anything else. IPv4 is dotted, with no leading
# zeros (010 is octal to some readers and decimal to others); IPv6 is hex,
# with at most one "::", and without an IPv4 part or a zone ("%eth0").
address_bits() {
  local address="$1" bits="" part

  if [[ "$address" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]]; then
    for part in "${BASH_REMATCH[@]:1}"; do
      [[ "$part" =~ ^(0|[1-9][0-9]*)$ ]] && [ "$part" -le 255 ] || return 1
      bits+="$(binary "$part" 8)"
    done
    echo "4:$bits"
    return 0
  fi

  [[ "$address" == *:* && "$address" =~ ^[0-9a-f:]+$ ]] || return 1

  # The groups before and after "::", which stands for as many zero groups
  # as are missing; without one, there must be all eight.
  local head="$address" tail="" compressed=0
  if [[ "$address" == *::* ]]; then
    head="${address%%::*}"
    tail="${address#*::}"
    compressed=1
    [[ "$tail" != *::* ]] || return 1
  fi

  local groups_re='^[0-9a-f]{1,4}(:[0-9a-f]{1,4})*$'
  local head_groups=() tail_groups=()
  if [ -n "$head" ]; then
    [[ "$head" =~ $groups_re ]] || return 1
    IFS=: read -ra head_groups <<<"$head"
  fi
  if [ -n "$tail" ]; then
    [[ "$tail" =~ $groups_re ]] || return 1
    IFS=: read -ra tail_groups <<<"$tail"
  fi

  local count=$(( ${#head_groups[@]} + ${#tail_groups[@]} ))
  if [ "$compressed" = 1 ]; then
    [ "$count" -le 7 ] || return 1
  else
    [ "$count" -eq 8 ] || return 1
  fi

  local groups=("${head_groups[@]}") missing
  for (( missing = 8 - count; missing > 0; missing-- )); do groups+=(0); done
  groups+=("${tail_groups[@]}")

  for part in "${groups[@]}"; do
    bits+="$(binary "$((16#$part))" 16)"
  done
  echo "6:$bits"
}

# in_range <bits> <range>: whether an address, as address_bits writes it, is
# inside <range> (an address, or address/prefix) of the same family.
in_range() {
  local bits="$1" range="${2,,}"
  local network="${range%%/*}" network_bits
  network_bits="$(address_bits "$network")" || return 1
  [ "${network_bits%%:*}" = "${bits%%:*}" ] || return 1

  network_bits="${network_bits#*:}"
  local prefix="${#network_bits}"
  if [[ "$range" == */* ]]; then
    prefix="${range#*/}"
    [[ "$prefix" =~ ^[0-9]+$ ]] && [ "$prefix" -le "${#network_bits}" ] || return 1
  fi

  [ "${bits:2:prefix}" = "${network_bits:0:prefix}" ]
}

# ---------------------------------------------------------------------------
# Reading the entries
# ---------------------------------------------------------------------------

# add_pin <line number> <name> <address>: let exactly <name> reach exactly
# <address> on your network.
add_pin() {
  local number="$1" name="${2,,}" address="${3,,}"

  [ -n "$name" ] && [ -n "$address" ] \
    || die "line $number: format: lan <name> <address>"
  [[ "$name" != .* && "$name" != */* && "$name" != *:* ]] \
    || die "line $number: 'lan $name': only an exact name can reach your network, without a wildcard, path or port"
  valid_host "$name" \
    || die "line $number: '$name' is not a valid hostname"

  local bits range inside=0
  bits="$(address_bits "$address")" \
    || die "line $number: '$address' is not an IP address"
  for range in $refused_ranges; do
    ! in_range "$bits" "$range" \
      || die "line $number: $address can't be pinned: loopback, link-local, multicast and 0.0.0.0/8 addresses never can"
  done
  for range in $internal_ranges; do
    if in_range "$bits" "$range"; then
      inside=1
      break
    fi
  done
  [ "$inside" = 1 ] \
    || die "line $number: $address isn't on your network (outside INTERNAL_RANGES), so there is nothing to pin"

  local pinned
  for pinned in "${pin_names[@]}"; do
    [ "$pinned" != "$name" ] || die "line $number: $name is already on your network, on another line"
  done

  pin_names+=("$name")
  pin_addresses+=("$address")
}

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

  # A tunnel to exactly this host and port, never to an internal address
  # unless it is the one the host is pinned to, passed through without
  # inspection (it isn't TLS). Refused, it is reset, and the log says why.
  ssh_rules+=(
    "# $host:$port"
    "acl ssh_${n} dstdomain -n ${host}"
    "acl ssh_${n}_port port ${port}"
    "http_access allow CONNECT ssh_${n} ssh_${n}_port !to_internal"
    "http_access allow CONNECT ssh_${n} ssh_${n}_port pinned"
    "http_access deny CONNECT ssh_${n} ssh_${n}_port refused_internal refuse_tunnel"
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

  read -r entry flag address extra <<<"$line" || true
  [ -n "${entry:-}" ] || continue

  # "lan <name> <address>": a pin, not an entry.
  if [ "$entry" = lan ]; then
    [ -z "${extra:-}" ] || die "line $number: unexpected '$extra' (format: lan <name> <address>)"
    add_pin "$number" "${flag:-}" "${address:-}"
    continue
  fi

  extra="${address:-}${extra:+ $extra}"
  [ -z "$extra" ] || die "line $number: unexpected '$extra' (format: <entry> [rw|ssh])"

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

# The pins: "pinned" matches a request for exactly a pinned name going to
# exactly its address. With none, it is an annotation nothing ever makes, so
# it never matches.
{
  echo "# Generated from allowlist.txt by allowlist-render. Do not edit."
  echo "# Names pinned to an address on your network (lan lines)."
  if [ "${#pin_names[@]}" -eq 0 ]; then
    echo "acl pinned note hardcode_nothing_is_pinned"
  else
    pins=()
    for index in "${!pin_names[@]}"; do
      n=$(( index + 1 ))
      echo "# ${pin_names[$index]} ${pin_addresses[$index]}"
      echo "acl pin_${n}_name dstdomain -n ${pin_names[$index]}"
      echo "acl pin_${n}_address dst ${pin_addresses[$index]}"
      echo "acl pin_${n} all-of pin_${n}_name pin_${n}_address"
      pins+=("pin_${n}")
    done
    echo "acl pinned any-of ${pins[*]}"
  fi
} > "$out/squid/pins.conf.tmp"

{
  echo "# Generated from allowlist.txt by allowlist-render. Do not edit."
  echo "# The pinned names' addresses: squid resolves them from here, never from DNS."
  for index in "${!pin_names[@]}"; do
    echo "${pin_addresses[$index]} ${pin_names[$index]}"
  done
} > "$out/squid/pins.hosts.tmp"

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
            squid/pins.conf squid/pins.hosts squid/clients.txt squid/internal.txt Corefile; do
  mv "$out/$file.tmp" "$out/$file"
done

# Squid and CoreDNS run as users of their own; they read, nothing more.
chmod -R a+rX "$out"

summary="${#domains[@]} hosts, ${#ro[@]} read-only and ${#rw[@]} read-write URL rules, ${#ssh_hosts[@]} ssh tunnels"
summary="$summary, ${#pin_names[@]} names on your network"
if [ "${#domains[@]}" -eq 0 ] && [ "${#ssh_hosts[@]}" -eq 0 ]; then
  summary="$summary (nothing allowed: deny-all)"
fi
echo "allowlist: $summary"
