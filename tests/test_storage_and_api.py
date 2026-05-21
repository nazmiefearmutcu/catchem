"""Storage round-trip and FastAPI surface (TestClient)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem.schemas import (
    FinancialImpactRecord,
    ProcessingMode,
    SentimentLabel,
)
from catchem.settings import load_settings, reload_settings
from catchem.storage import Storage


def _record(capture_id: str = "c1") -> FinancialImpactRecord:
    return FinancialImpactRecord(
        capture_id=capture_id,
        doc_id=f"d-{capture_id}",
        title="Test record",
        text_excerpt="example body",
        url="https://example.com/1",
        domain="example.com",
        language="en",
        is_finance_relevant=True,
        finance_relevance_score=0.7,
        asset_classes=["equities"],
        impact_reason_codes=["earnings"],
        candidate_symbols=["AAPL"],
        candidate_entities=["Apple"],
        impact_horizons=["one_day"],
        sentiment_label=SentimentLabel.POSITIVE,
        sentiment_score=0.8,
        evidence_sentences=["Apple beats earnings"],
        reason_text="equities | earnings | sentiment=positive",
        component_scores={"asset_class_max": 0.8, "raw_relevance_score": 0.7},
        processing_mode=ProcessingMode.REPLAY_EXISTING,
        model_versions={"zero_shot": "stub-zero-shot/v1"},
        created_at=datetime.now(timezone.utc),
    )


def test_storage_round_trip(tmp_path: Path) -> None:
    s = Storage(db_path=tmp_path / "catchem.sqlite3",
                parquet_dir=tmp_path / "parq", dlq_dir=tmp_path / "dlq")
    rec = _record("c-rt")
    s.insert_record(rec)
    fetched = s.get_record("c-rt")
    assert fetched is not None
    assert fetched["text_excerpt"] == "example body"
    assert fetched["candidate_symbols"] == ["AAPL"]
    by_sym = s.by_label("symbol", "AAPL")
    assert len(by_sym) == 1
    by_ac = s.by_label("asset_class", "equities")
    assert len(by_ac) == 1
    counts = s.count_records()
    assert counts["total"] == 1 and counts["finance_relevant"] == 1
    s.flush()
    s.close()


def test_api_healthz_and_recent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    reload_settings()
    from catchem.api import create_app

    s = load_settings()
    app = create_app(s)
    with TestClient(app) as client:
        r = client.get("/healthz")
        assert r.status_code == 200 and r.json()["status"] == "ok"

        r = client.get("/recent?limit=5&relevant_only=false")
        assert r.status_code == 200
        items = r.json()["items"]
        assert isinstance(items, list)

        r = client.get("/config")
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] in ("production_safe", "replay_existing", "live_tail", "research_diagnostic")
        assert body["diagnostic_allowed"] is False


def test_api_process_one_and_lookup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synth_capture) -> None:
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    reload_settings()
    from catchem.api import create_app

    app = create_app(load_settings())
    with TestClient(app) as client:
        cap = synth_capture()
        r = client.post("/process-one", json=cap.model_dump(mode="json"))
        assert r.status_code == 200, r.text
        rec = r.json()
        assert rec["capture_id"] == cap.capture_id

        r = client.get(f"/record/{cap.capture_id}")
        assert r.status_code == 200
        assert r.json()["capture_id"] == cap.capture_id

        r = client.get("/records/by-asset-class/rates")
        assert r.status_code == 200
