"""Coordinator. One place that owns Settings → Storage → Service → Replay/Tail.

Both CLI and API construct a Supervisor and call ``run_*`` / ``process_one``.
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import Any

from .awareness_reader import discover_awareness_jsonl_root
from .awareness_replay import ReplayRunner
from .embeddings import VectorIndex
from .logging import configure_logging, get_logger
from .schemas import AwarenessCaptureView, FinancialImpactRecord, ProcessingMode
from .service import FusionService, build_service
from .settings import FusionMode, Settings
from .storage import Storage, load_storage_from_settings

logger = get_logger("fusion.supervisor")


class Supervisor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        configure_logging(
            level=settings.logging_.level,
            log_file=settings.paths.fusion_output_dir / Path(settings.logging_.file).relative_to("data") if settings.logging_.file.startswith("data/") else None,
            json_mode=settings.logging_.json_logs,
        )
        self.storage = load_storage_from_settings(settings)
        vector_dir = settings.paths.fusion_output_dir / Path(settings.storage.vector_index_dir).relative_to("data") if settings.storage.vector_index_dir.startswith("data/") else settings.paths.fusion_output_dir / "vector_index"
        self.vector_index = VectorIndex(vector_dir)
        self.service: FusionService = build_service(settings, vector_index=self.vector_index)
        logger.info(
            "supervisor_initialized",
            mode=settings.mode.value,
            diagnostic_enabled=self.service.diagnostic_enabled,
            stubs=settings.models_.use_ml_stubs,
        )

    # ── lifecycle ─────────────────────────────────────────────────────────
    def close(self) -> None:
        with suppress(Exception):
            self.storage.close()

    # ── single-capture path (used by API /process-one) ───────────────────
    def process_capture(self, cap: AwarenessCaptureView) -> FinancialImpactRecord:
        rec = self.service.process(cap)
        self.storage.insert_record(rec)
        return rec

    # ── replay ───────────────────────────────────────────────────────────
    def run_replay(self, max_records: int | None = None) -> dict[str, int]:
        root = discover_awareness_jsonl_root(self.settings.paths.awareness_data_dir)
        runner = ReplayRunner(root=root, storage=self.storage, offset_persist_seconds=self.settings.replay.offset_persist_seconds)

        def handle(cap: AwarenessCaptureView) -> None:
            try:
                self.process_capture(cap)
            except Exception as exc:
                self.storage.record_failure(cap.capture_id, str(exc), (cap.text or "")[:2000])

        counts = runner.run_once(handle, max_records=max_records)
        self.storage.flush()
        return counts

    # ── live tail ────────────────────────────────────────────────────────
    def run_tail(self, stop: Any = None) -> None:
        root = discover_awareness_jsonl_root(self.settings.paths.awareness_data_dir)
        runner = ReplayRunner(root=root, storage=self.storage, offset_persist_seconds=self.settings.replay.offset_persist_seconds)

        def handle(cap: AwarenessCaptureView) -> None:
            try:
                self.process_capture(cap)
            except Exception as exc:
                self.storage.record_failure(cap.capture_id, str(exc), (cap.text or "")[:2000])

        runner.tail(
            handle,
            poll_seconds=self.settings.live.poll_seconds,
            max_per_tick=self.settings.live.tail_max_per_tick,
            stop=stop,
        )

    # ── status / metrics ─────────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        counts = self.storage.count_records()
        return {
            "mode": self.settings.mode.value,
            "diagnostic_enabled": self.service.diagnostic_enabled,
            "use_ml_stubs": self.settings.models_.use_ml_stubs,
            "records": counts,
            "dlq": self.storage.dlq_count(),
            "model_versions": dict(self.service.model_versions),
        }
