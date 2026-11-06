#!/usr/bin/env bash
# Builds the desktop app and the CLI binary in the build image
# (images/build/run.sh takes care of getting there):
#
#   app/build.sh build     the debug binary
#                          -> app/src-tauri/target/debug/hardcode-app
#   app/build.sh cli [DIR] the standalone hardcode binary, with its stack
#                          -> dist/hardcode-linux-<arch> (or DIR/…)
#   app/build.sh wheel [DIR]
#                          the CLI's Python package, with its stack
#                          -> dist/hardcode_cli-<version>-py3-none-any.whl
#   app/build.sh bundle    the CLI as the app's sidecar, then the installers
#                          (.deb, .rpm, AppImage)
#                          -> app/src-tauri/target/release/bundle/
#                          and then, on a Linux desktop, the launcher below
#   app/build.sh desktop   a launcher for the release build in your app grid
#                          (Linux; your user only, run on this machine)
#   app/build.sh shell     a shell in the image
#
# tests/app.py tests it: the Rust unit tests, a build, a headless launch, and
# the launcher.
#
# The version is the one in the files (release/release.py stamp writes a
# release's). HARDCODE_UPDATER_CONFIG names more Tauri configuration for a
# bundle: the release workflow's, with the updater (release/release.py
# updater-config). Nothing here signs: the release signs the installers
# afterwards, in a job that builds nothing (release/release.py sign). The app isn't run
# from the container, since it drives Docker on the machine it runs on: start
# it from the launcher, install the .deb, or run the AppImage.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TAURI="$ROOT/app/src-tauri"

# Every build is --locked: Cargo.lock as it is, never resolved afresh, so no
# crate enters that the age gate (maintenance/pins.py ages) hasn't seen.
build() {
  cargo build --quiet --locked
  ls -la "$TAURI/target/debug/hardcode-app"
}

# pyinstaller_cli <name> <directory>: the CLI as one executable, <directory>/<name>,
# with the stack it runs profiles with (compose.yaml, the images' build
# contexts, the template) staged into its package first, and carried inside
# at the same place. --add-data's source is absolute: a relative one would
# be read from the spec's folder.
pyinstaller_cli() {
  local name="$1" directory="$2"

  (cd "$ROOT" && PYTHONPATH=cli python3 -m hardcode_cli.stack cli/hardcode_cli/stack)

  (cd "$ROOT" && pyinstaller --onefile --name "$name" --paths cli cli/hardcode \
    --add-data "$ROOT/cli/hardcode_cli/stack:hardcode_cli/stack" \
    --distpath "$directory" \
    --workpath "$TMPDIR/pyinstaller" \
    --specpath "$TMPDIR/pyinstaller" \
    --log-level WARN)
}

# cli [directory]: the standalone binary, in dist/ unless another directory
# is named (tests/cli.py builds one in a scratch directory).
cli() {
  local directory="${1:-$ROOT/dist}"
  local arch binary
  arch="$(uname -m | sed 's/x86_64/x64/; s/aarch64/arm64/')"
  binary="$directory/hardcode-linux-$arch"

  pyinstaller_cli "hardcode-linux-$arch" "$directory"
  "$binary" --version
  ls -la "$binary"
}

# wheel [directory]: the CLI's Python package, in dist/ unless another
# directory is named. setup.py stages the stack into it; uv builds it with
# the setuptools the build image's lock installed, so nothing is fetched.
# setuptools works in the source folder, and what it leaves there goes
# afterwards.
wheel() {
  local directory="${1:-$ROOT/dist}"

  uv build \
    --quiet \
    --wheel \
    --no-build-isolation \
    --python "$(command -v python3)" \
    --out-dir "$directory" \
    "$ROOT/cli"
  rm -rf "$ROOT/cli/build" "$ROOT/cli/hardcode_cli.egg-info"
  ls -la "$directory"/hardcode_cli-*.whl
}

bundle() {
  # Tauri looks for the sidecar under the name of the platform it builds for.
  local triple sidecar
  triple="$(rustc -vV | sed -n 's/^host: //p')"
  sidecar="binaries/hardcode-$triple"

  pyinstaller_cli hardcode "$TMPDIR/sidecar"
  mkdir -p binaries
  cp "$TMPDIR/sidecar/hardcode" "$sidecar"
  echo "sidecar: $sidecar ($("$sidecar" --version))"

  # A release's configuration on top: the updater.
  local configs=(--config src-tauri/tauri.release.conf.json)
  if [ -n "${HARDCODE_UPDATER_CONFIG:-}" ]; then
    configs+=(--config "$HARDCODE_UPDATER_CONFIG")
  fi

  # The AppImage's tools: the Tauri CLI downloads them unchecked unless its
  # tools folder (XDG_CACHE_HOME/tauri) has them. A fresh folder each time,
  # with the build image's pinned copies (images/build/Dockerfile), copied
  # since the CLI patches linuxdeploy in place; the AppImage runtime, taken
  # out of the pinned plugin, is with them.
  # TOOLS_CACHE is global, for the trap, which runs after this returns.
  local tools
  TOOLS_CACHE="$(mktemp -d)"
  trap 'rm -rf "$TOOLS_CACHE"' EXIT
  tools="$TOOLS_CACHE/tauri"
  mkdir "$tools"
  cp /opt/tauri-tools/* "$tools/"

  # The runtime the AppImage starts with, which the AppImage plugin's
  # appimagetool would otherwise download, unchecked, from a moving release:
  # the build image's, taken out of the pinned plugin itself.
  local runtime
  runtime="$tools/runtime-$(uname -m)"
  if [ ! -f "$runtime" ]; then
    echo "no AppImage runtime at $runtime: the build image takes it out of the pinned plugin" >&2
    return 1
  fi

  # Arguments after -- go to cargo build.
  (cd "$ROOT/app" && XDG_CACHE_HOME="$TOOLS_CACHE" LDAI_RUNTIME_FILE="$runtime" cargo tauri build "${configs[@]}" -- --locked)

  # Afterwards, nothing but those and the two plugin scripts the CLI carries:
  # anything else is a tool it fetched itself (a newer CLI naming a new
  # one), unchecked, so the bundle is refused until that tool is pinned.
  local fetched
  fetched="$(
    cd "$tools" \
      && ls -A \
      | grep -vxF -f <(ls -A /opt/tauri-tools) \
      | grep -vxF -e linuxdeploy-plugin-gtk.sh -e linuxdeploy-plugin-gstreamer.sh \
      || true
  )"
  if [ -n "$fetched" ]; then
    echo "the Tauri CLI fetched AppImage tools of its own, unchecked: $fetched" >&2
    echo "pin them in images/build/Dockerfile, beside the others" >&2
    return 1
  fi

  echo
  echo "installers:"
  find target/release/bundle -maxdepth 2 -type f \( -name '*.deb' -o -name '*.rpm' -o -name '*.AppImage' -o -name '*.sig' \) \
    -exec ls -la {} \;
}

# desktop: a launcher for the release build in the user's app grid, so that
# GNOME (and other desktops) show HardCode's icon in the dock. They match a
# window, whose app id is "hardcode-app", to an installed .desktop file, and
# on Wayland ignore the icon a window sets itself. The launcher points at
# target/release, so every bundle is picked up without installing anything
# again. It has the .deb's file name, HardCode.desktop, so a system install
# beside it doesn't make two entries: the user's one wins.
#
# HARDCODE_APP_BINARY points it at another build (tests/app.py uses the
# debug one); XDG_DATA_HOME moves where it is written, as it does for every
# desktop.
desktop() {
  local data="${XDG_DATA_HOME:-$HOME/.local/share}"
  local binary="${HARDCODE_APP_BINARY:-$TAURI/target/release/hardcode-app}"
  local launcher="$data/applications/HardCode.desktop"
  local size

  if [ "$(uname -s)" != "Linux" ]; then
    echo "the launcher is for Linux desktops; on macOS and Windows install the app's bundle"
    return 0
  fi

  if [ ! -x "$binary" ]; then
    echo "no build at $binary yet: app/build.sh bundle" >&2
    return 1
  fi

  # The icons, under the name the launcher (and the .deb) gives them.
  for size in 32x32 128x128; do
    install -D -m 0644 "$TAURI/icons/$size.png" "$data/icons/hicolor/$size/apps/hardcode-app.png"
  done
  install -D -m 0644 "$TAURI/icons/128x128@2x.png" "$data/icons/hicolor/256x256/apps/hardcode-app.png"

  # Exec is quoted, as the desktop entry format wants a path with spaces.
  install -d "$data/applications"
  cat > "$launcher" <<DESKTOP
[Desktop Entry]
Type=Application
Name=HardCode
Comment=Sandboxed development environments (the build in $TAURI/target)
Exec="$binary"
Icon=hardcode-app
StartupWMClass=hardcode-app
Categories=Development;
Terminal=false
DESKTOP
  chmod 0644 "$launcher"

  # Desktops notice new files by themselves; these just make it immediate.
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q "$data/applications" || true
  fi
  if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t "$data/icons/hicolor" || true
  fi

  echo "launcher: $launcher -> $binary"
}

# On this machine: the launcher is written here, where the desktop is; a
# bundle is built in the image first. Everything else runs in the image.
if [ -z "${HARDCODE_BUILD_IMAGE:-}" ]; then
  case "${1:-}" in
    desktop)
      desktop
      exit
      ;;
    bundle)
      "$ROOT/images/build/run.sh" "$ROOT/app/build.sh" bundle
      if [ "$(uname -s)" = "Linux" ] && [ -n "${XDG_CURRENT_DESKTOP:-}${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
        desktop
      fi
      exit
      ;;
    *)
      exec "$ROOT/images/build/run.sh" "$ROOT/app/build.sh" "$@"
      ;;
  esac
fi

cd "$TAURI"

case "${1:-}" in
  build) build ;;
  cli) cli "${2:-}" ;;
  wheel) wheel "${2:-}" ;;
  bundle) bundle ;;
  desktop) desktop ;;
  shell) exec bash ;;
  *)
    # The header comment: what each command does.
    awk 'NR > 1 && /^#/ { print; next } NR > 1 { exit }' "$0"
    exit 2
    ;;
esac
