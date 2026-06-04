"""Regression tests for bug-hunt group B-embed-archive.

Covers five confirmed findings across:
  * src/catchem/embeddings.py
  * src/catchem/archive.py

Each test FAILS on the pre-fix code and PASSES after the minimal fix.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pytest

from catchem.archive import (
    DriveArchiver,
    _archived_capture_ids,
    csv_path_for_today,
)
from catchem.embeddings import (
    EMBED_DIM_STUB,
    EmbedderStub,
    VectorIndex,
    cosine,
)

# ── Finding #1: encode_many on an empty iterator ────────────────────────────


def test_encode_many_empty_generator_returns_empty_dim_array() -> None:
    """An empty generator is a legal (truthy) Iterable; the old `if texts`
    guard let it fall through to np.stack([]) and crash. Must return the
    documented (0, EMBED_DIM_STUB) array instead."""
    embedder = EmbedderStub()

    def _empty() -> Iterator[str]:
        if False:  # never yields
            yield ""

    out = embedder.encode_many(_empty())
    assert out.shape == (0, EMBED_DIM_STUB)
    assert out.dtype == np.float32


def test_encode_many_empty_list_returns_empty_dim_array() -> None:
    embedder = EmbedderStub()
    out = embedder.encode_many([])
    assert out.shape == (0, EMBED_DIM_STUB)


def test_encode_many_nonempty_generator_still_stacks() -> None:
    """The fix must not break the happy path: a non-empty generator (which the
    old code never double-consumed because list-comp materialized it) still
    produces a 2-D stack."""
    embedder = EmbedderStub()

    def _gen() -> Iterator[str]:
        yield "fed raises rates"
        yield "equities sold off"

    out = embedder.encode_many(_gen())
    assert out.shape == (2, EMBED_DIM_STUB)


# ── Finding #3: cosine / nearest crash on dimension mismatch ────────────────


def test_cosine_dimension_mismatch_returns_zero_not_crash() -> None:
    """A stub (64-dim) vector vs a MiniLM-shaped (384-dim) vector must not
    raise 'shapes not aligned'; treat as zero similarity."""
    a = np.ones(EMBED_DIM_STUB, dtype=np.float32)
    b = np.ones(384, dtype=np.float32)
    assert cosine(a, b) == 0.0
    assert cosine(b, a) == 0.0


def test_nearest_survives_stale_dim_vector_on_disk(tmp_path: Path) -> None:
    """If the on-disk pool mixes 64-dim and 384-dim vectors (embedder flip /
    silent fallback), nearest() must not crash the whole query."""
    idx = VectorIndex(tmp_path / "vec")
    idx.save("good", np.ones(EMBED_DIM_STUB, dtype=np.float32))
    # Drop a stale, wrong-dim vector straight onto disk (simulates a prior
    # embedder having written it), bypassing the cache.
    np.save(tmp_path / "vec" / "stale.npy", np.ones(384, dtype=np.float32))

    results = idx.nearest(np.ones(EMBED_DIM_STUB, dtype=np.float32), k=5)
    by_id = dict(results)
    assert "good" in by_id
    # stale-dim file ranks last as 0.0 rather than crashing the sweep
    assert by_id.get("stale", 0.0) == 0.0


# ── Finding #2: orphaned .npy never deleted on archive/drain ────────────────


def test_vector_index_delete_unlinks_file_and_cache(tmp_path: Path) -> None:
    idx = VectorIndex(tmp_path / "vec")
    idx.save("cap-1", np.ones(EMBED_DIM_STUB, dtype=np.float32))
    npy = tmp_path / "vec" / "cap-1.npy"
    assert npy.exists()
    assert idx.load("cap-1") is not None

    idx.delete("cap-1")
    assert not npy.exists()
    assert idx.load("cap-1") is None
    # Idempotent / best-effort: deleting a missing capture must not raise.
    idx.delete("cap-1")
    idx.delete("never-existed")


# ── Archive test scaffolding ────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE records (
    capture_id TEXT PRIMARY KEY,
    domain TEXT,
    title TEXT,
    url TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE record_labels (
    capture_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    value TEXT NOT NULL
);
"""


class _FakeStorage:
    """Minimal stand-in mimicking Storage's `_lock` + `_connection()` contract:
    `_connection()` wraps the body in `with conn:` (commit on success, rollback
    on any exception) — exactly what archive.py relies on."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        with self._connection() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def insert(self, capture_id: str, created_at: str) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT INTO records (capture_id, domain, title, url, created_at) VALUES (?,?,?,?,?)",
                (capture_id, "reuters.com", f"title {capture_id}", f"https://x/{capture_id}", created_at),
            )

    def count(self) -> int:
        with self._lock, self._connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]


class _RecordingVectorIndex:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, capture_id: str) -> None:
        self.deleted.append(capture_id)


class _FakeSupervisor:
    def __init__(self, storage: _FakeStorage, vector_index: object | None) -> None:
        self.storage = storage
        self.vector_index = vector_index


# DriveArchiver clamps local_cap_rows to a floor of 50, so tests insert
# `_KEEP + N` rows and expect the oldest N archived.
_KEEP = 50


def _make_archiver(tmp_path: Path, *, vector_index: object | None = None, local_cap: int = _KEEP):
    storage = _FakeStorage(tmp_path / "db.sqlite")
    sup = _FakeSupervisor(storage, vector_index)
    archiver = DriveArchiver(
        supervisor=sup,  # type: ignore[arg-type]
        settings=object(),  # type: ignore[arg-type]  (never touched in _archive_once)
        drive_dir=tmp_path / "drive",
        interval_seconds=30.0,
        local_cap_rows=local_cap,
    )
    return storage, archiver


def _seed(storage: _FakeStorage, n: int) -> None:
    """Insert n rows with strictly increasing created_at (cap-0 oldest)."""
    for i in range(n):
        storage.insert(f"cap-{i:03d}", f"2026-05-29T{i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}+00:00")


# ── Finding #2 wiring: archive sweep deletes vectors for drained rows ───────


def test_archive_once_deletes_vectors_for_drained_rows(tmp_path: Path) -> None:
    rec = _RecordingVectorIndex()
    storage, archiver = _make_archiver(tmp_path, vector_index=rec)
    # _KEEP+3 rows; the cap keeps the newest _KEEP, archives the 3 oldest.
    _seed(storage, _KEEP + 3)

    result = archiver._archive_once()
    assert result.archived == 3
    assert storage.count() == _KEEP
    # The 3 oldest were drained — their vectors must be deleted too.
    assert sorted(rec.deleted) == ["cap-000", "cap-001", "cap-002"]


# ── Finding #4: CSV append idempotent against a DELETE rollback ──────────────


def test_archive_does_not_duplicate_rows_when_delete_fails_then_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the DELETE chunk raises after the CSV is fsync'd, the `with conn:`
    rolls the deletes back but the CSV bytes are durable. The next tick must
    NOT re-append the same capture_ids to the same daily CSV."""
    storage, archiver = _make_archiver(tmp_path)
    _seed(storage, _KEEP + 3)
    oldest3 = {"cap-000", "cap-001", "cap-002"}

    csv_path = csv_path_for_today(tmp_path / "drive")

    # First tick: force the DELETE to blow up *after* the CSV fsync, so the
    # `with conn:` transaction rolls back (rows survive in SQLite) but the CSV
    # is already durable. We trip the DELETE by renaming record_labels away so
    # the first DELETE statement raises OperationalError.
    with storage._lock, storage._connection() as conn:
        conn.execute("ALTER TABLE record_labels RENAME TO record_labels_hidden")

    # The DELETE inside the storage transaction raises; `with conn:` rolls the
    # deletes back (rows survive) but the CSV bytes were already fsync'd. In
    # production _run() catches this into last_error — here we assert the raise
    # AND that the durable side-effects match the bug's premise.
    with pytest.raises(sqlite3.OperationalError):
        archiver._archive_once()
    assert storage.count() == _KEEP + 3  # rolled back, rows still present
    assert csv_path.exists()
    first_ids = _archived_capture_ids(csv_path)
    assert first_ids == oldest3  # 3 oldest written to CSV

    # Restore the table so the retry's DELETE can succeed.
    with storage._lock, storage._connection() as conn:
        conn.execute("ALTER TABLE record_labels_hidden RENAME TO record_labels")

    # Second tick (retry): DELETE now works; CSV must NOT gain duplicates.
    result2 = archiver._archive_once()
    assert result2.archived == 3
    assert storage.count() == _KEEP

    # Count physical CSV data rows (excluding header). Each capture_id must
    # appear exactly once despite being written on tick 1 and re-seen on tick 2.
    import csv as _csv

    with csv_path.open("r", newline="", encoding="utf-8") as f:
        data_rows = list(_csv.DictReader(f))
    ids = [r["capture_id"] for r in data_rows]
    assert sorted(ids) == sorted(oldest3)
    assert len(ids) == len(set(ids))  # zero duplicates


# ── Finding #5: current_csv_path only set after a successful write ──────────


def test_current_csv_path_not_set_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the OSError write-failure path, current_csv_path must remain None
    (it was previously set BEFORE the write, lying about a never-written file)."""
    storage, archiver = _make_archiver(tmp_path)
    _seed(storage, _KEEP + 3)

    real_open = Path.open

    def _boom_open(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.name.startswith("news_archive_") and "a" in (args[0] if args else kwargs.get("mode", "")):
            raise OSError("disk full")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _boom_open)

    assert archiver.current_csv_path is None
    result = archiver._archive_once()
    monkeypatch.undo()

    assert result.archived == 0
    assert result.error is not None and "write failed" in result.error
    # The lie: pre-fix this is the never-written csv_path. Post-fix it stays None.
    assert archiver.current_csv_path is None
    # Rows untouched on write failure.
    assert storage.count() == _KEEP + 3
