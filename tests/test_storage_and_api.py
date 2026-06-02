"""Storage round-trip and FastAPI surface (TestClient)."""

from __future__ import annotations

from datetime import UTC, datetime
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
        created_at=datetime.now(UTC),
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


def test_api_process_one_rejects_malformed_input_with_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /process-one with a body missing required AwarenessCaptureView
    fields MUST surface a 422 with structured pydantic errors — not a 500
    Internal Server Error.

    Pre-fix bug: the route signature was `capture: dict = Body(...)`, which
    bypassed FastAPI's request-body validation. The route then called
    `AwarenessCaptureView.model_validate(capture)` manually, raising a
    pydantic ValidationError that escaped as a 500 with a noisy traceback
    in the sidecar log instead of a clean 422 for the client.
    """
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    reload_settings()
    from catchem.api import create_app

    app = create_app(load_settings())
    with TestClient(app) as client:
        r = client.post("/process-one", json={"bogus": 1})
        assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"
        body = r.json()
        assert "detail" in body
        # Field-level errors must call out the missing required fields.
        errs = body["detail"] if isinstance(body["detail"], list) else []
        missing = {e.get("loc", [None, None])[-1] for e in errs if e.get("type") == "missing"}
        assert {"capture_id", "doc_id", "text"} <= missing, missing


def test_storage_prune_dlq_keeps_most_recent_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-fix: `record_failure` only inserted — no prune. On a long-running
    deployment with many handler failures, the `dlq` table grew unbounded
    (one row per failure, 4 KB+ payload excerpt each). This test pins the
    contract for the new `prune_dlq(max_rows)` method: it keeps the most
    recent N rows and drops the rest.
    """
    s = Storage(
        db_path=tmp_path / "catchem.sqlite3",
        parquet_dir=tmp_path / "parq",
        dlq_dir=tmp_path / "dlq",
    )
    # Insert 20 failures, ascending by created_at.
    for i in range(20):
        s.record_failure(capture_id=f"c{i:02d}", error=f"err{i}", payload_excerpt=f"body {i}")
    assert s.dlq_count() == 20

    # Prune to keep newest 5.
    dropped = s.prune_dlq(max_rows=5)
    assert dropped == 15
    assert s.dlq_count() == 5

    # No-op when already under cap.
    dropped_again = s.prune_dlq(max_rows=5)
    assert dropped_again == 0
    assert s.dlq_count() == 5

    # max_rows=0 wipes the table.
    dropped_zero = s.prune_dlq(max_rows=0)
    assert dropped_zero == 5
    assert s.dlq_count() == 0

    s.close()


def test_crypto_only_text_does_not_get_equities_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synth_capture
) -> None:
    """BUG-BB.1 regression: the first BUG-BB fix treated EVERY ticker hit
    as evidence of equity. EntityLinker resolves alias 'Bitcoin' → ticker
    'BTC-USD', so a crypto-only headline used to come back with
    asset_classes=['crypto', 'equities']. The refinement filters tickers
    by format: -USD/=X/=F/^ prefixes are not equity.
    """
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    reload_settings()
    from catchem.api import create_app

    app = create_app(load_settings())
    cap = synth_capture(
        title="Bitcoin rallies past $80,000 amid ETF inflows",
        text=(
            "Bitcoin pushed past the mark as spot ETF flows accelerated. "
            "Analysts attribute the move to institutional demand."
        ),
        domain="coindesk.com",
    )
    with TestClient(app) as client:
        r = client.post("/process-one", json=cap.model_dump(mode="json"))
        assert r.status_code == 200, r.text
        rec = r.json()
        assert "equities" not in rec["asset_classes"], (
            f"Crypto-only text must not be tagged equities. "
            f"asset_classes={rec['asset_classes']}"
        )
        assert "crypto" in rec["asset_classes"], (
            f"Crypto-only text must surface crypto. "
            f"asset_classes={rec['asset_classes']}"
        )


def test_cashtag_in_text_pushes_equities_into_asset_classes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synth_capture
) -> None:
    """BUG-BB regression: pre-fix the zero-shot stub only flagged equities
    when the literal alias set (`stocks`/`shares`/`equity`) appeared in
    the text. A press release that read `$AAPL rose 4% in after-hours
    trading` carried the ticker but none of the aliases, so the record
    came back with `asset_classes=[]` despite obvious equity relevance.
    The fix bridges entity-linker cashtag/ticker hits into ac_scores
    so equities surfaces alongside the symbol on the record.
    """
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    reload_settings()
    from catchem.api import create_app

    app = create_app(load_settings())
    cap = synth_capture(
        title="Apple beats earnings and raises full-year guidance",
        text=(
            "Apple Inc reported revenue above consensus and raised guidance. "
            "$AAPL rose 4% in after-hours trading on the news."
        ),
    )
    with TestClient(app) as client:
        r = client.post("/process-one", json=cap.model_dump(mode="json"))
        assert r.status_code == 200, r.text
        rec = r.json()
        assert "equities" in rec["asset_classes"], (
            f"Cashtag/ticker hit must surface 'equities' in asset_classes. "
            f"asset_classes={rec['asset_classes']}"
        )


def test_api_process_one_accepts_extra_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synth_capture
) -> None:
    """AwarenessCaptureView has `extra='allow'` — extra fields on the
    inbound payload must NOT trigger a 422. This pins the back-compat
    surface the Awareness producer relies on.
    """
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    reload_settings()
    from catchem.api import create_app

    app = create_app(load_settings())
    with TestClient(app) as client:
        cap = synth_capture()
        payload = cap.model_dump(mode="json")
        payload["unknown_future_field"] = "still allowed"
        r = client.post("/process-one", json=payload)
        assert r.status_code == 200, r.text


def test_storage_extra_coverage(tmp_path: Path) -> None:
    from unittest.mock import MagicMock, patch
    from catchem.storage import _validate_tag
    import sqlite3

    # 1. validate_tag type check
    with pytest.raises(ValueError, match="tag must be a string"):
        _validate_tag(123)  # type: ignore

    # 2. Storage instance setup
    s = Storage(
        db_path=tmp_path / "catchem.sqlite3",
        parquet_dir=tmp_path / "parq",
        dlq_dir=tmp_path / "dlq",
    )

    # 3. _migrate_records_table missing column
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE records (capture_id TEXT PRIMARY KEY, title TEXT)")
    s._migrate_records_table(conn)

    columns = {r["name"] for r in conn.execute("PRAGMA table_info(records)").fetchall()}
    assert "text_excerpt" in columns
    conn.close()

    # 4. rotate_parquet_records auto-flush
    s.rotate_parquet_records = 1
    s.insert_record(_record("c-parq"))
    assert len(s._pending_rows) == 0

    # 5. recent_reviews
    s.upsert_review({
        "capture_id": "c-parq",
        "reviewer_id": "rev-1",
        "reviewer_version": "v1.0",
        "payload_json": {"some": "json"},
        "input_tokens": 10,
        "output_tokens": 20,
        "usd_cost": 0.01,
        "latency_ms": 100,
        "created_at": "2026-06-03T00:00:00",
        "error_code": None,
    })
    revs = s.recent_reviews("rev-1")

    assert len(revs) == 1
    assert revs[0]["reviewer_id"] == "rev-1"

    # 6. review_token_totals with None row (unreachable branch in sqlite covered via mock)
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = None
    class DummyCM:
        def __enter__(self):
            return mock_conn
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass
    with patch.object(s, "_connection", return_value=DummyCM()):
        totals = s.review_token_totals("dummy-rev")
        assert totals == {"input": 0, "output": 0, "calls": 0, "errors": 0}

    # 7. add_holding empty symbol raises ValueError
    with pytest.raises(ValueError, match="symbol must not be empty"):
        s.add_holding("")

    # 8. get_holding and _coerce_float with invalid type/value
    h = s.add_holding("AAPL", shares="invalid-float")
    assert h["shares"] is None
    holding_id = h["id"]
    h_fetched = s.get_holding(holding_id)
    assert h_fetched is not None
    assert h_fetched["symbol"] == "AAPL"
    assert s.get_holding(holding_id + 999) is None

    # 9. except json.JSONDecodeError inside _row_to_review
    with s._connection() as conn:
        conn.execute(
            """INSERT INTO reviews (capture_id, reviewer_id, reviewer_version, payload_json, input_tokens, output_tokens, usd_cost, latency_ms, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("c-invalid-json", "rev-2", "v1.0", "invalid json string", 0, 0, 0.0, 0, "2026-06-03T00:00:00")
        )
    revs_invalid = s.get_reviews_for_capture("c-invalid-json")
    assert len(revs_invalid) == 1
    assert revs_invalid[0]["payload"] == {}

    s.close()

