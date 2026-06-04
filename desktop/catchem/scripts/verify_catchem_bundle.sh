#!/usr/bin/env bash
# Verify that a Catchem.app bundle is safe to install or ship.
set -euo pipefail

APP_PATH="${1:-}"
if [ -z "$APP_PATH" ]; then
  echo "usage: $0 <path/to/Catchem.app>" >&2
  exit 2
fi
if [ ! -d "$APP_PATH" ]; then
  echo "ERROR: bundle missing: $APP_PATH" >&2
  exit 2
fi

PLIST="$APP_PATH/Contents/Info.plist"
if [ ! -f "$PLIST" ]; then
  echo "ERROR: Info.plist missing: $PLIST" >&2
  exit 2
fi

plist_get() {
  /usr/bin/plutil -extract "$1" raw -o - "$PLIST" 2>/dev/null || true
}

BUNDLE_ID="$(plist_get CFBundleIdentifier)"
BUNDLE_NAME="$(plist_get CFBundleName)"
EXECUTABLE="$(plist_get CFBundleExecutable)"
SIDECAR="$APP_PATH/Contents/Resources/sidecar/catchem-sidecar"
TAXONOMY="$APP_PATH/Contents/Resources/sidecar/configs/taxonomy.yaml"

echo "[verify_catchem_bundle] app=$APP_PATH"
echo "[verify_catchem_bundle] CFBundleIdentifier=$BUNDLE_ID"
echo "[verify_catchem_bundle] CFBundleName=$BUNDLE_NAME"
echo "[verify_catchem_bundle] CFBundleExecutable=$EXECUTABLE"

if [ "$BUNDLE_ID" != "com.catchem.app" ]; then
  echo "ERROR: unexpected CFBundleIdentifier: $BUNDLE_ID" >&2
  exit 1
fi
if [ "$BUNDLE_NAME" != "Catchem" ]; then
  echo "ERROR: unexpected CFBundleName: $BUNDLE_NAME" >&2
  exit 1
fi
if [ "$EXECUTABLE" != "catchem" ]; then
  echo "ERROR: unexpected CFBundleExecutable: $EXECUTABLE" >&2
  exit 1
fi

if /usr/bin/plutil -convert xml1 -o - "$PLIST" | /usr/bin/grep -Eiq 'fusion_stack|fusionstack|com\.fusion'; then
  echo "ERROR: Info.plist still contains fusion_stack/fusionstack/com.fusion reference(s)" >&2
  exit 1
fi
if /usr/bin/grep -RIEiq 'fusion_stack|fusionstack|com\.fusion' "$APP_PATH/Contents/Resources" 2>/dev/null; then
  echo "ERROR: Resources still contain fusion_stack/fusionstack/com.fusion reference(s)" >&2
  exit 1
fi

if [ ! -f "$SIDECAR" ]; then
  echo "ERROR: bundled sidecar missing: $SIDECAR" >&2
  exit 1
fi
if [ ! -x "$SIDECAR" ]; then
  echo "ERROR: bundled sidecar is not executable: $SIDECAR" >&2
  exit 1
fi
if [ ! -f "$TAXONOMY" ]; then
  echo "ERROR: bundled taxonomy config missing: $TAXONOMY" >&2
  exit 1
fi

echo "[verify_catchem_bundle] sidecar=$SIDECAR"
echo "[verify_catchem_bundle] taxonomy=$TAXONOMY"
echo "[verify_catchem_bundle] ok"
