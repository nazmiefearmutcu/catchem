"""Tests for ``GET /api/symbols/{symbol}/sentiment-trend``.

The endpoint backs the Symbol Detail page's stacked sentiment-area chart
and the 30-day mention-velocity sparkline. These tests pin down:

  * shape contract — symbol, days, series ordering, zero-fill rules,
  * symbol resolution via ``record_labels`` (exact match on the label,
    not a substring LIKE — otherwise ``AAP`` would match ``AAPL`` mentions),
  * day bucketing under UTC,
  * empty-symbol fallback (zero-filled window, never a 404),
  * windowing via ``days`` query param within the documented bounds.

The tests boot the full FastAPI app via TestClient so we exercise the
exact code path the UI sees, including the supervisor / storage hookup.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem.api import create_app
from catchem.schemas import FinancialImpactRecord, ProcessingMode, SentimentLabel
from catchem.settings import load_settings, reload_settings


def _record(
    capture_id: str,
    *,
    symbols: list[str],
    sentiment: SentimentLabel | None,
    published_ts: datetime | None,
    domain: str = "reuters.com",
) -> FinancialImpactRecord:
    """Build a finance-relevant record with the bits the endpoint reads."""
    return FinancialImpactRecord(
        capture_id=capture_id,
        doc_id=f"d-{capture_id}",
        title=f"Test record {capture_id}",
        text_excerpt="example body for sentiment-trend tests",
        url=f"https://{domain}/{capture_id}",
        domain=domain,
        language="en",
        is_finance_relevant=True,
        finance_relevance_score=0.7,
        asset_classes=["equities"],
        impact_reason_codes=["earnings"],
        candidate_symbols=symbols,
        candidate_entities=[],
        impact_horizons=["one_day"],
        sentiment_label=sentiment,
        sentiment_score=0.5 if sentiment else None,
        evidence_sentences=["evidence"],
        reason_text="equities | earnings",
        component_scores={"raw_relevance_score": 0.7},
        processing_mode=ProcessingMode.REPLAY_EXISTING,
        model_versions={"zero_shot": "stub-zero-shot/v1"},
        published_ts=published_ts,
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def client_with_trend_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Boot the FastAPI app + seed three AAPL records on distinct UTC days.

    Records distribution (relative to ``today``):
      * AAPL, positive, today
      * AAPL, positive, today  (same day — count should add)
      * AAPL, negative, today - 2d
      * MSFT, neutral,  today  (different symbol — must not leak)

    A fourth AAPL record carries ``published_ts=None`` and must be
    excluded by the ``IS NOT NULL`` filter.
    """
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    reload_settings()
    settings = load_settings()
    app = create_app(settings)
    client = TestClient(app)
    client.__enter__()  # trigger lifespan so the supervisor is initialised

    from catchem.api import _get_supervisor

    storage = _get_supervisor().storage
    today = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    storage.insert_record(
        _record("c-1", symbols=["AAPL"], sentiment=SentimentLabel.POSITIVE, published_ts=today),
    )
    storage.insert_record(
        _record("c-2", symbols=["AAPL"], sentiment=SentimentLabel.POSITIVE, published_ts=today),
    )
    storage.insert_record(
        _record(
            "c-3",
            symbols=["AAPL"],
            sentiment=SentimentLabel.NEGATIVE,
            published_ts=today - timedelta(days=2),
        ),
    )
    storage.insert_record(
        _record(
            "c-4",
            symbols=["MSFT"],
            sentiment=SentimentLabel.NEUTRAL,
            published_ts=today,
        ),
    )
    storage.insert_record(
        _record(
            "c-5-no-ts",
            symbols=["AAPL"],
            sentiment=SentimentLabel.POSITIVE,
            published_ts=None,
        ),
    )
    yield client
    client.__exit__(None, None, None)


def test_sentiment_trend_shape_and_zero_fill(client_with_trend_records: TestClient) -> None:
    """Endpoint returns one row per UTC day in the window, in ascending order."""
    r = client_with_trend_records.get("/api/symbols/AAPL/sentiment-trend?days=7")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["symbol"] == "AAPL"
    assert body["days"] == 7
    assert isinstance(body["series"], list)
    assert len(body["series"]) == 7  # zero-filled window
    days = [row["day"] for row in body["series"]]
    assert days == sorted(days), "series must be ascending by day"
    for row in body["series"]:
        # Contract: every row carries all three buckets as integers.
        for key in ("positive", "neutral", "negative"):
            assert key in row
            assert isinstance(row[key], int)


def test_sentiment_trend_counts_today_records(client_with_trend_records: TestClient) -> None:
    """Two AAPL/positive records published today must yield positive=2 on the latest day."""
    r = client_with_trend_records.get("/api/symbols/AAPL/sentiment-trend?days=7")
    body = r.json()
    # Latest row = today (UTC). Two positive records were inserted with
    # today's timestamp; one negative was 2 days back.
    latest = body["series"][-1]
    assert latest["positive"] == 2
    assert latest["neutral"] == 0
    assert latest["negative"] == 0
    # The negative record is 2 days back (index -3 in the 7-day window).
    two_days_back = body["series"][-3]
    assert two_days_back["negative"] == 1
    assert two_days_back["positive"] == 0
    # Sum across the window must equal 3 (two pos today + one neg 2 days back).
    totals = sum(d["positive"] + d["neutral"] + d["negative"] for d in body["series"])
    assert totals == 3


def test_sentiment_trend_isolates_by_symbol(client_with_trend_records: TestClient) -> None:
    """MSFT records must not appear in AAPL's trend and vice-versa.

    Guards against the naive LIKE-on-JSON pitfall the spec warned about
    (``candidate_symbols LIKE '%X%'`` would smear ``AAP`` into ``AAPL``).
    The endpoint joins ``record_labels`` so the match is exact.
    """
    r = client_with_trend_records.get("/api/symbols/MSFT/sentiment-trend?days=7")
    body = r.json()
    totals = sum(d["positive"] + d["neutral"] + d["negative"] for d in body["series"])
    assert totals == 1  # exactly the one MSFT/neutral record
    latest = body["series"][-1]
    assert latest["neutral"] == 1
    assert latest["positive"] == 0
    assert latest["negative"] == 0


def test_sentiment_trend_unknown_symbol_returns_zero_filled(client_with_trend_records: TestClient) -> None:
    """Unknown symbols must return a zero-filled window — never a 404."""
    r = client_with_trend_records.get("/api/symbols/NOPE/sentiment-trend?days=7")
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "NOPE"
    assert len(body["series"]) == 7
    totals = sum(d["positive"] + d["neutral"] + d["negative"] for d in body["series"])
    assert totals == 0


def test_sentiment_trend_days_param_bounds(client_with_trend_records: TestClient) -> None:
    """``days`` honours its 1..30 bounds and shapes the series accordingly."""
    r1 = client_with_trend_records.get("/api/symbols/AAPL/sentiment-trend?days=1")
    assert r1.status_code == 200
    assert len(r1.json()["series"]) == 1

    r30 = client_with_trend_records.get("/api/symbols/AAPL/sentiment-trend?days=30")
    assert r30.status_code == 200
    assert len(r30.json()["series"]) == 30

    # 0 and 31 must be rejected by the Query validator.
    assert client_with_trend_records.get("/api/symbols/AAPL/sentiment-trend?days=0").status_code == 422
    assert client_with_trend_records.get("/api/symbols/AAPL/sentiment-trend?days=31").status_code == 422
