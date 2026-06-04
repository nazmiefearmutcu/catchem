"""Contract tests for /api/reviews/spend-history.

Backs the 7-day spend sparkline on the Settings → DeepSeek reviewer card.
Tests pin:
  * empty reviews → empty history + zero totals
  * multiple days with calls → per-day aggregation (calls + USD)
  * `days` param clamps to [1, 30] (FastAPI's Query(ge, le))
  * only the `deepseek` reviewer is summed (stub rows excluded)
  * generated_at + schema_version surface for client cache busting
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem.api import create_app
from catchem.settings import load_settings, reload_settings


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A fresh sidecar with an empty DB in tmp_path."""
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CATCHEM_MODE", "production_safe")
    reload_settings()
    app = create_app(load_settings())
    c = TestClient(app)
    c.__enter__()
    yield c
    c.__exit__(None, None, None)


def _insert_review(
    db_path: Path,
    *,
    capture_id: str,
    reviewer_id: str,
    usd_cost: float,
    created_at: datetime,
) -> None:
    """Insert a single row into the reviews table for test setup.

    Uses the same column layout as `Storage.upsert_review` but writes
    directly so the test doesn't need to spin a full review payload.
    """
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30.0)
    try:
        conn.execute(
            """INSERT OR REPLACE INTO reviews (
                capture_id, reviewer_id, reviewer_version, payload_json,
                input_tokens, output_tokens, usd_cost, latency_ms,
                created_at, error_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                capture_id,
                reviewer_id,
                "test-v1",
                "{}",
                0,
                0,
                float(usd_cost),
                0,
                created_at.isoformat(),
                None,
            ),
        )
    finally:
        conn.close()


def _db_path(client: TestClient) -> Path:
    """Pull the live SQLite path off the running supervisor."""
    from catchem.api import _get_supervisor

    sup = _get_supervisor()
    return sup.storage.db_path


def test_spend_history_empty_reviews(client: TestClient) -> None:
    """No reviews in DB → history=[], totals zero, but envelope still valid."""
    r = client.get("/api/reviews/spend-history")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schema_version"] == 1
    assert body["days"] == 7
    assert body["history"] == []
    assert body["totals"] == {"calls": 0, "cost_usd": 0.0}
    assert "generated_at" in body


def test_spend_history_aggregates_multiple_days(client: TestClient) -> None:
    """Two days with multiple calls each → 2 rows, correct USD + count."""
    now = datetime.now(UTC)
    db = _db_path(client)
    # Day 0 (today): 2 calls, $0.01 + $0.02 = $0.03. Both anchored to the SAME
    # instant (`now`) so the two calls always share a calendar day. (A prior
    # `now - 1h` offset straddled midnight when the suite ran in the 00:00-01:00
    # UTC window, splitting Day 0 into two day-rows and failing the count.)
    _insert_review(db, capture_id="cap-A", reviewer_id="deepseek", usd_cost=0.01, created_at=now)
    _insert_review(db, capture_id="cap-B", reviewer_id="deepseek", usd_cost=0.02, created_at=now)
    # Day -2: 1 call, $0.005
    _insert_review(
        db,
        capture_id="cap-C",
        reviewer_id="deepseek",
        usd_cost=0.005,
        created_at=now - timedelta(days=2),
    )
    r = client.get("/api/reviews/spend-history?days=7")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["history"]) == 2
    assert body["totals"]["calls"] == 3
    assert body["totals"]["cost_usd"] == pytest.approx(0.035, abs=1e-6)
    # Newest day first — DB sort order.
    today = body["history"][0]
    assert today["call_count"] == 2
    assert today["total_cost_usd"] == pytest.approx(0.03, abs=1e-6)


def test_spend_history_excludes_non_deepseek_reviewers(client: TestClient) -> None:
    """The stub reviewer's rows must NOT show up in the DeepSeek sparkline."""
    now = datetime.now(UTC)
    db = _db_path(client)
    _insert_review(db, capture_id="cap-1", reviewer_id="stub", usd_cost=0.99, created_at=now)
    _insert_review(db, capture_id="cap-2", reviewer_id="deepseek", usd_cost=0.01, created_at=now)
    r = client.get("/api/reviews/spend-history")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["totals"]["calls"] == 1
    assert body["totals"]["cost_usd"] == pytest.approx(0.01, abs=1e-6)


def test_spend_history_respects_days_window(client: TestClient) -> None:
    """A review older than `days` must be excluded from the aggregate."""
    now = datetime.now(UTC)
    db = _db_path(client)
    # Inside the 3-day window
    _insert_review(
        db, capture_id="cap-fresh", reviewer_id="deepseek", usd_cost=0.05, created_at=now
    )
    # Outside the 3-day window
    _insert_review(
        db,
        capture_id="cap-stale",
        reviewer_id="deepseek",
        usd_cost=99.0,
        created_at=now - timedelta(days=10),
    )
    r = client.get("/api/reviews/spend-history?days=3")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["days"] == 3
    assert body["totals"]["calls"] == 1
    assert body["totals"]["cost_usd"] == pytest.approx(0.05, abs=1e-6)


def test_spend_history_days_param_clamped(client: TestClient) -> None:
    """`days` must satisfy 1 ≤ days ≤ 30; out-of-band values → 422."""
    assert client.get("/api/reviews/spend-history?days=0").status_code == 422
    assert client.get("/api/reviews/spend-history?days=31").status_code == 422
    assert client.get("/api/reviews/spend-history?days=1").status_code == 200
    assert client.get("/api/reviews/spend-history?days=30").status_code == 200


def test_spend_history_status_endpoint_unaffected(client: TestClient) -> None:
    """Sanity guard — the new endpoint must not break /api/reviews/status."""
    r = client.get("/api/reviews/status")
    assert r.status_code == 200, r.text
    body = r.json()
    # Existing contract surfaces — these MUST remain.
    assert "deepseek_enabled" in body
    assert "usd_spent" in body
    assert "usd_cap" in body
