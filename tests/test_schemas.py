"""Dedicated unit tests for catchem.schemas.

`FinancialImpactRecord` is the single durable artifact this stack emits, and
`AwarenessCaptureView` is the upstream shape catchem consumes. This file pins:

  * enum value stability (the (str, Enum) idiom is contract, not incidental),
  * AwarenessCaptureView extra="allow" + the naive→UTC timestamp coercion,
  * FinancialImpactRecord extra="forbid", field bounds, the non-empty
    text_excerpt validator, defaults, and a JSON round-trip,
  * ReplayOffset defaults + extra="forbid".
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from catchem.schemas import (
    AwarenessCaptureView,
    FinancialImpactRecord,
    ProcessingMode,
    ReplayOffset,
    SentimentLabel,
)


def _valid_record_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "capture_id": "cap-1",
        "doc_id": "doc-1",
        "text_excerpt": "The Fed raised rates by 25bps.",
        "is_finance_relevant": True,
        "finance_relevance_score": 0.85,
        "processing_mode": ProcessingMode.PRODUCTION_SAFE,
    }
    base.update(overrides)
    return base


# ── enums: value stability ──────────────────────────────────────────────────


def test_processing_mode_values_are_stable() -> None:
    assert ProcessingMode.PRODUCTION_SAFE.value == "production_safe"
    assert ProcessingMode.REPLAY_EXISTING.value == "replay_existing"
    assert ProcessingMode.LIVE_TAIL.value == "live_tail"
    assert ProcessingMode.RESEARCH_DIAGNOSTIC.value == "research_diagnostic"


def test_sentiment_label_values_are_stable() -> None:
    assert {m.value for m in SentimentLabel} == {
        "positive",
        "neutral",
        "negative",
        "unknown",
    }


def test_processing_mode_constructs_from_string_value() -> None:
    assert ProcessingMode("live_tail") is ProcessingMode.LIVE_TAIL


# ── AwarenessCaptureView ─────────────────────────────────────────────────────


def test_capture_view_minimal_required_fields() -> None:
    cap = AwarenessCaptureView(capture_id="c", doc_id="d", text="hello world")
    assert cap.capture_id == "c"
    assert cap.title is None
    assert cap.published_ts is None


def test_capture_view_missing_required_field_raises() -> None:
    with pytest.raises(ValidationError):
        AwarenessCaptureView(capture_id="c", doc_id="d")  # type: ignore[call-arg]


def test_capture_view_allows_unknown_extra_fields() -> None:
    # extra="allow" → forward-compatible with new Awareness fields.
    cap = AwarenessCaptureView.model_validate(
        {"capture_id": "c", "doc_id": "d", "text": "x", "brand_new_field": 42}
    )
    assert cap.text == "x"
    assert cap.model_dump()["brand_new_field"] == 42


def test_capture_view_naive_datetime_coerced_to_utc() -> None:
    # Covers schemas.py line 70: naive datetime gets tzinfo=UTC stamped.
    naive = datetime(2026, 5, 16, 12, 0, 0)
    assert naive.tzinfo is None
    cap = AwarenessCaptureView(
        capture_id="c",
        doc_id="d",
        text="x",
        fetch_ts=naive,
        observed_ts=naive,
        published_ts=naive,
    )
    for ts in (cap.fetch_ts, cap.observed_ts, cap.published_ts):
        assert ts is not None
        assert ts.tzinfo is not None
        assert ts.utcoffset() == UTC.utcoffset(None)


def test_capture_view_aware_datetime_preserved() -> None:
    aware = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
    cap = AwarenessCaptureView(capture_id="c", doc_id="d", text="x", fetch_ts=aware)
    assert cap.fetch_ts == aware


def test_capture_view_string_timestamp_parsed_by_pydantic() -> None:
    # Covers the `isinstance(v, str): return v` pass-through (let pydantic parse).
    cap = AwarenessCaptureView(capture_id="c", doc_id="d", text="x", fetch_ts="2026-05-16T12:00:00Z")
    assert cap.fetch_ts is not None
    assert cap.fetch_ts.year == 2026


def test_capture_view_int_epoch_timestamp_falls_through_validator() -> None:
    # Covers schemas.py line 74: a non-None, non-datetime, non-str value is
    # returned unchanged from the before-validator and parsed by pydantic
    # (pydantic accepts int epoch seconds for datetime fields).
    cap = AwarenessCaptureView(capture_id="c", doc_id="d", text="x", fetch_ts=1_700_000_000)
    assert cap.fetch_ts is not None
    assert cap.fetch_ts.tzinfo is not None


# ── FinancialImpactRecord: construction + defaults ──────────────────────────


def test_record_minimal_valid_construction_and_defaults() -> None:
    rec = FinancialImpactRecord(**_valid_record_kwargs())  # type: ignore[arg-type]
    # List/dict defaults are independent instances.
    assert rec.asset_classes == []
    assert rec.impact_reason_codes == []
    assert rec.candidate_symbols == []
    assert rec.candidate_entities == []
    assert rec.impact_horizons == []
    assert rec.evidence_sentences == []
    assert rec.component_scores == {}
    assert rec.model_versions == {}
    # Diagnostic defaults OFF.
    assert rec.diagnostic_multimodal_enabled is False
    assert rec.diagnostic_multimodal_result is None
    # created_at auto-populated as aware datetime.
    assert isinstance(rec.created_at, datetime)
    assert rec.created_at.tzinfo is not None
    # Optional fields default None.
    assert rec.sentiment_label is None
    assert rec.sentiment_score is None


def test_record_default_lists_are_not_shared_between_instances() -> None:
    a = FinancialImpactRecord(**_valid_record_kwargs())  # type: ignore[arg-type]
    b = FinancialImpactRecord(**_valid_record_kwargs())  # type: ignore[arg-type]
    a.asset_classes.append("equities")
    assert b.asset_classes == []


def test_record_forbids_unknown_fields() -> None:
    # extra="forbid" → additions must be explicit.
    with pytest.raises(ValidationError):
        FinancialImpactRecord(**_valid_record_kwargs(surprise_field="boom"))  # type: ignore[arg-type]


# ── FinancialImpactRecord: validators / bounds ──────────────────────────────


@pytest.mark.parametrize("bad", ["", "   ", "\n\t "])
def test_record_rejects_empty_text_excerpt(bad: str) -> None:
    # Covers schemas.py line 127.
    with pytest.raises(ValidationError, match="text_excerpt must be non-empty"):
        FinancialImpactRecord(**_valid_record_kwargs(text_excerpt=bad))  # type: ignore[arg-type]


def test_record_text_excerpt_with_content_passes() -> None:
    rec = FinancialImpactRecord(**_valid_record_kwargs(text_excerpt="ok"))  # type: ignore[arg-type]
    assert rec.text_excerpt == "ok"


@pytest.mark.parametrize("score", [-0.01, 1.01, 2.0, -5.0])
def test_record_relevance_score_out_of_bounds_rejected(score: float) -> None:
    with pytest.raises(ValidationError):
        FinancialImpactRecord(**_valid_record_kwargs(finance_relevance_score=score))  # type: ignore[arg-type]


@pytest.mark.parametrize("score", [0.0, 0.5, 1.0])
def test_record_relevance_score_boundaries_accepted(score: float) -> None:
    rec = FinancialImpactRecord(**_valid_record_kwargs(finance_relevance_score=score))  # type: ignore[arg-type]
    assert rec.finance_relevance_score == score


def test_record_sentiment_label_accepts_enum_and_string() -> None:
    rec_enum = FinancialImpactRecord(
        **_valid_record_kwargs(sentiment_label=SentimentLabel.NEGATIVE)  # type: ignore[arg-type]
    )
    assert rec_enum.sentiment_label is SentimentLabel.NEGATIVE
    rec_str = FinancialImpactRecord(**_valid_record_kwargs(sentiment_label="positive"))  # type: ignore[arg-type]
    assert rec_str.sentiment_label is SentimentLabel.POSITIVE


def test_record_invalid_sentiment_label_rejected() -> None:
    with pytest.raises(ValidationError):
        FinancialImpactRecord(**_valid_record_kwargs(sentiment_label="ecstatic"))  # type: ignore[arg-type]


# ── FinancialImpactRecord: serialization round-trip ─────────────────────────


def test_record_json_round_trip_preserves_payload() -> None:
    rec = FinancialImpactRecord(
        **_valid_record_kwargs(  # type: ignore[arg-type]
            title="Fed hikes",
            asset_classes=["rates", "equities"],
            impact_reason_codes=["monetary_policy"],
            candidate_symbols=["SPY"],
            candidate_entities=["Federal Reserve"],
            impact_horizons=["one_day"],
            sentiment_label=SentimentLabel.NEGATIVE,
            sentiment_score=-0.4,
            evidence_sentences=["Yields jumped."],
            reason_text="rates | monetary_policy",
            component_scores={"asset_class_max": 0.9},
            model_versions={"zero_shot": "stub/v1"},
        )
    )
    restored = FinancialImpactRecord.model_validate_json(rec.model_dump_json())
    assert restored == rec
    assert restored.asset_classes == ["rates", "equities"]
    assert restored.sentiment_label is SentimentLabel.NEGATIVE


def test_record_model_dump_enum_serializes_to_value_in_json() -> None:
    rec = FinancialImpactRecord(**_valid_record_kwargs())  # type: ignore[arg-type]
    dumped = rec.model_dump(mode="json")
    assert dumped["processing_mode"] == "production_safe"


# ── ReplayOffset ─────────────────────────────────────────────────────────────


def test_replay_offset_defaults() -> None:
    off = ReplayOffset(source_path="/x/y.jsonl")
    assert off.line_offset == 0
    assert off.last_capture_id is None
    assert isinstance(off.updated_at, datetime)
    assert off.updated_at.tzinfo is not None


def test_replay_offset_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        ReplayOffset(source_path="/x", bogus=1)  # type: ignore[call-arg]


def test_replay_offset_requires_source_path() -> None:
    with pytest.raises(ValidationError):
        ReplayOffset()  # type: ignore[call-arg]


def test_replay_offset_round_trip() -> None:
    off = ReplayOffset(source_path="/x/y.jsonl", line_offset=12, last_capture_id="c-9")
    restored = ReplayOffset.model_validate_json(off.model_dump_json())
    assert restored == off


def test_schemas_coverage_gaps() -> None:
    # 1. ValueError exception in date string parsing (lines 81-82)
    with pytest.raises(ValidationError):
        AwarenessCaptureView(capture_id="c", doc_id="d", text="x", fetch_ts="not-a-valid-date")

    # 2. String timestamp without offset is naive, gets tzinfo=UTC (line 84)
    cap = AwarenessCaptureView(capture_id="c", doc_id="d", text="x", fetch_ts="2026-05-16T12:00:00")
    assert cap.fetch_ts is not None
    assert cap.fetch_ts.tzinfo == UTC

    # 3. Non-finite sentiment score maps to None (line 150)
    rec_nan = FinancialImpactRecord(
        **_valid_record_kwargs(sentiment_score=float("nan"))  # type: ignore[arg-type]
    )
    assert rec_nan.sentiment_score is None

    rec_inf = FinancialImpactRecord(
        **_valid_record_kwargs(sentiment_score=float("inf"))  # type: ignore[arg-type]
    )
    assert rec_inf.sentiment_score is None

    # 4. Naive datetime for published_ts or created_at coerced to UTC (line 171)
    naive = datetime(2026, 6, 2, 23, 0, 0)
    rec_tz = FinancialImpactRecord(
        **_valid_record_kwargs(published_ts=naive, created_at=naive)  # type: ignore[arg-type]
    )
    assert rec_tz.published_ts is not None
    assert rec_tz.published_ts.tzinfo == UTC
    assert rec_tz.created_at.tzinfo == UTC


def test_schema_timestamp_parsing_caching_and_fast_paths() -> None:
    # 1. 20-character Z/z format
    cap1 = AwarenessCaptureView(
        capture_id="c",
        doc_id="d",
        text="x",
        fetch_ts="2026-05-27T14:30:00Z",
        observed_ts="2026-05-27T14:30:00z",
    )
    assert cap1.fetch_ts is not None and cap1.fetch_ts.tzinfo == UTC
    assert cap1.observed_ts is not None and cap1.observed_ts.tzinfo == UTC

    # 2. 24-character Z/z format
    cap2 = AwarenessCaptureView(
        capture_id="c",
        doc_id="d",
        text="x",
        fetch_ts="2026-05-27T14:30:00.123Z",
        observed_ts="2026-05-27T14:30:00.123z",
    )
    assert cap2.fetch_ts is not None and cap2.fetch_ts.microsecond == 123000
    assert cap2.observed_ts is not None and cap2.observed_ts.microsecond == 123000

    # 3. 19-character naive format
    cap3 = AwarenessCaptureView(
        capture_id="c",
        doc_id="d",
        text="x",
        fetch_ts="2026-05-27T14:30:00",
        observed_ts="2026-05-27t14:30:00",
    )
    assert cap3.fetch_ts is not None and cap3.fetch_ts.tzinfo == UTC
    assert cap3.observed_ts is not None and cap3.observed_ts.tzinfo == UTC

    # 3.5. Lowercase 't' in 20/24 char formats
    cap3_5 = AwarenessCaptureView(
        capture_id="c",
        doc_id="d",
        text="x",
        fetch_ts="2026-05-27t14:30:00Z",
        observed_ts="2026-05-27t14:30:00.123Z",
    )
    assert cap3_5.fetch_ts is not None and cap3_5.observed_ts is not None

    # 4. Offset bearing format (standard and short fallback)
    cap4 = AwarenessCaptureView(
        capture_id="c",
        doc_id="d",
        text="x",
        fetch_ts="2026-05-27T14:30:00+03:00",
        observed_ts="2026-05-27T14:30+03:00",
    )
    assert cap4.fetch_ts is not None and cap4.fetch_ts.tzinfo is not None
    assert cap4.observed_ts is not None and cap4.observed_ts.tzinfo is not None

    # 4.5. Short naive format (fallback path with naive string)
    cap4_5 = AwarenessCaptureView(capture_id="c", doc_id="d", text="x", fetch_ts="2026-05-27T14:30")
    assert cap4_5.fetch_ts is not None and cap4_5.fetch_ts.tzinfo == UTC

    # 5. Invalid values triggering ValueError in fast paths and standard fallback
    # YYYY-MM-DDTHH:MM:SSZ with invalid month/day
    with pytest.raises(ValidationError):
        AwarenessCaptureView(capture_id="c", doc_id="d", text="x", fetch_ts="2026-99-99T99:99:99Z")
    # YYYY-MM-DDTHH:MM:SS.mmmZ with invalid month/day
    with pytest.raises(ValidationError):
        AwarenessCaptureView(capture_id="c", doc_id="d", text="x", fetch_ts="2026-99-99T99:99:99.999Z")
    # YYYY-MM-DDTHH:MM:SS with invalid month/day
    with pytest.raises(ValidationError):
        AwarenessCaptureView(capture_id="c", doc_id="d", text="x", fetch_ts="2026-99-99T99:99:99")

    # 6. Verify caching (direct helper call checks cache efficiency)
    from catchem.schemas import _parse_utc_ts_cached

    dt1 = _parse_utc_ts_cached("2026-05-27T14:30:00Z")
    dt2 = _parse_utc_ts_cached("2026-05-27T14:30:00Z")
    assert dt1 is dt2  # cache hit yields the exact same object
