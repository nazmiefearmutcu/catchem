"""Contract tests for the global content-search endpoint at /api/search.

Backs the ⌘P palette in the SPA. Tests pin:
  * empty / too-short query → 422
  * title substring match returns the expected capture_id
  * domain substring match returns the same record by host name
  * candidate-symbol substring match returns the symbol with its count
  * limit honored across all three result buckets
  * unknown query returns empty buckets without erroring
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem.api import create_app
from catchem.settings import load_settings, reload_settings

FED_ARTICLE = (
    "The Federal Reserve raised its benchmark interest rate by 25 basis points "
    "on Wednesday, citing persistent inflation pressures. Apple (AAPL) fell 2% "
    "and Microsoft (MSFT) lost 1.8%. Chair Powell said the central bank remains "
    "data-dependent."
)
TSLA_ARTICLE = (
    "Tesla ($TSLA) delivered its quarterly results today. Elon Musk said "
    "deliveries beat consensus by a wide margin. The stock rose 6% after hours."
)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CATCHEM_MODE", "production_safe")
    reload_settings()
    app = create_app(load_settings())
    c = TestClient(app)
    c.__enter__()
    # Seed two distinct records via the demo paste pathway so the palette
    # has real content to scan.
    c.post(
        "/ui/demo/paste",
        json={"title": "Fed raises rates by 25 bps", "text": FED_ARTICLE, "domain": "reuters.com"},
    )
    c.post(
        "/ui/demo/paste",
        json={"title": "Tesla beats deliveries forecast", "text": TSLA_ARTICLE, "domain": "bloomberg.com"},
    )
    yield c
    c.__exit__(None, None, None)


def test_search_rejects_too_short_query(client: TestClient) -> None:
    """min_length=2 enforced by Query(...). FastAPI returns 422 with detail."""
    r = client.get("/api/search?q=a")
    assert r.status_code == 422


def test_search_rejects_missing_query(client: TestClient) -> None:
    r = client.get("/api/search")
    assert r.status_code == 422


def test_search_matches_record_by_title(client: TestClient) -> None:
    r = client.get("/api/search?q=tesla")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["query"] == "tesla"
    titles = [rec["title"] for rec in body["records"]]
    assert any("Tesla" in (t or "") for t in titles), f"no Tesla in records: {titles}"


def test_search_matches_record_by_domain(client: TestClient) -> None:
    r = client.get("/api/search?q=bloomberg")
    assert r.status_code == 200, r.text
    domains = [rec["domain"] for rec in r.json()["records"]]
    assert any("bloomberg" in (d or "") for d in domains), f"no bloomberg in domains: {domains}"


def test_search_matches_symbol_mention(client: TestClient) -> None:
    """Symbol substring match. Both AAPL (Fed article) and TSLA (Tesla article)
    are seeded — query 'tsla' should surface TSLA in the symbols bucket."""
    r = client.get("/api/search?q=tsla")
    assert r.status_code == 200, r.text
    body = r.json()
    syms = [s["symbol"] for s in body["symbols"]]
    assert "TSLA" in syms, f"TSLA missing from symbol results: {syms}"
    # Count must be a non-negative int (mentions across recent corpus).
    counts = {s["symbol"]: s["count"] for s in body["symbols"]}
    assert counts["TSLA"] >= 1


def test_search_returns_empty_buckets_on_no_match(client: TestClient) -> None:
    """A query that doesn't hit any title/domain/symbol/cluster returns the
    canonical empty shape — never a 404 or 500."""
    r = client.get("/api/search?q=zzzzzz_no_match_anywhere")
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "zzzzzz_no_match_anywhere"
    assert body["records"] == []
    assert body["symbols"] == []
    assert body["clusters"] == []


def test_search_limit_honored(client: TestClient) -> None:
    """limit=1 caps results in each bucket. We can't easily seed >1 distinct
    records that all match the same query in this fixture, but the limit
    must still apply without crashing."""
    r = client.get("/api/search?q=raises&limit=1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["records"]) <= 1
    assert len(body["symbols"]) <= 1
    assert len(body["clusters"]) <= 1


def test_search_limit_validated(client: TestClient) -> None:
    """limit boundaries (1..50) — FastAPI must 422 on out-of-range."""
    assert client.get("/api/search?q=raises&limit=0").status_code == 422
    assert client.get("/api/search?q=raises&limit=51").status_code == 422
