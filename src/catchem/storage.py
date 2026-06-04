"""Local-first storage layer.

Three sinks:
  1. SQLite (metadata / offsets / model versions / DLQ index)
  2. JSONL append (durable result rotation, mirrors Awareness style)
  3. Parquet snapshot (export-friendly, rotated by record count)

Design priorities:
  * crash-safe: SQLite is the index of truth, parquet/jsonl are denormalized exports
  * single-writer per process (tests use in-memory or temp dirs)
  * no async — we serialize commit boundaries explicitly
"""

from __future__ import annotations

import functools
import json
import queue
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .logging import get_logger
from .schemas import FinancialImpactRecord, ReplayOffset

logger = get_logger("catchem.storage")


SCHEMA_VERSION = 1


# User-defined tag validation: 1-50 chars, alphanumeric + `-_.`, no whitespace.
# The leading/trailing strip happens in :meth:`Storage.add_record_tag` /
# :meth:`Storage.remove_record_tag` (and the API layer) so callers can pass
# raw form input without thinking about it.
_TAG_PATTERN = re.compile(r"^[a-zA-Z0-9_\-.]+$")


def _validate_tag(tag: str) -> str:
    """Normalize + validate a user-tag. Raises :class:`ValueError` on reject.

    Rules:
      * 1-50 characters after stripping leading/trailing whitespace.
      * `[a-zA-Z0-9_\\-.]` only — no whitespace, no slashes, no unicode
        punctuation. Matches the API regex so the storage layer is the
        single source of truth and the API just re-checks for HTTP-422
        framing.
    """
    if not isinstance(tag, str):
        raise ValueError("tag must be a string")
    cleaned = tag.strip()
    if not cleaned:
        raise ValueError("tag must not be empty")
    if len(cleaned) > 50:
        raise ValueError("tag must be <= 50 characters")
    if not _TAG_PATTERN.match(cleaned):
        raise ValueError("tag must match [a-zA-Z0-9_-.]+ (no whitespace)")
    return cleaned


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
INSERT OR IGNORE INTO schema_version (version) VALUES (1);

CREATE TABLE IF NOT EXISTS records (
    capture_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    title TEXT,
    text_excerpt TEXT NOT NULL DEFAULT '',
    domain TEXT,
    language TEXT,
    is_finance_relevant INTEGER NOT NULL,
    finance_relevance_score REAL NOT NULL,
    asset_classes_json TEXT NOT NULL,
    impact_reason_codes_json TEXT NOT NULL,
    candidate_symbols_json TEXT NOT NULL,
    candidate_entities_json TEXT NOT NULL,
    impact_horizons_json TEXT NOT NULL,
    sentiment_label TEXT,
    sentiment_score REAL,
    evidence_json TEXT NOT NULL,
    reason_text TEXT,
    component_scores_json TEXT NOT NULL,
    diagnostic_enabled INTEGER NOT NULL,
    diagnostic_json TEXT,
    processing_mode TEXT NOT NULL,
    model_versions_json TEXT NOT NULL,
    published_ts TEXT,
    created_at TEXT NOT NULL,
    url TEXT
);

CREATE INDEX IF NOT EXISTS idx_records_relevance ON records(is_finance_relevant, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_records_domain ON records(domain);
CREATE INDEX IF NOT EXISTS idx_records_published ON records(published_ts);

CREATE TABLE IF NOT EXISTS offsets (
    source_path TEXT PRIMARY KEY,
    line_offset INTEGER NOT NULL,
    last_capture_id TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_versions (
    component TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dlq (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id TEXT,
    error TEXT NOT NULL,
    payload_excerpt TEXT,
    created_at TEXT NOT NULL
);

-- Inverted index for fast multi-label filters. Populated alongside records.
CREATE TABLE IF NOT EXISTS record_labels (
    capture_id TEXT NOT NULL,
    kind TEXT NOT NULL,             -- 'asset_class' | 'reason_code' | 'symbol' | 'entity' | 'horizon'
    value TEXT NOT NULL,
    PRIMARY KEY (capture_id, kind, value)
);
CREATE INDEX IF NOT EXISTS idx_record_labels ON record_labels(kind, value);

-- Second-opinion reviewer outputs (DeepSeek etc.). Composite PK on
-- (capture_id, reviewer_id) lets multiple reviewers coexist; the
-- compare page joins on capture_id and pivots by reviewer_id.
CREATE TABLE IF NOT EXISTS reviews (
    capture_id TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    reviewer_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    usd_cost REAL NOT NULL DEFAULT 0.0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    error_code TEXT,
    PRIMARY KEY (capture_id, reviewer_id)
);
CREATE INDEX IF NOT EXISTS idx_reviews_reviewer ON reviews(reviewer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reviews_capture ON reviews(capture_id);
"""


@functools.lru_cache(maxsize=1024)
def _parse_iso_ts_cached(raw: str) -> datetime:
    """Cached fast-path parser for ISO-8601 strings, stamping naive as UTC."""
    if raw.endswith("+00:00"):
        sub = raw[:-6]
        sub_len = len(sub)
        if (
            sub_len == 26
            and sub[4] == "-"
            and sub[7] == "-"
            and sub[10] == "T"
            and sub[13] == ":"
            and sub[16] == ":"
            and sub[19] == "."
        ):
            try:
                return datetime(
                    int(sub[0:4]),
                    int(sub[5:7]),
                    int(sub[8:10]),
                    int(sub[11:13]),
                    int(sub[14:16]),
                    int(sub[17:19]),
                    int(sub[20:26]),
                    tzinfo=UTC,
                )
            except ValueError:
                pass
        elif (
            sub_len == 19
            and sub[4] == "-"
            and sub[7] == "-"
            and sub[10] == "T"
            and sub[13] == ":"
            and sub[16] == ":"
        ):
            try:
                return datetime(
                    int(sub[0:4]),
                    int(sub[5:7]),
                    int(sub[8:10]),
                    int(sub[11:13]),
                    int(sub[14:16]),
                    int(sub[17:19]),
                    tzinfo=UTC,
                )
            except ValueError:
                pass
    elif raw.endswith("Z") or raw.endswith("z"):
        sub = raw[:-1]
        sub_len = len(sub)
        if (
            sub_len == 23
            and sub[4] == "-"
            and sub[7] == "-"
            and sub[10] == "T"
            and sub[13] == ":"
            and sub[16] == ":"
            and sub[19] == "."
        ):
            try:
                return datetime(
                    int(sub[0:4]),
                    int(sub[5:7]),
                    int(sub[8:10]),
                    int(sub[11:13]),
                    int(sub[14:16]),
                    int(sub[17:19]),
                    int(sub[20:23]) * 1000,
                    tzinfo=UTC,
                )
            except ValueError:
                pass
        elif (
            sub_len == 19
            and sub[4] == "-"
            and sub[7] == "-"
            and sub[10] == "T"
            and sub[13] == ":"
            and sub[16] == ":"
        ):
            try:
                return datetime(
                    int(sub[0:4]),
                    int(sub[5:7]),
                    int(sub[8:10]),
                    int(sub[11:13]),
                    int(sub[14:16]),
                    int(sub[17:19]),
                    tzinfo=UTC,
                )
            except ValueError:
                pass

    normalized = raw.replace("Z", "+00:00").replace("z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


class Storage:
    """SQLite + parquet + jsonl storage. Thread-safe via instance lock."""

    def __init__(
        self,
        db_path: Path,
        parquet_dir: Path,
        dlq_dir: Path,
        rotate_parquet_records: int = 5000,
        wal_autocheckpoint: int = 10000,
    ) -> None:
        self.db_path = db_path
        self.parquet_dir = parquet_dir
        self.dlq_dir = dlq_dir
        self.rotate_parquet_records = max(100, int(rotate_parquet_records))
        self.wal_autocheckpoint = max(0, int(wal_autocheckpoint))

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.parquet_dir.mkdir(parents=True, exist_ok=True)
        self.dlq_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._conn_pool = queue.Queue()
        self._closed = False
        self._pending_rows: list[dict[str, Any]] = []
        # Monotonic flush sequence: the parquet filename used to key only on
        # whole-second epoch + row count, so two flushes in the same second
        # with the same pending count (e.g. close() right after a rotate, or
        # small rotate thresholds in tests) collided and the second silently
        # overwrote the first batch. The ever-increasing sequence guarantees
        # a unique filename per flush regardless of clock resolution.
        self._parquet_seq = 0
        # Per-instance token so two SEPARATE Storage objects pointed at the
        # same parquet_dir (e.g. the demo path's transient Storage and the
        # live supervisor's) can't mint byte-identical filenames on a
        # same-second / same-seq / same-row-count flush and silently overwrite
        # each other's batch — the seq counter alone only defeats collisions
        # WITHIN one instance.
        self._instance_id = uuid.uuid4().hex[:8]
        self._init_db()

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    # ── DB --------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=30.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(f"PRAGMA wal_autocheckpoint={self.wal_autocheckpoint}")
        # SQLite ships with foreign keys disabled per-connection (legacy
        # default the project maintainers refuse to change). Without this
        # PRAGMA, ``ON DELETE CASCADE`` declarations like the one on
        # ``record_tags.capture_id`` are silently ignored — deleting a
        # row from ``records`` leaves orphan tag rows behind, which then
        # corrupt ``top_tags()`` counts and the Tags page totals.
        # The pragma is per-connection, so it MUST be set on every
        # connection this class hands out, not just the schema init.
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def _get_conn(self) -> sqlite3.Connection:
        try:
            is_monkeypatched = self._connect.__func__ is not Storage._connect
        except AttributeError:
            is_monkeypatched = True

        if is_monkeypatched:
            return self._connect()

        try:
            return self._conn_pool.get_nowait()
        except queue.Empty:
            return self._connect()

    def _return_conn(self, conn: sqlite3.Connection) -> None:
        try:
            is_monkeypatched = self._connect.__func__ is not Storage._connect
        except AttributeError:
            is_monkeypatched = True

        if self._closed or is_monkeypatched:
            conn.close()
        else:
            self._conn_pool.put(conn)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._get_conn()
        try:
            with conn:
                yield conn
        finally:
            self._return_conn(conn)

    def _init_db(self) -> None:
        from .migrations import apply_migrations

        try:
            with self._lock, self._connection() as conn:
                conn.executescript(_SCHEMA_SQL)
                self._migrate_records_table(conn)
                # Apply versioned migrations after the legacy ``IF NOT EXISTS``
                # bootstrap. A pre-existing DB from before the migration
                # framework lands at ``user_version = 0``; baseline migration
                # #1 claims it idempotently. Future ALTER/CREATE INDEX work
                # ships as appended entries in ``catchem.migrations``.
                applied = apply_migrations(conn)
        except sqlite3.DatabaseError as e:
            logger.error("database_corrupted_attempting_recovery", error=str(e), db=str(self.db_path))
            if self.db_path.is_file():
                import uuid
                corrupt_path = self.db_path.with_name(f"{self.db_path.name}.corrupt.{uuid.uuid4().hex[:8]}")
                try:
                    self.db_path.rename(corrupt_path)
                    logger.warning("database_quarantined", corrupt_path=str(corrupt_path))
                except Exception as rename_err:
                    logger.error("failed_to_quarantine_database", error=str(rename_err))

                for suffix in ["-wal", "-shm"]:
                    sidecar = self.db_path.with_name(self.db_path.name + suffix)
                    if sidecar.exists():
                        try:
                            sidecar.unlink()
                            logger.info("database_sidecar_removed", path=str(sidecar))
                        except Exception as unlink_err:
                            logger.error("failed_to_remove_sidecar", error=str(unlink_err))

                # Retry initialization after quarantining/removing files
                with self._lock, self._connection() as conn:
                    conn.executescript(_SCHEMA_SQL)
                    self._migrate_records_table(conn)
                    applied = apply_migrations(conn)
            else:
                raise

        if applied:
            logger.info(
                "schema_migrated",
                count=len(applied),
                versions=[m.version for m in applied],
                names=[m.name for m in applied],
            )
        logger.info("storage_initialized", db=str(self.db_path), parquet=str(self.parquet_dir))


    def _migrate_records_table(self, conn: sqlite3.Connection) -> None:
        columns = {str(r["name"]) for r in conn.execute("PRAGMA table_info(records)").fetchall()}
        if "text_excerpt" not in columns:
            conn.execute("ALTER TABLE records ADD COLUMN text_excerpt TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        """**Deprecated** — short-lived sqlite cursor helper.

        .. danger::
            **Do not use in new code.** This helper holds
            ``self._lock`` (a re-entrant lock) for the ENTIRE yield
            duration. The known failure modes are:

            * **Deadlock across threads.** If thread A is inside the
              ``with cursor():`` block and calls into ANY other code
              path that also acquires ``self._lock`` from a different
              thread (e.g. a background worker that does
              ``insert_record``), the second thread blocks. If the
              first thread is itself waiting on the second (e.g. an
              ``asyncio.to_thread`` round-trip), the deadlock is
              permanent. This was the root cause of the v33 archive
              hang and is the reason the archive code path now uses
              ``self._lock`` + ``self._connection()`` explicitly
              instead of going through ``cursor()``.

            * **Lock-held-across-IO**. A slow iterator or any network
              IO inside the ``with`` block stalls every other writer
              and reader on the storage layer, which is fatal for the
              news poller's insert rate.

            * **No auto-commit semantics.** ``yield cursor`` from a
              raw ``conn`` without a wrapping ``with conn:`` means
              the caller must remember to commit. Forgetting it
              silently drops the write — the connection close on the
              ``finally`` path will not commit either.

            Use :meth:`_connection` instead. It correctly scopes the
            transaction via ``with conn:`` and you can opt INTO the
            lock by wrapping the ``with`` in ``with self._lock:`` if
            you really need writer-serialisation. That pattern is
            what every modern callsite uses.

            This method is kept for binary-compatibility with any
            external consumer that imported ``Storage.cursor()`` from
            an older API version. There are no in-tree callers as of
            v45 (verified via ``grep -rn '\\.cursor()' src/catchem/``).
        """
        with self._lock:
            conn = self._connect()
            try:
                yield conn.cursor()
            finally:
                conn.close()

    # ── record write ---------------------------------------------------------
    def insert_record(self, rec: FinancialImpactRecord) -> bool:
        """Insert or replace a record.

        Returns True when the capture_id was new to the SQLite truth store,
        False when an existing record was replaced.
        """
        with self._lock, self._connection() as conn:
            # Explicit transaction. The connection runs with
            # ``isolation_level=None`` (autocommit), under which the
            # surrounding ``with conn:`` context manager's commit/rollback
            # are no-ops because no transaction is open — so a failure
            # midway through this multi-statement write (records + the
            # record_labels rebuild + model_versions) would otherwise leave
            # the inverted index inconsistent with the row. Issuing BEGIN
            # opens a real transaction so ``with conn:`` rolls the whole
            # write back atomically on any error and commits it as a unit.
            #
            # BEGIN IMMEDIATE (not plain BEGIN/DEFERRED): a deferred
            # transaction takes only a SHARED lock for the leading SELECT and
            # tries to UPGRADE to RESERVED at the INSERT. If another
            # connection to the SAME file (e.g. the demo path's transient
            # Storage, which has its own in-process lock and so isn't
            # serialized with us) also holds a SHARED lock, SQLite cannot
            # resolve the upgrade by waiting and returns SQLITE_BUSY
            # IMMEDIATELY — busy_timeout does NOT cover upgrade contention.
            # IMMEDIATE acquires the write lock up front, so the second writer
            # waits up to ``timeout`` instead of erroring out.
            conn.execute("BEGIN IMMEDIATE")
            existed = (
                conn.execute(
                    "SELECT 1 FROM records WHERE capture_id = ?",
                    (rec.capture_id,),
                ).fetchone()
                is not None
            )
            # UPSERT (not INSERT OR REPLACE). REPLACE resolves a PK conflict
            # by DELETE-then-INSERT, and because ``PRAGMA foreign_keys=ON``
            # is set on every connection, that implicit DELETE cascades
            # through ``record_tags`` (ON DELETE CASCADE, migration v2) and
            # silently wipes every user-applied tag on the capture. Since
            # re-processing a capture_id is a routine, documented operation
            # (replay, live-tail re-poll, demo), the analyst's tags vanished
            # on every reprocess. ON CONFLICT ... DO UPDATE updates the row
            # in place — no DELETE, so the cascade never fires and tags
            # survive.
            conn.execute(
                """
                INSERT INTO records (
                    capture_id, doc_id, title, text_excerpt, domain, language,
                    is_finance_relevant, finance_relevance_score,
                    asset_classes_json, impact_reason_codes_json,
                    candidate_symbols_json, candidate_entities_json,
                    impact_horizons_json,
                    sentiment_label, sentiment_score,
                    evidence_json, reason_text, component_scores_json,
                    diagnostic_enabled, diagnostic_json,
                    processing_mode, model_versions_json,
                    published_ts, created_at, url
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(capture_id) DO UPDATE SET
                    doc_id=excluded.doc_id,
                    title=excluded.title,
                    text_excerpt=excluded.text_excerpt,
                    domain=excluded.domain,
                    language=excluded.language,
                    is_finance_relevant=excluded.is_finance_relevant,
                    finance_relevance_score=excluded.finance_relevance_score,
                    asset_classes_json=excluded.asset_classes_json,
                    impact_reason_codes_json=excluded.impact_reason_codes_json,
                    candidate_symbols_json=excluded.candidate_symbols_json,
                    candidate_entities_json=excluded.candidate_entities_json,
                    impact_horizons_json=excluded.impact_horizons_json,
                    sentiment_label=excluded.sentiment_label,
                    sentiment_score=excluded.sentiment_score,
                    evidence_json=excluded.evidence_json,
                    reason_text=excluded.reason_text,
                    component_scores_json=excluded.component_scores_json,
                    diagnostic_enabled=excluded.diagnostic_enabled,
                    diagnostic_json=excluded.diagnostic_json,
                    processing_mode=excluded.processing_mode,
                    model_versions_json=excluded.model_versions_json,
                    published_ts=excluded.published_ts,
                    created_at=excluded.created_at,
                    url=excluded.url
                """,
                (
                    rec.capture_id,
                    rec.doc_id,
                    rec.title,
                    rec.text_excerpt,
                    rec.domain,
                    rec.language,
                    int(rec.is_finance_relevant),
                    float(rec.finance_relevance_score),
                    json.dumps(rec.asset_classes),
                    json.dumps(rec.impact_reason_codes),
                    json.dumps(rec.candidate_symbols),
                    json.dumps(rec.candidate_entities),
                    json.dumps(rec.impact_horizons),
                    rec.sentiment_label.value if rec.sentiment_label else None,
                    rec.sentiment_score,
                    json.dumps(rec.evidence_sentences),
                    rec.reason_text,
                    json.dumps(rec.component_scores),
                    int(rec.diagnostic_multimodal_enabled),
                    json.dumps(rec.diagnostic_multimodal_result)
                    if rec.diagnostic_multimodal_result
                    else None,
                    rec.processing_mode.value,
                    json.dumps(rec.model_versions),
                    rec.published_ts.isoformat() if rec.published_ts else None,
                    rec.created_at.isoformat(),
                    rec.url,
                ),
            )
            # inverted index
            conn.execute("DELETE FROM record_labels WHERE capture_id=?", (rec.capture_id,))
            tuples: list[tuple[str, str, str]] = []
            for v in rec.asset_classes:
                tuples.append((rec.capture_id, "asset_class", v))
            for v in rec.impact_reason_codes:
                tuples.append((rec.capture_id, "reason_code", v))
            for v in rec.candidate_symbols:
                tuples.append((rec.capture_id, "symbol", v))
            for v in rec.candidate_entities:
                tuples.append((rec.capture_id, "entity", v))
            for v in rec.impact_horizons:
                tuples.append((rec.capture_id, "horizon", v))
            if tuples:
                conn.executemany(
                    "INSERT OR IGNORE INTO record_labels (capture_id, kind, value) VALUES (?,?,?)",
                    tuples,
                )

            for component, ver in rec.model_versions.items():
                conn.execute(
                    """INSERT OR REPLACE INTO model_versions (component, version, updated_at)
                       VALUES (?, ?, ?)""",
                    (component, ver, datetime.now(UTC).isoformat()),
                )

            self._pending_rows.append(_record_to_row(rec))

        # Perform the parquet flush outside the SQLite transaction and lock if threshold met
        rows_to_flush = []
        parquet_seq = 0
        with self._lock:
            if len(self._pending_rows) >= self.rotate_parquet_records:
                rows_to_flush = list(self._pending_rows)
                self._pending_rows.clear()
                self._parquet_seq += 1
                parquet_seq = self._parquet_seq

        if rows_to_flush:
            self._write_parquet(rows_to_flush, parquet_seq)

        return not existed

    def flush(self) -> None:
        rows_to_flush = []
        parquet_seq = 0
        with self._lock:
            if self._pending_rows:
                rows_to_flush = list(self._pending_rows)
                self._pending_rows.clear()
                self._parquet_seq += 1
                parquet_seq = self._parquet_seq

        if rows_to_flush:
            self._write_parquet(rows_to_flush, parquet_seq)

    def checkpoint(self) -> None:
        """Manually execute a non-blocking PASSIVE WAL checkpoint.

        Under WAL mode, commits append to the WAL file. Auto-checkpoints
        periodically write WAL frames back to the database, which can stall
        active writers. Invoking this manually (e.g. between poller ticks or
        after replay blocks) outside of active write transactions allows the database
        to reclaim WAL space asynchronously without stalling active ingestion.
        """
        with self._lock, self._connection() as conn:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def _write_parquet(self, rows: list[dict[str, Any]], seq: int) -> None:
        if not rows:
            return
        table = pa.Table.from_pylist(rows)
        now = datetime.now(UTC)
        path = (
            self.parquet_dir
            / f"records-{int(now.timestamp())}-{self._instance_id}-{seq:06d}-{len(rows):05d}.parquet"
        )
        pq.write_table(table, path)
        logger.info("parquet_flushed", path=str(path), rows=len(rows))

    # ── queries --------------------------------------------------------------
    def recent_records(self, limit: int = 50, relevant_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM records"
        params: list[Any] = []
        if relevant_only:
            sql += " WHERE is_finance_relevant = 1"
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        with self._connection() as conn:
            return [_row_to_payload(dict(r)) for r in conn.execute(sql, params)]

    def get_record(self, capture_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            r = conn.execute("SELECT * FROM records WHERE capture_id = ?", (capture_id,)).fetchone()
            return _row_to_payload(dict(r)) if r else None

    def by_label(self, kind: str, value: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT records.* FROM records
                     JOIN record_labels ON records.capture_id = record_labels.capture_id
                    WHERE record_labels.kind = ? AND record_labels.value = ?
                    ORDER BY records.created_at DESC
                    LIMIT ?""",
                (kind, value, int(limit)),
            ).fetchall()
            return [_row_to_payload(dict(r)) for r in rows]

    def count_records(self) -> dict[str, int]:
        with self._connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            relevant = conn.execute("SELECT COUNT(*) FROM records WHERE is_finance_relevant = 1").fetchone()[
                0
            ]
            return {"total": int(total), "finance_relevant": int(relevant)}

    # ── offsets --------------------------------------------------------------
    def get_offset(self, source_path: str) -> ReplayOffset:
        with self._connection() as conn:
            r = conn.execute("SELECT * FROM offsets WHERE source_path=?", (source_path,)).fetchone()
            if r is None:
                return ReplayOffset(source_path=source_path)
            return ReplayOffset(
                source_path=source_path,
                line_offset=int(r["line_offset"]),
                last_capture_id=r["last_capture_id"],
                updated_at=_parse_iso_ts_cached(r["updated_at"]),
            )

    def save_offset(self, offset: ReplayOffset) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO offsets
                   (source_path, line_offset, last_capture_id, updated_at)
                   VALUES (?,?,?,?)""",
                (
                    offset.source_path,
                    int(offset.line_offset),
                    offset.last_capture_id,
                    datetime.now(UTC).isoformat(),
                ),
            )

    # ── DLQ ------------------------------------------------------------------
    def record_failure(self, capture_id: str | None, error: str, payload_excerpt: str) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                """INSERT INTO dlq (capture_id, error, payload_excerpt, created_at)
                   VALUES (?,?,?,?)""",
                (capture_id, error, payload_excerpt[:4000], datetime.now(UTC).isoformat()),
            )

    def dlq_count(self) -> int:
        with self._connection() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM dlq").fetchone()[0])

    # ── reviews (second-opinion) ─────────────────────────────────────────────
    def upsert_review(self, row: dict[str, Any]) -> None:
        """Persist a `ReviewPayload.to_storage_row()` dict.

        Idempotent via the (capture_id, reviewer_id) primary key — a
        re-run of the same reviewer against the same capture replaces
        the prior row in-place so the compare page never shows stale
        token/cost data after a model bump.
        """
        with self._lock, self._connection() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO reviews (
                    capture_id, reviewer_id, reviewer_version, payload_json,
                    input_tokens, output_tokens, usd_cost, latency_ms,
                    created_at, error_code
                ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["capture_id"],
                    row["reviewer_id"],
                    row["reviewer_version"],
                    json.dumps(row["payload_json"]),
                    int(row.get("input_tokens", 0)),
                    int(row.get("output_tokens", 0)),
                    float(row.get("usd_cost", 0.0)),
                    int(row.get("latency_ms", 0)),
                    row["created_at"],
                    row.get("error_code"),
                ),
            )

    def get_reviews_for_capture(self, capture_id: str) -> list[dict[str, Any]]:
        """All reviewer rows attached to a capture (stable order by reviewer_id)."""
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT * FROM reviews WHERE capture_id = ? ORDER BY reviewer_id""",
                (capture_id,),
            ).fetchall()
            return [_row_to_review(dict(r)) for r in rows]

    def recent_reviews(self, reviewer_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """Recent rows for a single reviewer — feeds the compare dashboard."""
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT * FROM reviews WHERE reviewer_id = ?
                    ORDER BY created_at DESC LIMIT ?""",
                (reviewer_id, int(limit)),
            ).fetchall()
            return [_row_to_review(dict(r)) for r in rows]

    def reviews_with_pair(
        self, reviewer_a: str, reviewer_b: str, limit: int = 500
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Captures where BOTH reviewers have a row — paired for compare.

        Returns a list of (row_a, row_b) tuples ordered by reviewer_b's
        created_at DESC (the "newer" reviewer typically being DeepSeek).
        """
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT a.payload_json AS a_payload, a.reviewer_id AS a_id,
                          a.reviewer_version AS a_ver, a.created_at AS a_ts,
                          a.error_code AS a_err,
                          b.payload_json AS b_payload, b.reviewer_id AS b_id,
                          b.reviewer_version AS b_ver, b.created_at AS b_ts,
                          b.error_code AS b_err,
                          b.input_tokens AS b_in_tokens,
                          b.output_tokens AS b_out_tokens,
                          b.usd_cost AS b_usd_cost,
                          b.latency_ms AS b_latency_ms,
                          a.capture_id AS capture_id
                     FROM reviews a
                     JOIN reviews b ON a.capture_id = b.capture_id
                    WHERE a.reviewer_id = ? AND b.reviewer_id = ?
                    ORDER BY b.created_at DESC LIMIT ?""",
                (reviewer_a, reviewer_b, int(limit)),
            ).fetchall()
            out: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for r in rows:
                d = dict(r)
                a_payload_text = d["a_payload"] or "{}"
                if a_payload_text == "{}":
                    a_payload = {}
                elif a_payload_text == "[]":
                    a_payload = []
                else:
                    a_payload = json.loads(a_payload_text)

                b_payload_text = d["b_payload"] or "{}"
                if b_payload_text == "{}":
                    b_payload = {}
                elif b_payload_text == "[]":
                    b_payload = []
                else:
                    b_payload = json.loads(b_payload_text)

                a = {
                    "capture_id": d["capture_id"],
                    "reviewer_id": d["a_id"],
                    "reviewer_version": d["a_ver"],
                    "created_at": d["a_ts"],
                    "error_code": d["a_err"],
                    "payload": a_payload,
                }
                b = {
                    "capture_id": d["capture_id"],
                    "reviewer_id": d["b_id"],
                    "reviewer_version": d["b_ver"],
                    "created_at": d["b_ts"],
                    "error_code": d["b_err"],
                    "input_tokens": int(d["b_in_tokens"] or 0),
                    "output_tokens": int(d["b_out_tokens"] or 0),
                    "usd_cost": float(d["b_usd_cost"] or 0.0),
                    "latency_ms": int(d["b_latency_ms"] or 0),
                    "payload": b_payload,
                }
                out.append((a, b))
            return out

    def sum_review_cost(self, reviewer_id: str) -> float:
        """Cumulative USD spend for a reviewer (used by the budget guard)."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(usd_cost), 0.0) FROM reviews WHERE reviewer_id = ?",
                (reviewer_id,),
            ).fetchone()
            return float(row[0]) if row else 0.0

    def review_token_totals(self, reviewer_id: str) -> dict[str, int]:
        """Cumulative token counts for a reviewer (compare dashboard footer)."""
        with self._connection() as conn:
            row = conn.execute(
                """SELECT COALESCE(SUM(input_tokens), 0),
                          COALESCE(SUM(output_tokens), 0),
                          COUNT(*),
                          COUNT(CASE WHEN error_code IS NOT NULL THEN 1 END)
                     FROM reviews WHERE reviewer_id = ?""",
                (reviewer_id,),
            ).fetchone()
            if not row:
                return {"input": 0, "output": 0, "calls": 0, "errors": 0}
            return {
                "input": int(row[0]),
                "output": int(row[1]),
                "calls": int(row[2]),
                "errors": int(row[3]),
            }

    def prune_dlq(self, max_rows: int) -> int:
        """Drop oldest DLQ rows past `max_rows`. Returns count deleted.

        `record_failure` is unbounded by design — every handler exception
        gets a row with the original payload excerpt. Long-running
        deployments with chronic upstream failures grew the table to GB
        scale, which (a) bloated the sqlite file backups had to copy and
        (b) slowed unrelated INSERTs as the file grew. Pruning oldest-first
        preserves debug value (recent failures are most useful) while
        bounding the worst case.

        Idempotent: a second call within cap returns 0. `max_rows=0` wipes
        the table entirely (useful in tests).
        """
        max_rows = max(0, int(max_rows))
        with self._lock, self._connection() as conn:
            # Single-statement prune so a crash mid-execution can't leave
            # us with a gap. Uses DELETE ... WHERE id NOT IN (SELECT id
            # ORDER BY id DESC LIMIT N) because sqlite has no LIMIT on
            # plain DELETE.
            cur = conn.execute(
                "DELETE FROM dlq WHERE id NOT IN (  SELECT id FROM dlq ORDER BY id DESC LIMIT ?)",
                (max_rows,),
            )
            return int(cur.rowcount or 0)

    # ── user-defined record tags ────────────────────────────────────────────
    def add_record_tag(self, capture_id: str, tag: str) -> bool:
        """Attach a user tag to a record.

        Returns ``True`` when the (capture_id, tag) pair was new, ``False``
        when it already existed (INSERT OR IGNORE). Raises :class:`ValueError`
        if the tag fails validation — see :func:`_validate_tag` for rules.
        """
        cleaned = _validate_tag(tag)
        with self._lock, self._connection() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO record_tags (capture_id, tag, created_at)
                   VALUES (?, ?, ?)""",
                (capture_id, cleaned, datetime.now(UTC).isoformat()),
            )
            return int(cur.rowcount or 0) > 0

    def remove_record_tag(self, capture_id: str, tag: str) -> bool:
        """Detach a user tag from a record.

        Returns ``True`` when a row was deleted, ``False`` when the pair was
        already absent. Invalid-shape tags are normalized through
        :func:`_validate_tag`; an out-of-range value can never have matched
        a stored row so we'd return False either way, but normalizing keeps
        the API and storage layers consistent.
        """
        cleaned = _validate_tag(tag)
        with self._lock, self._connection() as conn:
            cur = conn.execute(
                "DELETE FROM record_tags WHERE capture_id = ? AND tag = ?",
                (capture_id, cleaned),
            )
            return int(cur.rowcount or 0) > 0

    def get_record_tags(self, capture_id: str) -> list[str]:
        """Sorted list of tag strings attached to ``capture_id``."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT tag FROM record_tags WHERE capture_id = ? ORDER BY tag",
                (capture_id,),
            ).fetchall()
            return [str(r["tag"]) for r in rows]

    def top_tags(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most-used tags as ``[{tag, count}, ...]`` — count desc.

        Powers the Feed sidebar facet group; ties broken by alphabetical
        order so the list is stable across snapshots.
        """
        limit = max(1, int(limit))
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT tag, COUNT(*) AS n
                     FROM record_tags
                    GROUP BY tag
                    ORDER BY n DESC, tag ASC
                    LIMIT ?""",
                (limit,),
            ).fetchall()
            return [{"tag": str(r["tag"]), "count": int(r["n"])} for r in rows]

    def records_by_tag(self, tag: str, limit: int = 50) -> list[dict[str, Any]]:
        """Records whose user-tags include ``tag`` — newest first."""
        cleaned = _validate_tag(tag)
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT records.* FROM records
                     JOIN record_tags ON records.capture_id = record_tags.capture_id
                    WHERE record_tags.tag = ?
                    ORDER BY records.created_at DESC
                    LIMIT ?""",
                (cleaned, int(limit)),
            ).fetchall()
            return [_row_to_payload(dict(r)) for r in rows]

    # ── portfolio (READ-ONLY holdings tracker) ───────────────────────────────
    # Analyst-entered positions joined to the awareness/quant layers for
    # context. No order execution, no money movement — see
    # :mod:`catchem.portfolio` for the enrichment join. Backed by the
    # ``portfolio`` table (migration v3); numeric columns are nullable so a
    # holding may be a bare watch entry with just a symbol.
    def add_holding(
        self,
        symbol: str,
        *,
        label: str | None = None,
        shares: float | None = None,
        weight: float | None = None,
        cost_basis: float | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Insert a holding and return its hydrated row.

        ``symbol`` is stripped and required (raises :class:`ValueError` when
        blank). Numeric fields are coerced to ``float`` or kept ``None``.
        ``added_at`` is stamped with the current UTC instant in ISO-8601.
        """
        sym = str(symbol or "").strip()
        if not sym:
            raise ValueError("symbol must not be empty")
        added_at = datetime.now(UTC).isoformat()
        with self._lock, self._connection() as conn:
            cur = conn.execute(
                """INSERT INTO portfolio
                   (symbol, label, shares, weight, cost_basis, notes, added_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    sym,
                    label,
                    _coerce_float(shares),
                    _coerce_float(weight),
                    _coerce_float(cost_basis),
                    notes,
                    added_at,
                ),
            )
            holding_id = int(cur.lastrowid or 0)
            row = conn.execute("SELECT * FROM portfolio WHERE id = ?", (holding_id,)).fetchone()
            return _row_to_holding(dict(row))

    def list_holdings(self) -> list[dict[str, Any]]:
        """All holdings, newest-added first (ties broken by id desc)."""
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM portfolio ORDER BY added_at DESC, id DESC").fetchall()
            return [_row_to_holding(dict(r)) for r in rows]

    def get_holding(self, holding_id: int) -> dict[str, Any] | None:
        """Single holding by id, or ``None`` if absent."""
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM portfolio WHERE id = ?", (int(holding_id),)).fetchone()
            return _row_to_holding(dict(row)) if row else None

    def delete_holding(self, holding_id: int) -> bool:
        """Delete a holding. Returns ``True`` when a row was removed."""
        with self._lock, self._connection() as conn:
            cur = conn.execute("DELETE FROM portfolio WHERE id = ?", (int(holding_id),))
            return int(cur.rowcount or 0) > 0

    # ── housekeeping ---------------------------------------------------------
    def close(self) -> None:
        self.flush()
        self._closed = True
        while not self._conn_pool.empty():
            try:
                conn = self._conn_pool.get_nowait()
                conn.close()
            except queue.Empty:
                break


def _record_to_row(rec: FinancialImpactRecord) -> dict[str, Any]:
    """Flatten one record into pyarrow-friendly primitives.

    Parquet can't represent open-ended `dict[str, float]` cleanly via from_pylist,
    so we serialize nested structures as JSON strings. Downstream consumers can
    `json.loads` to recover them.
    """
    return {
        "capture_id": rec.capture_id,
        "doc_id": rec.doc_id,
        "title": rec.title,
        "text_excerpt": rec.text_excerpt,
        "domain": rec.domain,
        "language": rec.language,
        "is_finance_relevant": rec.is_finance_relevant,
        "finance_relevance_score": float(rec.finance_relevance_score),
        "asset_classes": list(rec.asset_classes),
        "impact_reason_codes": list(rec.impact_reason_codes),
        "candidate_symbols": list(rec.candidate_symbols),
        "candidate_entities": list(rec.candidate_entities),
        "impact_horizons": list(rec.impact_horizons),
        "sentiment_label": rec.sentiment_label.value if rec.sentiment_label else None,
        "sentiment_score": float(rec.sentiment_score) if rec.sentiment_score is not None else None,
        "evidence_sentences": list(rec.evidence_sentences),
        "reason_text": rec.reason_text,
        "component_scores_json": json.dumps(rec.component_scores),
        "diagnostic_enabled": bool(rec.diagnostic_multimodal_enabled),
        "diagnostic_json": json.dumps(rec.diagnostic_multimodal_result)
        if rec.diagnostic_multimodal_result
        else None,
        "model_versions_json": json.dumps(rec.model_versions),
        "processing_mode": rec.processing_mode.value,
        "published_ts": rec.published_ts.isoformat() if rec.published_ts else None,
        "created_at": rec.created_at.isoformat(),
        "url": rec.url,
    }


def _fast_json_loads(val: str) -> Any:
    """Fast-path JSON deserialization for common shapes."""
    if val == "[]":
        return []
    if val == "{}":
        return {}
    return json.loads(val)


def _row_to_payload(r: dict[str, Any]) -> dict[str, Any]:
    diag = r.get("diagnostic_json")
    diag_res = None
    if diag:
        if diag == "{}":
            diag_res = {}
        elif diag == "[]":
            diag_res = []
        else:
            diag_res = json.loads(diag)

    return {
        "capture_id": r["capture_id"],
        "doc_id": r["doc_id"],
        "title": r["title"],
        "text_excerpt": r.get("text_excerpt") or "",
        "domain": r["domain"],
        "language": r["language"],
        "url": r["url"],
        "is_finance_relevant": bool(r["is_finance_relevant"]),
        "finance_relevance_score": float(r["finance_relevance_score"]),
        "asset_classes": _fast_json_loads(r["asset_classes_json"]),
        "impact_reason_codes": _fast_json_loads(r["impact_reason_codes_json"]),
        "candidate_symbols": _fast_json_loads(r["candidate_symbols_json"]),
        "candidate_entities": _fast_json_loads(r["candidate_entities_json"]),
        "impact_horizons": _fast_json_loads(r["impact_horizons_json"]),
        "sentiment_label": r["sentiment_label"],
        "sentiment_score": r["sentiment_score"],
        "evidence_sentences": _fast_json_loads(r["evidence_json"]),
        "reason_text": r["reason_text"],
        "component_scores": _fast_json_loads(r["component_scores_json"]),
        "diagnostic_multimodal_enabled": bool(r["diagnostic_enabled"]),
        "diagnostic_multimodal_result": diag_res,
        "processing_mode": r["processing_mode"],
        "model_versions": _fast_json_loads(r["model_versions_json"]),
        "published_ts": r["published_ts"],
        "created_at": r["created_at"],
    }


def _coerce_float(value: Any) -> float | None:
    """Best-effort float coercion; ``None`` / unparseable → ``None``.

    Keeps portfolio numeric columns tolerant of missing form fields without
    storing ``"none"`` strings or raising on blank input.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _row_to_holding(r: dict[str, Any]) -> dict[str, Any]:
    """Hydrate a SQLite ``portfolio`` row into the API-shaped dict."""
    return {
        "id": int(r["id"]),
        "symbol": r["symbol"],
        "label": r.get("label"),
        "shares": r.get("shares"),
        "weight": r.get("weight"),
        "cost_basis": r.get("cost_basis"),
        "notes": r.get("notes"),
        "added_at": r["added_at"],
    }


def _row_to_review(r: dict[str, Any]) -> dict[str, Any]:
    """Hydrate a SQLite `reviews` row into the API-shaped dict.

    The `payload_json` column is decoded inline so the API layer never
    touches JSON parsing — keeps the FastAPI handlers thin.
    """
    payload_text = r.get("payload_json") or "{}"
    if payload_text == "{}":
        payload = {}
    elif payload_text == "[]":
        payload = []
    else:
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            payload = {}
    return {
        "capture_id": r["capture_id"],
        "reviewer_id": r["reviewer_id"],
        "reviewer_version": r["reviewer_version"],
        "payload": payload,
        "input_tokens": int(r.get("input_tokens") or 0),
        "output_tokens": int(r.get("output_tokens") or 0),
        "usd_cost": float(r.get("usd_cost") or 0.0),
        "latency_ms": int(r.get("latency_ms") or 0),
        "created_at": r["created_at"],
        "error_code": r.get("error_code"),
    }


def _resolve_storage_dir(out_dir: Path, value: str) -> Path:
    """Resolve a configured storage subdir the way :meth:`Settings.sqlite_path` does.

    A ``data/...``-prefixed value is rebased under the output dir; an absolute
    path is used verbatim; anything else is treated as relative to the output
    dir's parent. This mirrors the ``sqlite_path`` guard so an operator override
    that doesn't start with ``data/`` yields a usable path instead of a bare
    ``ValueError`` from ``Path.relative_to`` (the two code paths previously
    disagreed: sqlite_path tolerated it, this builder crashed).
    """
    p = Path(value)
    if str(p).startswith("data/"):
        return out_dir / p.relative_to("data")
    if p.is_absolute():
        return p
    return out_dir.parent / p


def load_storage_from_settings(settings: Any) -> Storage:
    """Helper: build a Storage from a Settings object."""
    out_dir = settings.paths.catchem_output_dir
    return Storage(
        db_path=settings.sqlite_path(),
        parquet_dir=_resolve_storage_dir(out_dir, settings.storage.parquet_results_dir),
        dlq_dir=_resolve_storage_dir(out_dir, settings.storage.dlq_dir),
        rotate_parquet_records=settings.storage.rotate_parquet_records,
        wal_autocheckpoint=settings.storage.wal_autocheckpoint,
    )
