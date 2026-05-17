#!/usr/bin/env bash
# Wrap a pre-built Catchem.app into a Catchem.dmg.
# Tauri's bundle workflow already does this; this script exists for users
# who want to repackage a .app they signed elsewhere.
set -euo pipefail
CATCHEM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_PATH="${1:-$CATCHEM_DIR/src-tauri/target/release/bundle/macos/Catchem.app}"
OUT="${2:-$CATCHEM_DIR/Catchem.dmg}"

if [ ! -d "$APP_PATH" ]; then
  echo "ERROR: $APP_PATH not found. Run build_catchem_release.sh first." >&2
  exit 1
fi

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT
cp -R "$APP_PATH" "$TMPDIR/"
ln -s /Applications "$TMPDIR/Applications"

hdiutil create -fs HFS+ \
  -srcfolder "$TMPDIR" \
  -volname Catchem \
  -format UDZO \
  -o "$OUT" \
  -ov
echo "wrote $OUT"
