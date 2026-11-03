#!/bin/bash
# Prints the networks this container is attached to, one range per line, as
# squid's dst ACLs read them: each interface's subnet and its own addresses,
# and each route's gateway, which is the host or the node behind the
# network. Squid's entrypoint writes them where squid.conf's own_networks
# ACL reads them, at every start: only squid can see its own networks, and
# no request may reach them, pinned to an address on your network or not.
#
# The image has no "ip", so this reads the kernel's tables in /proc:
#
#   /proc/net/route       IPv4 routes: one without a gateway is a subnet squid
#                         is on, and one with a gateway names it
#   /proc/net/fib_trie    IPv4 addresses of squid's own (a pod's /32 address
#                         has no subnet route of its own)
#   /proc/net/ipv6_route  IPv6 routes, the same way; squid's own IPv6
#                         addresses are routes too (/128, on lo)
#
# A range already inside another one is left out, since squid warns about
# each such line.
#
#   squid-own-networks
set -euo pipefail

# The sixteen hex digits in base 2.
NIBBLES=(0000 0001 0010 0011 0100 0101 0110 0111 1000 1001 1010 1011 1100 1101 1110 1111)

# The ranges found, before overlaps are left out: "<prefix> <family> <hex>",
# the address in hex, most significant digit first (8 digits for IPv4, 32
# for IPv6).
found=()

# hex_bits <hex>: the hex digits in base 2, four digits each.
hex_bits() {
  local hex="${1,,}" bits="" index
  for (( index = 0; index < ${#hex}; index++ )); do
    bits+="${NIBBLES[16#${hex:index:1}]}"
  done
  echo "$bits"
}

# route_hex <hex>: an IPv4 address as /proc/net/route writes it, which is
# the address's bytes read as one number on a little-endian machine (amd64,
# arm64): "010011AC" is 172.17.0.1. The bytes in the order they are written.
route_hex() {
  local hex="$1"
  echo "${hex:6:2}${hex:4:2}${hex:2:2}${hex:0:2}"
}

# dotted_hex <a.b.c.d>: a dotted IPv4 address in hex.
dotted_hex() {
  local a b c d
  IFS=. read -r a b c d <<<"$1"
  printf '%02x%02x%02x%02x\n' "$a" "$b" "$c" "$d"
}

# prefix_length <hex>: how many leading ones a netmask has.
prefix_length() {
  local bits ones
  bits="$(hex_bits "$1")"
  ones="${bits%%0*}"
  echo "${#ones}"
}

# IPv4 routes. One with a gateway names the gateway; one without is a subnet
# squid is attached to, unless it is a default route through no gateway at
# all, which would cover everything. A route through a gateway to some
# other network (your LAN, say) is not squid's own: only its gateway is.
read_ipv4_routes() {
  local interface destination gateway mask

  # The columns: Iface Destination Gateway Flags RefCnt Use Metric Mask
  # MTU Window IRTT, the first line their names.
  while read -r interface destination gateway _ _ _ _ mask _; do
    [ "$interface" != Iface ] || continue

    if [ "$gateway" != 00000000 ]; then
      found+=("32 4 $(route_hex "$gateway")")
    elif [ "$mask" != 00000000 ]; then
      found+=("$(prefix_length "$(route_hex "$mask")") 4 $(route_hex "$destination")")
    fi
  done < /proc/net/route
}

# Squid's own IPv4 addresses: the kernel's local routes ("/32 host LOCAL"
# under the address, and "/8 host LOCAL" for loopback's whole range).
read_ipv4_addresses() {
  [ -r /proc/net/fib_trie ] || return 0

  local prefix address
  while read -r prefix address; do
    found+=("$prefix 4 $(dotted_hex "$address")")
  done < <(awk '
    $1 == "|--" { address = $2 }
    $1 ~ /^\/[0-9]+$/ && $NF == "LOCAL" { print substr($1, 2), address }
  ' /proc/net/fib_trie)
}

# IPv6 routes, as for IPv4: a next hop is a gateway, a route without one a
# subnet (or, on lo, one of squid's own addresses). Without IPv6 the file
# may be missing, and there is nothing to add.
read_ipv6_routes() {
  [ -r /proc/net/ipv6_route ] || return 0

  local destination length next_hop
  local nowhere=00000000000000000000000000000000

  # The columns: destination, its prefix length (hex), source and its
  # length, next hop, metric, references, use, flags and the interface.
  while read -r destination length _ _ next_hop _; do
    if [ "$next_hop" != "$nowhere" ]; then
      found+=("128 6 $next_hop")
    elif [ "$length" != 00 ]; then
      found+=("$(( 16#$length )) 6 $destination")
    fi
  done < /proc/net/ipv6_route
}

# print_range <family> <hex> <prefix>: the range as squid reads it.
print_range() {
  local family="$1" hex="$2" prefix="$3"

  if [ "$family" = 4 ]; then
    printf '%d.%d.%d.%d/%s\n' "$((16#${hex:0:2}))" "$((16#${hex:2:2}))" "$((16#${hex:4:2}))" "$((16#${hex:6:2}))" "$prefix"
    return
  fi

  local groups=() index
  for (( index = 0; index < 32; index += 4 )); do
    groups+=("${hex:index:4}")
  done
  local IFS=:
  echo "${groups[*]}/$prefix"
}

read_ipv4_routes
read_ipv4_addresses
read_ipv6_routes

# Nothing found prints nothing, which the entrypoint refuses to start with.
[ "${#found[@]}" -gt 0 ] || exit 0

# The widest ranges first, so that a range is only ever covered by one
# already kept: one of the same family whose first <its prefix> bits match.
kept_families=()
kept_bits=()
kept_prefixes=()

while read -r prefix family hex; do
  bits="$(hex_bits "$hex")"

  covered=0
  for index in "${!kept_bits[@]}"; do
    length="${kept_prefixes[$index]}"
    if [ "${kept_families[$index]}" = "$family" ] && [ "${bits:0:length}" = "${kept_bits[$index]:0:length}" ]; then
      covered=1
      break
    fi
  done
  [ "$covered" = 0 ] || continue

  kept_families+=("$family")
  kept_bits+=("$bits")
  kept_prefixes+=("$prefix")
  print_range "$family" "$hex" "$prefix"
done < <(printf '%s\n' "${found[@]}" | sort -n -k1,1)
