"""Pin v45-critical-1: deleting a record cascades to its record_tags rows.

Background
----------
Migration ``add_record_tags_table`` declares::

    FOREIGN KEY (capture_id) REFERENCES records(capture_id) ON DELETE CASCADE

SQLite silently ignores ``ON DELETE CASCADE`` unless ``PRAGMA
foreign_keys=ON`` is set per-connection. ``Storage._connect`` did NOT
issue that pragma until this fix landed, so every archive sweep
(:meth:`catchem.archive.DriveArchiver._archive_once`) that DELETEd from
``records`` left orphan rows in ``record_tags`` behind. Those orphans
inflated ``top_tags()`` counts forever and never went away because
nothing else cleaned the table up.

The fix is one line in ``Storage._connect`` — see the storage.py PRAGMA
comment block. These tests pin the behaviour so a future refactor that
forgets the pragma fails noisily here instead of silently corrupting
analyst tag counts in production.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from catchem.schemas import FinancialImpactRecord, ProcessingMode
from catchem.storage import Storage

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_storage(tmp_path: Path) -> Storage:
    return Storage(
        db_path=tmp_path / "catchem.db",
        parquet_dir=tmp_path / "parquet",
        dlq_dir=tmp_path / "dlq",
    )


def _seed_record(storage: Storage, capture_id: str = "cap-cascade-001") -> str:
    """Insert a minimal record and return its capture_id."""
    rec = FinancialImpactRecord(
        capture_id=capture_id,
        doc_id="doc-cascade-001",
        title="Fed lifts rates 25 bps",
        text_excerpt="The Fed raised rates citing sticky inflation.",
        domain="reuters.com",
        language="en",
        is_finance_relevant=True,
        finance_relevance_score=0.78,
        asset_classes=["rates"],
        impact_reason_codes=["central_bank"],
        candidate_symbols=["TLT"],
        candidate_entities=["Federal Reserve"],
        impact_horizons=["short"],
        sentiment_label=None,
        sentiment_score=None,
        evidence_sentences=["The Fed raised rates citing sticky inflation."],
        reason_text="central bank tightening",
        component_scores={"central_bank": 1.0},
        diagnostic_multimodal_enabled=False,
        diagnostic_multimodal_result=None,
        processing_mode=ProcessingMode.PRODUCTION_SAFE,
        model_versions={"finance_filter": "1.0"},
        published_ts=datetime.now(UTC),
        created_at=datetime.now(UTC),
        url="https://reuters.com/article/fed-cascade",
    )
    storage.insert_record(rec)
    return rec.capture_id


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_pragma_foreign_keys_is_on_for_every_connection(tmp_path: Path) -> None:
    """Every new connection must report ``foreign_keys = 1``.

    The pragma is per-connection, so a single ``PRAGMA foreign_keys=ON``
    at schema-init time wouldn't have helped — every subsequent
    ``_connect()`` must also set it. This guard rules out a regression
    where the pragma is moved to ``_init_db`` only.
    """
    storage = _make_storage(tmp_path)
    for _ in range(5):
        with storage._connection() as conn:
            (fk,) = conn.execute("PRAGMA foreign_keys").fetchone()
            assert int(fk) == 1, "foreign_keys must be enabled on every connection"


def test_delete_record_cascades_to_record_tags(tmp_path: Path) -> None:
    """The orphan-row regression — once and forever."""
    storage = _make_storage(tmp_path)
    capture_id = _seed_record(storage)

    storage.add_record_tag(capture_id, "watch")
    storage.add_record_tag(capture_id, "earnings")
    assert storage.get_record_tags(capture_id) == ["earnings", "watch"]

    with storage._lock, storage._connection() as conn:
        conn.execute("DELETE FROM records WHERE capture_id = ?", (capture_id,))

    # The cascade must have fired; the record_tags rows should be gone.
    with storage._connection() as conn:
        orphan_rows = conn.execute(
            "SELECT COUNT(*) FROM record_tags WHERE capture_id = ?",
            (capture_id,),
        ).fetchone()[0]
    assert orphan_rows == 0, (
        f"record_tags cascade did not fire — {orphan_rows} orphan rows remain. "
        "Verify PRAGMA foreign_keys=ON is set in Storage._connect."
    )

    # And the public helper agrees.
    assert storage.get_record_tags(capture_id) == []


def test_top_tags_count_after_record_deletion(tmp_path: Path) -> None:
    """Without the cascade, ``top_tags`` over-counted dead-record tags forever."""
    storage = _make_storage(tmp_path)
    keep_id = _seed_record(storage, "cap-cascade-keep")
    drop_id = _seed_record(storage, "cap-cascade-drop")

    storage.add_record_tag(keep_id, "watch")
    storage.add_record_tag(drop_id, "watch")
    storage.add_record_tag(drop_id, "fade")

    top = {item["tag"]: item["count"] for item in storage.top_tags()}
    assert top.get("watch") == 2
    assert top.get("fade") == 1

    with storage._lock, storage._connection() as conn:
        conn.execute("DELETE FROM records WHERE capture_id = ?", (drop_id,))

    top_after = {item["tag"]: item["count"] for item in storage.top_tags()}
    assert top_after.get("watch") == 1, (
        f"top_tags still over-counts 'watch' after record deletion: {top_after}. "
        "Cascade either did not fire or the SQL was not actually run."
    )
    assert "fade" not in top_after, (
        f"'fade' tag should be gone (only attached to deleted record) — got {top_after}"
    )
