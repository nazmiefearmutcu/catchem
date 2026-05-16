#!/usr/bin/env bash
# fusion_stack one-command bootstrap.
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
#   9. initialize fusion_stack storage
#  10. run the chosen mode (default: replay_existing)
#  11. start the FastAPI server in the background
#  12. print a summary of where outputs/logs/results live

set -euo pipefail

FUSION_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$FUSION_ROOT"

# ── parse flags ───────────────────────────────────────────────────────────────
MODE="${FUSION_MODE:-replay_existing}"
WITH_ML="${FUSION_WITH_ML:-0}"
NO_API="${FUSION_NO_API:-0}"
MAX_RECORDS="${FUSION_MAX_RECORDS:-50}"
SKIP_BOOTSTRAP_RUN="${FUSION_SKIP_RUN:-0}"

for arg in "$@"; do
  case "$arg" in
    --with-ml) WITH_ML=1 ;;
    --no-api) NO_API=1 ;;
    --mode=*) MODE="${arg#*=}" ;;
    --max=*) MAX_RECORDS="${arg#*=}" ;;
    --skip-run) SKIP_BOOTSTRAP_RUN=1 ;;
    --help|-h)
      cat <<EOF
fusion_stack bootstrap

Usage: bash scripts/fusion_bootstrap_and_run.sh [--mode=replay_existing] [--with-ml]
                                                [--no-api] [--max=50] [--skip-run]

Env overrides:
  FUSION_MODE, FUSION_WITH_ML, FUSION_NO_API, FUSION_MAX_RECORDS, FUSION_SKIP_RUN,
  AWARENESS_REPO_PATH, NEWSIMPACT_REPO_PATH
EOF
      exit 0
      ;;
  esac
done

log() { printf "\033[36m[bootstrap]\033[0m %s\n" "$*"; }
warn() { printf "\033[33m[bootstrap]\033[0m %s\n" "$*" >&2; }
fail() { printf "\033[31m[bootstrap]\033[0m %s\n" "$*" >&2; exit 1; }

# ── 1+2+3 venv + installs ────────────────────────────────────────────────────
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

# ── 9. init storage and verify ───────────────────────────────────────────────
log "initializing fusion_stack storage + sanity checks"
export FUSION_MODE="$MODE"
# Force stubs for the bootstrap run unless ML extras are present
if [ "$WITH_ML" != "1" ]; then
  export FUSION_MODELS__USE_ML_STUBS=true
fi
python -m fusion_stack.cli bootstrap-init --skip-warm

# ── 10. optionally run the pipeline ─────────────────────────────────────────
if [ "$SKIP_BOOTSTRAP_RUN" != "1" ] && [ "$MODE" != "live_tail" ]; then
  log "running fusion-stack in mode=$MODE (max=$MAX_RECORDS)"
  python -m fusion_stack.cli run --mode "$MODE" --max-records "$MAX_RECORDS" || warn "run returned non-zero"
fi

# ── 11. background API server ────────────────────────────────────────────────
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

# ── 12. summary ──────────────────────────────────────────────────────────────
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
  guard:              OK (NewsImpact still quarantined)

Try:
  curl -s http://127.0.0.1:8087/healthz
  curl -s http://127.0.0.1:8087/metrics  | python -m json.tool
  curl -s http://127.0.0.1:8087/dashboard| python -m json.tool

────────────────────────────────────────────────────────────────────────────
EOF
