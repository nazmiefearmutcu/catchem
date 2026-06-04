"""Timezone-safe timestamp handling.

Published / fetch / observed timestamps may arrive naive, missing, or as
strings. Storage and API must never crash on them, and must serialize as
ISO 8601 strings on the wire.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from catchem.awareness_reader import parse_capture_line
from catchem.schemas import AwarenessCaptureView
from catchem.storage import Storage


def test_capture_view_coerces_naive_datetime_to_utc() -> None:
    naive = datetime(2026, 5, 16, 10, 0, 0)   # no tzinfo
    cap = AwarenessCaptureView(
        capture_id="t1", doc_id="d1", text="x",
        fetch_ts=naive, observed_ts=naive,
    )
    assert cap.fetch_ts is not None and cap.fetch_ts.tzinfo is not None
    assert cap.observed_ts is not None and cap.observed_ts.tzinfo is not None


def test_capture_view_accepts_iso_string_published_ts() -> None:
    raw = json.dumps({
        "capture_id": "t2", "doc_id": "d2", "text": "x",
        "published_ts": "2026-05-16T10:00:00+00:00",
    })
    cap = parse_capture_line(raw)
    assert cap is not None
    assert cap.published_ts is not None
    assert cap.published_ts.tzinfo is not None


def test_capture_view_accepts_missing_published_ts() -> None:
    raw = json.dumps({"capture_id": "t3", "doc_id": "d3", "text": "x"})
    cap = parse_capture_line(raw)
    assert cap is not None
    assert cap.published_ts is None


def test_storage_serializes_published_ts_as_iso_string(tmp_path: Path) -> None:
    from catchem.schemas import FinancialImpactRecord, ProcessingMode, SentimentLabel
    with Storage(db_path=tmp_path / "catchem.sqlite3",
                 parquet_dir=tmp_path / "parq", dlq_dir=tmp_path / "dlq") as s:
        pub = datetime(2026, 5, 16, 10, 0, 0, tzinfo=UTC)
        rec = FinancialImpactRecord(
            capture_id="ts1", doc_id="d", title="t", text_excerpt="x",
            published_ts=pub, domain="x.com", url="https://x.com/1",
            is_finance_relevant=True, finance_relevance_score=0.4,
            sentiment_label=SentimentLabel.NEUTRAL, sentiment_score=0.5,
            evidence_sentences=[], reason_text=None,
            component_scores={"raw_relevance_score": 0.4},
            processing_mode=ProcessingMode.PRODUCTION_SAFE,
            model_versions={"x": "v"},
            created_at=datetime.now(UTC),
        )
        s.insert_record(rec)
        row = s.get_record("ts1")
        assert row is not None
        assert isinstance(row["published_ts"], str)
        assert "2026-05-16" in row["published_ts"]
        # round-trippable
        parsed = datetime.fromisoformat(row["published_ts"])
        assert parsed.tzinfo is not None


def test_storage_handles_record_with_no_published_ts(tmp_path: Path) -> None:
    from catchem.schemas import FinancialImpactRecord, ProcessingMode, SentimentLabel
    with Storage(db_path=tmp_path / "catchem.sqlite3",
                 parquet_dir=tmp_path / "parq", dlq_dir=tmp_path / "dlq") as s:
        rec = FinancialImpactRecord(
            capture_id="ts2", doc_id="d", title="t", text_excerpt="x",
            published_ts=None, domain="x.com", url=None,
            is_finance_relevant=False, finance_relevance_score=0.1,
            sentiment_label=SentimentLabel.NEUTRAL, sentiment_score=0.5,
            evidence_sentences=[], reason_text=None,
            component_scores={"raw_relevance_score": 0.1},
            processing_mode=ProcessingMode.PRODUCTION_SAFE,
            model_versions={"x": "v"},
            created_at=datetime.now(UTC),
        )
        s.insert_record(rec)
        row = s.get_record("ts2")
        assert row is not None and row["published_ts"] is None
