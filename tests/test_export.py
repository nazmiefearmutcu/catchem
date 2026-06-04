"""Contract tests for the analyst-workflow export endpoints.

Endpoints covered:
  * GET /api/export/records  — CSV / JSON, filterable
  * GET /api/export/reviews  — paired (stub, DeepSeek) reviews
  * GET /api/export/quant    — nested quant-signal snapshot (JSON only)

Each test goes through the full lifespan-managed TestClient so the
supervisor + storage actually open the SQLite DB; the records seeded via
the demo paste pathway prove the export endpoints read from the same
truth-store the live UI does.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem.api import create_app
from catchem.rate_limit import reset_all_buckets
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


@pytest.fixture(autouse=True)
def _reset_rate_limit() -> None:
    """Reset module-level rate-limit buckets between tests.

    /api/export/* uses cost=3 on the default bucket — several sequential
    tests would otherwise drain it and the last one would hit a spurious
    429. Same pattern as test_db_management.py.
    """
    reset_all_buckets()
    yield
    reset_all_buckets()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CATCHEM_MODE", "production_safe")
    reload_settings()
    app = create_app(load_settings())
    c = TestClient(app)
    c.__enter__()
    # Seed two distinct finance-relevant records so filters have something
    # to bite on.
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


# ── /api/export/records ──────────────────────────────────────────────────────


def test_records_json_export_returns_filename_and_count(client: TestClient) -> None:
    r = client.get("/api/export/records?format=json&limit=10")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    disp = r.headers["content-disposition"]
    assert "attachment" in disp
    assert "catchem_records_" in disp and disp.endswith('.json"')
    body = r.json()
    assert "items" in body and "count" in body and "exported_at" in body
    assert body["count"] == len(body["items"])
    assert body["count"] >= 2  # we seeded two records


def test_records_csv_export_has_header_and_row_count_matches(client: TestClient) -> None:
    r = client.get("/api/export/records?format=csv&limit=10")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert "catchem_records_" in r.headers["content-disposition"]
    reader = csv.DictReader(io.StringIO(r.text))
    rows = list(reader)
    # Header should contain the load-bearing analyst columns
    assert "capture_id" in reader.fieldnames
    assert "asset_classes" in reader.fieldnames
    assert "impact_reason_codes" in reader.fieldnames
    assert "candidate_symbols" in reader.fieldnames
    assert len(rows) >= 2
    # List fields must be ';'-joined strings, not Python repr.
    for row in rows:
        ac = row["asset_classes"]
        assert "[" not in ac and "'" not in ac, f"list field looks like repr: {ac!r}"


def test_records_invalid_format_returns_422(client: TestClient) -> None:
    r = client.get("/api/export/records?format=xml")
    assert r.status_code == 422


def test_records_min_score_filter_drops_low_relevance(client: TestClient) -> None:
    """min_score=1.0 is unreachable by the stub scorer, so we expect 0 rows."""
    r = client.get("/api/export/records?format=json&min_score=1.0&limit=100")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 0
    assert body["items"] == []
    assert body["filters"]["min_score"] == 1.0


def test_records_symbol_filter_returns_matching_subset(client: TestClient) -> None:
    """Filtering by symbol must return ONLY records carrying that symbol."""
    # First confirm at least one seeded record carries TSLA.
    full = client.get("/api/export/records?format=json&limit=100").json()
    has_tsla = [r for r in full["items"] if "TSLA" in (r.get("candidate_symbols") or [])]
    if not has_tsla:
        pytest.skip("stub scorer did not extract TSLA from the seed article")
    r = client.get("/api/export/records?format=json&symbol=TSLA&limit=100")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1
    for it in body["items"]:
        assert "TSLA" in (it.get("candidate_symbols") or [])


def test_records_limit_honored(client: TestClient) -> None:
    r = client.get("/api/export/records?format=json&limit=1")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] <= 1
    assert body["filters"]["limit"] == 1


# ── /api/export/reviews ──────────────────────────────────────────────────────


def test_reviews_json_export_shape(client: TestClient) -> None:
    """No DeepSeek pairs in production_safe-without-key, but envelope must validate."""
    r = client.get("/api/export/reviews?format=json&limit=50")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) >= {"items", "count", "exported_at", "filters"}
    assert isinstance(body["items"], list)
    assert body["count"] == len(body["items"])


def test_reviews_csv_export_has_pair_columns(client: TestClient) -> None:
    r = client.get("/api/export/reviews?format=csv&limit=50")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    reader = csv.DictReader(io.StringIO(r.text))
    cols = set(reader.fieldnames or ())
    assert {"capture_id", "stub_score", "ds_score", "agreement_overall"} <= cols


# ── /api/export/quant ────────────────────────────────────────────────────────


def test_quant_json_export_returns_signals_envelope(client: TestClient) -> None:
    r = client.get("/api/export/quant?format=json&limit=50")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "signals" in body
    assert "exported_at" in body
    assert body["limit"] == 50
    assert isinstance(body["signals"], dict)


def test_quant_csv_returns_415(client: TestClient) -> None:
    """Quant signals are nested; CSV would be lossy → 415."""
    r = client.get("/api/export/quant?format=csv")
    assert r.status_code == 415
    detail = r.json().get("detail", "")
    assert "json" in str(detail).lower()
