"""Read Awareness output post-commit. Two modes:

  * ``iter_finalized_files``: glob-based scan of committed JSONL files (skips ``.tmp``).
  * ``iter_captures_with_offsets``: per-file resumable iterator that respects a
    persisted line offset.

We deliberately do NOT mutate any Awareness state. We only read finalized
artifacts. ``.jsonl.tmp`` files are skipped because they represent in-flight
chunks the JSONL writer hasn't atomically renamed yet.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path

from .logging import get_logger
from .schemas import AwarenessCaptureView

logger = get_logger("catchem.awareness_reader")


def iter_finalized_files(root: Path, pattern: str = "**/*.jsonl") -> list[Path]:
    """List committed JSONL files under ``root``. Excludes .tmp.

    Sorted by modified time (oldest first) so replay is deterministic in tests.
    """
    if not root.exists():
        return []
    files = [p for p in root.glob(pattern) if not p.name.endswith(".tmp")]
    files.sort(key=lambda p: (p.stat().st_mtime, str(p)))
    return files


def parse_capture_line(line: str) -> AwarenessCaptureView | None:
    line = line.strip()
    if not line:
        return None
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        logger.warning("jsonl_decode_error", line_excerpt=line[:200])
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return AwarenessCaptureView.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError
        logger.warning("capture_validate_failed", err=str(exc), excerpt=line[:200])
        return None


def iter_captures(path: Path, start_offset: int = 0) -> Generator[tuple[int, AwarenessCaptureView], None, None]:
    """Yield ``(new_line_offset, capture)`` for each captured row starting at ``start_offset``.

    ``new_line_offset`` is the count of lines fully consumed including the current
    one, i.e. it is the offset to persist if the consumer commits this capture.
    """
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh, start=1):
            if i <= start_offset:
                continue
            cap = parse_capture_line(line)
            if cap is None:
                continue
            yield i, cap


def replay_directory(
    root: Path,
    pattern: str = "**/*.jsonl",
    limit: int | None = None,
) -> Generator[AwarenessCaptureView, None, None]:
    """Iterate captures across a directory, ignoring offsets. Useful for tests and
    bulk replays. The supervisor uses ``iter_captures`` with persisted offsets
    instead."""
    count = 0
    for path in iter_finalized_files(root, pattern):
        for _, cap in iter_captures(path):
            yield cap
            count += 1
            if limit and count >= limit:
                return


def discover_awareness_jsonl_root(awareness_data_dir: Path) -> Path:
    """Resolve <awareness>/data/jsonl/captures from a generic data dir."""
    candidate = awareness_data_dir / "jsonl"
    if candidate.exists():
        return candidate
    return awareness_data_dir
