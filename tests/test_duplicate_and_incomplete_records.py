"""Duplicate + incomplete record handling.

Storage layer must dedupe by capture_id. JSONL reader must skip rows that
cannot be parsed without crashing the API. DLQ must capture failures.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from catchem.awareness_reader import iter_captures, parse_capture_line
from catchem.schemas import FinancialImpactRecord, ProcessingMode, SentimentLabel
from catchem.storage import Storage


def _make_record(capture_id: str, score: float, title: str) -> FinancialImpactRecord:
    return FinancialImpactRecord(
        capture_id=capture_id,
        doc_id=f"d-{capture_id}",
        title=title,
        text_excerpt="body",
        domain="example.com",
        url="https://example.com/x",
        is_finance_relevant=True,
        finance_relevance_score=score,
        asset_classes=["equities"],
        impact_reason_codes=["earnings"],
        candidate_symbols=["AAPL"],
        candidate_entities=[],
        impact_horizons=["one_day"],
        sentiment_label=SentimentLabel.NEUTRAL,
        sentiment_score=0.5,
        evidence_sentences=["x"],
        reason_text="x",
        component_scores={"raw_relevance_score": score},
        processing_mode=ProcessingMode.REPLAY_EXISTING,
        model_versions={"zero_shot": "stub"},
        created_at=datetime.now(UTC),
    )


def test_storage_dedupes_by_capture_id_on_insert(tmp_path: Path) -> None:
    with Storage(db_path=tmp_path / "catchem.sqlite3",
                 parquet_dir=tmp_path / "parq", dlq_dir=tmp_path / "dlq") as s:
        # Insert the same capture_id twice with different content
        s.insert_record(_make_record("dup-1", 0.4, "first"))
        s.insert_record(_make_record("dup-1", 0.9, "second"))
        counts = s.count_records()
        assert counts["total"] == 1, "storage did not dedupe by capture_id"
        fetched = s.get_record("dup-1")
        assert fetched["finance_relevance_score"] == 0.9
        assert fetched["title"] == "second"


def test_storage_label_index_rebuilds_on_overwrite(tmp_path: Path) -> None:
    """The inverted label index must reflect the LATEST record, not stale rows."""
    with Storage(db_path=tmp_path / "catchem.sqlite3",
                 parquet_dir=tmp_path / "parq", dlq_dir=tmp_path / "dlq") as s:
        r1 = _make_record("dup-2", 0.5, "first")
        s.insert_record(r1)
        # Overwrite with different symbols
        r2 = FinancialImpactRecord(**{**r1.model_dump(), "candidate_symbols": ["MSFT"], "title": "second"})
        s.insert_record(r2)
        aapl = s.by_label("symbol", "AAPL")
        msft = s.by_label("symbol", "MSFT")
        assert len(aapl) == 0, "stale label-index row not cleaned up"
        assert len(msft) == 1


def test_parse_capture_line_returns_none_on_garbage() -> None:
    # JSON gibberish
    assert parse_capture_line("{not json}") is None
    # Empty
    assert parse_capture_line("") is None
    assert parse_capture_line("   ") is None
    # Valid JSON but not a dict
    assert parse_capture_line("[1, 2, 3]") is None


def test_parse_capture_line_rejects_missing_required_fields() -> None:
    # AwarenessCaptureView requires capture_id, doc_id, text
    raw = json.dumps({"capture_id": "x"})   # missing doc_id, text
    assert parse_capture_line(raw) is None


def test_iter_captures_skips_bad_lines_and_emits_good(tmp_path: Path) -> None:
    """JSONL with mixed good/bad lines must yield the good ones, no exception."""
    p = tmp_path / "mixed.jsonl"
    good = json.dumps({"capture_id": "g1", "doc_id": "d1", "text": "ok body"})
    p.write_text(good + "\n" + "{garbage}\n" + "\n" + good.replace('"g1"', '"g2"').replace('"d1"', '"d2"') + "\n",
                 encoding="utf-8")
    seen = list(iter_captures(p))
    assert [cap.capture_id for _, cap in seen] == ["g1", "g2"]


def test_dlq_records_failure(tmp_path: Path) -> None:
    with Storage(db_path=tmp_path / "catchem.sqlite3",
                 parquet_dir=tmp_path / "parq", dlq_dir=tmp_path / "dlq") as s:
        s.record_failure("bad-1", "parse failed: missing title", "<excerpt>")
        assert s.dlq_count() == 1
