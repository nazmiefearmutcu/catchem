"""Regression tests for round-4 backend bug-hunt fixes.

Covers:
  * symbol_mapper fuzzy fallback word-boundary guard (no substring false tickers).
  * symbol_mapper cashtag denylist (org/macro acronyms not emitted as tickers).
  * parquet filename uniqueness across separate Storage instances (no overwrite).
  * two Storage instances on the same DB can both write (BEGIN IMMEDIATE — no
    SQLITE_BUSY upgrade deadlock regression).
  * VectorIndex.nearest tolerates a .npy unlinked between glob and load.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from catchem.embeddings import VectorIndex
from catchem.storage import Storage
from catchem.symbol_mapper import SymbolMapper

# ── symbol_mapper: fuzzy word-boundary ───────────────────────────────────────


def test_fuzzy_fallback_rejects_substring_false_positives() -> None:
    sm = SymbolMapper()
    # >=5-char aliases embedded inside unrelated words must NOT resolve.
    for text, bad_sym in [
        ("Brentwood real estate prices climb", "BZ=F"),   # Brent
        ("Disneyland Paris reopens", "DIS"),               # Disney (alias)
        ("Appleton museum opens downtown", "AAPL"),        # Apple
    ]:
        syms = {m.symbol for m in sm.map_text(text)}
        assert bad_sym not in syms, f"{text!r} fabricated {bad_sym}: {syms}"


def test_genuine_alias_mentions_still_resolve() -> None:
    sm = SymbolMapper()
    # The guard must not suppress real, token-boundary mentions.
    assert "AAPL" in {m.symbol for m in sm.map_text("Apple beat earnings")}
    assert "BZ=F" in {m.symbol for m in sm.map_text("Brent crude rose 2%")}


def test_cashtag_path_honors_ticker_denylist() -> None:
    sm = SymbolMapper()
    syms = {m.symbol for m in sm.map_text("$CEO resigns while $AAPL rallies")}
    assert "AAPL" in syms
    assert "CEO" not in syms  # org acronym, not a tradeable ticker


# ── storage: parquet filename uniqueness across instances ────────────────────


def _mk_record(capture_id: str):
    from datetime import UTC, datetime

    from catchem.schemas import FinancialImpactRecord, ProcessingMode, SentimentLabel

    return FinancialImpactRecord(
        capture_id=capture_id, doc_id=f"d-{capture_id}", title="t", text_excerpt="x",
        domain="reuters.com", language="en", is_finance_relevant=True,
        finance_relevance_score=0.5, asset_classes=[], impact_reason_codes=[],
        candidate_symbols=[], candidate_entities=[], impact_horizons=[],
        sentiment_label=SentimentLabel.NEUTRAL, sentiment_score=0.0,
        evidence_sentences=[], reason_text=None, component_scores={},
        diagnostic_multimodal_enabled=False, diagnostic_multimodal_result=None,
        processing_mode=ProcessingMode.LIVE_TAIL, model_versions={},
        published_ts=datetime.now(UTC), created_at=datetime.now(UTC), url=None,
    )


def test_two_storage_instances_share_parquet_dir_without_collision(tmp_path: Path) -> None:
    parquet = tmp_path / "parquet"
    a = Storage(db_path=tmp_path / "a" / "db.sqlite3", parquet_dir=parquet, dlq_dir=tmp_path / "dq")
    b = Storage(db_path=tmp_path / "b" / "db.sqlite3", parquet_dir=parquet, dlq_dir=tmp_path / "dq")
    try:
        # Distinct per-instance ids → distinct filenames even on identical
        # second/seq/row-count.
        assert a._instance_id != b._instance_id
        a.insert_record(_mk_record("a1"))
        a.flush()
        b.insert_record(_mk_record("b1"))
        b.flush()
        files = sorted(p.name for p in parquet.glob("*.parquet"))
        assert len(files) == 2 and len(set(files)) == 2, files
    finally:
        a.close()
        b.close()


def test_two_storage_instances_same_db_both_write(tmp_path: Path) -> None:
    """BEGIN IMMEDIATE: two connections to one DB file serialize instead of
    racing the SHARED→RESERVED upgrade into an instant SQLITE_BUSY."""
    db = tmp_path / "shared.sqlite3"
    a = Storage(db_path=db, parquet_dir=tmp_path / "pa", dlq_dir=tmp_path / "da")
    b = Storage(db_path=db, parquet_dir=tmp_path / "pb", dlq_dir=tmp_path / "db")
    try:
        assert a.insert_record(_mk_record("x")) is True
        assert b.insert_record(_mk_record("y")) is True  # must not raise "database is locked"
        assert a.count_records()["total"] == 2
    finally:
        a.close()
        b.close()


# ── embeddings: nearest tolerates a vanished .npy ────────────────────────────


def test_nearest_tolerates_npy_deleted_mid_glob(tmp_path: Path, monkeypatch) -> None:
    import catchem.embeddings as emb

    idx = VectorIndex(tmp_path / "vec")
    idx.save("real", np.ones(64, dtype=np.float32))
    idx.save("ghost", np.ones(64, dtype=np.float32))
    idx._cache.clear()  # force the lazy disk-load path in nearest()

    real_load = emb.np.load

    def flaky_load(p, *a, **k):
        # Simulate the archiver unlinking ghost.npy between glob and load.
        if Path(p).stem == "ghost":
            raise FileNotFoundError(str(p))
        return real_load(p, *a, **k)

    monkeypatch.setattr(emb.np, "load", flaky_load)
    # The vanished file must be skipped, not propagated as a crash.
    out = idx.nearest(np.ones(64, dtype=np.float32), k=5)
    cids = {cid for cid, _ in out}
    assert "real" in cids
    assert "ghost" not in cids
