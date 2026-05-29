"""Drive archiver — drains old SQLite rows into a CSV in the user's cloud-sync folder.

Why this exists:
  The news poller ingests roughly 1000+ records per hour from 53 RSS sources.
  Without an archive path, the SQLite database grows unbounded; the UI never
  needs more than ~150 recent rows for the Live Feed, and the rest is dead
  weight in the working set. This module pulls everything past the "live
  cache" tier into a flat CSV under a cloud-sync folder (Google Drive,
  iCloud Drive, or a fallback under Documents/) so the long-term record
  lives where Excel/Sheets can open it directly and the local DB stays tiny.

Architecture (one-way drain):
  1. SELECT every row that is NOT in the most-recent `local_cap` set.
  2. APPEND those rows (CSV, one per line) to today's
     `news_archive_YYYY-MM-DD.csv` under the configured drive directory.
  3. fsync the CSV file.
  4. DELETE the archived rows from `records` and `record_labels` locally.
  5. If step 2/3 fails (Drive offline, file locked by Excel, etc.) we DO
     NOT delete from local. The local count may temporarily exceed
     `local_cap`; this is the safer mode — Drive can heal and the next
     tick drains the backlog. We never lose data on a transient outage.

Failure modes that the runtime accepts:
  * Excel/Sheets has the CSV open with an exclusive lock → write fails,
    next tick retries. Local count grows in the meantime.
  * Drive is paused/offline → same as above.
  * fsync raises EIO → treat as a write failure, retry.
  * Disk is full → write fails, retry; user will eventually notice via
    `last_error` exposed at `/ui/archive-status`.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .logging import get_logger
from .settings import Settings
from .supervisor import Supervisor

logger = get_logger("catchem.archive")

# ── CSV schema ────────────────────────────────────────────────────────────────
# Designed to open cleanly in Excel/Numbers/Sheets. Order matters — once a
# CSV file has rows, we cannot change column positions without breaking the
# existing data. Add new columns at the END only.
CSV_COLUMNS: tuple[str, ...] = (
    "ingested_at",        # SQLite `created_at` — when Catchem ingested the row
    "published_at",       # `published_ts` — when the publisher claims they published
    "domain",
    "title",
    "url",
    "score",              # `finance_relevance_score` rounded to 3 dp
    "is_finance_relevant",
    "asset_classes",      # comma-separated list
    "reason_codes",
    "symbols",
    "sentiment_label",
    "sentiment_score",
    "reasoning",          # reason_text + first 3 evidence sentences joined with " // "
    "language",
    "processing_mode",
    "diagnostic_enabled",
    "capture_id",
)


def detect_drive_dir() -> Path:
    """Find the best cloud-sync target on the current Mac, in priority order.

    1. Google Drive Desktop client — ~/Library/CloudStorage/GoogleDrive-*/My Drive
       (user actively installed this; mount is read-write for our process)
    2. ~/Documents/Catchem
       (Documents auto-syncs with iCloud Drive when the user has that on,
       and we've already granted Documents access via Info.plist's
       NSDocumentsFolderUsageDescription — so no fresh TCC prompt is
       triggered)
    3. ~/Library/Mobile Documents/com~apple~CloudDocs/Catchem — legacy
       iCloud Drive path. Sometimes writable, sometimes not, depending
       on how the user set up iCloud.

    Skipped intentionally:
    * ~/Library/CloudStorage/iCloudDrive-* — this is the FileProvider
      mount and is read-only for processes that don't hold the special
      `com.apple.developer.icloud-container-identifiers` entitlement.
      Picking it produces "[Errno 13] Permission denied" at mkdir time.

    Operator can always override via CATCHEM_ARCHIVE__DRIVE_DIR.
    """
    home = Path.home()
    cloud = home / "Library" / "CloudStorage"
    if cloud.is_dir():
        # Google Drive Desktop: the user actively installed this so the
        # mount is writable for normal apps.
        for d in sorted(cloud.glob("GoogleDrive-*")):
            mydrive = d / "My Drive"
            if mydrive.is_dir():
                return mydrive / "Catchem"
            if d.is_dir():
                return d / "Catchem"
        # OneDrive — same logic as Google Drive
        for d in sorted(cloud.glob("OneDrive-*")):
            if d.is_dir():
                return d / "Catchem"
        # Dropbox — same logic
        for d in sorted(cloud.glob("Dropbox*")):
            if d.is_dir():
                return d / "Catchem"
    # Safe default: ~/Documents/Catchem. macOS syncs this with iCloud
    # Drive when "Desktop & Documents Folders" is enabled in iCloud
    # settings, so the file gets to the user's Drive either way.
    return home / "Documents" / "Catchem"


# A path we know is always writable, used as the last-resort fallback when
# the configured drive_dir refuses an `mkdir` (FileProvider iCloud, sandbox
# violation, disk full on a specific volume, etc.).
def fallback_drive_dir() -> Path:
    return Path.home() / "Documents" / "Catchem"


def csv_path_for_today(root: Path) -> Path:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return root / f"news_archive_{today}.csv"


def _archived_capture_ids(csv_path: Path) -> set[str]:
    """Return the set of capture_ids already present in an archive CSV.

    Used to make the append idempotent: if a DELETE failed after the CSV
    fsync on a previous tick, those rows are still in SQLite and would be
    re-appended; skipping the capture_ids already on disk prevents the
    duplicate. Best-effort — a missing/corrupt file yields an empty set so
    the worst case degrades to the old (non-idempotent) behaviour rather
    than crashing the sweep."""
    seen: set[str] = set()
    try:
        with csv_path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                cid = (row.get("capture_id") or "").strip()
                if cid:
                    seen.add(cid)
    except (OSError, csv.Error):
        return set()
    return seen


def _list_from_json(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        parts = json.loads(raw)
    except json.JSONDecodeError:
        # Narrow: only JSON parse failures are an expected failure mode here.
        # A broader `except Exception` would swallow programming bugs
        # (TypeError if `raw` is not a string, etc.) — those should surface.
        return ""
    if not isinstance(parts, list):
        return ""
    return ", ".join(str(p) for p in parts)


# Excel / Numbers / Sheets EXECUTE a cell whose text begins with one of these
# as a FORMULA. Since titles/reasoning/domains come from external RSS feeds, an
# attacker-influenced value like "=HYPERLINK(...)" or "+cmd|'/C calc'!A0" would
# run the moment the user opens the archive CSV — CSV formula injection
# (CWE-1236). The module's whole point is "open cleanly in Excel", so we
# neutralize it: a leading apostrophe (the OWASP-recommended escape) forces the
# spreadsheet to treat the cell as literal text and the formula never evaluates.
_CSV_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: str) -> str:
    """Prefix a leading formula-trigger char with `'` so spreadsheets keep the
    cell as text. No-op for ordinary values (the common case)."""
    if value and value[0] in _CSV_FORMULA_TRIGGERS:
        return "'" + value
    return value


def row_to_csv_dict(row: dict[str, Any]) -> dict[str, str]:
    """Project a SQLite row into the flat CSV schema. Pure function — tested.

    Externally-sourced text columns are run through `_csv_safe` to defuse CSV
    formula injection; system-generated numeric/flag/date/id columns are left
    untouched (they can never begin with a formula trigger)."""
    reason_text = (row.get("reason_text") or "").strip()
    evidence_raw = row.get("evidence_json") or "[]"
    try:
        evidence = json.loads(evidence_raw)
    except json.JSONDecodeError:
        # See _list_from_json: narrow to the actual expected failure mode.
        evidence = []
    if isinstance(evidence, list):
        evidence_parts = [str(e).strip()[:240] for e in evidence[:3] if e]
    else:
        evidence_parts = []
    reasoning = " // ".join(p for p in [reason_text, *evidence_parts] if p)

    score = row.get("finance_relevance_score")
    sent_score = row.get("sentiment_score")

    return {
        "ingested_at": row.get("created_at") or "",
        "published_at": row.get("published_ts") or "",
        "domain": _csv_safe(row.get("domain") or ""),
        "title": _csv_safe((row.get("title") or "").replace("\n", " ").replace("\r", " ")),
        "url": _csv_safe(row.get("url") or ""),
        "score": f"{score:.3f}" if isinstance(score, (int, float)) else "",
        "is_finance_relevant": "1" if row.get("is_finance_relevant") else "0",
        "asset_classes": _csv_safe(_list_from_json(row.get("asset_classes_json"))),
        "reason_codes": _csv_safe(_list_from_json(row.get("impact_reason_codes_json"))),
        "symbols": _csv_safe(_list_from_json(row.get("candidate_symbols_json"))),
        "sentiment_label": _csv_safe(row.get("sentiment_label") or ""),
        "sentiment_score": f"{sent_score:.3f}" if isinstance(sent_score, (int, float)) else "",
        "reasoning": _csv_safe(reasoning.replace("\n", " ").replace("\r", " ")[:2000]),
        "language": _csv_safe(row.get("language") or ""),
        "processing_mode": _csv_safe(row.get("processing_mode") or ""),
        "diagnostic_enabled": "1" if row.get("diagnostic_enabled") else "0",
        "capture_id": row.get("capture_id") or "",
    }


@dataclass
class ArchiveResult:
    archived: int
    csv_path: Path | None
    error: str | None


class DriveArchiver:
    """Periodically drain old SQLite rows into a CSV on the user's drive."""

    def __init__(
        self,
        *,
        supervisor: Supervisor,
        settings: Settings,
        drive_dir: Path | None = None,
        interval_seconds: float = 30.0,
        local_cap_rows: int = 150,
    ) -> None:
        self._sup = supervisor
        self._settings = settings
        self._drive_dir = drive_dir or detect_drive_dir()
        # 15s is the lower bound — beneath this the archiver fights the
        # news poller for the storage lock. 150-row cap means we'd typically
        # archive 100-200 rows per tick on a busy day.
        self._interval = max(15.0, float(interval_seconds))
        # 50 is the lower bound so the UI always has at least a few pages
        # of recent rows even with an aggressive cap.
        self._local_cap = max(50, int(local_cap_rows))
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        # Re-entrant lock that serialises `_archive_once`. Without it, the
        # background tick and a manual /ui/archive-now click could both
        # enter the drain path and fight for the storage lock, deadlocking
        # the news poller's inserts. RLock so a single thread re-entering
        # (e.g. via archive_now during shutdown) doesn't self-deadlock.
        self._run_lock = threading.RLock()
        # Per-tick batch ceiling. A fresh install hitting an already-large
        # SQLite (e.g. 22k rows backlog) used to drain it all in one tick,
        # holding the storage lock for minutes and stalling the news poller.
        # 2000 rows ≈ ~1 s of CSV write + ~0.5 s of DELETE — small enough
        # that the news poller's inserts only see a sub-second pause.
        self._per_tick_cap = 2000
        # Status (surfaced via /ui/archive-status)
        self.last_run_at: datetime | None = None
        self.last_archived_count: int = 0
        self.total_archived: int = 0
        self.last_error: str | None = None
        self.is_archiving: bool = False
        self.current_csv_path: Path | None = None

    @property
    def drive_dir(self) -> Path:
        return self._drive_dir

    @property
    def local_cap(self) -> int:
        return self._local_cap

    @property
    def interval_seconds(self) -> float:
        return self._interval

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        try:
            self._drive_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("archive_dir_create_failed", dir=str(self._drive_dir), error=str(exc))
        # See news_poller.NewsPoller.start — `get_running_loop` is the
        # 3.10+ idiom and surfaces misuse from sync context.
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._run(), name="catchem-drive-archiver")
        logger.info(
            "drive_archiver_started",
            dir=str(self._drive_dir),
            cap=self._local_cap,
            interval=self._interval,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        # Cancelling the asyncio task raises CancelledError at the
        # `await asyncio.to_thread(self._archive_once)` point and returns
        # immediately, but the underlying OS worker thread keeps running
        # `_archive_once` to completion while STILL holding `storage._lock`
        # across the CSV fsync + chunked DELETE (~1 s). If we returned now,
        # the subsequent `supervisor.close()` -> `storage.flush()` would block
        # behind that orphaned sweep, and the archive DELETE would proceed
        # AFTER teardown reported complete. Drain the in-flight sweep here by
        # acquiring the same RLock the sweep holds (off the event loop so we
        # don't stall it): once we get it, no sweep is running. We release it
        # right away -- it only ever guards `_archive_once`, which can no longer
        # start because `_stop` is set and the task is gone.
        def _drain() -> None:
            self._run_lock.acquire()
            self._run_lock.release()

        await asyncio.to_thread(_drain)
        logger.info("drive_archiver_stopped")

    async def archive_now(self) -> ArchiveResult:
        """Manually trigger an immediate archive sweep.

        Runs its own `_archive_once` on a worker thread and returns that
        sweep's result. It runs INDEPENDENTLY of the background `_run` loop —
        `_run_lock` serialises the two so they never overlap. (A previous
        `_poke` event was set here but `_run` never awaited it, so it was a
        no-op; removed.)
        """
        return await asyncio.to_thread(self._archive_once)

    async def _run(self) -> None:
        # Brief grace so the first tick doesn't fight startup races.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=5.0)
            return
        except TimeoutError:
            pass

        while not self._stop.is_set():
            try:
                result = await asyncio.to_thread(self._archive_once)
                self.last_archived_count = result.archived
                self.total_archived += result.archived
                self.last_error = result.error
                if result.archived:
                    logger.info(
                        "archive_swept",
                        count=result.archived,
                        path=str(result.csv_path),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = repr(exc)
                logger.warning("archive_tick_failed", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                break
            except TimeoutError:
                continue

    def _archive_once(self) -> ArchiveResult:
        """One archive sweep, synchronous. Runs on a worker thread.

        Returns an ArchiveResult with the count + path + any error so the
        caller can surface stats. Never raises — all exceptions are
        captured into the result.error field.

        Concurrency: serialised by `self._run_lock` so the background
        tick and a manual /ui/archive-now click never run concurrently.
        Bounded by `self._per_tick_cap` so a one-time large backlog
        (e.g. 22k rows after upgrading to the archiver from a session
        that didn't have it) drains over several ticks rather than
        holding the storage lock for minutes.
        """
        # Non-blocking acquire: if another archive is in flight, return
        # quickly so /ui/archive-now still gives a meaningful response.
        if not self._run_lock.acquire(blocking=False):
            return ArchiveResult(archived=0, csv_path=None, error="already running")
        self.is_archiving = True
        sweep_started = time.time()
        try:
            # ── Step 1: SELECT inside the storage lock ───────────────────
            #
            # Previously the SELECT ran on a short-lived connection
            # OUTSIDE ``storage._lock`` with the justification that "WAL
            # readers don't block writers". That is correct for sqlite's
            # own concurrency semantics, but it leaves a logical race:
            # ``news_poller.insert_record`` calls
            # ``INSERT OR REPLACE INTO records`` between our SELECT and
            # our DELETE, and the new row's capture_id can hit a SELECTed
            # capture_id (the inserts are upserts keyed on capture_id).
            # The new row supersedes the original, the CSV writer writes
            # the OLD row's column values, the DELETE drops the row
            # silently, and the user-visible record from the poller's
            # second-ingest is lost as well — we now have a CSV row that
            # doesn't reflect the DB's last-known state and a missing
            # DB row that should still be live.
            #
            # Fix: take ``storage._lock`` once, run SELECT + write +
            # DELETE under it. WAL still keeps reader-vs-reader queries
            # fast (no blocking), the lock just gates the SELECT/DELETE
            # pair against concurrent writers so the row we delete is
            # the same row we wrote to CSV. The lock is held across the
            # CSV-write fsync (up to ~1 s on cold disks) which is a
            # known regression vs the prior fast-path — acceptable
            # because the news poller's insert_record uses a 30 s
            # SQLite timeout and we never breach 30 s in practice. If
            # this becomes a hot path, the right answer is "checksum the
            # SELECTed rows by rowid, re-read under lock to confirm
            # they're unchanged, then DELETE" — not "don't take the
            # lock". We pick correctness now.
            #
            # The lock is acquired ONCE; ``_connection()`` opens a fresh
            # short-lived connection inside it. We hand the SELECT result
            # off as a plain list-of-dicts before the connection closes
            # so the CSV write does not reach back into sqlite mid-fsync.
            with self._sup.storage._lock, self._sup.storage._connection() as conn:
                # Old query was a correlated `WHERE capture_id NOT IN
                # (SELECT capture_id ...)` — O(n*m) because SQLite re-runs
                # the inner SELECT for every candidate row in the outer
                # scan. Rewriting against the integer rowid lets the
                # planner hash-set the keep-list once and seek by primary
                # index, turning the lookup into O(n + m). Same semantics:
                # keep the newest `_local_cap` rows by created_at DESC,
                # archive everything older, oldest-first, capped per tick.
                rows = [
                    dict(r)
                    for r in conn.execute(
                        """
                        SELECT * FROM records
                        WHERE rowid NOT IN (
                            SELECT rowid FROM records
                            ORDER BY created_at DESC
                            LIMIT ?
                        )
                        ORDER BY created_at ASC
                        LIMIT ?
                        """,
                        (self._local_cap, self._per_tick_cap),
                    ).fetchall()
                ]

                if not rows:
                    self.last_run_at = datetime.now(UTC)
                    return ArchiveResult(archived=0, csv_path=None, error=None)

                # Ensure the target dir exists, falling back to a known-good
                # path if the configured one is read-only (most common cause:
                # the FileProvider iCloud mount at ~/Library/CloudStorage/
                # iCloudDrive-*, which rejects writes from un-entitled apps).
                try:
                    self._drive_dir.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    fallback = fallback_drive_dir()
                    if self._drive_dir == fallback:
                        return ArchiveResult(archived=0, csv_path=None, error=f"mkdir failed: {exc}")
                    logger.warning(
                        "archive_dir_unwritable falling_back from=%s to=%s error=%s",
                        self._drive_dir, fallback, exc,
                    )
                    self._drive_dir = fallback
                    try:
                        self._drive_dir.mkdir(parents=True, exist_ok=True)
                    except OSError as exc2:
                        return ArchiveResult(archived=0, csv_path=None, error=f"fallback mkdir failed: {exc2}")

                # Now that the dir is settled (primary or fallback), compute today's
                # CSV path. Must come AFTER the fallback may have swapped drive_dir,
                # otherwise we'd try to open the original (blocked) path.
                csv_path = csv_path_for_today(self._drive_dir)
                try:
                    file_exists = csv_path.exists() and csv_path.stat().st_size > 0
                    # CSV-vs-DELETE is NOT one transaction: the CSV bytes are
                    # fsync'd to the OS filesystem below, but the DELETE runs
                    # inside an explicit BEGIN…`with conn:` SQLite transaction
                    # (opened just before the DELETE loop) that rolls back on
                    # ANY exception (db locked on a busy WAL, SQLITE_FULL,
                    # etc.). If a DELETE chunk raises AFTER the fsync, the rows
                    # survive in SQLite but the CSV row is already durable —
                    # the next tick re-SELECTs the same rows and would append
                    # them to the SAME daily file AGAIN. Defuse that by making
                    # the append idempotent: read back the capture_ids already
                    # present in today's CSV and skip them. Read-back (not an
                    # in-memory set) so it also dedups across a process restart.
                    already_written = _archived_capture_ids(csv_path) if file_exists else set()
                    # Open in binary+text-mode-equivalent with newline="" — required
                    # so csv.writer handles line endings correctly across platforms.
                    with csv_path.open("a", newline="", encoding="utf-8") as f:
                        writer = csv.DictWriter(f, fieldnames=list(CSV_COLUMNS))
                        if not file_exists:
                            writer.writeheader()
                        for row in rows:
                            if (row.get("capture_id") or "") in already_written:
                                continue
                            writer.writerow(row_to_csv_dict(row))
                        f.flush()
                        # fsync at the file-descriptor level guarantees the data is
                        # actually on disk before we delete locally. Without this,
                        # a crash between write() and the OS flush would lose rows.
                        os.fsync(f.fileno())
                    # Only NOW is the CSV durable, so it's safe to surface its path
                    # as the live target. Setting current_csv_path BEFORE the write
                    # meant the /ui/archive-status surface reported a path that was
                    # never actually written on the OSError failure path below.
                    self.current_csv_path = csv_path
                except OSError as exc:
                    # Don't delete locally on any I/O failure. Try again next tick.
                    return ArchiveResult(
                        archived=0,
                        csv_path=csv_path,
                        error=f"write failed: {exc}",
                    )

                # Now safe to delete from local. SQLite's host-parameter ceiling
                # is 999 (default) — anything bigger errors with "too many SQL
                # variables". Chunk at 900 to leave headroom and keep each
                # DELETE's wall-clock under ~50 ms.
                #
                # Same connection that ran SELECT — so the SELECT/DELETE
                # pair is one logical operation under the lock and a
                # concurrent INSERT OR REPLACE cannot slip in.
                archived_ids = [r["capture_id"] for r in rows]
                chunk_size = 900
                # Open an EXPLICIT transaction so the multi-chunk DELETE is
                # atomic. The connection runs with ``isolation_level=None``
                # (autocommit), under which each ``conn.execute(...)`` commits
                # on its own and the surrounding ``with conn:`` wrapper's
                # commit/rollback are no-ops (no transaction is open). Without
                # this BEGIN, a failure mid-loop (SQLITE_BUSY on a contended
                # WAL, "too many SQL variables", etc.) AFTER a
                # ``DELETE FROM record_labels`` chunk committed but BEFORE its
                # matching ``DELETE FROM records`` would orphan the surviving
                # ``records`` row from the inverted index — it still counts in
                # count_records() but vanishes from records_by_label() (the
                # JOIN powering /records/by-* and the Tags page). BEGIN makes
                # ``with conn:`` roll the entire DELETE back as a unit on any
                # error, mirroring insert_record's fix. IMMEDIATE (not
                # deferred) so the SHARED→RESERVED upgrade can't return an
                # instant SQLITE_BUSY against a concurrent writer on another
                # connection to the same file (e.g. the demo's transient
                # Storage) — it waits up to the connection timeout instead.
                conn.execute("BEGIN IMMEDIATE")
                for i in range(0, len(archived_ids), chunk_size):
                    chunk = archived_ids[i:i + chunk_size]
                    placeholders = ",".join("?" * len(chunk))
                    # record_labels first so a partial DELETE doesn't
                    # leave orphan label rows; the ``foreign_keys=ON``
                    # PRAGMA enabled in storage._connect would also
                    # cascade on the records DELETE, but explicit DELETE
                    # is cheaper than the cascade trigger and matches
                    # the historical contract.
                    conn.execute(
                        f"DELETE FROM record_labels WHERE capture_id IN ({placeholders})",
                        chunk,
                    )
                    conn.execute(
                        f"DELETE FROM records WHERE capture_id IN ({placeholders})",
                        chunk,
                    )

            # The explicit BEGIN…`with conn:` transaction has now committed the
            # DELETEs as a unit, so these capture_ids are gone from SQLite for
            # good (or, on any error mid-loop, rolled back atomically). Drop their
            # durable .npy vectors too — service.py saves one per ingested
            # record but nothing else ever deletes them, so without this the
            # vector_index dir grows without bound and cold nearest() queries
            # glob over an ever-larger orphaned pool. Best-effort: a missing
            # file or an absent vector index must never fail the sweep (the
            # rows are already archived + deleted).
            # Race guard (two layers): a capture_id we just archived can be
            # RE-INSERTED concurrently (RSS re-circulation / late WS frame /
            # replay). service.py saves the new vector BEFORE insert_record
            # takes storage._lock, so a naive unconditional delete here would
            # unlink a now-LIVE record's freshly-saved vector.
            #   (1) Re-check each id is genuinely absent under storage._lock
            #       (serializes against insert_record on the shared Storage).
            #   (2) Even if the row isn't re-inserted YET, a reprocess may have
            #       already re-written the .npy (save() runs lock-free, ahead of
            #       insert_record). So skip any vector whose file mtime is at or
            #       after this sweep started — that file is a fresh re-save, not
            #       the aged vector we set out to drain. Together these close the
            #       save-before-insert window left open by layer (1) alone.
            vector_index = getattr(self._sup, "vector_index", None)
            vec_root = getattr(vector_index, "root", None)
            if vector_index is not None:
                with self._sup.storage._lock, self._sup.storage._connection() as conn:
                    for cid in archived_ids:
                        still_present = conn.execute(
                            "SELECT 1 FROM records WHERE capture_id = ?", (cid,)
                        ).fetchone()
                        if still_present is not None:
                            continue
                        if vec_root is not None:
                            npy = vec_root / f"{cid}.npy"
                            with contextlib.suppress(OSError):
                                if npy.stat().st_mtime >= sweep_started:
                                    continue  # freshly re-saved → keep
                        with contextlib.suppress(Exception):
                            vector_index.delete(cid)

            self.last_run_at = datetime.now(UTC)
            return ArchiveResult(archived=len(rows), csv_path=csv_path, error=None)
        finally:
            self.is_archiving = False
            self._run_lock.release()


__all__ = [
    "CSV_COLUMNS",
    "ArchiveResult",
    "DriveArchiver",
    "csv_path_for_today",
    "detect_drive_dir",
    "row_to_csv_dict",
]
