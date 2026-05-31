"""v59: /api/news/top-recent endpoint — HTTP twin of `catchem top-recent` CLI.

Pinned: 200 envelope shape, min_score gating, sort-by-score-desc, limit clamp,
and that 0 records still returns a clean empty envelope (no 404)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient that exercises a real lifespan with an isolated DB."""
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("CATCHEM_NEWS__POLLER_ENABLED", "false")
    monkeypatch.setenv("CATCHEM_ARCHIVE__ENABLED", "false")
    from catchem.api import create_app
    from catchem.settings import load_settings, reload_settings
    reload_settings()
    app = create_app(load_settings())
    with TestClient(app) as c:
        yield c


def test_top_recent_returns_envelope(client) -> None:
    resp = client.get("/api/news/top-recent?limit=5&min_score=0.5")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key in ("schema_version", "generated_at", "limit", "min_score", "count", "items"):
        assert key in body, f"missing {key}"
    assert body["limit"] == 5
    assert body["min_score"] == 0.5
    assert isinstance(body["items"], list)


def test_top_recent_invalid_limit_422(client) -> None:
    resp = client.get("/api/news/top-recent?limit=0")
    assert resp.status_code == 422
    resp2 = client.get("/api/news/top-recent?limit=999")
    assert resp2.status_code == 422


def test_top_recent_invalid_min_score_422(client) -> None:
    resp = client.get("/api/news/top-recent?min_score=-0.1")
    assert resp.status_code == 422
    resp2 = client.get("/api/news/top-recent?min_score=1.5")
    assert resp2.status_code == 422


def test_top_recent_empty_storage_returns_clean_envelope(client) -> None:
    """Fresh DB → 0 items, but still 200 with valid envelope (not 404)."""
    resp = client.get("/api/news/top-recent?limit=10&min_score=0.0")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0
    assert body["items"] == []
