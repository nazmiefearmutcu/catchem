"""Round-2 bug hunt — group RC-nan-tz.

Covers four confirmed findings:
  1. schemas.FinancialImpactRecord: NaN sentiment_score / component_scores
     reach JSON serialization and 500 the response (no finite validation).
  2. quant.intensity._coerce_float: propagates NaN/Inf unlike every sibling
     quant module, so the intensity panel can emit NaN.
  3. schemas.AwarenessCaptureView._utc: naive ISO *string* timestamps stay
     naive, defeating the UTC-coercion contract.
  4. contracts.FinancialImpactSummary.from_record_dict: created_at fallback
     used naive datetime.utcnow() → tz-less wire string.

ML stubs are forced on by conftest's isolated_env fixture.
"""

from __future__ import annotations

import json
import math

import pytest

from catchem.contracts import FinancialImpactSummary
from catchem.quant import intensity
from catchem.reviewers.deepseek import _as_clamped_float, _as_optional_clamped_float
from catchem.schemas import (
    AwarenessCaptureView,
    FinancialImpactRecord,
    ProcessingMode,
)


def _base_record_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        capture_id="cap-1",
        doc_id="doc-1",
        text_excerpt="The Fed raised rates.",
        is_finance_relevant=True,
        finance_relevance_score=0.9,
        processing_mode=ProcessingMode.PRODUCTION_SAFE,
    )
    base.update(overrides)
    return base


# ── Finding 1: NaN at the schema boundary ────────────────────────────────


def test_nan_sentiment_score_is_dropped_to_none() -> None:
    rec = FinancialImpactRecord(
        **_base_record_kwargs(sentiment_score=float("nan"))
    )
    assert rec.sentiment_score is None
    # The whole point: model_dump → json.dumps(allow_nan=False) must not raise.
    json.dumps(rec.model_dump(mode="json"), allow_nan=False)


@pytest.mark.parametrize("bad", [float("inf"), float("-inf")])
def test_inf_sentiment_score_is_dropped(bad: float) -> None:
    rec = FinancialImpactRecord(**_base_record_kwargs(sentiment_score=bad))
    assert rec.sentiment_score is None


def test_finite_sentiment_score_is_preserved() -> None:
    rec = FinancialImpactRecord(**_base_record_kwargs(sentiment_score=-0.42))
    assert rec.sentiment_score == pytest.approx(-0.42)


def test_nan_component_score_is_filtered_out() -> None:
    rec = FinancialImpactRecord(
        **_base_record_kwargs(
            component_scores={"a": 0.5, "bad": float("nan"), "inf": float("inf")}
        )
    )
    assert rec.component_scores == {"a": 0.5}
    json.dumps(rec.model_dump(mode="json"), allow_nan=False)


# ── Finding 1b: DeepSeek reviewer coercion ───────────────────────────────


def test_reviewer_clamped_float_rejects_nan() -> None:
    assert _as_clamped_float(float("nan")) == 0.0
    assert _as_clamped_float(float("inf")) == 0.0
    assert _as_clamped_float(float("-inf")) == 0.0
    # Sanity: finite values still clamp to [0,1].
    assert _as_clamped_float(0.5) == 0.5
    assert _as_clamped_float(2.0) == 1.0


def test_reviewer_optional_clamped_float_nan_path() -> None:
    # A DeepSeek `{"sentiment_score": NaN}` parses to float('nan'); after the
    # fix it can no longer leak through as a non-finite value.
    val = _as_optional_clamped_float(float("nan"))
    assert val is not None
    assert math.isfinite(val)


# ── Finding 2: intensity._coerce_float ───────────────────────────────────


def test_intensity_coerce_float_rejects_nonfinite() -> None:
    assert intensity._coerce_float(float("nan")) == 0.0
    assert intensity._coerce_float(float("inf")) == 0.0
    assert intensity._coerce_float(float("-inf")) == 0.0
    assert intensity._coerce_float(0.7) == pytest.approx(0.7)


def test_intensity_overall_is_finite_with_nan_inputs() -> None:
    records = [
        {
            "capture_id": "c1",
            "finance_relevance_score": float("nan"),
            "sentiment_score": 0.8,
            "asset_classes": ["equities"],
        },
        {
            "capture_id": "c2",
            "finance_relevance_score": 0.9,
            "sentiment_score": float("nan"),
            "asset_classes": ["equities"],
        },
        {
            "capture_id": "c3",
            "finance_relevance_score": 0.5,
            "sentiment_score": -0.6,
            "asset_classes": ["equities"],
        },
    ]
    overall = intensity.compute_overall(records)
    assert math.isfinite(overall.mean_intensity)
    assert math.isfinite(overall.max_intensity)
    # Must JSON-serialize under the allow_nan=False renderer.
    json.dumps(
        {"mean": overall.mean_intensity, "max": overall.max_intensity},
        allow_nan=False,
    )

    buckets = intensity.compute_by_scope(records, "asset_classes")
    for b in buckets:
        assert math.isfinite(b.mean_intensity)
        assert math.isfinite(b.max_intensity)
        json.dumps(
            {"mean": b.mean_intensity, "max": b.max_intensity}, allow_nan=False
        )


# ── Finding 3: naive ISO string timestamp coercion ───────────────────────


def test_capture_view_naive_iso_string_is_utc_stamped() -> None:
    # The exact untested gap: a tz-less ISO string (Awareness/RSS shape).
    cap = AwarenessCaptureView(
        capture_id="cap-2",
        doc_id="doc-2",
        text="body",
        published_ts="2026-05-27T14:30:00",
        fetch_ts="2026-05-27T14:30:00",
        observed_ts="2026-05-27T14:30:00",
    )
    assert cap.published_ts is not None and cap.published_ts.tzinfo is not None
    assert cap.fetch_ts is not None and cap.fetch_ts.tzinfo is not None
    assert cap.observed_ts is not None and cap.observed_ts.tzinfo is not None
    # Naive 14:30 must be interpreted AS UTC, not shifted.
    assert cap.published_ts.hour == 14
    assert cap.published_ts.utcoffset() is not None
    assert cap.published_ts.utcoffset().total_seconds() == 0


def test_capture_view_offset_string_still_preserved() -> None:
    # Backward-compat: offset-bearing strings keep their tz unchanged.
    cap = AwarenessCaptureView(
        capture_id="cap-3",
        doc_id="doc-3",
        text="body",
        published_ts="2026-05-16T10:00:00+00:00",
    )
    assert cap.published_ts is not None and cap.published_ts.tzinfo is not None


def test_capture_view_z_suffix_string_is_utc() -> None:
    cap = AwarenessCaptureView(
        capture_id="cap-4",
        doc_id="doc-4",
        text="body",
        published_ts="2026-05-16T10:00:00Z",
    )
    assert cap.published_ts is not None
    assert cap.published_ts.utcoffset().total_seconds() == 0


def test_record_naive_published_ts_is_utc_stamped() -> None:
    # Defense-in-depth validator on FinancialImpactRecord itself.
    from datetime import datetime

    rec = FinancialImpactRecord(
        **_base_record_kwargs(published_ts=datetime(2026, 5, 27, 14, 30, 0))
    )
    assert rec.published_ts is not None and rec.published_ts.tzinfo is not None
    iso = rec.model_dump(mode="json")["published_ts"]
    assert iso.endswith("+00:00") or iso.endswith("Z")


# ── Finding 4: contracts created_at fallback ─────────────────────────────


def test_summary_created_at_fallback_is_tz_aware() -> None:
    s = FinancialImpactSummary.from_record_dict(
        {
            "capture_id": "cap-5",
            "doc_id": "doc-5",
            "is_finance_relevant": False,
            "finance_relevance_score": 0.1,
            # created_at intentionally absent → fallback path.
        }
    )
    assert "+00:00" in s.created_at
    # Frontend Date.parse on this string treats it as UTC, not local.
