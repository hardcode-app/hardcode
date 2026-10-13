#!/usr/bin/env bash
# Checks that VS Code extensions come from publishers whose domain the
# Marketplace has verified; hardcode-sync-extensions runs it on every
# extension before installing it, and on whatever that extension pulled in:
#
#   hardcode-verify-extensions id...
#
# Exits 0 when every one is verified, or is a reviewed exception: a line of
# the profile's extension list ($HARDCODE_EXTENSIONS_WANTED) marked
# "unverified-ok", which comes from the catalogue's APPROVED_UNVERIFIED.
# Exits 1, naming them, when any isn't ("without a verified domain"), and
# with curl's or jq's code when the Marketplace couldn't be asked.
set -euo pipefail

wanted_file="${HARDCODE_EXTENSIONS_WANTED:-/etc/hardcode/extensions/wanted}"
gallery=https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery

if [ "$#" -eq 0 ]; then
  echo "nothing to verify"
  exit 0
fi
checked=("$@")

# The reviewed exceptions, from the profile's list.
approved=()
if [ -f "$wanted_file" ]; then
  while read -r id approval _; do
    case "${id:-}" in '' | '#'*) continue ;; esac
    if [ "${approval:-}" = "unverified-ok" ]; then
      approved+=("${id,,}")
    fi
  done < "$wanted_file"
fi

# Ask the Marketplace about every extension at once: one criterion per id.
# Within time limits: a stalled answer is a failure, tried again later.
query='{filters: [{criteria: [$ARGS.positional[] | {filterType: 7, value: .}], pageSize: 1000}], flags: 0}'
result="$(
  jq -n "$query" --args "${checked[@]}" \
    | curl -fsSL -X POST "$gallery" \
        --connect-timeout 10 \
        --max-time 30 \
        -H 'Content-Type: application/json' \
        -H 'Accept: application/json;api-version=3.0-preview.1' \
        --data-binary @-
)"

found="$(jq '.results[0].extensions | length' <<<"$result")"
if [ "$found" -ne "${#checked[@]}" ]; then
  echo "Only $found of ${#checked[@]} extensions were found on the Marketplace" >&2
  exit 1
fi

# The extensions whose publisher isn't domain-verified, less the approved exceptions.
unverified="$(jq -r '.results[0].extensions[]
  | select(.publisher.isDomainVerified | not)
  | ("\(.publisher.publisherName).\(.extensionName)" | ascii_downcase) as $id
  | select($id | IN($ARGS.positional[]) | not)
  | "\($id) (\(.publisher.displayName))"' --args "${approved[@]}" <<<"$result")"

if [ -n "$unverified" ]; then
  printf 'Extensions from publishers without a verified domain:\n%s\n' "$unverified" >&2
  exit 1
fi

echo "Verified ${#checked[@]} extensions; unverified publishers only where approved: ${approved[*]:-none}"
