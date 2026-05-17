#!/usr/bin/env bash
# Build + install Catchem.app to /Applications.
#
# Why this exists:
#   The dev workflow defaults to launching Catchem from
#   `target/debug/bundle/macos/Catchem.app`. Each `cargo tauri build`
#   produces a new binary (different timestamps + build IDs), which
#   changes the bundle cdhash, which causes macOS TCC to forget the
#   user's Files-and-Folders grant and re-prompt on every launch.
#
#   Installing to `/Applications/Catchem.app` and launching from there
#   means the user only sees the TCC prompt ONCE per intentional
#   reinstall. Day-to-day work (poking around, restarting the app)
#   reuses the same cdhash and TCC stays happy.
#
# Usage:
#   bash desktop/catchem/scripts/install_catchem.sh
#       — defaults to a debug build (fast iteration)
#   bash desktop/catchem/scripts/install_catchem.sh --release
#       — release build (slower, optimized, ready for distribution)
#
# After this runs, open Catchem from Finder → /Applications/Catchem.app
# (or Spotlight). The first launch shows the macOS "Catchem would like
# to access your Desktop folder" dialog once; click Allow. Subsequent
# launches don't re-prompt.

set -euo pipefail

CATCHEM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$CATCHEM_DIR/../.." && pwd)"
PROFILE="debug"
BUILD_FLAG="--debug"
if [ "${1:-}" = "--release" ]; then
  PROFILE="release"
  BUILD_FLAG=""
fi

cd "$CATCHEM_DIR"

# 1. Build the React bundle (the actual UI — served by FastAPI inside
#    the .app's spawned sidecar).
echo "[install_catchem] rebuilding React bundle"
(cd "$REPO_ROOT/frontend" && npm install --silent --no-audit --no-fund && npm run build)

# 2. Cargo-tauri build the bundle. Skip if the binary is already current
#    AND the user passed --skip-build (saves ~20s on iteration).
if [ "${SKIP_BUILD:-0}" != "1" ]; then
  echo "[install_catchem] cargo-tauri build ($PROFILE)"
  cd src-tauri
  if [ -n "$BUILD_FLAG" ]; then
    cargo-tauri build $BUILD_FLAG
  else
    cargo-tauri build
  fi
  cd ..
fi

APP_SRC="$CATCHEM_DIR/src-tauri/target/$PROFILE/bundle/macos/Catchem.app"
if [ ! -d "$APP_SRC" ]; then
  echo "ERROR: built bundle missing at $APP_SRC" >&2
  exit 2
fi

# 3. Inject the privacy-keys plist + ad-hoc sign + strip quarantine.
echo "[install_catchem] injecting plist + signing"
bash "$CATCHEM_DIR/scripts/inject_info_plist.sh" "$APP_SRC"

# 4. Copy to /Applications. Use ditto (preserves resource forks +
#    metadata correctly across .app bundles — cp -R is *not* safe for
#    .app on macOS).
APP_DST="/Applications/Catchem.app"
echo "[install_catchem] installing to $APP_DST"
if [ -d "$APP_DST" ]; then
  # Don't delete the dest; we want LaunchServices to see this as an
  # in-place update (preserves TCC entries keyed by inode in some
  # configurations). `ditto --rsrc` overwrites in place.
  /usr/bin/ditto --rsrc "$APP_SRC" "$APP_DST"
else
  /usr/bin/ditto --rsrc "$APP_SRC" "$APP_DST"
fi

# 5. Re-register with LaunchServices so the menu bar + Spotlight pick
#    up the (possibly updated) bundle immediately.
/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister \
  -f -v "$APP_DST" >/dev/null 2>&1 || true

# 6. Strip quarantine on the installed copy too (the ditto may have
#    inherited it from the build output).
/usr/bin/xattr -dr com.apple.quarantine "$APP_DST" 2>/dev/null || true

echo
echo "[install_catchem] done."
echo "  installed:  $APP_DST"
echo "  open with:  open '$APP_DST'   (or launch from /Applications)"
echo
echo "First launch shows ONE macOS Files-and-Folders prompt (Desktop)."
echo "Click Allow — TCC remembers the choice for this bundle's signature."
echo "Subsequent launches won't re-prompt unless you rebuild."
