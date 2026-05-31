"""Regression tests for the D-api bug-hunt group (src/catchem/api.py).

Each test FAILS against the pre-fix code and PASSES after the fix:

  * CSV formula injection in /api/export/records and /api/export/reviews
    (the externally-sourced title/domain/url/list columns must be run
    through archive._csv_safe before they reach a spreadsheet cell).
  * db_import must remove the OLD database's stale `-wal` / `-shm`
    sidecars so SQLite can't replay them onto the imported snapshot.
  * lifespan teardown must isolate each stop() so a failing component
    still lets the supervisor flush run, and must reset the module-level
    `_QUANT_ENGINE` singleton so an in-process restart rebuilds it.
  * the per-request counter must bucket unmatched (no-route) requests
    under a single `<unmatched>` sentinel, not the attacker-varied raw
    path (unbounded `_REQUEST_COUNTS` growth).
"""

from __future__ import annotations

import asyncio
import csv
import sqlite3
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem.schemas import (
    FinancialImpactRecord,
    ProcessingMode,
    SentimentLabel,
)
from catchem.settings import load_settings, reload_settings


def _malicious_record(capture_id: str = "c-evil") -> FinancialImpactRecord:
    """A record whose externally-sourced text columns all begin with a
    spreadsheet formula trigger."""
    return FinancialImpactRecord(
        capture_id=capture_id,
        doc_id=f"d-{capture_id}",
        title="=HYPERLINK(\"http://evil\",\"click\")",
        text_excerpt="body",
        url="@SUM(1+1)",
        domain="-cmd|'/C calc'!A0",
        language="en",
        is_finance_relevant=True,
        finance_relevance_score=0.9,
        asset_classes=["equities"],
        impact_reason_codes=["earnings"],
        candidate_symbols=["=BADSYM"],
        candidate_entities=["Evil"],
        impact_horizons=["one_day"],
        sentiment_label=SentimentLabel.POSITIVE,
        sentiment_score=0.8,
        evidence_sentences=["evidence"],
        reason_text="equities | earnings",
        component_scores={"raw_relevance_score": 0.9},
        processing_mode=ProcessingMode.REPLAY_EXISTING,
        model_versions={"zero_shot": "stub-zero-shot/v1"},
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    reload_settings()
    from catchem.api import create_app

    app = create_app(load_settings())
    c = TestClient(app)
    c.__enter__()
    yield c
    c.__exit__(None, None, None)


def test_create_app_lifespan_uses_explicit_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller-provided Settings object must control startup side effects.

    This pins the factory contract used by isolated smoke/tests: a cached
    process config may allow background tasks, but ``create_app(explicit)``
    must honor the explicit object rather than silently re-reading globals.
    """
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CATCHEM_NEWS__POLLER_ENABLED", "true")
    monkeypatch.setenv("CATCHEM_ARCHIVE__ENABLED", "true")
    reload_settings()
    cached = load_settings()
    assert cached.news.poller_enabled is True
    assert cached.archive.enabled is True

    explicit = cached.model_copy(deep=True)
    explicit.news.poller_enabled = False
    explicit.archive.enabled = False

    from catchem import api as api_mod
    from catchem.api import create_app

    observed: dict[str, bool] = {}

    def _fake_build_poller(supervisor, settings):  # type: ignore[no-untyped-def]
        observed["poller_enabled"] = settings.news.poller_enabled
        if settings.news.poller_enabled:
            raise AssertionError("create_app ignored explicit news.poller_enabled=false")
        return None

    def _fake_build_archiver(supervisor, settings):  # type: ignore[no-untyped-def]
        observed["archive_enabled"] = settings.archive.enabled
        if settings.archive.enabled:
            raise AssertionError("create_app ignored explicit archive.enabled=false")
        return None

    monkeypatch.setattr(api_mod, "_build_news_poller", _fake_build_poller)
    monkeypatch.setattr(api_mod, "_build_archiver", _fake_build_archiver)

    app = create_app(explicit)
    with TestClient(app) as c:
        assert c.get("/healthz").json() == {"status": "ok"}
        assert api_mod._SETTINGS is explicit
        assert observed == {"poller_enabled": False, "archive_enabled": False}


# ── Finding 1 / 5: CSV formula injection in /api/export/records ─────────────


def test_export_records_csv_neutralizes_formula_injection(client: TestClient) -> None:
    import catchem.api as api

    api._SUPERVISOR.storage.insert_record(_malicious_record("c-rec"))

    r = client.get("/api/export/records", params={"format": "csv"})
    assert r.status_code == 200
    rows = list(csv.DictReader(StringIO(r.text)))
    assert rows, "expected at least one exported row"
    row = next(rw for rw in rows if rw["capture_id"] == "c-rec")

    # Every externally-sourced text column must be defused with a leading "'".
    assert row["title"].startswith("'=")
    assert row["url"].startswith("'@")
    assert row["domain"].startswith("'-")
    assert row["candidate_symbols"].startswith("'=")
    # System-generated columns are untouched.
    assert row["capture_id"] == "c-rec"
    assert not row["finance_relevance_score"].startswith("'")


# ── Finding 1 / 5: CSV formula injection in /api/export/reviews ─────────────


def _review_row(capture_id: str, reviewer_id: str, *, sentiment: str) -> dict:
    """A minimal review-storage row dict (shape of ReviewPayload.to_storage_row)."""
    return {
        "capture_id": capture_id,
        "reviewer_id": reviewer_id,
        "reviewer_version": "v1",
        "payload_json": {
            "is_finance_relevant": True,
            "finance_relevance_score": 0.9,
            "sentiment_label": sentiment,
            "asset_classes": ["=evilasset"],
            "impact_reason_codes": ["earnings"],
            "candidate_symbols": ["=BADSYM"],
        },
        "input_tokens": 0,
        "output_tokens": 0,
        "usd_cost": 0.0,
        "latency_ms": 0,
        "created_at": datetime.now(UTC).isoformat(),
        "error_code": None,
    }


def test_export_reviews_csv_neutralizes_formula_injection(client: TestClient) -> None:
    import catchem.api as api

    storage = api._SUPERVISOR.storage
    storage.insert_record(_malicious_record("c-rev"))
    # Seed a stub + deepseek review pair so reviews_with_pair("stub","deepseek")
    # returns it. The malicious title/domain/url come from the record; the
    # malicious sentiment + list fields come from the payloads.
    storage.upsert_review(_review_row("c-rev", "stub", sentiment="=evilsent"))
    storage.upsert_review(_review_row("c-rev", "deepseek", sentiment="positive"))

    r = client.get("/api/export/reviews", params={"format": "csv"})
    assert r.status_code == 200
    rows = list(csv.DictReader(StringIO(r.text)))
    assert rows, "expected the seeded review pair to export"
    row = next(rw for rw in rows if rw["capture_id"] == "c-rev")

    # Record-derived externally-sourced text columns are defused.
    assert row["title"].startswith("'=")
    assert row["url"].startswith("'@")
    assert row["domain"].startswith("'-")
    # Payload-derived text columns are defused too.
    assert row["stub_sentiment"].startswith("'=")
    assert row["stub_assets"].startswith("'=")
    assert row["stub_symbols"].startswith("'=")


def test_csv_safe_helper_escapes() -> None:
    """Pins the shared defense both export serializers rely on (imported from
    .archive into api.py)."""
    from catchem.api import _csv_safe

    assert _csv_safe("=evil") == "'=evil"
    assert _csv_safe("@evil") == "'@evil"
    assert _csv_safe("-evil") == "'-evil"
    assert _csv_safe("+evil") == "'+evil"
    assert _csv_safe("normal") == "normal"
    assert _csv_safe("") == ""


# ── Finding 2: db_import removes stale WAL/SHM sidecars ─────────────────────


def test_db_import_removes_stale_wal_shm_sidecars(client: TestClient) -> None:
    import catchem.api as api

    s = api._SETTINGS or load_settings()
    db_path = s.sqlite_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Simulate the OLD database's WAL mode leaving non-empty sidecars next to
    # the live DB. SQLite ties these to the DB by filename, so an import that
    # leaves them risks replaying stale frames onto the new snapshot.
    wal = db_path.with_name(db_path.name + "-wal")
    shm = db_path.with_name(db_path.name + "-shm")
    wal.write_bytes(b"STALE-WAL-FRAMES")
    shm.write_bytes(b"STALE-SHM")
    assert wal.exists() and shm.exists()

    # Build a valid (minimal) SQLite snapshot to import.
    snap_path = db_path.with_name("snapshot_import.sqlite3")
    conn = sqlite3.connect(snap_path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    content = snap_path.read_bytes()

    r = client.post(
        "/api/db/import",
        files={"file": ("snap.sqlite3", content, "application/octet-stream")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # The stale sidecars belonging to the OLD DB must be gone.
    assert not wal.exists(), "stale -wal sidecar was left on disk (corruption risk)"
    assert not shm.exists(), "stale -shm sidecar was left on disk (corruption risk)"


# ── Finding 6: unmatched requests bucket under a single sentinel ────────────


def test_unmatched_requests_bucket_under_sentinel(client: TestClient) -> None:
    import catchem.api as api

    before = dict(api._REQUEST_COUNTS)

    # POST to several distinct, never-routed paths. There is no POST route for
    # any of these (the GET catch-all does not cover POST), so each resolves
    # to no route.
    for i in range(5):
        client.post(f"/definitely/not/a/route/{i}")

    after = dict(api._REQUEST_COUNTS)
    # The anti-unbounded-growth guarantee: no attacker-varied raw path is ever
    # minted as a key.
    new_keys = set(after) - set(before)
    raw_path_keys = {k for k in new_keys if k.startswith("/definitely/not/a/route/")}
    assert not raw_path_keys, f"raw unmatched paths leaked into counter: {raw_path_keys}"
    # They were absorbed into BOUNDED keys instead — either the SPA catch-all
    # template ("/{full_path:path}", which partial-matches even non-GET methods
    # so Starlette still sets scope["route"]) or the "<unmatched>" sentinel when
    # no route resolves at all. Both are finite, route-derived keys, never the
    # per-request raw path.
    absorbed = api._REQUEST_COUNTS.get("/{full_path:path}", 0) + api._REQUEST_COUNTS.get(
        "<unmatched>", 0
    )
    assert absorbed >= 5, f"requests not absorbed into a bounded key: {after}"


# ── Findings 3 & 5(quant): lifespan teardown isolation + engine reset ───────


def test_lifespan_resets_quant_engine_and_isolates_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    reload_settings()
    import catchem.api as api
    from catchem.api import create_app

    app = create_app(load_settings())

    flush_calls: list[int] = []

    async def _run() -> None:
        async with api.lifespan(app):
            # Force the quant engine to build against THIS supervisor's storage.
            engine = api._get_quant_engine()
            assert engine is not None
            assert api._QUANT_ENGINE is engine

            # Wrap the supervisor's close() so we can prove it still runs even
            # if an earlier teardown step raises.
            orig_close = api._SUPERVISOR.close

            def _tracked_close() -> None:
                flush_calls.append(1)
                orig_close()

            api._SUPERVISOR.close = _tracked_close  # type: ignore[method-assign]

            # Make the archiver's stop() raise a NON-CancelledError. Pre-fix
            # this propagates out of the finally and skips the supervisor
            # close (the parquet flush + pool shutdown). Post-fix each step
            # is isolated so close() still runs.
            class _BoomArchiver:
                async def stop(self) -> None:
                    raise RuntimeError("archiver teardown boom")

            api._ARCHIVER = _BoomArchiver()  # type: ignore[assignment]

        # After the lifespan finally block:
        assert flush_calls == [1], "supervisor flush/close was skipped on teardown error"
        assert api._QUANT_ENGINE is None, "stale _QUANT_ENGINE not reset on shutdown"
        assert api._SUPERVISOR is None

    asyncio.run(_run())
