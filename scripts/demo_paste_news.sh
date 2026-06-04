#!/usr/bin/env bash
# Wrapper around `catchem demo` for the spec's "paste-news" canary.
# Usage:
#   bash scripts/demo_paste_news.sh                       # uses docs/examples/news_fed.txt
#   bash scripts/demo_paste_news.sh some/article.txt
#   echo "..." | bash scripts/demo_paste_news.sh - --title "Custom"
set -euo pipefail
CATCHEM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$CATCHEM_ROOT"
[ -d .venv ] && source .venv/bin/activate

ARTICLE="${1:-docs/examples/news_fed.txt}"
TITLE="${2:-Federal Reserve raises rates by 25 bps amid sticky inflation}"

if [ "$ARTICLE" = "-" ]; then
  catchem demo --title "$TITLE" --domain reuters.com
else
  catchem demo --title "$TITLE" --text-file "$ARTICLE" --domain reuters.com
fi
