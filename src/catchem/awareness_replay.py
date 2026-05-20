"""Resumable replay loop. Uses Storage offsets to ensure each capture is
processed exactly once across restarts."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

from .awareness_reader import iter_captures, iter_finalized_files
from .logging import get_logger
from .schemas import AwarenessCaptureView, ReplayOffset
from .storage import Storage

logger = get_logger("catchem.awareness_replay")


class ReplayRunner:
    """Replay finalized Awareness JSONL into a callback. Persists per-file offsets."""

    def __init__(
        self,
        root: Path,
        storage: Storage,
        pattern: str = "**/*.jsonl",
        offset_persist_seconds: float = 5.0,
    ) -> None:
        self.root = root
        self.storage = storage
        self.pattern = pattern
        self.offset_persist_seconds = float(offset_persist_seconds)

    def files(self) -> list[Path]:
        return iter_finalized_files(self.root, self.pattern)

    def run_once(
        self,
        handle: Callable[[AwarenessCaptureView], None],
        max_records: int | None = None,
    ) -> dict[str, int]:
        """Process one pass over all finalized files. Returns counters."""
        processed = 0
        skipped = 0
        for path in self.files():
            offset = self.storage.get_offset(str(path))
            last_persist = time.monotonic()
            for line_idx, cap in iter_captures(path, start_offset=offset.line_offset):
                try:
                    handle(cap)
                    processed += 1
                except Exception as exc:
                    logger.exception("handler_error", capture_id=cap.capture_id, err=str(exc))
                    self.storage.record_failure(cap.capture_id, str(exc), cap.text[:2000])
                    skipped += 1

                offset = ReplayOffset(
                    source_path=str(path),
                    line_offset=line_idx,
                    last_capture_id=cap.capture_id,
                    updated_at=datetime.now(timezone.utc),
                )
                # Persist periodically so we don't double-process on crash.
                if (time.monotonic() - last_persist) >= self.offset_persist_seconds:
                    self.storage.save_offset(offset)
                    last_persist = time.monotonic()

                if max_records and processed >= max_records:
                    self.storage.save_offset(offset)
                    return {"processed": processed, "skipped": skipped}

            # End of file — persist final offset.
            self.storage.save_offset(offset)
        return {"processed": processed, "skipped": skipped}

    def tail(
        self,
        handle: Callable[[AwarenessCaptureView], None],
        poll_seconds: float = 10.0,
        max_per_tick: int = 50,
        stop: Callable[[], bool] | None = None,
    ) -> None:
        """Long-running: repeatedly run_once with a sleep. Cooperative stop via callable."""
        logger.info("tail_started", root=str(self.root), poll=poll_seconds)
        while True:
            if stop is not None and stop():
                logger.info("tail_stopped_by_caller")
                return
            counts = self.run_once(handle, max_records=max_per_tick)
            if counts["processed"] == 0 and counts["skipped"] == 0:
                time.sleep(poll_seconds)
