"""Integration: end-to-end replay against synthetic JSONL."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from catchem.schemas import AwarenessCaptureView
from catchem.settings import load_settings
from catchem.supervisor import Supervisor


@pytest.mark.integration
def test_replay_produces_records(tmp_path: Path, write_jsonl, synth_capture, monkeypatch: pytest.MonkeyPatch) -> None:
    cap_fed = synth_capture(capture_id="c-fed", doc_id="d-fed",
                            title="Federal Reserve raises interest rates",
                            text="The Fed hiked rates 25bps citing sticky inflation.")
    cap_aapl = synth_capture(capture_id="c-aapl", doc_id="d-aapl", domain="reuters.com",
                             title="Apple beats earnings, raises guidance",
                             text="Apple Inc beat consensus EPS and raised full-year guidance. $AAPL rose 4%.")
    cap_sports = synth_capture(capture_id="c-sport", doc_id="d-sport", domain="espn.com",
                                title="Local team wins championship",
                                text="Scoreboard tells the story; last-minute goal seals the trophy.")
    rows = [json.loads(c.model_dump_json()) for c in (cap_fed, cap_aapl, cap_sports)]
    # write_jsonl puts the file at tmp_path/jsonl/captures/2026/05/16/captures.jsonl
    write_jsonl(rows)
    # Awareness data dir is tmp_path; discover_awareness_jsonl_root walks tmp_path/jsonl.
    monkeypatch.setenv("CATCHEM_PATHS__AWARENESS_DATA_DIR", str(tmp_path))

    from catchem.settings import reload_settings
    reload_settings()
    s = load_settings()
    sup = Supervisor(s)
    try:
        counts = sup.run_replay()
        assert counts["processed"] == 3, counts

        # Fed news → relevant; sports → not relevant
        fed = sup.storage.get_record("c-fed")
        assert fed is not None and fed["is_finance_relevant"] is True
        assert "rates" in fed["asset_classes"] or "macro" in fed["asset_classes"]
        assert "central_bank" in fed["impact_reason_codes"]
        assert fed["evidence_sentences"], "missing evidence"
        assert fed["component_scores"]["asset_class_max"] > 0
        assert fed["model_versions"]["zero_shot"].startswith("stub-")

        aapl = sup.storage.get_record("c-aapl")
        assert aapl is not None and aapl["is_finance_relevant"] is True
        assert "AAPL" in aapl["candidate_symbols"]

        sports = sup.storage.get_record("c-sport")
        assert sports is not None
        assert sports["is_finance_relevant"] is False
    finally:
        sup.close()


@pytest.mark.integration
def test_replay_idempotent(tmp_path: Path, write_jsonl, synth_capture, monkeypatch: pytest.MonkeyPatch) -> None:
    cap = synth_capture()
    write_jsonl([json.loads(cap.model_dump_json())])
    monkeypatch.setenv("CATCHEM_PATHS__AWARENESS_DATA_DIR", str(tmp_path))
    from catchem.settings import reload_settings
    reload_settings()
    sup = Supervisor(load_settings())
    try:
        c1 = sup.run_replay()
        c2 = sup.run_replay()
        assert c1["processed"] == 1
        assert c2["processed"] == 0
    finally:
        sup.close()


@pytest.mark.integration
def test_replay_reports_storage_truth_fields(
    tmp_path: Path, write_jsonl, synth_capture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = synth_capture(capture_id="c-truth", doc_id="d-truth")
    write_jsonl([json.loads(cap.model_dump_json())])
    monkeypatch.setenv("CATCHEM_PATHS__AWARENESS_DATA_DIR", str(tmp_path))
    from catchem.settings import reload_settings
    reload_settings()
    sup = Supervisor(load_settings())
    try:
        c1 = sup.run_replay()
        assert c1["processed"] == 1
        assert c1["failed"] == 0
        assert c1["inserted"] == 1
        assert c1["replaced"] == 0
        assert c1["net_new_records"] == 1
        assert c1["records_before"]["total"] == 0
        assert c1["records_after"]["total"] == 1
        assert c1["dlq_delta"] == 0

        c2 = sup.run_replay()
        assert c2["processed"] == 0
        assert c2["failed"] == 0
        assert c2["inserted"] == 0
        assert c2["replaced"] == 0
        assert c2["net_new_records"] == 0
    finally:
        sup.close()


@pytest.mark.integration
def test_replay_reports_handler_failures_as_failed_and_dlq(
    tmp_path: Path, write_jsonl, synth_capture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = synth_capture(capture_id="c-fail", doc_id="d-fail")
    write_jsonl([json.loads(cap.model_dump_json())])
    monkeypatch.setenv("CATCHEM_PATHS__AWARENESS_DATA_DIR", str(tmp_path))
    from catchem.settings import reload_settings
    reload_settings()
    sup = Supervisor(load_settings())
    try:
        def boom(_cap: AwarenessCaptureView):
            raise RuntimeError("synthetic process failure")

        monkeypatch.setattr(sup.service, "process", boom)
        counts = sup.run_replay()
        assert counts["processed"] == 0
        assert counts["skipped"] == 1
        assert counts["failed"] == 1
        assert counts["dlq_delta"] == 1
        assert counts["dlq"] == 1
        assert counts["inserted"] == 0
        assert counts["replaced"] == 0
        assert counts["net_new_records"] == 0
    finally:
        sup.close()
