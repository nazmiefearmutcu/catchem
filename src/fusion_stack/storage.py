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
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import pyarrow as pa
import pyarrow.parquet as pq

from .logging import get_logger
from .schemas import FinancialImpactRecord, ReplayOffset

logger = get_logger("fusion.storage")


SCHEMA_VERSION = 1


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
INSERT OR IGNORE INTO schema_version (version) VALUES (1);

CREATE TABLE IF NOT EXISTS records (
    capture_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    title TEXT,
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
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)
        logger.info("storage_initialized", db=str(self.db_path), parquet=str(self.parquet_dir))

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            conn = self._connect()
            try:
                yield conn.cursor()
            finally:
                conn.close()

    # ── record write ---------------------------------------------------------
    def insert_record(self, rec: FinancialImpactRecord) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO records (
                    capture_id, doc_id, title, domain, language,
                    is_finance_relevant, finance_relevance_score,
                    asset_classes_json, impact_reason_codes_json,
                    candidate_symbols_json, candidate_entities_json,
                    impact_horizons_json,
                    sentiment_label, sentiment_score,
                    evidence_json, reason_text, component_scores_json,
                    diagnostic_enabled, diagnostic_json,
                    processing_mode, model_versions_json,
                    published_ts, created_at, url
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    rec.capture_id,
                    rec.doc_id,
                    rec.title,
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
                    (component, ver, datetime.now(timezone.utc).isoformat()),
                )

            self._pending_rows.append(_record_to_row(rec))
            if len(self._pending_rows) >= self.rotate_parquet_records:
                self._flush_parquet_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_parquet_locked()

    def _flush_parquet_locked(self) -> None:
        if not self._pending_rows:
            return
        table = pa.Table.from_pylist(self._pending_rows)
        now = datetime.now(timezone.utc)
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
        with self._lock, self._connect() as conn:
            return [_row_to_payload(dict(r)) for r in conn.execute(sql, params)]

    def get_record(self, capture_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            r = conn.execute("SELECT * FROM records WHERE capture_id = ?", (capture_id,)).fetchone()
            return _row_to_payload(dict(r)) if r else None

    def by_label(self, kind: str, value: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
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
        with self._lock, self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            relevant = conn.execute("SELECT COUNT(*) FROM records WHERE is_finance_relevant = 1").fetchone()[0]
            return {"total": int(total), "finance_relevant": int(relevant)}

    # ── offsets --------------------------------------------------------------
    def get_offset(self, source_path: str) -> ReplayOffset:
        with self._lock, self._connect() as conn:
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
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO offsets
                   (source_path, line_offset, last_capture_id, updated_at)
                   VALUES (?,?,?,?)""",
                (
                    offset.source_path,
                    int(offset.line_offset),
                    offset.last_capture_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    # ── DLQ ------------------------------------------------------------------
    def record_failure(self, capture_id: str | None, error: str, payload_excerpt: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO dlq (capture_id, error, payload_excerpt, created_at)
                   VALUES (?,?,?,?)""",
                (capture_id, error, payload_excerpt[:4000], datetime.now(timezone.utc).isoformat()),
            )

    def dlq_count(self) -> int:
        with self._lock, self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM dlq").fetchone()[0])

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


def load_storage_from_settings(settings: "Any") -> Storage:
    """Helper: build a Storage from a Settings object."""
    out_dir = settings.paths.fusion_output_dir
    return Storage(
        db_path=settings.sqlite_path(),
        parquet_dir=out_dir / Path(settings.storage.parquet_results_dir).relative_to("data"),
        dlq_dir=out_dir / Path(settings.storage.dlq_dir).relative_to("data"),
        rotate_parquet_records=settings.storage.rotate_parquet_records,
    )
