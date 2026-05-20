#!/usr/bin/env bash
# Optional. Exits 0 cleanly if creds are missing.
#
# Required env (only when actually downloading):
#   KAGGLE_USERNAME
#   KAGGLE_KEY

set -euo pipefail
CATCHEM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="$CATCHEM_ROOT/data/kaggle"
mkdir -p "$TARGET"

if [ -z "${KAGGLE_USERNAME:-}" ] || [ -z "${KAGGLE_KEY:-}" ]; then
  echo "[kaggle] credentials not set (KAGGLE_USERNAME/KAGGLE_KEY); skipping downloads."
  exit 0
fi

if ! command -v kaggle >/dev/null 2>&1; then
  echo "[kaggle] kaggle CLI not installed; attempting pip install"
  pip install --quiet kaggle || { echo "[kaggle] install failed; skipping"; exit 0; }
fi

# Write kaggle.json if not present
KAGGLE_CFG="${HOME}/.kaggle"
mkdir -p "$KAGGLE_CFG"
if [ ! -f "$KAGGLE_CFG/kaggle.json" ]; then
  printf '{"username":"%s","key":"%s"}\n' "$KAGGLE_USERNAME" "$KAGGLE_KEY" > "$KAGGLE_CFG/kaggle.json"
  chmod 600 "$KAGGLE_CFG/kaggle.json"
fi

DATASETS=(
  "ankurzing/sentiment-analysis-for-financial-news"
  "aaron7sun/stocknews"
  "borismarjanovic/price-volume-data-for-all-us-stocks-etfs"
  "equinxx/stock-tweets-for-sentiment-analysis-and-prediction"
)

ok=0
fail=0
for ds in "${DATASETS[@]}"; do
  dest="$TARGET/${ds//\//_}"
  mkdir -p "$dest"
  echo "[kaggle] downloading $ds → $dest"
  if kaggle datasets download -d "$ds" -p "$dest" --unzip --force >/dev/null 2>&1; then
    ok=$((ok + 1))
  else
    fail=$((fail + 1))
  fi
done

echo "[kaggle] done. ok=$ok fail=$fail target=$TARGET"
exit 0
