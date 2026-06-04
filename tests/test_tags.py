"""User-defined record tag storage + API surface tests.

Covers migration v2 (record_tags table existence + indexes), the storage
helpers (validation, add/remove/get/top/records_by_tag), and the five HTTP
endpoints under ``/api/records/{id}/tags``, ``/api/tags``, and
``/api/tags/{tag}/records``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem.api import create_app
from catchem.migrations import current_version, max_known_version
from catchem.settings import Settings, load_settings, reload_settings
from catchem.storage import Storage


def _make_storage(tmp_path: Path) -> Storage:
    return Storage(
        db_path=tmp_path / "data" / "catchem.sqlite3",
        parquet_dir=tmp_path / "parquet",
        dlq_dir=tmp_path / "dlq",
    )


def _insert_minimal_record(storage: Storage, capture_id: str) -> None:
    """Insert a bare-bones FinancialImpactRecord so FK from record_tags resolves."""
    from catchem.schemas import FinancialImpactRecord, ProcessingMode, SentimentLabel

    rec = FinancialImpactRecord(
        capture_id=capture_id,
        doc_id=f"doc-{capture_id}",
        title=f"Title {capture_id}",
        text_excerpt=f"excerpt for {capture_id}",
        domain="reuters.com",
        language="en",
        is_finance_relevant=True,
        finance_relevance_score=0.5,
        asset_classes=[],
        impact_reason_codes=[],
        candidate_symbols=[],
        candidate_entities=[],
        impact_horizons=[],
        sentiment_label=SentimentLabel.NEUTRAL,
        sentiment_score=0.0,
        evidence_sentences=[],
        reason_text=None,
        component_scores={},
        diagnostic_multimodal_enabled=False,
        diagnostic_multimodal_result=None,
        processing_mode=ProcessingMode.LIVE_TAIL,
        model_versions={},
        published_ts=datetime.now(UTC),
        created_at=datetime.now(UTC),
        url=None,
    )
    storage.insert_record(rec)


# ── Migration v2 ────────────────────────────────────────────────────────────


def test_migration_v2_creates_record_tags_table(tmp_path: Path) -> None:
    """A fresh Storage should land on v2 and have record_tags + indexes."""
    storage = _make_storage(tmp_path)
    try:
        with storage._connection() as conn:
            assert current_version(conn) == max_known_version()
            tables = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "record_tags" in tables
            cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(record_tags)")
            }
            assert {"capture_id", "tag", "created_at"} <= cols
            indexes = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='record_tags'"
                )
            }
            # PK is implicit; we ship two explicit indexes.
            assert "idx_record_tags_tag" in indexes
            assert "idx_record_tags_capture" in indexes
    finally:
        storage.close()


# ── Storage helpers ─────────────────────────────────────────────────────────


def test_add_get_remove_tag_roundtrip(tmp_path: Path) -> None:
    storage = _make_storage(tmp_path)
    try:
        _insert_minimal_record(storage, "cap-a")
        assert storage.add_record_tag("cap-a", "watch") is True
        # Second add is idempotent (INSERT OR IGNORE).
        assert storage.add_record_tag("cap-a", "watch") is False
        assert storage.get_record_tags("cap-a") == ["watch"]
        assert storage.remove_record_tag("cap-a", "watch") is True
        assert storage.remove_record_tag("cap-a", "watch") is False
        assert storage.get_record_tags("cap-a") == []
    finally:
        storage.close()


@pytest.mark.parametrize(
    "bad_tag",
    [
        "",
        "   ",
        " has space ",
        "x" * 51,
        "with/slash",
        "with space",
        "üñiç0de",
    ],
)
def test_add_tag_rejects_invalid_input(tmp_path: Path, bad_tag: str) -> None:
    storage = _make_storage(tmp_path)
    try:
        _insert_minimal_record(storage, "cap-a")
        with pytest.raises(ValueError):
            storage.add_record_tag("cap-a", bad_tag)
    finally:
        storage.close()


def test_get_record_tags_returns_sorted(tmp_path: Path) -> None:
    storage = _make_storage(tmp_path)
    try:
        _insert_minimal_record(storage, "cap-a")
        storage.add_record_tag("cap-a", "zebra")
        storage.add_record_tag("cap-a", "alpha")
        storage.add_record_tag("cap-a", "mango")
        assert storage.get_record_tags("cap-a") == ["alpha", "mango", "zebra"]
    finally:
        storage.close()


def test_top_tags_returns_counts_desc(tmp_path: Path) -> None:
    storage = _make_storage(tmp_path)
    try:
        for cid in ("c1", "c2", "c3", "c4"):
            _insert_minimal_record(storage, cid)
        for cid in ("c1", "c2", "c3"):
            storage.add_record_tag(cid, "hot")
        for cid in ("c1", "c2"):
            storage.add_record_tag(cid, "mid")
        storage.add_record_tag("c4", "rare")

        items = storage.top_tags(limit=10)
        # Sorted by count desc, then alpha asc.
        assert items[0] == {"tag": "hot", "count": 3}
        assert items[1] == {"tag": "mid", "count": 2}
        assert items[2] == {"tag": "rare", "count": 1}
    finally:
        storage.close()


def test_records_by_tag_filters(tmp_path: Path) -> None:
    storage = _make_storage(tmp_path)
    try:
        for cid in ("c1", "c2", "c3"):
            _insert_minimal_record(storage, cid)
        storage.add_record_tag("c1", "watch")
        storage.add_record_tag("c3", "watch")
        records = storage.records_by_tag("watch", limit=10)
        ids = {r["capture_id"] for r in records}
        assert ids == {"c1", "c3"}
    finally:
        storage.close()


def test_remove_record_tag_normalizes_whitespace_returns_false(tmp_path: Path) -> None:
    """Validator rejects whitespace; raw-whitespace pass yields ValueError."""
    storage = _make_storage(tmp_path)
    try:
        _insert_minimal_record(storage, "cap-a")
        storage.add_record_tag("cap-a", "watch")
        # "watch  " strips down to "watch" — should remove.
        assert storage.remove_record_tag("cap-a", "watch  ") is True
        # Now it's gone — second call returns False.
        assert storage.remove_record_tag("cap-a", "watch") is False
    finally:
        storage.close()


# ── HTTP endpoints ──────────────────────────────────────────────────────────


def _make_client(tmp_settings: Settings) -> TestClient:
    reload_settings()
    s = load_settings()
    app = create_app(s)
    return TestClient(app)


def test_api_add_get_remove_tag_roundtrip(tmp_settings: Settings) -> None:
    client = _make_client(tmp_settings)
    with client as c:
        # Seed a record via storage so the endpoint can attach a tag to it.
        from catchem.api import _get_supervisor  # type: ignore[attr-defined]

        sup = _get_supervisor()
        _insert_minimal_record(sup.storage, "cap-api-1")

        r = c.post("/api/records/cap-api-1/tags", json={"tag": "earnings"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["added"] is True
        assert body["tags"] == ["earnings"]

        r = c.get("/api/records/cap-api-1/tags")
        assert r.status_code == 200
        assert r.json() == {"capture_id": "cap-api-1", "tags": ["earnings"]}

        r = c.delete("/api/records/cap-api-1/tags/earnings")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["removed"] is True
        assert body["tags"] == []


def test_api_add_tag_rejects_invalid_input(tmp_settings: Settings) -> None:
    client = _make_client(tmp_settings)
    with client as c:
        from catchem.api import _get_supervisor  # type: ignore[attr-defined]

        sup = _get_supervisor()
        _insert_minimal_record(sup.storage, "cap-api-2")

        # Empty
        r = c.post("/api/records/cap-api-2/tags", json={"tag": ""})
        assert r.status_code == 400
        # Whitespace-only
        r = c.post("/api/records/cap-api-2/tags", json={"tag": "  "})
        assert r.status_code == 400
        # Too long
        r = c.post("/api/records/cap-api-2/tags", json={"tag": "x" * 51})
        assert r.status_code == 400
        # Disallowed characters
        r = c.post("/api/records/cap-api-2/tags", json={"tag": "has space"})
        assert r.status_code == 400


def test_api_add_tag_404_on_unknown_capture(tmp_settings: Settings) -> None:
    client = _make_client(tmp_settings)
    with client as c:
        r = c.post("/api/records/does-not-exist/tags", json={"tag": "ok"})
        assert r.status_code == 404


def test_api_top_tags_and_records_by_tag(tmp_settings: Settings) -> None:
    client = _make_client(tmp_settings)
    with client as c:
        from catchem.api import _get_supervisor  # type: ignore[attr-defined]

        sup = _get_supervisor()
        for cid in ("cap-x1", "cap-x2", "cap-x3"):
            _insert_minimal_record(sup.storage, cid)
        sup.storage.add_record_tag("cap-x1", "alpha")
        sup.storage.add_record_tag("cap-x2", "alpha")
        sup.storage.add_record_tag("cap-x3", "beta")

        r = c.get("/api/tags")
        assert r.status_code == 200
        items = r.json()["items"]
        # alpha appears twice, beta once → alpha first.
        assert items[0]["tag"] == "alpha" and items[0]["count"] == 2
        assert items[1]["tag"] == "beta" and items[1]["count"] == 1

        r = c.get("/api/tags/alpha/records")
        assert r.status_code == 200, r.text
        body = r.json()
        ids = {item["capture_id"] for item in body["items"]}
        assert ids == {"cap-x1", "cap-x2"}
