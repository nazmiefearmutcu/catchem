"""Verify catchem consumes Awareness only AFTER durable JSONL commit.

We achieve this by checking three things:
  1. iter_finalized_files skips .tmp files (in-flight chunks).
  2. ReplayRunner advances the offset only for captures that succeed.
  3. catchem never imports awareness internals at module load time
     (so it cannot accidentally touch the worker engine).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from catchem.awareness_reader import iter_finalized_files
from catchem.awareness_replay import ReplayRunner
from catchem.schemas import AwarenessCaptureView
from catchem.settings import load_settings
from catchem.storage import Storage


@pytest.mark.regression
def test_skip_tmp_files(tmp_path: Path) -> None:
    root = tmp_path / "jsonl"
    root.mkdir()
    (root / "ok.jsonl").write_text('{"capture_id":"a","doc_id":"b","text":"x"}\n', encoding="utf-8")
    (root / "in-flight.jsonl.tmp").write_text("garbage", encoding="utf-8")
    files = iter_finalized_files(root)
    assert [p.name for p in files] == ["ok.jsonl"]


@pytest.mark.regression
def test_replay_offset_advances_only_on_success(tmp_path: Path, write_jsonl, synth_capture) -> None:
    cap1 = synth_capture(capture_id="c1", doc_id="d1")
    cap2 = synth_capture(capture_id="c2", doc_id="d2")
    rows = [json.loads(cap1.model_dump_json()), json.loads(cap2.model_dump_json())]
    path = write_jsonl(rows)

    with Storage(
        db_path=tmp_path / "catchem.sqlite3",
        parquet_dir=tmp_path / "parquet",
        dlq_dir=tmp_path / "dlq",
    ) as db:
        processed: list[str] = []

        def handle(cap: AwarenessCaptureView) -> None:
            processed.append(cap.capture_id)

        runner = ReplayRunner(root=path.parent, storage=db)
        counts = runner.run_once(handle)
        assert counts["processed"] == 2
        assert processed == ["c1", "c2"]

        # Re-run: should resume from offset and find zero new rows.
        counts2 = runner.run_once(handle)
        assert counts2["processed"] == 0


@pytest.mark.regression
def test_catchem_does_not_import_awareness_internals() -> None:
    """If awareness is not installed, catchem must still import cleanly."""
    # The package was already imported by the test runner; check that no awareness
    # submodule is loaded.
    awareness_internals = [k for k in sys.modules if k.startswith("awareness.workers") or k.startswith("awareness.dedup")]
    assert awareness_internals == [], f"catchem accidentally imported awareness internals: {awareness_internals}"


@pytest.mark.regression
def test_settings_default_mode_is_production_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CATCHEM_MODE", raising=False)
    # Pull the YAML default which is production_safe.
    from catchem.settings import reload_settings
    reload_settings()
    s = load_settings()
    # Either YAML default 'production_safe' or explicit env should win — env is unset here.
    assert s.mode.value == "production_safe"
