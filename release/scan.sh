#!/usr/bin/env bash
# Scans what a release is built from, and the images that are its boundary,
# for known vulnerabilities: OSV-Scanner from the build image, in a container
# with no network, a read-only filesystem, no capabilities and a memory cap,
# reading the databases release/scan.py downloaded and the images saved as
# archives, and writing a JSON report for each. The scanner never holds a
# token or a network: compromised, all it could do is lie in its report.
#
#   release/scan.sh DATABASES IMAGES REPORTS
#
# DATABASES is release/scan.py databases' folder; IMAGES holds NAME.tar for
# each image to scan (release/scan.py save), and may be empty; REPORTS receives
# source.json (the lock files) and NAME.json for each image. The build image
# is hardcode-build:local, as .github/actions/build-image and
# images/build/run.sh build it.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"

if [ "$#" -ne 3 ]; then
  echo "usage: release/scan.sh DATABASES IMAGES REPORTS" >&2
  exit 2
fi

databases="$(cd "$1" && pwd)"
images="$(cd "$2" && pwd)"
reports="$(cd "$3" && pwd)"

# OSV-Scanner, sealed, with the repository (read-only), the databases, the
# images and the reports mounted; the arguments are the scanner's. It exits
# 1 when it finds something, which is what a report is for.
scan() {
  local status=0

  docker run \
    --rm \
    --network none \
    --read-only \
    --tmpfs /tmp \
    --memory 4g \
    --memory-swap 4g \
    --pids-limit 512 \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --user "$(id -u):$(id -g)" \
    --env OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY=/db \
    --volume "$root:/repo:ro" \
    --volume "$databases:/db" \
    --volume "$images:/images:ro" \
    --volume "$reports:/reports" \
    hardcode-build:local \
    osv-scanner "$@" || status=$?

  if [ "$status" -gt 1 ]; then
    echo "scan: osv-scanner $* failed ($status)" >&2
    exit "$status"
  fi
}

# The lock files: the app's crates, and the Python packages the command, the
# admin and the build use.
scan scan source \
  --offline \
  --format json \
  --output-file /reports/source.json \
  --lockfile /repo/app/src-tauri/Cargo.lock \
  --lockfile /repo/cli/requirements.txt \
  --lockfile /repo/images/admin/uv.lock \
  --lockfile /repo/images/build/uv.lock \
  --lockfile /repo/images/build/uv/requirements.txt

# Each image: its packages, and the Go binaries in it.
for archive in "$images"/*.tar; do
  if [ ! -e "$archive" ]; then
    continue
  fi

  name="$(basename "$archive" .tar)"
  scan scan image \
    --offline \
    --format json \
    --output-file "/reports/$name.json" \
    --archive "/images/$name.tar"
done

echo "scan: reports in $reports"
