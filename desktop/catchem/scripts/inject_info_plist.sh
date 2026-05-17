#!/usr/bin/env bash
# Inject TCC privacy descriptions into the freshly-built Catchem.app's
# Info.plist.
#
# Why this exists:
#   Tauri 2's tauri.conf.json schema only exposes a fixed set of macOS bundle
#   keys — it has no first-class field for arbitrary Info.plist entries
#   (NSDesktopFolderUsageDescription, NSDocumentsFolderUsageDescription, etc).
#   Without those keys, macOS TCC silently blocks the sidecar Python from
#   reading any file under ~/Desktop, ~/Documents, or ~/Downloads — the
#   open() syscall hangs forever instead of returning EPERM. Symptom: the
#   sidecar Python process spawns, idles at 0% CPU, never writes a single
#   byte to its log, never binds port 8087. From the UI, the Catchem window
#   appears stuck on "Starting local sidecar…".
#
#   This script runs `plutil -replace` post-build to add the keys. If a
#   codesigning identity is provided, it also re-signs the bundle so the
#   modified plist doesn't invalidate the signature.
#
# Usage:
#   inject_info_plist.sh <path/to/Catchem.app>
#
# Env vars (optional):
#   APPLE_DEVELOPER_IDENTITY  — if set, re-sign the bundle after injection.
#   CATCHEM_ENTITLEMENTS      — entitlements plist to use for re-signing.
#                               Defaults to <repo>/desktop/catchem/src-tauri/Entitlements.plist.

set -euo pipefail

APP_PATH="${1:-}"
if [ -z "$APP_PATH" ]; then
  echo "usage: $0 <path/to/Catchem.app>" >&2
  exit 2
fi
if [ ! -d "$APP_PATH" ]; then
  echo "ERROR: not a directory: $APP_PATH" >&2
  exit 2
fi

PLIST="$APP_PATH/Contents/Info.plist"
if [ ! -f "$PLIST" ]; then
  echo "ERROR: Info.plist missing: $PLIST" >&2
  exit 2
fi

echo "[inject_info_plist] target: $PLIST"

# Use -replace so re-runs are idempotent (-insert errors if the key exists).
/usr/bin/plutil -replace NSDesktopFolderUsageDescription \
  -string "Catchem reads news articles, the local fusion_stack repository, and exports/imports analysis bundles from your Desktop." \
  "$PLIST"
/usr/bin/plutil -replace NSDocumentsFolderUsageDescription \
  -string "Catchem stores analysis exports and reads pasted news files from Documents." \
  "$PLIST"
/usr/bin/plutil -replace NSDownloadsFolderUsageDescription \
  -string "Catchem may save analysis bundles to Downloads." \
  "$PLIST"
/usr/bin/plutil -replace NSAppleEventsUsageDescription \
  -string "Catchem uses AppleEvents to coordinate the local fusion_stack sidecar process." \
  "$PLIST"
# Hint Gatekeeper that the app launches a child process — purely cosmetic
# but quiets a launchd warning on first run.
/usr/bin/plutil -replace LSUIElement -bool NO "$PLIST"

echo "[inject_info_plist] privacy keys inserted:"
/usr/bin/plutil -p "$PLIST" | grep -E 'NS(Desktop|Documents|Downloads|AppleEvents)' | sed 's/^/  /'

# Force LaunchServices to re-read the bundle so the modified plist takes
# effect on the *next* `open Catchem.app`.
/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister -f "$APP_PATH" >/dev/null 2>&1 || true

# If a signing identity is available, re-sign the bundle. Modifying Info.plist
# invalidates the existing signature, so unsigned-but-modified bundles work
# fine on dev machines (Gatekeeper warns once) but signed releases need a
# fresh signature.
if [ -n "${APPLE_DEVELOPER_IDENTITY:-}" ]; then
  ENT="${CATCHEM_ENTITLEMENTS:-$(cd "$(dirname "$0")/.." && pwd)/src-tauri/Entitlements.plist}"
  if [ ! -f "$ENT" ]; then
    echo "ERROR: entitlements file not found: $ENT" >&2
    exit 2
  fi
  echo "[inject_info_plist] re-signing with $APPLE_DEVELOPER_IDENTITY (entitlements=$ENT)"
  /usr/bin/codesign --force --deep --options runtime \
    --entitlements "$ENT" \
    --sign "$APPLE_DEVELOPER_IDENTITY" \
    "$APP_PATH"
  /usr/bin/codesign --verify --deep --strict --verbose=2 "$APP_PATH"
else
  echo "[inject_info_plist] no APPLE_DEVELOPER_IDENTITY set — bundle remains unsigned"
fi

echo "[inject_info_plist] done"
