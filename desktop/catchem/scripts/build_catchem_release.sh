#!/usr/bin/env bash
# Produce a release-style Catchem.app + .dmg.
#
# Requires:
#   - Rust + cargo-tauri
#   - Apple SDK (Xcode command line tools)
#   - Optionally signing+notarization credentials:
#       APPLE_DEVELOPER_IDENTITY="Developer ID Application: NAME (TEAMID)"
#       APPLE_ID=you@example.com
#       APPLE_TEAM_ID=TEAMID
#       APPLE_APP_SPECIFIC_PASSWORD=xxxx-xxxx-xxxx-xxxx
#
# If any of the env vars above are missing, the build still produces an
# *unsigned* .app + .dmg. Document the placeholders in CATCHEM_RELEASE.md.
set -euo pipefail
CATCHEM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$CATCHEM_DIR/../.." && pwd)"

cd "$CATCHEM_DIR"

# 1. Build the fusion_stack React bundle into src/fusion_stack/static/app
(cd "$REPO_ROOT/frontend" && npm install --silent --no-audit --no-fund && npm run build)

# 2. Build the Catchem boot shim
(cd web && npm install --silent --no-audit --no-fund && npm run build)

# 3. Package the sidecar binary (PyInstaller --onedir)
bash "$CATCHEM_DIR/scripts/build_sidecar.sh"

# 4. Stage the sidecar inside src-tauri so tauri.conf.json `resources` picks it up.
mkdir -p src-tauri/resources/sidecar
rm -rf src-tauri/resources/sidecar/*
cp -R sidecar-out/fusion-stack-sidecar/* src-tauri/resources/sidecar/

# 5. Patch tauri.conf.json `resources` at build time (without editing the
#    checked-in file) by passing -c '{"bundle":{"resources":[...]}}'.
RES_PATCH=$(cat <<'JSON'
{
  "bundle": {
    "resources": [
      "resources/sidecar/**/*"
    ]
  }
}
JSON
)

# 6. Tauri release build. Optionally signs if APPLE_DEVELOPER_IDENTITY is set.
SIGN_FLAGS=()
if [ -n "${APPLE_DEVELOPER_IDENTITY:-}" ]; then
  echo "Signing with identity: $APPLE_DEVELOPER_IDENTITY"
  SIGN_FLAGS+=("--config" "$(echo "$RES_PATCH" | jq -c '. + {bundle: (.bundle + {macOS: {signingIdentity: env.APPLE_DEVELOPER_IDENTITY}})}')")
else
  echo "WARNING: no APPLE_DEVELOPER_IDENTITY — building UNSIGNED app"
  SIGN_FLAGS+=("--config" "$RES_PATCH")
fi

cd src-tauri
cargo-tauri build "${SIGN_FLAGS[@]}"

# 6a. Inject NSDesktopFolderUsageDescription (and related TCC keys) into the
# bundled Info.plist. Tauri 2 doesn't expose arbitrary plist entries via its
# config schema; without these keys the sidecar Python hangs forever the
# first time it tries to read .venv files on the user's Desktop. The
# inject script also re-signs the bundle if APPLE_DEVELOPER_IDENTITY is set
# (modifying the plist invalidates any prior signature).
APP_BUNDLE="$CATCHEM_DIR/src-tauri/target/release/bundle/macos/Catchem.app"
bash "$CATCHEM_DIR/scripts/inject_info_plist.sh" "$APP_BUNDLE"

echo
echo "Release build complete:"
echo "  Catchem.app  → $APP_BUNDLE"
echo "  Catchem.dmg  → $CATCHEM_DIR/src-tauri/target/release/bundle/dmg/Catchem_*.dmg"

# 7. Optional notarization
if [ -n "${APPLE_ID:-}" ] && [ -n "${APPLE_TEAM_ID:-}" ] && [ -n "${APPLE_APP_SPECIFIC_PASSWORD:-}" ]; then
  DMG="$(ls "$CATCHEM_DIR/src-tauri/target/release/bundle/dmg/"Catchem_*.dmg | head -1 || true)"
  if [ -n "$DMG" ]; then
    echo
    echo "Notarizing $DMG …"
    xcrun notarytool submit "$DMG" \
      --apple-id "$APPLE_ID" \
      --team-id "$APPLE_TEAM_ID" \
      --password "$APPLE_APP_SPECIFIC_PASSWORD" \
      --wait
    xcrun stapler staple "$DMG"
    echo "Notarized + stapled."
  fi
else
  echo
  echo "Skipping notarization (set APPLE_ID + APPLE_TEAM_ID + APPLE_APP_SPECIFIC_PASSWORD to enable)."
fi
