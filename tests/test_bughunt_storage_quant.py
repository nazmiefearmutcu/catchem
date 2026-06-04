"""Regression tests for the bug-hunt fixes in storage.py + quant/symbol_correlation.py.

Covers:
  * CRITICAL: re-processing a capture must NOT wipe user tags (INSERT OR REPLACE
    → UPSERT, so the ON DELETE CASCADE on record_tags never fires).
  * insert_record atomicity (explicit BEGIN so a mid-write failure rolls back).
  * MED: parquet flush filenames don't collide within the same second.
  * LOW: _resolve_storage_dir tolerates non-``data/`` overrides.
  * CRITICAL: symbol correlation uses a dense time grid so symbols mentioned in
    distant, non-overlapping periods don't get a spurious ±1.0 Pearson r.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from catchem.storage import Storage, _resolve_storage_dir


def _make_storage(tmp_path: Path, **kw) -> Storage:
    return Storage(
        db_path=tmp_path / "data" / "catchem.sqlite3",
        parquet_dir=tmp_path / "parquet",
        dlq_dir=tmp_path / "dlq",
        **kw,
    )


def _rec(capture_id: str, *, title: str = "T", score: float = 0.5):
    from catchem.schemas import FinancialImpactRecord, ProcessingMode, SentimentLabel

    return FinancialImpactRecord(
        capture_id=capture_id,
        doc_id=f"doc-{capture_id}",
        title=title,
        text_excerpt="x",
        domain="reuters.com",
        language="en",
        is_finance_relevant=True,
        finance_relevance_score=score,
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


# ── CRITICAL: reprocess must not destroy user tags ───────────────────────────


def test_reprocess_preserves_user_tags(tmp_path: Path) -> None:
    storage = _make_storage(tmp_path)
    try:
        assert storage.insert_record(_rec("cap1", title="v1", score=0.1)) is True
        assert storage.add_record_tag("cap1", "important") is True
        assert storage.add_record_tag("cap1", "watch") is True
        assert storage.get_record_tags("cap1") == ["important", "watch"]

        # Reprocess the SAME capture (replay / live re-poll / demo all do this).
        assert storage.insert_record(_rec("cap1", title="v2", score=0.9)) is False

        # Tags survive (regression: used to be wiped by REPLACE→CASCADE).
        assert storage.get_record_tags("cap1") == ["important", "watch"]
        # ...and the row was actually updated in place.
        rec = storage.get_record("cap1")
        assert rec is not None
        assert rec["title"] == "v2"
        assert rec["finance_relevance_score"] == pytest.approx(0.9)
    finally:
        storage.close()


# ── insert_record atomicity ──────────────────────────────────────────────────


def test_insert_record_atomic_on_midwrite_failure(tmp_path: Path, monkeypatch) -> None:
    storage = _make_storage(tmp_path)
    try:
        storage.insert_record(_rec("base"))  # pre-existing unrelated row

        import catchem.storage as storage_mod

        def _boom(_rec_obj):
            raise RuntimeError("simulated mid-write failure")

        # _record_to_row runs AFTER the records INSERT + label rebuild, still
        # inside the transaction. Before the BEGIN fix the records row would
        # already be committed (autocommit) and survive this failure.
        monkeypatch.setattr(storage_mod, "_record_to_row", _boom)

        with pytest.raises(RuntimeError):
            storage.insert_record(_rec("cap-partial"))

        # The failed insert must have rolled back entirely.
        assert storage.get_record("cap-partial") is None
        with storage._connection() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM record_labels WHERE capture_id=?", ("cap-partial",)
            ).fetchone()[0]
        assert n == 0
        # The unrelated pre-existing row is untouched.
        assert storage.get_record("base") is not None
    finally:
        storage.close()


# ── MED: parquet filename collision ──────────────────────────────────────────


def test_parquet_flush_filenames_unique_same_second(tmp_path: Path) -> None:
    storage = _make_storage(tmp_path, rotate_parquet_records=100)
    try:
        storage.insert_record(_rec("a"))
        storage.flush()
        storage.insert_record(_rec("b"))
        storage.flush()
        files = sorted(p.name for p in (tmp_path / "parquet").glob("*.parquet"))
        # Two flushes → two distinct files (regression: same-second + same
        # row-count used to overwrite the first).
        assert len(files) == 2, files
        assert len(set(files)) == 2
    finally:
        storage.close()


# ── LOW: _resolve_storage_dir tolerates non-data/ overrides ──────────────────


def test_resolve_storage_dir_variants(tmp_path: Path) -> None:
    out = tmp_path / "out" / "data"
    # data/-prefixed → rebased under output dir
    assert _resolve_storage_dir(out, "data/parquet") == out / "parquet"
    # absolute → verbatim
    abs_dir = tmp_path / "elsewhere"
    assert _resolve_storage_dir(out, str(abs_dir)) == abs_dir
    # bare relative (no 'data/' prefix) → relative to output parent, no crash
    assert _resolve_storage_dir(out, "myparquet") == out.parent / "myparquet"


# ── CRITICAL: symbol correlation dense grid ──────────────────────────────────


def test_symbol_correlation_distant_symbols_not_spurious() -> None:
    from catchem.quant.symbol_correlation import compute_pairs

    base = datetime(2026, 1, 1, tzinfo=UTC)

    def mk(cid, sym, day):
        return {
            "capture_id": cid,
            "published_ts": (base + timedelta(days=day)).isoformat(),
            "candidate_symbols": [sym],
        }

    records = []
    # AAA mentioned only on day 0..0 (3 hits), BBB only on day 5..5 (3 hits):
    # completely non-contemporaneous → real correlation ≈ 0, NOT ±1.0.
    for i in range(3):
        records.append(mk(f"a{i}", "AAA", 0))
    for i in range(3):
        records.append(mk(f"b{i}", "BBB", 5))

    pairs = compute_pairs(records, bucket_minutes=60, min_mentions=2, top_n=10)
    # The pair exists but must NOT report a strong (near-±1) correlation.
    assert pairs, "expected at least the AAA/BBB pair"
    ab = next(p for p in pairs if {p.symbol_a, p.symbol_b} == {"AAA", "BBB"})
    assert abs(ab.pearson_r) < 0.2, f"spurious correlation r={ab.pearson_r}"
