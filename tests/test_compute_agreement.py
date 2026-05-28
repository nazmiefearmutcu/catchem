"""Pins the `symbol_jaccard` field's inclusion in `_compute_agreement.overall`.

Bug history: the helper's docstring lists six bounded fields contributing
to ``overall``, but the implementation summed only five and divided by
5.0 — `symbol_jaccard` was computed, returned in the result dict, yet
silently dropped from the equal-weight mean. That created a UI lie: the
"agreement" lead number on the compare page never reflected ticker
symbol overlap between the stub and the DeepSeek reviewer.

This test pins the post-fix behavior: when only `symbol_jaccard` differs
between two payloads, ``overall`` MUST change too.
"""

from __future__ import annotations

from catchem.api import _compute_agreement


def _base_payload() -> dict:
    """A pair of payloads that agree on every field except (configurable) symbols."""
    return {
        "is_finance_relevant": True,
        "finance_relevance_score": 0.8,
        "asset_classes": ["equity"],
        "impact_reason_codes": ["earnings"],
        "candidate_symbols": ["AAPL"],
        "sentiment_label": "positive",
    }


def test_symbol_jaccard_is_included_in_overall() -> None:
    """`overall` must differ when only `symbol_jaccard` differs.

    If symbols agree → jaccard=1.0; if symbols disagree completely →
    jaccard=0.0. With the bug, both cases produced the same `overall`
    (because the field was excluded from the divisor /5.0). After the
    fix the divisor is /6.0 and the field participates, so the two
    `overall` values must differ.
    """
    matching = _base_payload()
    out_match = _compute_agreement(matching, matching)

    differ_stub = _base_payload()
    differ_ds = _base_payload()
    differ_ds["candidate_symbols"] = ["MSFT"]   # zero-overlap set → jaccard=0
    out_differ = _compute_agreement(differ_stub, differ_ds)

    # When everything matches: overall is exactly 1.0.
    assert out_match["overall"] == 1.0
    assert out_match["symbol_jaccard"] == 1.0

    # When ONLY symbol_jaccard differs: overall MUST drop. With the
    # 6-field mean it lands at 5/6 ≈ 0.8333 (was 1.0 under the 5-field bug).
    assert out_differ["symbol_jaccard"] == 0.0
    assert out_differ["overall"] < 1.0
    assert abs(out_differ["overall"] - 5.0 / 6.0) < 1e-3


def test_overall_divisor_is_six_not_five() -> None:
    """The fix changed the divisor from /5.0 → /6.0. Pin that constant.

    Hand-computed: with all six fields agreeing → sum=6, overall=6/6=1.0.
    With four bools-true + 2 jaccards at 1/3 → sum = 4 + 1/3 + 1/3 + 1 = 5.6667,
    overall ≈ 5.6667/6 ≈ 0.9444 (vs 4.6667/5 = 0.9333 under bug — the
    test passes because the new divisor changes the answer; we also
    sanity check the symbol_jaccard=1.0 entry to confirm inclusion).
    """
    stub = {
        "is_finance_relevant": True,
        "finance_relevance_score": 0.0,
        "asset_classes": ["equity", "fx"],          # union {equity,fx,rates} ∩ {equity} → 1/3
        "impact_reason_codes": ["earnings", "tax"],  # union 3 ∩ 1 → 1/3
        "candidate_symbols": ["AAPL"],              # exact match → jaccard=1.0
        "sentiment_label": "positive",
    }
    ds = {
        "is_finance_relevant": True,
        "finance_relevance_score": 0.0,
        "asset_classes": ["equity", "rates"],
        "impact_reason_codes": ["earnings", "macro"],
        "candidate_symbols": ["AAPL"],
        "sentiment_label": "positive",
    }
    out = _compute_agreement(stub, ds)
    # Recompute exactly the way the impl does: jaccard rounds to 4 dp.
    asset_j = round(1 / 3, 4)
    reason_j = round(1 / 3, 4)
    symbol_j = 1.0
    # sum = 1(rel) + 1(1-score_delta) + asset_j + reason_j + symbol_j + 1(sent)
    expected_sum = 1.0 + 1.0 + asset_j + reason_j + symbol_j + 1.0
    expected = round(expected_sum / 6.0, 4)
    assert out["symbol_jaccard"] == symbol_j
    assert out["overall"] == expected
    # And pinning the alternative (buggy) calculation would have
    # produced a DIFFERENT number — proves the fix is observable.
    buggy = round((expected_sum - symbol_j) / 5.0, 4)
    assert out["overall"] != buggy
