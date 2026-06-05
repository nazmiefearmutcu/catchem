"""Resumable replay loop. Uses Storage offsets to ensure each capture is
processed exactly once across restarts."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .awareness_reader import iter_captures, iter_finalized_files
from .logging import get_logger
from .schemas import AwarenessCaptureView, ReplayOffset
from .storage import Storage

logger = get_logger("catchem.awareness_replay")


class BatchPartialFailure(Exception):
    """Raised when batch processing partially fails and runs item-by-item fallback."""

    def __init__(self, processed: int, failed: int, skipped: int) -> None:
        super().__init__(f"Batch partial failure: processed={processed}, failed={failed}, skipped={skipped}")
        self.processed = processed
        self.failed = failed
        self.skipped = skipped


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
        failed = 0
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
                    failed += 1
                    skipped += 1

                offset = ReplayOffset(
                    source_path=str(path),
                    line_offset=line_idx,
                    last_capture_id=cap.capture_id,
                    updated_at=datetime.now(UTC),
                )
                # Persist periodically so we don't double-process on crash.
                if (time.monotonic() - last_persist) >= self.offset_persist_seconds:
                    self.storage.save_offset(offset)
                    last_persist = time.monotonic()

                if max_records and processed >= max_records:
                    self.storage.save_offset(offset)
                    self.storage.checkpoint()
                    return {"processed": processed, "skipped": skipped, "failed": failed}

            # End of file — persist final offset.
            self.storage.save_offset(offset)
            self.storage.checkpoint()
        return {"processed": processed, "skipped": skipped, "failed": failed}

    def run_once_batch(
        self,
        handle_batch: Callable[[list[AwarenessCaptureView]], None],
        max_records: int | None = None,
        batch_size: int = 32,
    ) -> dict[str, int]:
        """Process one pass over all finalized files in batches. Returns counters."""
        processed = 0
        skipped = 0
        failed = 0
        batch_size = max(1, int(batch_size))

        for path in self.files():
            offset = self.storage.get_offset(str(path))
            last_persist = time.monotonic()
            batch = []
            offsets_in_batch = []

            for line_idx, cap in iter_captures(path, start_offset=offset.line_offset):
                limit = batch_size
                if max_records is not None:
                    remaining = max_records - processed
                    if remaining <= 0:
                        break
                    limit = min(batch_size, remaining)

                batch.append(cap)
                offsets_in_batch.append(
                    ReplayOffset(
                        source_path=str(path),
                        line_offset=line_idx,
                        last_capture_id=cap.capture_id,
                        updated_at=datetime.now(UTC),
                    )
                )

                if len(batch) >= limit:
                    try:
                        handle_batch(batch)
                        processed += len(batch)
                    except BatchPartialFailure as bpf:
                        processed += bpf.processed
                        failed += bpf.failed
                        skipped += bpf.skipped
                    except Exception as exc:
                        logger.exception("batch_handler_error", err=str(exc))
                        failed += len(batch)
                        skipped += len(batch)

                    offset = offsets_in_batch[-1]
                    batch = []
                    offsets_in_batch = []

                    if (time.monotonic() - last_persist) >= self.offset_persist_seconds:
                        self.storage.save_offset(offset)
                        last_persist = time.monotonic()

                    if max_records and processed >= max_records:
                        self.storage.save_offset(offset)
                        self.storage.checkpoint()
                        return {"processed": processed, "skipped": skipped, "failed": failed}

            if batch:
                if max_records is not None:
                    remaining = max_records - processed
                    if remaining > 0:
                        allowed_batch = batch[:remaining]
                        try:
                            handle_batch(allowed_batch)
                            processed += len(allowed_batch)
                        except BatchPartialFailure as bpf:
                            processed += bpf.processed
                            failed += bpf.failed
                            skipped += bpf.skipped
                        except Exception as exc:
                            logger.exception("batch_handler_error", err=str(exc))
                            failed += len(allowed_batch)
                            skipped += len(allowed_batch)
                        offset = offsets_in_batch[len(allowed_batch) - 1]
                else:
                    try:
                        handle_batch(batch)
                        processed += len(batch)
                    except BatchPartialFailure as bpf:
                        processed += bpf.processed
                        failed += bpf.failed
                        skipped += bpf.skipped
                    except Exception as exc:
                        logger.exception("batch_handler_error", err=str(exc))
                        failed += len(batch)
                        skipped += len(batch)
                    offset = offsets_in_batch[-1]

            # End of file — persist final offset.
            self.storage.save_offset(offset)
            self.storage.checkpoint()
        return {"processed": processed, "skipped": skipped, "failed": failed}

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
