"""Portfolio HTTP surface tests (READ-ONLY holdings tracker).

Covers the four endpoints under ``/api/portfolio``:
  * POST → GET → DELETE round-trip,
  * POST validation (empty symbol → 400),
  * DELETE of an unknown id → 404,
  * GET /api/portfolio/enriched envelope shape + the awareness/quote join
    against a seeded record + the fixture market provider.

Mirrors the TestClient pattern from ``tests/test_tags.py`` /
``tests/test_top_recent_endpoint.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from catchem.api import create_app
from catchem.settings import Settings, load_settings, reload_settings


def _make_client(tmp_settings: Settings) -> TestClient:
    reload_settings()
    s = load_settings()
    return TestClient(create_app(s))


def _seed_record(symbol: str, *, capture_id: str = "cap-port-1", score: float = 0.9) -> None:
    """Insert a finance-relevant record mentioning ``symbol`` into storage."""
    from catchem.api import _get_supervisor  # type: ignore[attr-defined]
    from catchem.schemas import FinancialImpactRecord, ProcessingMode, SentimentLabel

    sup = _get_supervisor()
    rec = FinancialImpactRecord(
        capture_id=capture_id,
        doc_id=f"doc-{capture_id}",
        title=f"{symbol} surges on strong earnings",
        text_excerpt=f"{symbol} reported a beat",
        domain="reuters.com",
        language="en",
        is_finance_relevant=True,
        finance_relevance_score=score,
        asset_classes=["equity"],
        impact_reason_codes=["earnings"],
        candidate_symbols=[symbol],
        candidate_entities=[],
        impact_horizons=["intraday"],
        sentiment_label=SentimentLabel.POSITIVE,
        sentiment_score=0.6,
        evidence_sentences=[],
        reason_text=None,
        component_scores={},
        diagnostic_multimodal_enabled=False,
        diagnostic_multimodal_result=None,
        processing_mode=ProcessingMode.LIVE_TAIL,
        model_versions={},
        published_ts=datetime.now(UTC),
        created_at=datetime.now(UTC),
        url="https://example.com/a",
    )
    sup.storage.insert_record(rec)


def test_portfolio_post_get_delete_roundtrip(tmp_settings: Settings) -> None:
    client = _make_client(tmp_settings)
    with client as c:
        # Empty list to start.
        r = c.get("/api/portfolio")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["schema_version"] == 1
        assert "generated_at" in body
        assert body["holdings"] == []

        # Create.
        r = c.post(
            "/api/portfolio",
            json={"symbol": "aapl", "label": "Core", "shares": 12.5, "cost_basis": 150.0},
        )
        assert r.status_code == 201, r.text
        holding = r.json()
        assert holding["symbol"] == "aapl"  # stored stripped, not upper-cased
        assert holding["label"] == "Core"
        assert holding["shares"] == 12.5
        assert holding["cost_basis"] == 150.0
        assert isinstance(holding["id"], int)
        assert "added_at" in holding
        hid = holding["id"]

        # Read back.
        r = c.get("/api/portfolio")
        assert r.status_code == 200
        holdings = r.json()["holdings"]
        assert len(holdings) == 1
        assert holdings[0]["id"] == hid

        # Delete.
        r = c.delete(f"/api/portfolio/{hid}")
        assert r.status_code == 200
        assert r.json() == {"ok": True}

        # Gone.
        r = c.get("/api/portfolio")
        assert r.json()["holdings"] == []


def test_portfolio_post_rejects_empty_symbol(tmp_settings: Settings) -> None:
    client = _make_client(tmp_settings)
    with client as c:
        r = c.post("/api/portfolio", json={"symbol": "   "})
        assert r.status_code == 400
        r = c.post("/api/portfolio", json={"label": "no symbol key"})
        assert r.status_code == 400


def test_portfolio_delete_unknown_returns_404(tmp_settings: Settings) -> None:
    client = _make_client(tmp_settings)
    with client as c:
        r = c.delete("/api/portfolio/999999")
        assert r.status_code == 404
        assert r.json()["detail"] == "holding_not_found"


def test_portfolio_enriched_envelope_and_join(tmp_settings: Settings) -> None:
    client = _make_client(tmp_settings)
    with client as c:
        # AAPL is a known fixture quote symbol; seed a matching record.
        _seed_record("AAPL")
        r = c.post("/api/portfolio", json={"symbol": "AAPL", "label": "core"})
        assert r.status_code == 201, r.text

        r = c.get("/api/portfolio/enriched")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["schema_version"] == 1
        assert "generated_at" in body
        assert len(body["holdings"]) == 1

        h = body["holdings"][0]
        # Original fields preserved.
        assert h["symbol"] == "AAPL"
        assert h["label"] == "core"
        # Enrichment join present.
        assert h["recent_news_count"] >= 1
        assert isinstance(h["recent_top"], list) and len(h["recent_top"]) >= 1
        assert h["recent_top"][0]["title"].startswith("AAPL")
        assert h["coverage"]["covered"] is True
        assert h["coverage"]["mention_count"] >= 1
        # AAPL fixture quote resolves with a price.
        assert h["quote"] is not None
        assert h["quote"]["last"] == 189.98


def test_portfolio_enriched_empty_is_clean_envelope(tmp_settings: Settings) -> None:
    client = _make_client(tmp_settings)
    with client as c:
        r = c.get("/api/portfolio/enriched")
        assert r.status_code == 200
        body = r.json()
        assert body["schema_version"] == 1
        assert body["holdings"] == []
