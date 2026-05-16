#!/usr/bin/env bash
# fusion_stack one-command bootstrap (v2 — builds the premium UI).
#
# This script is idempotent. Re-running is safe.
#
# Steps:
#   1. resolve project root
#   2. create/refresh local Python venv via uv
#   3. install fusion_stack + dev deps
#   4. install Awareness in editable mode (if available)
#   5. verify both repo paths
#   6. verify NewsImpact governance guard
#   7. (optional) warm HF model cache
#   8. (optional) attempt Kaggle dataset downloads
#   9. install frontend npm deps if needed and build the SPA into src/fusion_stack/static/app
#  10. initialize fusion_stack storage
#  11. run the chosen mode (default: replay_existing)
#  12. start the FastAPI server in the background (serves the premium UI at /)
#  13. print a summary of where outputs/logs/results live

set -euo pipefail

FUSION_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$FUSION_ROOT"

# ── flags ────────────────────────────────────────────────────────────────────
MODE="${FUSION_MODE:-replay_existing}"
WITH_ML="${FUSION_WITH_ML:-0}"
NO_API="${FUSION_NO_API:-0}"
MAX_RECORDS="${FUSION_MAX_RECORDS:-50}"
SKIP_BOOTSTRAP_RUN="${FUSION_SKIP_RUN:-0}"
SKIP_FRONTEND_BUILD="${FUSION_SKIP_FRONTEND_BUILD:-0}"
DEV_UI="${FUSION_DEV_UI:-0}"

for arg in "$@"; do
  case "$arg" in
    --with-ml) WITH_ML=1 ;;
    --no-api) NO_API=1 ;;
    --mode=*) MODE="${arg#*=}" ;;
    --max=*) MAX_RECORDS="${arg#*=}" ;;
    --skip-run) SKIP_BOOTSTRAP_RUN=1 ;;
    --skip-frontend-build) SKIP_FRONTEND_BUILD=1 ;;
    --dev-ui) DEV_UI=1 ;;
    --help|-h)
      cat <<EOF
fusion_stack bootstrap (v2)

Usage: bash scripts/fusion_bootstrap_and_run.sh [flags]

Flags:
  --mode=replay_existing|production_safe|live_tail|research_diagnostic
  --max=N                Record cap for the replay pass (default 50)
  --with-ml              Install the optional torch/transformers stack
  --no-api               Do not start the FastAPI server
  --skip-run             Setup only; do not run replay
  --skip-frontend-build  Reuse existing built bundle (faster restarts)
  --dev-ui               Print the Vite dev-server command and exit
                         (use this for hot-reload UI work alongside the API)

Env overrides:
  FUSION_MODE, FUSION_WITH_ML, FUSION_NO_API, FUSION_MAX_RECORDS,
  FUSION_SKIP_RUN, FUSION_SKIP_FRONTEND_BUILD, FUSION_DEV_UI,
  AWARENESS_REPO_PATH, NEWSIMPACT_REPO_PATH
EOF
      exit 0
      ;;
  esac
done

log() { printf "\033[36m[bootstrap]\033[0m %s\n" "$*"; }
warn() { printf "\033[33m[bootstrap]\033[0m %s\n" "$*" >&2; }
fail() { printf "\033[31m[bootstrap]\033[0m %s\n" "$*" >&2; exit 1; }

# ── 1+2+3 venv + python installs ─────────────────────────────────────────────
if ! command -v uv >/dev/null 2>&1; then
  warn "uv not found on PATH. Falling back to python3 -m venv + pip."
  USE_UV=0
else
  USE_UV=1
fi

if [ ! -d "$FUSION_ROOT/.venv" ]; then
  log "creating venv at $FUSION_ROOT/.venv"
  if [ "$USE_UV" = "1" ]; then
    uv venv --python 3.13 --seed
  else
    python3 -m venv .venv
  fi
fi

# shellcheck disable=SC1091
source "$FUSION_ROOT/.venv/bin/activate"

log "installing fusion_stack (editable, dev extras)"
if [ "$USE_UV" = "1" ]; then
  uv pip install -e ".[dev]" --quiet
else
  pip install --quiet -e ".[dev]"
fi

if [ "$WITH_ML" = "1" ]; then
  log "installing optional ML extras (torch + transformers + sentence-transformers)"
  if [ "$USE_UV" = "1" ]; then
    uv pip install -e ".[ml]" --quiet || warn "ML extras failed; continuing with stubs"
  else
    pip install --quiet -e ".[ml]" || warn "ML extras failed; continuing with stubs"
  fi
fi

# ── 4. install Awareness editable (best-effort) ──────────────────────────────
AWARENESS_REPO_PATH="${AWARENESS_REPO_PATH:-/Users/nazmi/Desktop/Projeler/proje/awareness}"
NEWSIMPACT_REPO_PATH="${NEWSIMPACT_REPO_PATH:-/Users/nazmi/Desktop/Projeler/proje/merged_news}"

if [ -d "$AWARENESS_REPO_PATH" ] && [ -f "$AWARENESS_REPO_PATH/pyproject.toml" ]; then
  log "installing awareness in editable mode from $AWARENESS_REPO_PATH"
  if [ "$USE_UV" = "1" ]; then
    uv pip install -e "$AWARENESS_REPO_PATH" --quiet || warn "awareness install failed (continuing — fusion_stack reads JSONL directly)"
  else
    pip install --quiet -e "$AWARENESS_REPO_PATH" || warn "awareness install failed (continuing — fusion_stack reads JSONL directly)"
  fi
else
  warn "awareness repo not at $AWARENESS_REPO_PATH — fusion_stack will still run via JSONL replay"
fi

# ── 5. verify paths ──────────────────────────────────────────────────────────
[ -d "$AWARENESS_REPO_PATH" ] || warn "awareness repo missing: $AWARENESS_REPO_PATH"
[ -d "$NEWSIMPACT_REPO_PATH" ] || warn "newsimpact repo missing: $NEWSIMPACT_REPO_PATH"

# ── 6. NewsImpact governance guard ──────────────────────────────────────────
log "verifying NewsImpact quarantine state"
if ! python "$FUSION_ROOT/scripts/verify_newsimpact_guard.py" "$NEWSIMPACT_REPO_PATH"; then
  fail "NewsImpact guard check failed — refusing to proceed"
fi

# ── 7. optional HF warm ──────────────────────────────────────────────────────
if [ "$WITH_ML" = "1" ]; then
  log "warming Hugging Face model caches (this may take several minutes)"
  python "$FUSION_ROOT/scripts/warm_hf_models.py" || warn "warm-cache had issues; pipeline will fall back to stubs"
fi

# ── 8. optional Kaggle ───────────────────────────────────────────────────────
log "checking for Kaggle credentials (optional)"
bash "$FUSION_ROOT/scripts/download_optional_kaggle_assets.sh" || true

# ── 9. frontend ──────────────────────────────────────────────────────────────
if [ "$DEV_UI" = "1" ]; then
  log "dev-ui flag set — start the Vite dev server manually:"
  echo "    (cd frontend && npm install && npm run dev)"
  echo "It will proxy /ui/* to the FastAPI server on 127.0.0.1:8087."
fi

BUNDLE_INDEX="$FUSION_ROOT/src/fusion_stack/static/app/index.html"
NEED_BUILD=1
if [ "$SKIP_FRONTEND_BUILD" = "1" ] && [ -f "$BUNDLE_INDEX" ]; then
  log "skipping frontend build (--skip-frontend-build, bundle exists)"
  NEED_BUILD=0
fi

if [ "$NEED_BUILD" = "1" ]; then
  if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
    if [ ! -d "$FUSION_ROOT/frontend/node_modules" ]; then
      log "installing frontend npm dependencies (one-time, ~30s)"
      (cd "$FUSION_ROOT/frontend" && npm install --silent --no-audit --no-fund) || warn "npm install failed; UI will fall back to placeholder page"
    fi
    log "building premium UI bundle into src/fusion_stack/static/app"
    if (cd "$FUSION_ROOT/frontend" && npm run build 2>&1 | tail -10); then
      log "UI bundle ready ($(du -sh "$FUSION_ROOT/src/fusion_stack/static/app" | cut -f1))"
    else
      warn "UI build failed — / will serve the placeholder page; /legacy is still available"
    fi
  else
    warn "Node/npm not on PATH — premium UI not built. Install Node 20+ and re-run."
    warn "Meanwhile, the API is fully functional and /legacy serves the vanilla dashboard."
  fi
fi

# ── 10. init storage and verify ──────────────────────────────────────────────
log "initializing fusion_stack storage + sanity checks"
export FUSION_MODE="$MODE"
if [ "$WITH_ML" != "1" ]; then
  export FUSION_MODELS__USE_ML_STUBS=true
fi
python -m fusion_stack.cli bootstrap-init --skip-warm

# ── 11. optionally run the pipeline ─────────────────────────────────────────
if [ "$SKIP_BOOTSTRAP_RUN" != "1" ] && [ "$MODE" != "live_tail" ]; then
  log "running fusion-stack in mode=$MODE (max=$MAX_RECORDS)"
  python -m fusion_stack.cli run --mode "$MODE" --max-records "$MAX_RECORDS" || warn "run returned non-zero"
fi

# ── 12. background API server ────────────────────────────────────────────────
PID_FILE="$FUSION_ROOT/data/logs/api.pid"
mkdir -p "$FUSION_ROOT/data/logs"
if [ "$NO_API" != "1" ]; then
  if [ -f "$PID_FILE" ]; then
    OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "${OLD_PID:-}" ] && kill -0 "$OLD_PID" 2>/dev/null; then
      log "API already running with PID $OLD_PID; not starting another"
    else
      rm -f "$PID_FILE"
    fi
  fi
  if [ ! -f "$PID_FILE" ]; then
    log "starting API in background"
    nohup python -m fusion_stack.cli serve >"$FUSION_ROOT/data/logs/api.out" 2>&1 &
    echo $! >"$PID_FILE"
    sleep 1
    if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      log "API up. PID=$(cat "$PID_FILE")  logs=$FUSION_ROOT/data/logs/api.out"
    else
      warn "API failed to start; check data/logs/api.out"
    fi
  fi
fi

# ── 13. summary ──────────────────────────────────────────────────────────────
cat <<EOF

────────────────────────────────────────────────────────────────────────────
fusion_stack bootstrap complete

  mode:               $MODE
  fusion root:        $FUSION_ROOT
  outputs:            $FUSION_ROOT/data/results
  sqlite:             $FUSION_ROOT/data/db/fusion.sqlite3
  logs:               $FUSION_ROOT/data/logs/
  api log:            $FUSION_ROOT/data/logs/api.out
  awareness data:     $AWARENESS_REPO_PATH/data/jsonl
  ui bundle:          $FUSION_ROOT/src/fusion_stack/static/app
  guard:              OK (NewsImpact still quarantined)

Open:
  http://127.0.0.1:8087/             ← premium analyst UI
  http://127.0.0.1:8087/legacy       ← vanilla dashboard (kept for fallback)
  http://127.0.0.1:8087/docs         ← OpenAPI
  http://127.0.0.1:8087/ui/summary   ← landing JSON

Try:
  curl -s http://127.0.0.1:8087/healthz
  curl -s http://127.0.0.1:8087/ui/summary | python -m json.tool | head -30
  curl -s http://127.0.0.1:8087/ui/benchmark/latest | python -m json.tool | head -20

────────────────────────────────────────────────────────────────────────────
EOF
