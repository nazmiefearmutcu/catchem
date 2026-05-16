#!/usr/bin/env python3
"""Pre-download Hugging Face models into the local cache.

If transformers/sentence-transformers aren't installed, exits 0 with a clear
note. We never make this a hard prerequisite.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


TARGETS = [
    ("transformers", "ProsusAI/finbert"),
    ("transformers", "facebook/bart-large-mnli"),
    ("sentence-transformers", "sentence-transformers/all-MiniLM-L6-v2"),
    ("cross-encoder", "cross-encoder/ms-marco-MiniLM-L6-v2"),
]


def warm() -> int:
    cache = Path(os.environ.get("HF_HOME", str(Path.home() / ".cache/huggingface"))).expanduser()
    cache.mkdir(parents=True, exist_ok=True)

    # Try huggingface_hub first (no torch dependency)
    try:
        from huggingface_hub import snapshot_download  # type: ignore[import-not-found]
    except Exception as exc:
        print(f"[warm] huggingface_hub not available ({exc}); skipping warm-cache.")
        return 0

    rc = 0
    for backend, repo in TARGETS:
        try:
            print(f"[warm] downloading {repo} …")
            snapshot_download(repo_id=repo, cache_dir=str(cache))
        except Exception as exc:
            print(f"[warm] FAILED {repo}: {exc}")
            rc = 1
    print(f"[warm] cache: {cache}")
    return rc


if __name__ == "__main__":
    sys.exit(warm())
