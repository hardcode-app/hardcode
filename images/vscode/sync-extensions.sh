#!/usr/bin/env bash
# Keeps the VS Code extensions in the home volume in step with the profile's
# list: the extensions every VS Code container has, those of the profile's
# languages and tools, and its own. The list is mounted read-only at
# $HARDCODE_EXTENSIONS_WANTED (on Compose, profiles/<name>/vscode-extensions.txt;
# on Kubernetes, a ConfigMap) and rewritten whenever the profile changes.
#
#   hardcode-sync-extensions           bring the extensions in step once
#   hardcode-sync-extensions --watch   and again whenever the list changes (the
#                                      entrypoint runs this in the background,
#                                      while VS Code is already up)
#
# What is wanted and missing is installed, one extension at a time, each
# held with what it pulls in to the rule that publishers must have a domain
# the Marketplace has verified (hardcode-verify-extensions). One that fails
# the rule is removed again and remembered as refused, so it isn't fetched
# again; one that couldn't be installed or checked is tried again shortly.
# What this script installed before (its record, with dependencies and pack
# members) and nobody wants now is removed. Extensions installed by hand in
# VS Code are never touched.
#
# How far it has got is written to ~/.vscode-server/.hardcode-extensions-status.json,
# which `hardcode extensions` and the app read, and logged as one line per
# step, which the Kubernetes admin reads.
set -uo pipefail

wanted_file="${HARDCODE_EXTENSIONS_WANTED:-/etc/hardcode/extensions/wanted}"
poll_seconds="${HARDCODE_EXTENSIONS_POLL:-3}"
retry_seconds="${HARDCODE_EXTENSIONS_RETRY:-60}"

state_dir="$HOME/.vscode-server"
extensions_dir="$state_dir/extensions"
record="$state_dir/.hardcode-extensions"
refused_file="$state_dir/.hardcode-extensions-refused"
status_file="$state_dir/.hardcode-extensions-status.json"
code_cli=(code --user-data-dir /tmp/vscode-cli --extensions-dir "$extensions_dir")

# A Marketplace extension id, lower case: publisher.name.
id_pattern='^[a-z0-9][a-z0-9-]*\.[a-z0-9][a-z0-9-]*$'

# log <message>: on stderr, which the container's log shows.
log() {
  printf '[vscode] %s\n' "$*" >&2
}

# lines <text>: the non-empty lines of a newline-separated set, sorted.
lines() {
  printf '%s\n' "$1" | grep . | sort -u || true
}

# count <text>: how many non-empty lines.
count() {
  lines "$1" | grep -c . || true
}

# read_wanted: the ids on the list, one per line, sorted. Fails while the list
# is missing, or caught half-written (it ends with "# end" once complete).
read_wanted() {
  local text

  [ -f "$wanted_file" ] || return 1
  text="$(cat "$wanted_file")" || return 1
  [ "$(printf '%s\n' "$text" | tail -n 1)" = "# end" ] || return 1

  printf '%s\n' "$text" | grep -v '^#' | awk '{ print tolower($1) }' | grep -E "$id_pattern" | sort -u || true
}

# list_installed: the installed extensions, one id per line, sorted.
list_installed() {
  "${code_cli[@]}" --list-extensions 2>/dev/null | tr '[:upper:]' '[:lower:]' | sort -u
}

# closure <id>...: the ids and everything they need (dependencies, the
# members of a pack), from the installed extensions' manifests, sorted.
closure() {
  local known

  [ "$#" -gt 0 ] || return 0

  known="$(
    for manifest in "$extensions_dir"/*/package.json; do
      [ -f "$manifest" ] || continue
      jq -c '{
        id: ("\(.publisher).\(.name)" | ascii_downcase),
        needs: (((.extensionDependencies // []) + (.extensionPack // [])) | map(ascii_downcase))
      }' "$manifest" 2>/dev/null
    done | jq -s '.'
  )"

  # Grow the set by what its members need until it stops growing.
  jq -rn --argjson known "$known" '
    ($known | map({(.id): .needs}) | add // {}) as $needs
    | def grow: . as $ids
        | ($ids + [$ids[] | ($needs[.] // [])[]] | unique) as $more
        | if $more == $ids then $ids else ($more | grow) end;
    [$ARGS.positional[] | ascii_downcase] | unique | grow | .[]
  ' --args "$@"
}

# write_status <state> <total> <installed> <pending> <refused> <failed>: the
# status file, the last four as newline-separated sets.
write_status() {
  local temporary="$status_file.tmp"

  mkdir -p "$state_dir"
  jq -n \
    --arg state "$1" \
    --argjson total "$2" \
    --argjson installed "$3" \
    --arg pending "$4" \
    --arg refused "$5" \
    --arg failed "$6" \
    --arg updated "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{
      state: $state,
      total: $total,
      installed: $installed,
      pending: ($pending | split("\n") | map(select(. != ""))),
      refused: ($refused | split("\n") | map(select(. != ""))),
      failed: ($failed | split("\n") | map(select(. != ""))),
      updated: $updated
    }' > "$temporary" && mv "$temporary" "$status_file"
}

# install_one <id>: check its publisher, install it, then check what
# arrived with it. Prints "ok", "refused" (the publisher rule turned it
# away) or "failed".
#
# VS Code is running and picks up what lands in its extensions folder, so
# the extension itself is checked before it is installed: one from a
# publisher without a verified domain never gets there. What it pulls in
# (dependencies, a pack's members) is only known once it is here, and is
# checked on arrival.
install_one() {
  local id="$1" before report extension
  local arrived

  if ! report="$(hardcode-verify-extensions "$id" 2>&1)"; then
    if grep -q "without a verified domain" <<<"$report"; then
      echo refused
    else
      echo failed
    fi
    return 0
  fi

  before="$(list_installed)"
  if ! "${code_cli[@]}" --install-extension "$id" >/dev/null 2>&1; then
    echo failed
    return 0
  fi

  mapfile -t arrived < <(comm -13 <(printf '%s\n' "$before") <(list_installed) | grep -vixF -- "$id" || true)
  if [ "${#arrived[@]}" -eq 0 ] || report="$(hardcode-verify-extensions "${arrived[@]}" 2>&1)"; then
    echo ok
    return 0
  fi

  # Not verified, or not checkable now: either way it doesn't stay.
  for extension in "${arrived[@]}"; do
    "${code_cli[@]}" --uninstall-extension "$extension" >/dev/null 2>&1 || true
  done

  if grep -q "without a verified domain" <<<"$report"; then
    echo refused
  else
    echo failed
  fi
}

# sync_once: one pass. Returns 2 when something failed and should be tried
# again, 1 when there is no complete list to go by, 0 otherwise.
sync_once() {
  local want refused failed installed missing total in_place pending keep id outcome
  local -a keep_ids

  want="$(read_wanted)" || return 1

  # Refusals are kept only for what is still wanted, and not tried again.
  refused=""
  if [ -s "$refused_file" ]; then
    refused="$(comm -12 <(sort -u "$refused_file") <(lines "$want"))"
  fi
  want="$(comm -23 <(lines "$want") <(lines "$refused"))"

  installed="$(list_installed)"
  missing="$(comm -23 <(lines "$want") <(lines "$installed"))"
  total="$(count "$want")"
  failed=""

  if [ -n "$missing" ]; then
    pending="$missing"
    in_place=$((total - $(count "$missing")))
    log "extensions: $in_place of $total installed, installing $(count "$missing")"
    write_status installing "$total" "$in_place" "$pending" "$refused" "$failed"

    while read -r id; do
      [ -n "$id" ] || continue

      outcome="$(install_one "$id")"
      case "$outcome" in
        ok)
          log "installed VS Code extension $id"
          in_place=$((in_place + 1))
          ;;
        refused)
          log "WARNING: VS Code extension $id refused: its publisher, or one it pulls in, has no verified domain"
          refused="$(lines "$refused"$'\n'"$id")"
          ;;
        *)
          log "WARNING: VS Code extension $id couldn't be installed or checked; will try again"
          failed="$(lines "$failed"$'\n'"$id")"
          ;;
      esac

      pending="$(comm -23 <(lines "$pending") <(printf '%s\n' "$id"))"
      write_status installing "$total" "$in_place" "$pending" "$refused" "$failed"
    done <<<"$missing"
  fi

  printf '%s\n' "$refused" | grep . > "$refused_file" || : > "$refused_file"

  # The installing is over: say so now, not after the tidying up below.
  installed="$(list_installed)"
  in_place="$(comm -12 <(lines "$want") <(lines "$installed") | grep -c . || true)"
  write_status done "$total" "$in_place" "" "$refused" "$failed"

  # What the wanted extensions are, with everything they need: what this
  # script answers for. Whatever it answered for before and nobody wants now
  # goes.
  mapfile -t keep_ids < <(comm -12 <(lines "$want") <(lines "$installed"))
  keep="$(closure "${keep_ids[@]}")"

  if [ -s "$record" ]; then
    while read -r id; do
      [ -n "$id" ] || continue
      grep -qxF "$id" <<<"$installed" || continue

      log "removing VS Code extension $id (not wanted by the profile any more)"
      "${code_cli[@]}" --uninstall-extension "$id" >/dev/null 2>&1 || log "WARNING: could not remove $id"
    done < <(comm -23 <(sort -u "$record") <(lines "$keep"))
  fi
  lines "$keep" > "$record"

  log "extensions: $in_place of $total installed$([ -n "$refused" ] && echo ", $(count "$refused") refused")$([ -n "$failed" ] && echo ", $(count "$failed") to try again")"

  rm -rf /tmp/vscode-cli
  [ -z "$failed" ] || return 2
}

# fingerprint: what the list says now, to notice when it changes.
fingerprint() {
  cksum < "$wanted_file" 2>/dev/null || echo missing
}

if [ "${1:-}" != "--watch" ]; then
  sync_once
  result=$?
  [ "$result" -ne 1 ] || log "WARNING: no complete extension list at $wanted_file"
  exit 0
fi

# Watch: a pass now, then one whenever the list changes, and one after a
# while when something failed. A failed pass never stops the watching.
seen=""
retry_at=0
while true; do
  current="$(fingerprint)"
  now="$(date +%s)"

  if [ "$current" != "$seen" ] || { [ "$retry_at" -gt 0 ] && [ "$now" -ge "$retry_at" ]; }; then
    sync_once
    case $? in
      0) seen="$current"; retry_at=0 ;;
      2) seen="$current"; retry_at=$((now + retry_seconds)) ;;
      *) ;; # no complete list yet: look again at the next poll
    esac
  fi

  sleep "$poll_seconds"
done
