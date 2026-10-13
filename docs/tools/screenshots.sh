#!/usr/bin/env bash
# Regenerates docs/images/*.png: every view of the desktop app and of the
# admin UI, rendered by headless Chrome from the real pages against fixture
# data. No Docker stack or cluster is needed. Run it after changing either
# page, then check that the pictures the docs reference still show what the
# docs say.
#
#   docs/tools/screenshots.sh                        all of them
#   docs/tools/screenshots.sh app-blocked admin-users   just these
#
# The pages are served on 127.0.0.1:18771 (the app) and :18772 (the admin);
# APP_PORT and ADMIN_PORT move them, so that two runs can't meet.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT/docs/images"
WIDTH=1280
HEIGHT=860
APP_PORT="${APP_PORT:-18771}"
ADMIN_PORT="${ADMIN_PORT:-18772}"

CHROME="${CHROME:-$(command -v google-chrome || command -v chromium || command -v chromium-browser || true)}"
if [ -z "$CHROME" ]; then
  echo "no chrome/chromium found (set CHROME=/path)"
  exit 1
fi

STAGE="$(mktemp -d)"
SERVERS=()
cleanup() {
  kill "${SERVERS[@]}" 2>/dev/null
  rm -rf "$STAGE"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# The pages as they are served, with the fixture scripts in place of the
# desktop app's bridge and the admin's API.
# ---------------------------------------------------------------------------

mkdir -p "$STAGE/app" "$STAGE/admin" "$STAGE/chrome" "$OUT"

cp "$ROOT/app/src/index.html" "$ROOT/app/src/app.js" "$ROOT/app/src/styles.css" "$STAGE/app/"
cp -r "$ROOT/app/src/assets" "$STAGE/app/"
cp "$ROOT/docs/tools/app-fixture.js" "$STAGE/app/bridge.js"

cp "$ROOT/admin/hardcode_admin/web/index.html" "$ROOT/admin/hardcode_admin/web/app.js" \
   "$ROOT/admin/hardcode_admin/web/admin.css" "$STAGE/admin/"
cp "$ROOT/app/src/styles.css" "$STAGE/admin/base.css"
cp "$ROOT/assets/hardcode-128.png" "$STAGE/admin/icon.png"
cp "$ROOT/docs/tools/admin-fixture.js" "$STAGE/admin/fixture.js"
sed -i 's|<script src="/app.js"></script>|<script src="/fixture.js"></script><script src="/app.js"></script>|' \
  "$STAGE/admin/index.html"

python3 "$ROOT/docs/tools/fixture-data.py" "$STAGE"

# Serve both, and wait until they answer.
(cd "$STAGE/app" && exec python3 -m http.server "$APP_PORT" --bind 127.0.0.1 >/dev/null 2>&1) &
SERVERS+=($!)
(cd "$STAGE/admin" && exec python3 -m http.server "$ADMIN_PORT" --bind 127.0.0.1 >/dev/null 2>&1) &
SERVERS+=($!)

for _ in $(seq 1 50); do
  if curl -fsS "http://127.0.0.1:$APP_PORT/index.html" >/dev/null 2>&1 \
    && curl -fsS "http://127.0.0.1:$ADMIN_PORT/index.html" >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done

# ---------------------------------------------------------------------------
# Taking the pictures
# ---------------------------------------------------------------------------

# capped <command...>: run with a hard memory cap, when the host can give one.
capped() {
  if command -v systemd-run >/dev/null 2>&1 && systemd-run --user --scope -q -p MemoryMax=2G true 2>/dev/null; then
    systemd-run --user --scope -q -p MemoryMax=2G "$@"
  else
    "$@"
  fi
}

# shoot <name> <url>: one screenshot, docs/images/<name>.png.
shoot() {
  local name="$1" url="$2"

  capped "$CHROME" \
    --headless=new --disable-gpu --hide-scrollbars --no-first-run --disable-extensions \
    --user-data-dir="$STAGE/chrome" \
    --window-size="${WIDTH},${HEIGHT}" \
    --virtual-time-budget=8000 \
    --screenshot="$OUT/$name.png" \
    "$url" >/dev/null 2>&1

  if [ ! -s "$OUT/$name.png" ]; then
    echo "FAILED $name"
    return 1
  fi
  echo "  $name.png"
}

APP="http://127.0.0.1:$APP_PORT/index.html"
ADMIN="http://127.0.0.1:$ADMIN_PORT/index.html"

declare -A SHOTS=(
  [app-overview]="$APP?view=overview"
  [app-langs]="$APP?view=langs"
  [app-extensions]="$APP?view=extensions"
  [app-allowlist]="$APP?view=allowlist"
  [app-allowlist-all]="$APP?view=allowlist&scope=all"
  [app-allowlist-search]="$APP?view=allowlist&q=github"
  [app-blocked]="$APP?view=blocked"
  [app-forwards]="$APP?view=forwards"
  [app-mounts]="$APP?view=mounts"
  [app-new-profile]="$APP?view=new-profile"
  [app-console]="$APP?view=console"
  [app-logs]="$APP?view=logs"
  [app-update]="$APP?view=overview&update=1"
  [admin-login]="$ADMIN?view=login"
  [admin-enrol]="$ADMIN?view=enrol"
  [admin-totp]="$ADMIN?view=totp"
  [admin-profiles]="$ADMIN?view=profiles"
  [admin-profile-languages]="$ADMIN?view=profile-languages"
  [admin-profile-extensions]="$ADMIN?view=profile-extensions"
  [admin-profile-allowlist]="$ADMIN?view=profile-allowlist"
  [admin-profile-blocked]="$ADMIN?view=profile-blocked"
  [admin-profile-access]="$ADMIN?view=profile-access"
  [admin-profile-settings]="$ADMIN?view=profile-settings"
  [admin-bulk]="$ADMIN?view=bulk"
  [admin-users]="$ADMIN?view=users"
  [admin-new-profile]="$ADMIN?view=new-profile"
  [admin-raw-allowlist]="$ADMIN?view=raw-allowlist"
  [admin-delete]="$ADMIN?view=delete"
)

names=("$@")
if [ "${#names[@]}" -eq 0 ]; then
  mapfile -t names < <(printf '%s\n' "${!SHOTS[@]}" | sort)
fi

failed=0
for name in "${names[@]}"; do
  shoot "$name" "${SHOTS[$name]}" || failed=1
done

echo "screenshots in $OUT"
exit $failed
