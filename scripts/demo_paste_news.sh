#!/usr/bin/env bash
# Wrapper around `fusion-stack demo` for the spec's "paste-news" canary.
# Usage:
#   bash scripts/demo_paste_news.sh                       # uses docs/examples/news_fed.txt
#   bash scripts/demo_paste_news.sh some/article.txt
#   echo "..." | bash scripts/demo_paste_news.sh - --title "Custom"
set -euo pipefail
FUSION_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$FUSION_ROOT"
[ -d .venv ] && source .venv/bin/activate

ARTICLE="${1:-docs/examples/news_fed.txt}"
TITLE="${2:-Federal Reserve raises rates by 25 bps amid sticky inflation}"

if [ "$ARTICLE" = "-" ]; then
  fusion-stack demo --title "$TITLE" --domain reuters.com
else
  fusion-stack demo --title "$TITLE" --text-file "$ARTICLE" --domain reuters.com
fi
