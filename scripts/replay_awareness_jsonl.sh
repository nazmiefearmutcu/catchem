#!/usr/bin/env bash
# Shortcut: run fusion-stack replay against the awareness data dir.
#
# Usage:
#   bash scripts/replay_awareness_jsonl.sh [--max=200]
#
# Env:
#   AWARENESS_REPO_PATH  (defaults to /Users/nazmi/Desktop/Projeler/proje/awareness)

set -euo pipefail
FUSION_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$FUSION_ROOT"

if [ ! -d ".venv" ]; then
  echo "[replay] venv missing; run bash scripts/fusion_bootstrap_and_run.sh first" >&2
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

export FUSION_MODE=replay_existing
export FUSION_MODELS__USE_ML_STUBS=true
python -m fusion_stack.cli run --mode replay_existing --max-records "$MAX"
