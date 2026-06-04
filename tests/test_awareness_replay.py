"""Dedicated unit coverage for ``catchem.awareness_replay.ReplayRunner``.

These tests drive the replay loop directly with an in-process handler (no
Supervisor, no network) so we can pin behavior that the integration tests
don't reach:

  * JSONL ingestion semantics via the runner: valid lines processed,
    malformed / blank / wrong-type / missing-field lines skipped, empty
    file is a no-op.
  * Periodic offset persistence (``offset_persist_seconds`` elapsed mid-file).
  * ``max_records`` short-circuit in the middle of a file, persisting the
    partial offset so the next pass resumes.
  * Handler exceptions counted as failed+skipped and routed to the DLQ.
  * ``tail`` honoring a cooperative ``stop`` callback.

Fixtures are tmp_path-based JSONL files; ``synth_capture`` /
``write_jsonl`` come from conftest.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from catchem.awareness_replay import ReplayRunner
from catchem.schemas import AwarenessCaptureView
from catchem.storage import Storage


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(
        db_path=tmp_path / "catchem.sqlite3",
        parquet_dir=tmp_path / "parquet",
        dlq_dir=tmp_path / "dlq",
    )


def _rows(synth_capture, *ids: str) -> list[dict]:
    return [json.loads(synth_capture(capture_id=i, doc_id=f"d-{i}").model_dump_json()) for i in ids]


def test_run_once_processes_valid_lines(tmp_path, write_jsonl, synth_capture, storage) -> None:
    path = write_jsonl(_rows(synth_capture, "a", "b", "c"))
    seen: list[str] = []
    runner = ReplayRunner(root=path.parent, storage=storage)
    counts = runner.run_once(lambda c: seen.append(c.capture_id))
    assert counts == {"processed": 3, "skipped": 0, "failed": 0}
    assert seen == ["a", "b", "c"]


def test_run_once_empty_file_is_noop(tmp_path, write_jsonl, storage) -> None:
    path = write_jsonl([])  # writes a 0-line file
    assert path.exists()
    runner = ReplayRunner(root=path.parent, storage=storage)
    counts = runner.run_once(lambda c: None)
    assert counts == {"processed": 0, "skipped": 0, "failed": 0}


def test_run_once_skips_malformed_blank_and_wrong_type_lines(
    tmp_path, write_jsonl, synth_capture, storage
) -> None:
    """Only the two well-formed captures survive; the junk is silently dropped."""
    good = _rows(synth_capture, "g1", "g2")
    path = write_jsonl([good[0]])
    # Append a mix of garbage that parse_capture_line must reject without raising.
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n")                                  # blank line
        fh.write("   \n")                               # whitespace-only
        fh.write("{not valid json\n")                   # JSONDecodeError
        fh.write(json.dumps([1, 2, 3]) + "\n")          # valid JSON, not a dict
        fh.write(json.dumps({"capture_id": "x"}) + "\n")  # dict, missing required fields
        fh.write(json.dumps(good[1], default=str) + "\n")  # second good capture

    seen: list[str] = []
    runner = ReplayRunner(root=path.parent, storage=storage)
    counts = runner.run_once(lambda c: seen.append(c.capture_id))
    assert seen == ["g1", "g2"]
    assert counts == {"processed": 2, "skipped": 0, "failed": 0}


def test_run_once_periodic_offset_persist_fires_mid_file(
    tmp_path, write_jsonl, synth_capture, storage
) -> None:
    """offset_persist_seconds=0 forces a save on every row (covers the periodic
    persist branch). The persisted offset must reach the final line."""
    path = write_jsonl(_rows(synth_capture, "p1", "p2", "p3"))
    runner = ReplayRunner(root=path.parent, storage=storage, offset_persist_seconds=0.0)
    counts = runner.run_once(lambda c: None)
    assert counts["processed"] == 3
    off = storage.get_offset(str(path))
    assert off.line_offset == 3
    assert off.last_capture_id == "p3"


def test_run_once_max_records_short_circuits_and_persists_partial_offset(
    tmp_path, write_jsonl, synth_capture, storage
) -> None:
    """max_records stops mid-file; the partial offset lets the next pass resume."""
    path = write_jsonl(_rows(synth_capture, "m1", "m2", "m3", "m4"))
    runner = ReplayRunner(root=path.parent, storage=storage)

    first = runner.run_once(lambda c: None, max_records=2)
    assert first["processed"] == 2
    off = storage.get_offset(str(path))
    assert off.line_offset == 2  # stopped after the 2nd line
    assert off.last_capture_id == "m2"

    # Resume: the remaining two rows get processed, nothing re-processed.
    seen: list[str] = []
    second = runner.run_once(lambda c: seen.append(c.capture_id))
    assert second["processed"] == 2
    assert seen == ["m3", "m4"]


def test_run_once_handler_failure_counts_failed_and_writes_dlq(
    tmp_path, write_jsonl, synth_capture, storage
) -> None:
    path = write_jsonl(_rows(synth_capture, "boom"))
    dlq_before = storage.dlq_count()

    def boom(_cap: AwarenessCaptureView) -> None:
        raise RuntimeError("synthetic handler failure")

    runner = ReplayRunner(root=path.parent, storage=storage)
    counts = runner.run_once(boom)
    assert counts["processed"] == 0
    assert counts["failed"] == 1
    assert counts["skipped"] == 1
    # the failure was recorded to the dead-letter queue
    assert storage.dlq_count() == dlq_before + 1
    # the offset still advances past the failed row so we don't loop on it
    off = storage.get_offset(str(path))
    assert off.line_offset == 1


def test_files_lists_finalized_jsonl_only(tmp_path, write_jsonl, synth_capture, storage) -> None:
    path = write_jsonl(_rows(synth_capture, "f1"))
    (path.parent / "in-flight.jsonl.tmp").write_text("garbage", encoding="utf-8")
    runner = ReplayRunner(root=path.parent, storage=storage)
    listed = runner.files()
    assert path in listed
    assert all(not p.name.endswith(".tmp") for p in listed)


def test_tail_stops_immediately_when_stop_returns_true(tmp_path, storage) -> None:
    """tail returns at once if the cooperative stop callback is already true,
    without ever invoking the handle or sleeping."""
    runner = ReplayRunner(root=tmp_path / "jsonl", storage=storage)
    calls: list[int] = []

    def handle(_cap: AwarenessCaptureView) -> None:  # pragma: no cover - must not run
        calls.append(1)

    runner.tail(handle, poll_seconds=999.0, stop=lambda: True)
    assert calls == []


def test_tail_processes_one_tick_then_stops(tmp_path, write_jsonl, synth_capture, storage) -> None:
    """tail runs run_once until stop flips. We flip stop after the first tick so
    the loop body (run_once + the post-tick stop check) is exercised without an
    actual sleep."""
    path = write_jsonl(_rows(synth_capture, "t1", "t2"))
    seen: list[str] = []
    ticks = {"n": 0}

    def stop() -> bool:
        # False on entry to the first iteration, True on the next check.
        should = ticks["n"] >= 1
        ticks["n"] += 1
        return should

    runner = ReplayRunner(root=path.parent, storage=storage)
    runner.tail(lambda c: seen.append(c.capture_id), poll_seconds=999.0, max_per_tick=50, stop=stop)
    assert seen == ["t1", "t2"]
