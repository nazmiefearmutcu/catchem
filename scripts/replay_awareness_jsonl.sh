#!/usr/bin/env bash
# Shortcut: run catchem replay against the awareness data dir.
#
# Usage:
#   bash scripts/replay_awareness_jsonl.sh [--max=200]
#
# Env:
#   AWARENESS_REPO_PATH  (defaults to /Users/nazmi/Desktop/Projeler/proje/awareness)

set -euo pipefail
CATCHEM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$CATCHEM_ROOT"

if [ ! -d ".venv" ]; then
  echo "[replay] venv missing; run bash scripts/catchem_bootstrap_and_run.sh first" >&2
  exit 1
fi
# shellcheck disable=SC1091
source ".venv/bin/activate"

MAX=200
for arg in "$@"; do
  case "$arg" in
    --max=*) MAX="${arg#*=}" ;;
  esac
done

export CATCHEM_MODE=replay_existing
export CATCHEM_MODELS__USE_ML_STUBS=true
python -m catchem.cli run --mode replay_existing --max-records "$MAX"
