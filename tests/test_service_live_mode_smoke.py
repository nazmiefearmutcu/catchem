"""Live-tail smoke: simulate Awareness committing new JSONL while we tail."""

from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Event, Thread

import pytest

from catchem.settings import load_settings, reload_settings
from catchem.supervisor import Supervisor


@pytest.mark.smoke
def test_tail_picks_up_new_files(tmp_path: Path, synth_capture, monkeypatch: pytest.MonkeyPatch) -> None:
    aware_dir = tmp_path / "aw"
    captures_dir = aware_dir / "jsonl" / "captures" / "2026" / "05" / "16"
    captures_dir.mkdir(parents=True)
    monkeypatch.setenv("CATCHEM_PATHS__AWARENESS_DATA_DIR", str(aware_dir))
    monkeypatch.setenv("CATCHEM_MODE", "live_tail")
    monkeypatch.setenv("CATCHEM_LIVE__POLL_SECONDS", "0.1")
    monkeypatch.setenv("CATCHEM_LIVE__TAIL_MAX_PER_TICK", "10")
    reload_settings()

    # Stage the first committed file BEFORE the tail starts. This guarantees the
    # first run_once iteration sees it and we don't depend on poll_seconds env
    # override propagating into the LiveConfig sub-model.
    cap = synth_capture(capture_id="t1", doc_id="d1")
    f = captures_dir / "captures-1.jsonl"
    f.write_text(json.dumps(cap.model_dump(mode="json"), default=str) + "\n", encoding="utf-8")

    sup = Supervisor(load_settings())

    stop_evt = Event()

    def runner() -> None:
        sup.run_tail(stop=stop_evt.is_set)

    t = Thread(target=runner, daemon=True)
    t.start()

    # Drop a second file shortly after start to exercise the polling path too.
    time.sleep(0.3)
    cap2 = synth_capture(capture_id="t2", doc_id="d2", title="$NVDA beats Q4 earnings")
    f2 = captures_dir / "captures-2.jsonl"
    f2.write_text(json.dumps(cap2.model_dump(mode="json"), default=str) + "\n", encoding="utf-8")

    deadline = time.time() + 30
    rec = None
    while time.time() < deadline:
        rec = sup.storage.get_record("t1")
        if rec is not None:
            break
        time.sleep(0.2)
    stop_evt.set()
    t.join(timeout=5.0)
    sup.close()

    assert rec is not None, "tail did not pick up newly committed file"
    assert rec["doc_id"] == "d1"
