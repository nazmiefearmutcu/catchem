#!/usr/bin/env bash
# Build & run Catchem in dev mode.
# Pre-reqs:
#   - Repo bootstrapped: bash scripts/catchem_bootstrap_and_run.sh (creates .venv)
#   - Rust toolchain + cargo-tauri (cargo install create-tauri-app tauri-cli --version '^2.0')
#   - Node + npm
#
# Behavior:
#   - Builds the Catchem boot shim
#   - Spawns the FastAPI sidecar via Tauri's process manager
#   - Opens Catchem with the React UI loaded from http://127.0.0.1:8087
set -euo pipefail
CATCHEM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$CATCHEM_DIR/../.." && pwd)"
cd "$CATCHEM_DIR"

# 1. Sanity-check repo venv
if [ ! -x "$REPO_ROOT/.venv/bin/python" ]; then
  echo "ERROR: $REPO_ROOT/.venv/bin/python missing. Run bash scripts/catchem_bootstrap_and_run.sh first." >&2
  exit 1
fi

# 2. Sanity-check Rust + Tauri
command -v cargo >/dev/null || { echo "ERROR: cargo not found"; exit 1; }
command -v cargo-tauri >/dev/null || { echo "ERROR: cargo-tauri not found. cargo install create-tauri-app tauri-cli --version '^2.0'"; exit 1; }

# 3. Build the boot shim (instant)
(cd web && npm install --silent --no-audit --no-fund && npm run build)

# 4. Build the catchem React bundle (the real UI)
(cd "$REPO_ROOT/frontend" && npm install --silent --no-audit --no-fund && npm run build)

# 5. Run Tauri dev (will spawn the sidecar via lib.rs setup)
cd src-tauri
cargo-tauri dev "$@"
