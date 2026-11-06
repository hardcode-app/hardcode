#!/usr/bin/env bash
# Takes the AppImage runtime out of an AppImage: the program at its front,
# before its filesystem, for appimagetool to build another AppImage on
# (--runtime-file, which linuxdeploy's AppImage plugin passes on from
# LDAI_RUNTIME_FILE). What the AppImage's own build wrote into its runtime
# (its update information, its digest, its signature and key) is cleared,
# for appimagetool to fill in for the AppImage it builds: kept, the app's
# AppImage would say it updates from wherever the plugin does.
#
#   appimage-runtime.sh APPIMAGE RUNTIME
#
# The build image's Dockerfile runs it on the pinned linuxdeploy AppImage
# plugin, whose checksum it has checked, so that packaging the app
# downloads no runtime: appimagetool would otherwise fetch one from
# type2-runtime's moving "continuous" release, unchecked.
set -euo pipefail

# The sections appimagetool writes into a runtime as it builds an AppImage.
SECTIONS=(.upd_info .sha256_sig .sig_key .digest_md5)

# section FILE NAME: the section NAME's offset in FILE and its size, both in
# hexadecimal, as readelf lists them (after the section's number, its name,
# type, address, offset, size, ...).
section() {
  readelf -S -W "$1" \
    | sed -E 's/^ *\[ *[0-9]+\] *//' \
    | awk -v name="$2" '$1 == name { print $4, $5 }'
}

# clear FILE NAME: overwrite the section NAME of FILE with zeros, in place.
clear() {
  local found offset size

  found="$(section "$1" "$2")"
  if [ -z "$found" ]; then
    echo "$1 has no $2 section: not an AppImage runtime this knows" >&2
    exit 1
  fi

  read -r offset size <<<"$found"
  dd if=/dev/zero of="$1" \
    bs=1 \
    seek="$((16#$offset))" \
    count="$((16#$size))" \
    conv=notrunc \
    status=none
}

main() {
  local appimage="$1"
  local runtime="$2"
  local offset name

  # The AppImage says where its filesystem starts: everything before it is
  # the runtime.
  offset="$("$appimage" --appimage-offset)"
  head -c "$offset" "$appimage" > "$runtime"

  for name in "${SECTIONS[@]}"; do
    clear "$runtime" "$name"
  done

  chmod 0755 "$runtime"
}

main "$@"
