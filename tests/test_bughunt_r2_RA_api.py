"""Bug-hunt Round 2 — file group RA-api regression tests.

Each test FAILS on the pre-fix ``src/catchem/api.py`` and PASSES after the
fix. They are deliberately self-contained: a TestClient with a full lifespan
gives us a live ``_SUPERVISOR`` / ``_SETTINGS`` so the endpoints behave
exactly as in production.

Findings covered:
  1. Cluster drill-down re-clustered with a hardcoded limit=2000 corpus, so
     a cluster_id from a different window 404'd. Fixed: ``window`` query param
     threads the dashboard window into the re-cluster.
  2. ``/api/quant/record/{id}/detail`` bypassed production-safe redaction.
  3. Quant cache post-ingest invalidation hook was dead — manual poll-now now
     drops the quant cache after a non-empty ingest.
  4. ``api_symbol_sentiment_trend`` bucketed by local-offset day and compared
     lexicographically — now UTC-normalized in SQL.
  5. ``reviews_patch_settings`` corrupted null model/base_url to "None".
  6. ``reviews_patch_settings`` 500'd on non-numeric sampling_rate/usd_cap.
  7. ``ui_demo_paste`` enforced the byte cap against char count.
  8. ``_get_quant_engine`` lazy singleton had no lock.
  9. ``db_export`` streamed only the main file, omitting un-checkpointed WAL.
 10. ``db_import`` swapped the DB file without holding ``storage._lock``.
 11. ``db_import`` buffered the whole upload into RAM before the cap check.
 12. Streaming live-read skipped spend accounting when usage frame omitted.
"""

from __future__ import annotations

import io
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem import api as api_mod
from catchem.api import create_app
from catchem.rate_limit import reset_all_buckets
from catchem.schemas import FinancialImpactRecord, ProcessingMode, SentimentLabel
from catchem.settings import load_settings, reload_settings


SQLITE_MAGIC = b"SQLite format 3\x00"


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    reset_all_buckets()
    yield
    reset_all_buckets()


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    reload_settings()
    s = load_settings()
    app = create_app(s)
    c = TestClient(app)
    c.__enter__()
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


def _make_record(
    *,
    capture_id: str,
    title: str,
    domain: str,
    published_ts: datetime,
    created_at: datetime,
    symbols: list[str] | None = None,
    sentiment: SentimentLabel = SentimentLabel.POSITIVE,
    diagnostic_result: dict | None = None,
) -> FinancialImpactRecord:
    return FinancialImpactRecord(
        capture_id=capture_id,
        doc_id=f"d-{capture_id}",
        title=title,
        text_excerpt=title,
        url=f"https://{domain}/{capture_id}",
        domain=domain,
        language="en",
        is_finance_relevant=True,
        finance_relevance_score=0.8,
        asset_classes=["equities"],
        impact_reason_codes=["earnings"],
        candidate_symbols=symbols or ["AAPL"],
        candidate_entities=["Apple"],
        impact_horizons=["one_day"],
        sentiment_label=sentiment,
        sentiment_score=0.8,
        evidence_sentences=[title],
        reason_text="equities | earnings",
        component_scores={"asset_class_max": 0.8, "raw_relevance_score": 0.8},
        processing_mode=ProcessingMode.REPLAY_EXISTING,
        model_versions={"zero_shot": "stub/v1"},
        diagnostic_multimodal_enabled=bool(diagnostic_result),
        diagnostic_multimodal_result=diagnostic_result,
        published_ts=published_ts,
        created_at=created_at,
    )


# --------------------------------------------------------------------------- #
# Finding 1 — cluster drill-down window mismatch 404                          #
# --------------------------------------------------------------------------- #


def test_f1_cluster_drilldown_recovers_with_matching_window(client: TestClient) -> None:
    """A cluster_id computed over a window-of-N corpus is recoverable by the
    drill-down ONLY when the same window is threaded back in.

    Pre-fix the route re-clustered with a hardcoded limit=2000, so the
    cluster_id from a 3-record window was absent → 404. Post-fix the route
    accepts ``window`` and reproduces the same corpus → 200.
    """
    sup = api_mod._SUPERVISOR
    base_pub = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    base_created = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)

    # 12 near-identical finance stories across 2 domains inside one 30-min
    # window — they form a single multi-source event cluster. (12 vs 10 so we
    # straddle the endpoint's window floor of 10.)
    titles = [
        f"Apple beats earnings as iPhone revenue jumps sharply, note {i}"
        for i in range(12)
    ]
    for i, title in enumerate(titles):
        dom = "reuters.com" if i % 2 == 0 else "cnbc.com"
        rec = _make_record(
            capture_id=f"clu-{i:02d}",
            title=title,
            domain=dom,
            published_ts=base_pub + timedelta(seconds=i * 60),
            # created_at strictly increasing so recent_records(limit=10) selects
            # the 10 NEWEST (clu-02..clu-11) — a different member set than the
            # full window-of-12.
            created_at=base_created + timedelta(seconds=i * 10),
        )
        assert sup.storage.insert_record(rec) is True

    engine = api_mod._get_quant_engine()

    # Cluster over the 10-newest window. cluster_id hashes those member ids.
    clusters_w10 = engine.clusters(limit=10)
    assert clusters_w10, "expected a cluster in the 10-record window"
    cid_w10 = clusters_w10[0].cluster_id
    members_w10 = set(clusters_w10[0].capture_ids)

    # Cluster over the full 12-record window — DIFFERENT membership → diff id.
    clusters_w12 = engine.clusters(limit=12)
    assert clusters_w12
    cid_w12 = clusters_w12[0].cluster_id
    assert cid_w10 != cid_w12, "windows must yield distinct cluster_ids (precondition)"
    assert members_w10 != set(clusters_w12[0].capture_ids)

    # Pre-fix behaviour: hitting the route at a DIFFERENT window re-clusters
    # at a wider corpus and 404s the 10-window cluster_id.
    r_default = client.get(f"/api/quant/cluster/{cid_w10}/members?window=12")
    assert r_default.status_code == 404

    # Post-fix: passing window=10 reproduces the corpus and recovers the cluster.
    r_match = client.get(f"/api/quant/cluster/{cid_w10}/members?window=10")
    assert r_match.status_code == 200, r_match.text
    body = r_match.json()
    assert body["cluster_id"] == cid_w10
    assert set(m["capture_id"] for m in body["members"]) == members_w10


# --------------------------------------------------------------------------- #
# Finding 2 — record detail redaction bypass                                  #
# --------------------------------------------------------------------------- #


def test_f2_record_detail_redacts_diagnostic_in_production_safe(client: TestClient) -> None:
    """The drill-down detail endpoint must scrub the diagnostic blob in
    production_safe mode, like every other record route."""
    sup = api_mod._SUPERVISOR
    assert api_mod._SETTINGS.is_production_safe(), "default mode is production_safe"

    rec = _make_record(
        capture_id="diag-1",
        title="Sensitive multimodal record",
        domain="reuters.com",
        published_ts=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
        created_at=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
        diagnostic_result={"leak": "should-be-scrubbed", "score": 0.99},
    )
    assert sup.storage.insert_record(rec) is True

    # Storage faithfully round-trips the diagnostic blob...
    raw = sup.storage.get_record("diag-1")
    assert raw["diagnostic_multimodal_result"] == {"leak": "should-be-scrubbed", "score": 0.99}

    # ...but the API drill-down must NOT leak it in production_safe mode.
    r = client.get("/api/quant/record/diag-1/detail")
    assert r.status_code == 200, r.text
    record = r.json()["record"]
    assert record["diagnostic_multimodal_enabled"] is False
    assert record["diagnostic_multimodal_result"] is None


# --------------------------------------------------------------------------- #
# Finding 3 — quant invalidation wired into poll-now                          #
# --------------------------------------------------------------------------- #


def test_f3_poll_now_invalidates_quant_cache(client: TestClient, monkeypatch) -> None:
    """A non-empty manual poll must drop the quant engine cache."""
    # Force the lazy engine to exist so the hook has something to invalidate.
    engine = api_mod._get_quant_engine()
    called = {"n": 0}
    real_invalidate = engine.invalidate

    def _spy() -> None:
        called["n"] += 1
        real_invalidate()

    monkeypatch.setattr(engine, "invalidate", _spy)

    # Stub a poller object that reports a non-empty ingest from poll_now().
    class _FakePoller:
        total_ingested = 7

        async def poll_now(self) -> int:
            return 3  # non-empty

    monkeypatch.setattr(api_mod, "_NEWS_POLLER", _FakePoller())

    r = client.post("/ui/news-poll-now")
    assert r.status_code == 200, r.text
    assert r.json()["ingested"] == 3
    assert called["n"] == 1, "non-empty poll must invalidate the quant cache"

    # An empty ingest must NOT invalidate (no churn when nothing arrived).
    class _EmptyPoller:
        total_ingested = 7

        async def poll_now(self) -> int:
            return 0

    monkeypatch.setattr(api_mod, "_NEWS_POLLER", _EmptyPoller())
    called["n"] = 0
    r = client.post("/ui/news-poll-now")
    assert r.status_code == 200
    assert called["n"] == 0


# --------------------------------------------------------------------------- #
# Finding 4 — sentiment-trend UTC day bucketing                               #
# --------------------------------------------------------------------------- #


def test_f4_sentiment_trend_buckets_by_utc_day(client: TestClient) -> None:
    """A record published at +05:00 must bucket on its UTC day, not the local
    wall-clock day from the raw string."""
    sup = api_mod._SUPERVISOR
    today = datetime.now(UTC).date()

    # Published at 01:00 +05:00 == 20:00 UTC the PREVIOUS day.
    local_dt = datetime(today.year, today.month, today.day, 1, 0, 0,
                        tzinfo=__import__("datetime").timezone(timedelta(hours=5)))
    utc_day = (local_dt.astimezone(UTC)).date().isoformat()
    local_day = local_dt.date().isoformat()
    assert utc_day != local_day  # offset crosses the day boundary

    rec = _make_record(
        capture_id="tz-1",
        title="Offset-zone story about NVDA",
        domain="reuters.com",
        published_ts=local_dt,
        created_at=datetime.now(UTC),
        symbols=["NVDA"],
        sentiment=SentimentLabel.POSITIVE,
    )
    assert sup.storage.insert_record(rec) is True

    r = client.get("/api/symbols/NVDA/sentiment-trend?days=7")
    assert r.status_code == 200, r.text
    series = {row["day"]: row for row in r.json()["series"]}
    # The record must land in the UTC day bucket, NOT the local-offset day.
    assert series[utc_day]["positive"] == 1, "record must bucket on UTC day"
    if local_day in series:
        assert series[local_day]["positive"] == 0, "must not double/mis-bucket on local day"


# --------------------------------------------------------------------------- #
# Finding 5 — reviews settings null model/base_url not corrupted              #
# --------------------------------------------------------------------------- #


def test_f5_reviews_patch_null_base_url_keeps_prior(client: TestClient) -> None:
    """PATCH {base_url: null} must keep the prior value, never write 'None'."""
    sup = api_mod._SUPERVISOR
    sup.settings.reviewers.deepseek.base_url = "https://api.deepseek.com"
    sup.settings.reviewers.deepseek.model = "deepseek-chat"

    r = client.patch("/api/reviews/settings", json={"base_url": None, "model": None})
    assert r.status_code == 200, r.text
    assert sup.settings.reviewers.deepseek.base_url == "https://api.deepseek.com"
    assert sup.settings.reviewers.deepseek.model == "deepseek-chat"
    # The literal corruption must never appear.
    assert sup.settings.reviewers.deepseek.base_url != "None"
    assert sup.settings.reviewers.deepseek.model != "None"

    # An empty string is also a "clear" signal that must keep the prior value.
    r = client.patch("/api/reviews/settings", json={"base_url": "   ", "model": ""})
    assert r.status_code == 200
    assert sup.settings.reviewers.deepseek.base_url == "https://api.deepseek.com"
    assert sup.settings.reviewers.deepseek.model == "deepseek-chat"


# --------------------------------------------------------------------------- #
# Finding 6 — reviews settings non-numeric → 422 not 500                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "patch",
    [
        {"sampling_rate": "abc"},
        {"usd_cap": "xyz"},
        {"sampling_rate": None},
        {"usd_cap": None},
        {"sampling_rate": [1, 2]},
    ],
)
def test_f6_reviews_patch_non_numeric_returns_422(client: TestClient, patch: dict) -> None:
    r = client.patch("/api/reviews/settings", json=patch)
    assert r.status_code == 422, (patch, r.status_code, r.text)


# --------------------------------------------------------------------------- #
# Finding 7 — paste byte cap (multibyte)                                      #
# --------------------------------------------------------------------------- #


def test_f7_demo_paste_enforces_byte_cap(client: TestClient, monkeypatch) -> None:
    """A multibyte paste must be measured in UTF-8 bytes, not characters."""
    from catchem import api as _api

    # Shrink the cap so the test is fast and deterministic. 10 chars of '世'
    # = 30 UTF-8 bytes. With a 20-byte cap the char-count check (10 < 20)
    # would WRONGLY pass; the byte-count check (30 > 20) correctly rejects.
    monkeypatch.setattr(_api, "MAX_UPLOAD_BYTES", 20)
    payload = {"title": "t", "text": "世" * 10, "domain": "demo.local"}
    r = client.post("/ui/demo/paste", json=payload)
    assert r.status_code == 413, r.text

    # An ASCII payload of equal BYTE length (20 bytes) stays under the cap.
    ok = {"title": "t", "text": "a" * 19, "domain": "demo.local"}
    r2 = client.post("/ui/demo/paste", json=ok)
    assert r2.status_code in (200, 422), r2.text  # 422 only if pipeline rejects


# --------------------------------------------------------------------------- #
# Finding 8 — quant engine lazy-singleton lock                                #
# --------------------------------------------------------------------------- #


def test_f8_quant_engine_singleton_under_concurrency(client: TestClient) -> None:
    """Concurrent first-hits must build exactly one engine instance."""
    # Reset so the build races from cold.
    api_mod._QUANT_ENGINE = None
    instances: list[int] = []
    barrier = threading.Barrier(8)

    def _hit() -> None:
        barrier.wait()
        instances.append(id(api_mod._get_quant_engine()))

    threads = [threading.Thread(target=_hit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(instances)) == 1, f"expected one engine, saw {len(set(instances))}"
    assert hasattr(api_mod, "_QUANT_ENGINE_LOCK")


# --------------------------------------------------------------------------- #
# Finding 9 — db_export checkpoints WAL before streaming                      #
# --------------------------------------------------------------------------- #


def test_f9_db_export_includes_uncheckpointed_rows(client: TestClient) -> None:
    """Export must contain rows committed but still living in the -wal sidecar.

    Storage opens a fresh connection per op and checkpoints on close ONLY when
    it is the last connection. We hold a second long-lived reader open on the
    same DB so the insert's close does NOT checkpoint — the committed row then
    genuinely lives in the `-wal`. Pre-fix db_export streamed only the main
    file and omitted that row; post-fix it checkpoints first.
    """
    import sqlite3

    sup = api_mod._SUPERVISOR
    db_path = sup.storage.db_path

    # A second IDLE open connection keeps the WAL from being auto-checkpointed
    # on the insert connection's close (close-time checkpoint only fires when
    # it is the LAST connection). It holds NO transaction, so it does not block
    # a TRUNCATE checkpoint — it just prevents the auto-fold.
    holder = sqlite3.connect(db_path, isolation_level=None, timeout=30.0)
    holder.execute("PRAGMA journal_mode=WAL")
    try:
        rec = _make_record(
            capture_id="wal-export-1",
            title="A committed row that lives in the WAL until checkpoint",
            domain="reuters.com",
            published_ts=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
            created_at=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
        )
        assert sup.storage.insert_record(rec) is True

        # Sanity: the row IS readable through the storage layer (committed),
        # confirming it's only the MAIN-FILE snapshot that could lag.
        assert sup.storage.get_record("wal-export-1") is not None

        r = client.get("/api/db/export")
        assert r.status_code == 200, r.text
        body = r.content
        assert body.startswith(SQLITE_MAGIC)
        # The capture_id must be present in the streamed MAIN file — only true
        # if a checkpoint folded the WAL frames in before FileResponse.
        assert b"wal-export-1" in body, "export omitted un-checkpointed committed row"
    finally:
        holder.close()


# --------------------------------------------------------------------------- #
# Finding 10 — db_import holds storage lock around the swap                   #
# --------------------------------------------------------------------------- #


def test_f10_db_import_acquires_storage_lock(client: TestClient, monkeypatch) -> None:
    """The destructive replace+unlink must run while holding storage._lock."""
    sup = api_mod._SUPERVISOR
    storage = sup.storage
    real_replace = Path.replace
    lock_held_during_replace = {"value": False}

    def _tracking_replace(self, target):  # type: ignore[no-untyped-def]
        # storage._lock is a re-entrant lock: acquire(blocking=False) succeeds
        # from the SAME thread that already holds it (our swap worker), but the
        # important assertion is simply that the swap is inside the lock body.
        # We detect that by checking the lock cannot be acquired by a *probe*
        # thread while replace runs.
        acquired_by_probe = {"v": None}

        def _probe() -> None:
            acquired_by_probe["v"] = storage._lock.acquire(blocking=False)
            if acquired_by_probe["v"]:
                storage._lock.release()

        t = threading.Thread(target=_probe)
        t.start()
        t.join()
        # If the probe could NOT acquire, the lock is held during replace.
        lock_held_during_replace["value"] = acquired_by_probe["v"] is False
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _tracking_replace)

    payload = SQLITE_MAGIC + b"\x00" * 256
    r = client.post(
        "/api/db/import",
        files={"file": ("u.sqlite3", io.BytesIO(payload), "application/octet-stream")},
    )
    assert r.status_code == 200, r.text
    assert lock_held_during_replace["value"], "swap ran without holding storage._lock"


# --------------------------------------------------------------------------- #
# Finding 11 — db_import streams + caps without buffering whole body          #
# --------------------------------------------------------------------------- #


def test_f11_db_import_streams_in_bounded_chunks(client: TestClient, monkeypatch) -> None:
    """The handler must pull the upload in bounded chunks (not one full read)
    and reject mid-stream, cleaning up the staged temp file.

    Pre-fix the whole body was read with a single argument-less
    ``await file.read()`` (one bytes object) before any size/magic check.
    Post-fix the handler calls ``read(chunk_size)`` repeatedly, validates the
    magic on the first chunk, and aborts once the running total crosses the
    cap.
    """
    # 1) The handler must call read WITH a bounded chunk size, never read()
    #    of the whole body. Wrap UploadFile.read to record the call args.
    import starlette.datastructures as _sds

    read_args: list[int] = []
    real_read = _sds.UploadFile.read

    async def _tracking_read(self, size: int = -1):  # type: ignore[no-untyped-def]
        read_args.append(size)
        return await real_read(self, size)

    monkeypatch.setattr(_sds.UploadFile, "read", _tracking_read)

    r_ok = client.post(
        "/api/db/import",
        files={"file": ("u.sqlite3", io.BytesIO(SQLITE_MAGIC + b"X" * 64), "application/octet-stream")},
    )
    assert r_ok.status_code == 200, r_ok.text
    assert r_ok.json()["imported_size_bytes"] == len(SQLITE_MAGIC) + 64
    # The handler must have requested a BOUNDED chunk (a positive size),
    # proving it streams rather than `await file.read()` (size=-1 / no arg).
    assert read_args, "handler never read the upload"
    assert all(n > 0 for n in read_args), f"unbounded read detected: {read_args}"

    # 2) An empty body still 422s and leaves no temp file.
    reset_all_buckets()
    r_empty = client.post(
        "/api/db/import",
        files={"file": ("u.sqlite3", io.BytesIO(b""), "application/octet-stream")},
    )
    assert r_empty.status_code == 422, r_empty.text

    # 3) A non-SQLite first chunk is rejected (after the first read) and the
    #    staged temp file is cleaned up.
    reset_all_buckets()
    r_bad = client.post(
        "/api/db/import",
        files={"file": ("u.bin", io.BytesIO(b"NOTSQLITE" + b"\x00" * 4096), "application/octet-stream")},
    )
    assert r_bad.status_code == 400, r_bad.text
    s = load_settings()
    db_path = s.sqlite_path()
    tmp = db_path.with_name(f".{db_path.name}.import.tmp")
    assert not tmp.exists(), "staged temp file must be cleaned up on rejection"


# --------------------------------------------------------------------------- #
# Finding 12 — streaming live-read always meters spend                        #
# --------------------------------------------------------------------------- #


def test_f12_stream_live_read_meters_without_usage_frame(client: TestClient, monkeypatch) -> None:
    """A successful streamed completion with NO usage frame must still call
    add_spend (parity with the non-streaming path)."""
    sup = api_mod._SUPERVISOR
    # Enable DeepSeek so use_deepseek is True.
    r = client.patch(
        "/api/reviews/settings",
        json={"enabled": True, "api_key": "sk-test", "usd_cap": 100.0},
    )
    assert r.status_code == 200, r.text
    assert sup.reviewers.deepseek() is not None

    spend_calls: list[float] = []
    real_add_spend = sup.reviewers.add_spend
    monkeypatch.setattr(
        sup.reviewers, "add_spend", lambda usd: spend_calls.append(usd) or real_add_spend(0.0)
    )

    # Stream content but NO usage frame, then done.
    async def _fake_stream(**_kwargs):
        yield {"type": "delta", "text": "Markets "}
        yield {"type": "delta", "text": "are calm."}
        yield {"type": "done"}

    monkeypatch.setattr("catchem.reviewers.deepseek.stream_chat_completion", _fake_stream)

    with client.stream("GET", "/api/quant/live-read-stream?limit=200") as resp:
        assert resp.status_code == 200
        text = "".join(resp.iter_text())

    assert "are calm." in text
    # The completion was consumed → add_spend MUST have been called exactly once
    # even though no usage frame arrived.
    assert len(spend_calls) == 1, f"expected one metered call, saw {spend_calls}"
