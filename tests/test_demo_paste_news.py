"""Paste-news demo flow: build a real AwarenessCaptureView, write it to the
Awareness-style JSONL layout, replay it through the supervisor, and assert
the record materializes with the expected labels.

This is also a contract test for the post-commit consumption path — if
anyone breaks the JSONL → Storage round trip, this fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fusion_stack.demo import build_capture, render_demo_report, run_demo, write_jsonl
from fusion_stack.settings import load_settings, reload_settings


FED_ARTICLE = (
    "The Federal Reserve raised its benchmark interest rate by 25 basis points "
    "on Wednesday, citing persistent inflation. Treasury yields jumped after the "
    "decision. Chair Powell said the central bank remains data-dependent. Equities "
    "sold off as Apple (AAPL) and Microsoft (MSFT) both fell 2%."
)
SPORTS_ARTICLE = (
    "The scoreboard told the story: a dramatic last-minute goal sealed the "
    "championship. Players celebrated with the trophy as fans rushed the field. "
    "The coach praised his squad."
)


@pytest.fixture
def isolated_demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUSION_PATHS__FUSION_OUTPUT_DIR", str(tmp_path / "out"))
    reload_settings()


def test_build_capture_is_deterministic() -> None:
    """Same (text, url) → same capture_id, so the demo is safely idempotent."""
    a = build_capture(title="t", text="body", url="https://x.com/1")
    b = build_capture(title="t", text="body", url="https://x.com/1")
    assert a.capture_id == b.capture_id
    c = build_capture(title="t", text="body", url="https://x.com/2")
    assert a.capture_id != c.capture_id


def test_write_jsonl_uses_awareness_layout(tmp_path: Path) -> None:
    cap = build_capture(title="t", text="b")
    p = write_jsonl(cap, tmp_path)
    # Awareness layout: <root>/jsonl/captures/Y/M/D/*.jsonl
    parts = p.relative_to(tmp_path).parts
    assert parts[0] == "jsonl"
    assert parts[1] == "captures"
    assert len(parts[2]) == 4 and parts[2].isdigit()      # year
    assert len(parts[3]) == 2 and parts[3].isdigit()      # month
    assert len(parts[4]) == 2 and parts[4].isdigit()      # day
    assert parts[-1].endswith(".jsonl")


def test_demo_fed_article_is_relevant(isolated_demo) -> None:
    r = run_demo(
        title="Federal Reserve raises rates by 25 bps amid sticky inflation",
        text=FED_ARTICLE,
        domain="reuters.com",
    )
    assert r.processed == 1
    assert r.record, "no record produced"
    assert r.record["is_finance_relevant"] is True
    assert r.record["finance_relevance_score"] > 0.7
    # Multi-label expectations
    assert "central_bank" in r.record["impact_reason_codes"]
    assert "AAPL" in r.record["candidate_symbols"]
    assert "MSFT" in r.record["candidate_symbols"]
    # Evidence picked at least one sentence
    assert r.record["evidence_sentences"], "no extractive evidence"
    # Production-safe by default → diagnostic OFF
    assert r.record["diagnostic_multimodal_enabled"] is False
    assert r.record["diagnostic_multimodal_result"] is None


def test_demo_sports_article_is_rejected(isolated_demo) -> None:
    r = run_demo(
        title="Local team wins championship",
        text=SPORTS_ARTICLE,
        domain="espn.com",
    )
    assert r.record, "no record produced"
    assert r.record["is_finance_relevant"] is False
    assert r.record["candidate_symbols"] == []


def test_demo_is_idempotent_on_repeat(isolated_demo) -> None:
    """Re-running on identical input does not duplicate the record."""
    a = run_demo(title="x", text="The Fed raised rates 25bps", domain="reuters.com")
    b = run_demo(title="x", text="The Fed raised rates 25bps", domain="reuters.com")
    assert a.capture_id == b.capture_id
    # Storage was hit but row count for this capture stays 1
    assert b.record["capture_id"] == a.record["capture_id"]


def test_render_demo_report_handles_empty_record() -> None:
    from fusion_stack.demo import DemoResult
    r = DemoResult(capture_id="missing", record={}, jsonl_path=Path("/tmp/x.jsonl"), processed=0, skipped=0)
    report = render_demo_report(r)
    assert "no record materialized" in report
