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
RELEASE=0
if [ "${1:-}" = "--release" ]; then
  PROFILE="release"
  BUILD_FLAG=""
  RELEASE=1
fi

cleanup_staged_sidecar() {
  rm -rf "$CATCHEM_DIR/src-tauri/resources"
}
trap cleanup_staged_sidecar EXIT

cd "$CATCHEM_DIR"

# 1. Build the bundle. Release installs must go through the release script
#    because it stages the PyInstaller sidecar into Contents/Resources.
if [ "${SKIP_BUILD:-0}" != "1" ]; then
  if [ "$RELEASE" -eq 1 ]; then
    echo "[install_catchem] building verified release bundle"
    bash "$CATCHEM_DIR/scripts/build_catchem_release.sh"
  else
    echo "[install_catchem] rebuilding React bundle"
    (cd "$REPO_ROOT/frontend" && npm install --silent --no-audit --no-fund && npm run build)

    echo "[install_catchem] packaging bundled sidecar"
    bash "$CATCHEM_DIR/scripts/build_sidecar.sh"
    mkdir -p src-tauri/resources/sidecar
    rm -rf src-tauri/resources/sidecar/*
    cp -R sidecar-out/catchem-sidecar/* src-tauri/resources/sidecar/

    echo "[install_catchem] cargo-tauri build ($PROFILE)"
    cd src-tauri
    cargo-tauri build $BUILD_FLAG --config '{"bundle":{"targets":["app"],"resources":{"resources/sidecar/":"sidecar"}}}'
    cd ..
  fi
fi

APP_SRC="$CATCHEM_DIR/src-tauri/target/$PROFILE/bundle/macos/Catchem.app"
if [ ! -d "$APP_SRC" ]; then
  echo "ERROR: built bundle missing at $APP_SRC" >&2
  exit 2
fi

# 3. Inject the privacy-keys plist + ad-hoc sign + strip quarantine.
echo "[install_catchem] injecting plist + signing"
bash "$CATCHEM_DIR/scripts/inject_info_plist.sh" "$APP_SRC"
bash "$CATCHEM_DIR/scripts/verify_catchem_bundle.sh" "$APP_SRC"

# 4. Copy to /Applications. Use ditto (preserves resource forks +
#    metadata correctly across .app bundles — cp -R is *not* safe for
#    .app on macOS).
APP_DST="/Applications/Catchem.app"
echo "[install_catchem] installing to $APP_DST"
if [ -d "$APP_DST" ]; then
  BACKUP="/Applications/Catchem.backup.$(/bin/date +%Y%m%d-%H%M%S)"
  echo "[install_catchem] backing up existing app to $BACKUP"
  /usr/bin/ditto --rsrc "$APP_DST" "$BACKUP"
  /bin/rm -rf "$APP_DST"

  # Rotate old backups: keep the 2 most recent (this one + one prior safety
  # net), delete older. Each Catchem.app is ~360MB; without this guard the
  # /Applications volume hits "No space left on device" after ~25 installs.
  KEEP=2
  TOTAL=$(ls -1d /Applications/Catchem.backup.* 2>/dev/null | wc -l | tr -d ' ')
  if [ "$TOTAL" -gt "$KEEP" ]; then
    PRUNE=$((TOTAL - KEEP))
    echo "[install_catchem] pruning $PRUNE old backup(s) (keeping $KEEP most recent)"
    ls -1d /Applications/Catchem.backup.* 2>/dev/null | sort | head -n "$PRUNE" | while read -r OLD; do
      /bin/rm -rf "$OLD"
    done
  fi
fi
/usr/bin/ditto --rsrc "$APP_SRC" "$APP_DST"
bash "$CATCHEM_DIR/scripts/verify_catchem_bundle.sh" "$APP_DST"

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
