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

import json
import re
import sqlite3
import threading
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


class Storage:
    """SQLite + parquet + jsonl storage. Thread-safe via instance lock."""

    def __init__(
        self,
        db_path: Path,
        parquet_dir: Path,
        dlq_dir: Path,
        rotate_parquet_records: int = 5000,
    ) -> None:
        self.db_path = db_path
        self.parquet_dir = parquet_dir
        self.dlq_dir = dlq_dir
        self.rotate_parquet_records = max(100, int(rotate_parquet_records))

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.parquet_dir.mkdir(parents=True, exist_ok=True)
        self.dlq_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._pending_rows: list[dict[str, Any]] = []
        self._init_db()

    # ── DB --------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
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

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        from .migrations import apply_migrations

        with self._lock, self._connection() as conn:
            conn.executescript(_SCHEMA_SQL)
            self._migrate_records_table(conn)
            # Apply versioned migrations after the legacy ``IF NOT EXISTS``
            # bootstrap. A pre-existing DB from before the migration
            # framework lands at ``user_version = 0``; baseline migration
            # #1 claims it idempotently. Future ALTER/CREATE INDEX work
            # ships as appended entries in ``catchem.migrations``.
            applied = apply_migrations(conn)
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
            existed = conn.execute(
                "SELECT 1 FROM records WHERE capture_id = ?",
                (rec.capture_id,),
            ).fetchone() is not None
            conn.execute(
                """
                INSERT OR REPLACE INTO records (
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
                    json.dumps(rec.diagnostic_multimodal_result) if rec.diagnostic_multimodal_result else None,
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
            if len(self._pending_rows) >= self.rotate_parquet_records:
                self._flush_parquet_locked()
            return not existed

    def flush(self) -> None:
        with self._lock:
            self._flush_parquet_locked()

    def _flush_parquet_locked(self) -> None:
        if not self._pending_rows:
            return
        table = pa.Table.from_pylist(self._pending_rows)
        now = datetime.now(UTC)
        path = self.parquet_dir / f"records-{int(now.timestamp())}-{len(self._pending_rows):05d}.parquet"
        pq.write_table(table, path)
        logger.info("parquet_flushed", path=str(path), rows=len(self._pending_rows))
        self._pending_rows.clear()

    # ── queries --------------------------------------------------------------
    def recent_records(self, limit: int = 50, relevant_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM records"
        params: list[Any] = []
        if relevant_only:
            sql += " WHERE is_finance_relevant = 1"
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        with self._lock, self._connection() as conn:
            return [_row_to_payload(dict(r)) for r in conn.execute(sql, params)]

    def get_record(self, capture_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as conn:
            r = conn.execute("SELECT * FROM records WHERE capture_id = ?", (capture_id,)).fetchone()
            return _row_to_payload(dict(r)) if r else None

    def by_label(self, kind: str, value: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connection() as conn:
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
        with self._lock, self._connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            relevant = conn.execute("SELECT COUNT(*) FROM records WHERE is_finance_relevant = 1").fetchone()[0]
            return {"total": int(total), "finance_relevant": int(relevant)}

    # ── offsets --------------------------------------------------------------
    def get_offset(self, source_path: str) -> ReplayOffset:
        with self._lock, self._connection() as conn:
            r = conn.execute("SELECT * FROM offsets WHERE source_path=?", (source_path,)).fetchone()
            if r is None:
                return ReplayOffset(source_path=source_path)
            return ReplayOffset(
                source_path=source_path,
                line_offset=int(r["line_offset"]),
                last_capture_id=r["last_capture_id"],
                updated_at=datetime.fromisoformat(r["updated_at"]),
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
        with self._lock, self._connection() as conn:
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
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """SELECT * FROM reviews WHERE capture_id = ? ORDER BY reviewer_id""",
                (capture_id,),
            ).fetchall()
            return [_row_to_review(dict(r)) for r in rows]

    def recent_reviews(self, reviewer_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """Recent rows for a single reviewer — feeds the compare dashboard."""
        with self._lock, self._connection() as conn:
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
        with self._lock, self._connection() as conn:
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
                a = {
                    "capture_id": d["capture_id"],
                    "reviewer_id": d["a_id"],
                    "reviewer_version": d["a_ver"],
                    "created_at": d["a_ts"],
                    "error_code": d["a_err"],
                    "payload": json.loads(d["a_payload"]) if d["a_payload"] else {},
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
                    "payload": json.loads(d["b_payload"]) if d["b_payload"] else {},
                }
                out.append((a, b))
            return out

    def sum_review_cost(self, reviewer_id: str) -> float:
        """Cumulative USD spend for a reviewer (used by the budget guard)."""
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(usd_cost), 0.0) FROM reviews WHERE reviewer_id = ?",
                (reviewer_id,),
            ).fetchone()
            return float(row[0]) if row else 0.0

    def review_token_totals(self, reviewer_id: str) -> dict[str, int]:
        """Cumulative token counts for a reviewer (compare dashboard footer)."""
        with self._lock, self._connection() as conn:
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
                "DELETE FROM dlq WHERE id NOT IN ("
                "  SELECT id FROM dlq ORDER BY id DESC LIMIT ?"
                ")",
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
        with self._lock, self._connection() as conn:
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
        with self._lock, self._connection() as conn:
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
        with self._lock, self._connection() as conn:
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
            row = conn.execute(
                "SELECT * FROM portfolio WHERE id = ?", (holding_id,)
            ).fetchone()
            return _row_to_holding(dict(row))

    def list_holdings(self) -> list[dict[str, Any]]:
        """All holdings, newest-added first (ties broken by id desc)."""
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM portfolio ORDER BY added_at DESC, id DESC"
            ).fetchall()
            return [_row_to_holding(dict(r)) for r in rows]

    def get_holding(self, holding_id: int) -> dict[str, Any] | None:
        """Single holding by id, or ``None`` if absent."""
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM portfolio WHERE id = ?", (int(holding_id),)
            ).fetchone()
            return _row_to_holding(dict(row)) if row else None

    def delete_holding(self, holding_id: int) -> bool:
        """Delete a holding. Returns ``True`` when a row was removed."""
        with self._lock, self._connection() as conn:
            cur = conn.execute(
                "DELETE FROM portfolio WHERE id = ?", (int(holding_id),)
            )
            return int(cur.rowcount or 0) > 0

    # ── housekeeping ---------------------------------------------------------
    def close(self) -> None:
        self.flush()


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
        "diagnostic_json": json.dumps(rec.diagnostic_multimodal_result) if rec.diagnostic_multimodal_result else None,
        "model_versions_json": json.dumps(rec.model_versions),
        "processing_mode": rec.processing_mode.value,
        "published_ts": rec.published_ts.isoformat() if rec.published_ts else None,
        "created_at": rec.created_at.isoformat(),
        "url": rec.url,
    }


def _row_to_payload(r: dict[str, Any]) -> dict[str, Any]:
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
        "asset_classes": json.loads(r["asset_classes_json"]),
        "impact_reason_codes": json.loads(r["impact_reason_codes_json"]),
        "candidate_symbols": json.loads(r["candidate_symbols_json"]),
        "candidate_entities": json.loads(r["candidate_entities_json"]),
        "impact_horizons": json.loads(r["impact_horizons_json"]),
        "sentiment_label": r["sentiment_label"],
        "sentiment_score": r["sentiment_score"],
        "evidence_sentences": json.loads(r["evidence_json"]),
        "reason_text": r["reason_text"],
        "component_scores": json.loads(r["component_scores_json"]),
        "diagnostic_multimodal_enabled": bool(r["diagnostic_enabled"]),
        "diagnostic_multimodal_result": json.loads(r["diagnostic_json"]) if r["diagnostic_json"] else None,
        "processing_mode": r["processing_mode"],
        "model_versions": json.loads(r["model_versions_json"]),
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


def load_storage_from_settings(settings: Any) -> Storage:
    """Helper: build a Storage from a Settings object."""
    out_dir = settings.paths.catchem_output_dir
    return Storage(
        db_path=settings.sqlite_path(),
        parquet_dir=out_dir / Path(settings.storage.parquet_results_dir).relative_to("data"),
        dlq_dir=out_dir / Path(settings.storage.dlq_dir).relative_to("data"),
        rotate_parquet_records=settings.storage.rotate_parquet_records,
    )
