"""Pre-shaped payloads for a thin dashboard. JSON-first, no templates."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .storage import Storage


def overview(storage: Storage, limit: int = 50) -> dict[str, Any]:
    rows = storage.recent_records(limit=limit, relevant_only=True)
    counts = storage.count_records()
    asset_counter: defaultdict[str, int] = defaultdict(int)
    reason_counter: defaultdict[str, int] = defaultdict(int)
    sentiment_counter: defaultdict[str, int] = defaultdict(int)
    diagnostic_count = 0
    for r in rows:
        ac_list = r.get("asset_classes")
        if ac_list:
            for ac in ac_list:
                asset_counter[ac] += 1
        rc_list = r.get("impact_reason_codes")
        if rc_list:
            for rc in rc_list:
                reason_counter[rc] += 1
        sentiment = r.get("sentiment_label")
        if sentiment:
            sentiment_counter[sentiment] += 1
        if r.get("diagnostic_multimodal_enabled"):
            diagnostic_count += 1
    return {
        "totals": counts,
        "diagnostic_count": diagnostic_count,
        "asset_class_distribution": dict(sorted(asset_counter.items(), key=lambda x: x[1], reverse=True)),
        "reason_code_distribution": dict(sorted(reason_counter.items(), key=lambda x: x[1], reverse=True)),
        "sentiment_distribution": dict(sentiment_counter),
        "recent": rows,
    }
