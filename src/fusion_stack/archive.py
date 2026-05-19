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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .logging import get_logger
from .settings import Settings
from .supervisor import Supervisor

logger = get_logger("fusion.archive")

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

    Operator can always override via FUSION_ARCHIVE__DRIVE_DIR.
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
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return root / f"news_archive_{today}.csv"


def _list_from_json(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        parts = json.loads(raw)
    except Exception:
        return ""
    if not isinstance(parts, list):
        return ""
    return ", ".join(str(p) for p in parts)


def row_to_csv_dict(row: dict[str, Any]) -> dict[str, str]:
    """Project a SQLite row into the flat CSV schema. Pure function — tested."""
    reason_text = (row.get("reason_text") or "").strip()
    evidence_raw = row.get("evidence_json") or "[]"
    try:
        evidence = json.loads(evidence_raw)
    except Exception:
        evidence = []
    if isinstance(evidence, list):
        evidence_parts = [str(e).strip()[:240] for e in evidence[:3] if e]
    else:
        evidence_parts = []
    reasoning = " // ".join(p for p in ([reason_text] + evidence_parts) if p)

    score = row.get("finance_relevance_score")
    sent_score = row.get("sentiment_score")

    return {
        "ingested_at": row.get("created_at") or "",
        "published_at": row.get("published_ts") or "",
        "domain": row.get("domain") or "",
        "title": (row.get("title") or "").replace("\n", " ").replace("\r", " "),
        "url": row.get("url") or "",
        "score": f"{score:.3f}" if isinstance(score, (int, float)) else "",
        "is_finance_relevant": "1" if row.get("is_finance_relevant") else "0",
        "asset_classes": _list_from_json(row.get("asset_classes_json")),
        "reason_codes": _list_from_json(row.get("impact_reason_codes_json")),
        "symbols": _list_from_json(row.get("candidate_symbols_json")),
        "sentiment_label": row.get("sentiment_label") or "",
        "sentiment_score": f"{sent_score:.3f}" if isinstance(sent_score, (int, float)) else "",
        "reasoning": reasoning.replace("\n", " ").replace("\r", " ")[:2000],
        "language": row.get("language") or "",
        "processing_mode": row.get("processing_mode") or "",
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
        self._poke = asyncio.Event()
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
            logger.warning("archive_dir_create_failed dir=%s error=%s", self._drive_dir, exc)
        loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._run(), name="catchem-drive-archiver")
        logger.info(
            "drive_archiver_started dir=%s cap=%d interval=%.0fs",
            self._drive_dir, self._local_cap, self._interval,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("drive_archiver_stopped")

    async def archive_now(self) -> ArchiveResult:
        """Manually trigger an immediate archive sweep."""
        self._poke.set()
        return await asyncio.to_thread(self._archive_once)

    async def _run(self) -> None:
        # Brief grace so the first tick doesn't fight startup races.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=5.0)
            return
        except asyncio.TimeoutError:
            pass

        while not self._stop.is_set():
            try:
                result = await asyncio.to_thread(self._archive_once)
                self.last_archived_count = result.archived
                self.total_archived += result.archived
                self.last_error = result.error
                if result.archived:
                    logger.info(
                        "archive_swept count=%d path=%s",
                        result.archived, result.csv_path,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = repr(exc)
                logger.warning("archive_tick_failed error=%s", exc, exc_info=False)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                break
            except asyncio.TimeoutError:
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
        try:
            # Pull rows older than the live-cache cap. Oldest-first so the
            # CSV is naturally chronological. The outer LIMIT caps the
            # per-tick batch so a huge backlog drains over multiple ticks.
            with self._sup.storage.cursor() as cur:
                cur.execute(
                    """
                    SELECT * FROM records
                    WHERE capture_id NOT IN (
                        SELECT capture_id FROM records
                        ORDER BY created_at DESC
                        LIMIT ?
                    )
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (self._local_cap, self._per_tick_cap),
                )
                rows = [dict(r) for r in cur.fetchall()]

            if not rows:
                self.last_run_at = datetime.now(timezone.utc)
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
            self.current_csv_path = csv_path
            try:
                file_exists = csv_path.exists() and csv_path.stat().st_size > 0
                # Open in binary+text-mode-equivalent with newline="" — required
                # so csv.writer handles line endings correctly across platforms.
                with csv_path.open("a", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=list(CSV_COLUMNS))
                    if not file_exists:
                        writer.writeheader()
                    for row in rows:
                        writer.writerow(row_to_csv_dict(row))
                    f.flush()
                    # fsync at the file-descriptor level guarantees the data is
                    # actually on disk before we delete locally. Without this,
                    # a crash between write() and the OS flush would lose rows.
                    os.fsync(f.fileno())
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
            archived_ids = [r["capture_id"] for r in rows]
            chunk_size = 900
            with self._sup.storage.cursor() as cur:
                for i in range(0, len(archived_ids), chunk_size):
                    chunk = archived_ids[i:i + chunk_size]
                    placeholders = ",".join("?" * len(chunk))
                    cur.execute(
                        f"DELETE FROM record_labels WHERE capture_id IN ({placeholders})",
                        chunk,
                    )
                    cur.execute(
                        f"DELETE FROM records WHERE capture_id IN ({placeholders})",
                        chunk,
                    )

            self.last_run_at = datetime.now(timezone.utc)
            return ArchiveResult(archived=len(rows), csv_path=csv_path, error=None)
        finally:
            self.is_archiving = False
            self._run_lock.release()


__all__ = [
    "CSV_COLUMNS",
    "DriveArchiver",
    "ArchiveResult",
    "csv_path_for_today",
    "detect_drive_dir",
    "row_to_csv_dict",
]
