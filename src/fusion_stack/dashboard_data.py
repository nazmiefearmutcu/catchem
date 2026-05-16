"""Pre-shaped payloads for a thin dashboard. JSON-first, no templates."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .storage import Storage


def overview(storage: Storage, limit: int = 50) -> dict[str, Any]:
    rows = storage.recent_records(limit=limit, relevant_only=True)
    counts = storage.count_records()
    asset_counter: Counter[str] = Counter()
    reason_counter: Counter[str] = Counter()
    sentiment_counter: Counter[str] = Counter()
    diagnostic_count = 0
    for r in rows:
        for ac in r.get("asset_classes", []):
            asset_counter[ac] += 1
        for rc in r.get("impact_reason_codes", []):
            reason_counter[rc] += 1
        if r.get("sentiment_label"):
            sentiment_counter[r["sentiment_label"]] += 1
        if r.get("diagnostic_multimodal_enabled"):
            diagnostic_count += 1
    return {
        "totals": counts,
        "diagnostic_count": diagnostic_count,
        "asset_class_distribution": dict(asset_counter.most_common()),
        "reason_code_distribution": dict(reason_counter.most_common()),
        "sentiment_distribution": dict(sentiment_counter),
        "recent": rows,
    }
