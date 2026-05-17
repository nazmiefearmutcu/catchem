#!/usr/bin/env bash
# Catchem benchmark asset puller. Optional — exits 0 cleanly when creds
# are absent so the bootstrap path never blocks on Kaggle.
#
# Required env (only when actually downloading):
#   KAGGLE_USERNAME
#   KAGGLE_KEY
#
# Downloads + unzips the catalog into data/kaggle/<slug>/ and optionally
# emits a normalized data/golden/extended.jsonl that
# `fusion-stack benchmark --golden --extended <path>` can ingest.
#
# Dataset URLs (documented for reviewers):
#   https://www.kaggle.com/datasets/ankurzing/sentiment-analysis-for-financial-news
#   https://www.kaggle.com/datasets/notlucasp/financial-news-headlines
#   https://www.kaggle.com/datasets/sbhatti/financial-sentiment-analysis
#   https://www.kaggle.com/datasets/jeet2016/us-financial-news-articles
#   https://www.kaggle.com/datasets/aaron7sun/stocknews
#   https://www.kaggle.com/datasets/equinxx/stock-tweets-for-sentiment-analysis-and-prediction
#   https://www.kaggle.com/datasets/omermetinn/tweets-about-the-top-companies-from-2015-to-2020
#   https://www.kaggle.com/datasets/borismarjanovic/price-volume-data-for-all-us-stocks-etfs

set -euo pipefail
FUSION_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="$FUSION_ROOT/data/kaggle"
GOLDEN="$FUSION_ROOT/data/golden"
mkdir -p "$TARGET" "$GOLDEN"

if [ -z "${KAGGLE_USERNAME:-}" ] || [ -z "${KAGGLE_KEY:-}" ]; then
  echo "[kaggle] credentials not set (KAGGLE_USERNAME / KAGGLE_KEY); skipping downloads."
  echo "[kaggle] To enable:"
  echo "  1. Sign in to https://www.kaggle.com/settings → 'Create API Token'"
  echo "  2. export KAGGLE_USERNAME=<your-username>"
  echo "  3. export KAGGLE_KEY=<token-string>"
  echo "  4. re-run this script"
  exit 0
fi

if ! command -v kaggle >/dev/null 2>&1; then
  echo "[kaggle] kaggle CLI not installed; pip install"
  pip install --quiet kaggle || { echo "[kaggle] install failed; skipping"; exit 0; }
fi

# Write kaggle.json (Kaggle CLI ignores env vars, requires file)
KAGGLE_CFG="${HOME}/.kaggle"
mkdir -p "$KAGGLE_CFG"
if [ ! -f "$KAGGLE_CFG/kaggle.json" ]; then
  printf '{"username":"%s","key":"%s"}\n' "$KAGGLE_USERNAME" "$KAGGLE_KEY" > "$KAGGLE_CFG/kaggle.json"
  chmod 600 "$KAGGLE_CFG/kaggle.json"
fi

# Bucket → list of slugs
declare -A BUCKETS
BUCKETS[finance_sentiment]="ankurzing/sentiment-analysis-for-financial-news sbhatti/financial-sentiment-analysis"
BUCKETS[finance_headlines]="notlucasp/financial-news-headlines aaron7sun/stocknews"
BUCKETS[long_form_articles]="jeet2016/us-financial-news-articles"
BUCKETS[social_finance]="equinxx/stock-tweets-for-sentiment-analysis-and-prediction omermetinn/tweets-about-the-top-companies-from-2015-to-2020"
BUCKETS[market_context]="borismarjanovic/price-volume-data-for-all-us-stocks-etfs"

ok=0; fail=0
for bucket in "${!BUCKETS[@]}"; do
  echo "[kaggle] bucket: $bucket"
  for slug in ${BUCKETS[$bucket]}; do
    safe="$(echo "$slug" | tr '/' '_')"
    dest="$TARGET/$bucket/$safe"
    mkdir -p "$dest"
    echo "[kaggle]   pulling $slug"
    if kaggle datasets download -d "$slug" -p "$dest" --unzip --force >/dev/null 2>&1; then
      ok=$((ok + 1))
    else
      echo "[kaggle]     FAILED (rate limit, auth, or 404)"
      fail=$((fail + 1))
    fi
  done
done

echo
echo "[kaggle] done. ok=$ok fail=$fail target=$TARGET"

# Best-effort: synthesize extended.jsonl from the finance_sentiment bucket if present.
SENT_DIR="$TARGET/finance_sentiment/ankurzing_sentiment-analysis-for-financial-news"
SENT_CSV="$(find "$SENT_DIR" -maxdepth 2 -iname '*.csv' 2>/dev/null | head -1 || true)"
if [ -n "$SENT_CSV" ]; then
  echo "[kaggle] converting $SENT_CSV → $GOLDEN/extended.jsonl"
  python3 - "$SENT_CSV" "$GOLDEN/extended.jsonl" <<'PY'
import csv, json, sys
src, dst = sys.argv[1], sys.argv[2]
LABEL_MAP = {"positive": "positive", "negative": "negative", "neutral": "neutral"}
n = 0
with open(src, encoding="utf-8", errors="replace") as fh, open(dst, "w", encoding="utf-8") as out:
    # ankurzing uses Latin-1 occasionally; reader handles via errors=replace above
    rd = csv.reader(fh)
    rows = list(rd)
    # header detection
    start = 1 if rows and rows[0] and rows[0][0].lower() in ("sentiment", "label") else 0
    for r in rows[start:]:
        if len(r) < 2: continue
        label, text = r[0].strip().lower(), r[1].strip()
        if not text: continue
        sentiment = LABEL_MAP.get(label)
        # We treat finance-news sentiment items as finance-relevant when the
        # text plausibly references markets — for the golden harness we
        # default expected_finance_relevant=True (the bucket is finance news).
        out.write(json.dumps({
            "capture_id": f"kaggle-ankurzing-{n:06d}",
            "title": text[:120],
            "text": text,
            "expected_finance_relevant": True,
            "expected_sentiment": sentiment,
        }) + "\n")
        n += 1
print(f"  wrote {n} rows")
PY
  echo "[kaggle] run: fusion-stack benchmark --golden --extended $GOLDEN/extended.jsonl"
else
  echo "[kaggle] no finance_sentiment CSV found — skipped extended.jsonl synthesis"
fi
