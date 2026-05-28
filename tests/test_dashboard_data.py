"""Unit coverage for ``catchem.dashboard_data.overview`` aggregation.

The aggregation only depends on two ``Storage`` reads (``recent_records`` and
``count_records``), so these tests drive it through a tiny deterministic
in-memory double instead of touching SQLite or the network. This keeps the
distribution / counting logic under test in isolation.
"""

from __future__ import annotations

from typing import Any

from catchem.dashboard_data import overview


class _FakeStorage:
    """Duck-typed stand-in exposing only what ``overview`` consumes."""

    def __init__(self, rows: list[dict[str, Any]], counts: dict[str, int]) -> None:
        self._rows = rows
        self._counts = counts
        self.recent_calls: list[tuple[int, bool]] = []

    def recent_records(self, limit: int = 50, relevant_only: bool = True) -> list[dict[str, Any]]:
        self.recent_calls.append((limit, relevant_only))
        return list(self._rows[:limit])

    def count_records(self) -> dict[str, int]:
        return dict(self._counts)


def test_overview_empty_records_yields_empty_distributions() -> None:
    storage = _FakeStorage(rows=[], counts={"total": 0, "finance_relevant": 0})

    payload = overview(storage)  # type: ignore[arg-type]

    assert payload["totals"] == {"total": 0, "finance_relevant": 0}
    assert payload["diagnostic_count"] == 0
    assert payload["asset_class_distribution"] == {}
    assert payload["reason_code_distribution"] == {}
    assert payload["sentiment_distribution"] == {}
    assert payload["recent"] == []


def test_overview_populated_records_count_each_distribution() -> None:
    rows = [
        {
            "asset_classes": ["EQUITY", "FX"],
            "impact_reason_codes": ["RATE_DECISION", "EARNINGS"],
            "sentiment_label": "bullish",
            "diagnostic_multimodal_enabled": True,
        },
        {
            "asset_classes": ["EQUITY"],
            "impact_reason_codes": ["RATE_DECISION"],
            "sentiment_label": "bearish",
            "diagnostic_multimodal_enabled": False,
        },
        {
            "asset_classes": ["EQUITY", "CRYPTO"],
            "impact_reason_codes": ["EARNINGS"],
            "sentiment_label": "bullish",
            "diagnostic_multimodal_enabled": True,
        },
    ]
    storage = _FakeStorage(rows=rows, counts={"total": 9, "finance_relevant": 3})

    payload = overview(storage)  # type: ignore[arg-type]

    assert payload["totals"] == {"total": 9, "finance_relevant": 3}
    # EQUITY appears in all 3 rows, FX/CRYPTO once each.
    assert payload["asset_class_distribution"] == {"EQUITY": 3, "FX": 1, "CRYPTO": 1}
    assert payload["reason_code_distribution"] == {"RATE_DECISION": 2, "EARNINGS": 2}
    assert payload["sentiment_distribution"] == {"bullish": 2, "bearish": 1}
    assert payload["diagnostic_count"] == 2
    assert payload["recent"] == rows


def test_overview_distributions_are_sorted_most_common_first() -> None:
    rows = [
        {"asset_classes": ["A"], "impact_reason_codes": ["R1"]},
        {"asset_classes": ["B", "B"], "impact_reason_codes": ["R2", "R2", "R2"]},
        {"asset_classes": ["B"], "impact_reason_codes": []},
    ]
    storage = _FakeStorage(rows=rows, counts={"total": 3, "finance_relevant": 3})

    payload = overview(storage)  # type: ignore[arg-type]

    # B (3) must precede A (1); most_common ordering is preserved by dict().
    assert list(payload["asset_class_distribution"]) == ["B", "A"]
    assert payload["asset_class_distribution"] == {"B": 3, "A": 1}
    assert list(payload["reason_code_distribution"]) == ["R2", "R1"]


def test_overview_tolerates_missing_and_falsy_fields() -> None:
    rows = [
        {},  # no keys at all
        {"asset_classes": [], "impact_reason_codes": [], "sentiment_label": ""},
        {"sentiment_label": None, "diagnostic_multimodal_enabled": False},
    ]
    storage = _FakeStorage(rows=rows, counts={"total": 3, "finance_relevant": 1})

    payload = overview(storage)  # type: ignore[arg-type]

    assert payload["asset_class_distribution"] == {}
    assert payload["reason_code_distribution"] == {}
    # Empty-string and None sentiment labels must not be counted.
    assert payload["sentiment_distribution"] == {}
    assert payload["diagnostic_count"] == 0


def test_overview_passes_limit_and_relevant_only_to_storage() -> None:
    storage = _FakeStorage(rows=[], counts={"total": 0, "finance_relevant": 0})

    overview(storage, limit=7)  # type: ignore[arg-type]

    assert storage.recent_calls == [(7, True)]
