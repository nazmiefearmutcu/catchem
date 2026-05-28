"""Endpoint coverage for the quant/diagnostics + db-stats HTTP surfaces.

Targets three under-tested read-only endpoints:

  * ``GET /api/quant/diagnostics`` — QuantEngine fail-soft observability.
    Healthy steady state on stub data is an EMPTY failure ring.
  * ``GET /api/db/stats``          — per-table SQLite row counts + indexes.
  * ``GET /api/news/top-recent``   — highest-scoring recent records, the
    deeper sort-desc + ``min_score`` gating invariants on SEEDED data
    (the existing ``test_top_recent_endpoint.py`` only pins the empty /
    validation envelope cases).

Pattern mirrors ``tests/test_ui_endpoints.py`` /
``tests/test_storage_and_api.py``: build the app via ``create_app`` under a
real lifespan with an isolated temp DB, then seed storage through the live
supervisor via ``POST /process-one`` so the row-count + ranking assertions
exercise the same path the production sidecar uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem.api import create_app
from catchem.settings import load_settings, reload_settings


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient over a real lifespan with an isolated DB.

    Background tasks (news poller / drive archiver) are disabled so the DB
    only ever contains rows this test explicitly seeds — the diagnostics
    ring buffer + db-stats counts stay deterministic.
    """
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("CATCHEM_NEWS__POLLER_ENABLED", "false")
    monkeypatch.setenv("CATCHEM_ARCHIVE__ENABLED", "false")
    reload_settings()
    app = create_app(load_settings())
    with TestClient(app) as c:
        yield c


@pytest.fixture
def seeded_client(client: TestClient, synth_capture) -> TestClient:
    """``client`` with two finance-relevant records processed into storage.

    Uses two distinct-score captures so the top-recent ranking + min_score
    gating have something to bite on:
      * a Fed rate headline (default synth_capture body)
      * an Apple earnings beat headline ($AAPL cashtag → equities)
    """
    cap_fed = synth_capture(capture_id="qd-fed", doc_id="qd-d1")
    cap_aapl = synth_capture(
        capture_id="qd-aapl",
        doc_id="qd-d2",
        title="Apple beats earnings and raises full-year guidance",
        text=(
            "Apple Inc reported revenue above consensus and raised guidance. "
            "$AAPL rose 4% in after-hours trading on the news."
        ),
        domain="wsj.com",
    )
    for cap in (cap_fed, cap_aapl):
        r = client.post("/process-one", json=cap.model_dump(mode="json"))
        assert r.status_code == 200, r.text
    return client


# ──────────────────────────────────────────────────────────────────────────
# GET /api/quant/diagnostics
# ──────────────────────────────────────────────────────────────────────────

def test_quant_diagnostics_shape_and_healthy_steady_state(client: TestClient) -> None:
    """The documented envelope + the healthy steady state (no failures).

    On stub models with no signal having crashed, the in-process ring buffer
    is empty: ``total_failures == 0``, ``per_signal == {}``, ``recent == []``.
    ``buffer_capacity`` is the deque maxlen — a fixed positive bound.
    """
    # The failure ring is module-level process state; clear it so an
    # unrelated earlier test in the same process can't leak a failure in.
    from catchem.quant.engine import _diagnostics_clear

    _diagnostics_clear()

    r = client.get("/api/quant/diagnostics")
    assert r.status_code == 200, r.text
    data = r.json()

    for key in (
        "schema_version",
        "generated_at",
        "total_failures",
        "per_signal",
        "recent",
        "buffer_capacity",
    ):
        assert key in data, f"missing {key}"

    assert data["schema_version"] == 1
    assert isinstance(data["generated_at"], str) and data["generated_at"]
    # Healthy steady state.
    assert data["total_failures"] == 0
    assert data["per_signal"] == {}
    assert data["recent"] == []
    # Ring-buffer capacity is a fixed positive bound (deque maxlen).
    assert isinstance(data["buffer_capacity"], int)
    assert data["buffer_capacity"] > 0


def test_quant_diagnostics_total_failures_matches_recent_and_counts(
    client: TestClient,
) -> None:
    """Cross-field invariants hold regardless of how many failures exist.

    ``total_failures`` must equal ``len(recent)`` and the sum of the
    ``per_signal`` counts — the endpoint derives all three from one
    snapshot, so they can never disagree.
    """
    r = client.get("/api/quant/diagnostics")
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["total_failures"] == len(data["recent"])
    assert data["total_failures"] == sum(data["per_signal"].values())
    # recent is newest-first; entries (if any) carry a per-signal label.
    for entry in data["recent"]:
        assert "signal" in entry


# ──────────────────────────────────────────────────────────────────────────
# GET /api/db/stats
# ──────────────────────────────────────────────────────────────────────────

def test_db_stats_shape_and_core_tables_present(seeded_client: TestClient) -> None:
    """Per-table rows + index summary, with the schema-bootstrap tables."""
    r = seeded_client.get("/api/db/stats")
    assert r.status_code == 200, r.text
    data = r.json()

    for key in (
        "schema_version",
        "generated_at",
        "tables",
        "indexes",
        "total_tables",
        "total_indexes",
        "page_count",
        "page_size_bytes",
        "estimated_size_bytes",
    ):
        assert key in data, f"missing {key}"

    assert isinstance(data["tables"], list)
    assert isinstance(data["indexes"], list)
    # Counters must agree with the arrays they summarize.
    assert data["total_tables"] == len(data["tables"])
    assert data["total_indexes"] == len(data["indexes"])

    # Each table entry has a name + an int row count (>= 0; -1 only on a
    # query failure, which must not happen on a healthy DB).
    table_names = set()
    for t in data["tables"]:
        assert set(t.keys()) >= {"name", "rows"}
        assert isinstance(t["rows"], int) and t["rows"] >= 0, t
        table_names.add(t["name"])

    # The schema bootstrap guarantees these three core tables exist.
    assert {"records", "reviews", "dlq"} <= table_names, table_names

    for ix in data["indexes"]:
        assert set(ix.keys()) >= {"name", "table"}


def test_db_stats_records_count_reflects_seeded_rows(
    seeded_client: TestClient,
) -> None:
    """The ``records`` row count tracks the two seeded captures.

    Cross-checked against ``/api/stats``' own ``db.records`` counter so the
    two telemetry surfaces agree on the same underlying table.
    """
    r = seeded_client.get("/api/db/stats")
    assert r.status_code == 200, r.text
    data = r.json()
    rows_by_name = {t["name"]: t["rows"] for t in data["tables"]}
    assert rows_by_name["records"] >= 2

    # Page geometry is internally consistent.
    assert data["page_count"] >= 0
    assert data["page_size_bytes"] >= 0
    assert data["estimated_size_bytes"] == data["page_count"] * data["page_size_bytes"]

    # Agreement with /api/stats db.records on the same live DB.
    r2 = seeded_client.get("/api/stats")
    assert r2.status_code == 200, r2.text
    assert r2.json()["db"]["records"] == rows_by_name["records"]


# ──────────────────────────────────────────────────────────────────────────
# GET /api/news/top-recent
# ──────────────────────────────────────────────────────────────────────────

def test_top_recent_items_shape_and_sorted_desc(seeded_client: TestClient) -> None:
    """Items carry the documented fields and are sorted by score descending."""
    r = seeded_client.get("/api/news/top-recent?limit=10&min_score=0.0")
    assert r.status_code == 200, r.text
    data = r.json()

    for key in ("schema_version", "generated_at", "limit", "min_score", "count", "items"):
        assert key in data, f"missing {key}"
    assert data["limit"] == 10
    assert data["min_score"] == 0.0
    assert data["count"] == len(data["items"])
    # Both seeded records are finance-relevant → surface at min_score=0.
    assert data["count"] >= 2

    item_keys = {
        "capture_id", "title", "domain", "url", "score",
        "sentiment", "asset_classes", "symbols", "published_ts",
    }
    scores: list[float] = []
    for it in data["items"]:
        assert item_keys <= set(it.keys()), it
        assert isinstance(it["asset_classes"], list)
        assert isinstance(it["symbols"], list)
        scores.append(float(it["score"] or 0.0))

    # Sorted by score, descending.
    assert scores == sorted(scores, reverse=True), scores


def test_top_recent_respects_min_score_gate(seeded_client: TestClient) -> None:
    """A high ``min_score`` floor drops everything below it.

    Every returned item must clear the floor, and a near-1.0 floor that no
    stub record can reach yields a clean empty list (count == 0), never a
    404 or an unfiltered payload.
    """
    # All items returned at a mid floor must clear that floor.
    mid = seeded_client.get("/api/news/top-recent?limit=10&min_score=0.5")
    assert mid.status_code == 200, mid.text
    for it in mid.json()["items"]:
        assert float(it["score"] or 0.0) >= 0.5, it

    # Raising the floor cannot return MORE items than a lower floor.
    low = seeded_client.get("/api/news/top-recent?limit=10&min_score=0.0")
    assert low.status_code == 200
    assert mid.json()["count"] <= low.json()["count"]

    # An unreachable floor → empty, but still a well-formed 200 envelope.
    top = seeded_client.get("/api/news/top-recent?limit=10&min_score=1.0")
    assert top.status_code == 200, top.text
    body = top.json()
    assert body["min_score"] == 1.0
    assert body["count"] == 0
    assert body["items"] == []


def test_top_recent_limit_clamps_returned_count(seeded_client: TestClient) -> None:
    """``limit`` caps the number of items even when more qualify."""
    r = seeded_client.get("/api/news/top-recent?limit=1&min_score=0.0")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["limit"] == 1
    assert data["count"] <= 1
    assert len(data["items"]) <= 1
