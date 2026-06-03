"""Dedicated unit coverage for ``catchem.demo``.

Focuses on the two public entry points the CLI relies on:

  * ``render_demo_report`` — the populated-record formatting path (every
    line of the human-readable summary), plus the score/evidence/asset-class
    fallback branches that fire when fields are missing or non-numeric.
  * ``run_demo`` — the end-to-end paste→JSONL→replay→record flow against a
    synthetic article, driven entirely through ``tmp_path`` so it never
    touches the real Awareness data dir and never hits the network.

These complement ``test_demo_paste_news.py`` (which is the post-commit
contract test); here we exercise the rendering branches directly so the
formatting can't silently regress.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from catchem.demo import (
    DemoResult,
    build_capture,
    render_demo_report,
    run_demo,
)
from catchem.settings import reload_settings

FED_ARTICLE = (
    "The Federal Reserve raised its benchmark interest rate by 25 basis points "
    "on Wednesday, citing persistent inflation. Treasury yields jumped after the "
    "decision. Chair Powell said the central bank remains data-dependent. Equities "
    "sold off as Apple (AAPL) and Microsoft (MSFT) both fell 2%."
)


@pytest.fixture
def isolated_demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point catchem output at tmp_path so run_demo writes nowhere real."""
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "out"))
    reload_settings()


def _full_record() -> dict:
    """A fully-populated record dict shaped like Storage.get_record output."""
    return {
        "capture_id": "demo-abc",
        "title": "Fed raises rates",
        "domain": "reuters.com",
        "is_finance_relevant": True,
        "finance_relevance_score": 0.876,
        "asset_classes": ["rates", "equity"],
        "impact_reason_codes": ["central_bank", "earnings"],
        "candidate_symbols": ["AAPL", "MSFT"],
        "sentiment_label": "negative",
        "sentiment_score": -0.42,
        "evidence_sentences": ["Treasury yields jumped after the decision."],
        "diagnostic_multimodal_enabled": False,
        "processing_mode": "production_safe",
    }


def test_render_demo_report_populated_record_has_all_lines() -> None:
    """The happy path renders every labeled field (covers demo.py 173-194)."""
    result = DemoResult(
        capture_id="demo-abc",
        record=_full_record(),
        jsonl_path=Path("/tmp/demo-abc.jsonl"),
        processed=1,
        skipped=0,
    )
    report = render_demo_report(result)

    assert "demo capture: demo-abc" in report
    assert "/tmp/demo-abc.jsonl" in report
    assert "processed   1" in report
    assert "title       Fed raises rates" in report
    assert "domain      reuters.com" in report
    assert "relevant    True" in report
    # numeric score formatted to 3 decimals
    assert "score       0.876" in report
    # asset classes + reasons + symbols joined with ", "
    assert "asset_cls   rates, equity" in report
    assert "reasons     central_bank, earnings" in report
    assert "symbols     AAPL, MSFT" in report
    assert "sentiment   negative (-0.42)" in report
    # evidence truncated to the first sentence
    assert "Treasury yields jumped after the decision." in report
    assert "diag        enabled=False" in report
    assert "mode        production_safe" in report
    # UI links use the canonical capture_id
    assert "http://127.0.0.1:8087/feed/demo-abc" in report
    assert "/record/demo-abc" in report


def test_render_demo_report_non_numeric_score_falls_back_to_dash() -> None:
    """A None / non-numeric score renders the em-dash placeholder, not a crash."""
    rec = _full_record()
    rec["finance_relevance_score"] = None
    result = DemoResult(
        capture_id="demo-x",
        record=rec,
        jsonl_path=Path("/tmp/x.jsonl"),
        processed=1,
        skipped=0,
    )
    report = render_demo_report(result)
    assert "score       —" in report
    assert "0.000" not in report


def test_render_demo_report_empty_lists_render_dash() -> None:
    """Missing asset_classes / reasons / symbols / evidence collapse to —."""
    rec = _full_record()
    rec["asset_classes"] = []
    rec["impact_reason_codes"] = []
    rec["candidate_symbols"] = []
    rec["evidence_sentences"] = []
    result = DemoResult(
        capture_id="demo-y",
        record=rec,
        jsonl_path=Path("/tmp/y.jsonl"),
        processed=1,
        skipped=0,
    )
    report = render_demo_report(result)
    assert "asset_cls   —" in report
    assert "reasons     —" in report
    assert "symbols     —" in report
    assert "evidence    —" in report


def test_render_demo_report_truncates_long_evidence_to_120_chars() -> None:
    rec = _full_record()
    rec["evidence_sentences"] = ["x" * 500]
    result = DemoResult(
        capture_id="demo-z",
        record=rec,
        jsonl_path=Path("/tmp/z.jsonl"),
        processed=1,
        skipped=0,
    )
    report = render_demo_report(result)
    # find the evidence line and confirm the rendered slice is exactly 120 chars
    evidence_line = next(line for line in report.splitlines() if line.startswith("  evidence"))
    rendered = evidence_line.split("evidence", 1)[1].strip()
    assert len(rendered) == 120


def test_render_demo_report_empty_record_points_to_dlq() -> None:
    """Falsy record → the 'no record materialized' diagnostic block."""
    result = DemoResult(
        capture_id="missing",
        record={},
        jsonl_path=Path("/tmp/m.jsonl"),
        processed=0,
        skipped=1,
    )
    report = render_demo_report(result)
    assert "no record materialized" in report
    assert "check DLQ" in report
    assert "/tmp/m.jsonl" in report
    assert "skipped=1" in report


def test_run_demo_end_to_end_with_synthetic_article(isolated_demo) -> None:
    """run_demo writes JSONL under tmp output and materializes a record."""
    result = run_demo(
        title="Federal Reserve raises rates by 25 bps amid sticky inflation",
        text=FED_ARTICLE,
        domain="reuters.com",
    )
    # capture_id is the deterministic demo-<hash> shape
    assert result.capture_id.startswith("demo-")
    assert result.processed == 1
    assert result.skipped == 0
    # JSONL was written under the tmp demo-input subtree, not anywhere real
    assert result.jsonl_path.exists()
    assert "demo-input" in result.jsonl_path.parts
    assert "out" in result.jsonl_path.parts
    # record materialized and is finance-relevant
    assert result.record
    assert result.record["capture_id"] == result.capture_id
    assert result.record["is_finance_relevant"] is True
    # the populated report renders cleanly end to end
    report = render_demo_report(result)
    assert result.capture_id in report
    assert "no record materialized" not in report


def test_run_demo_default_url_is_derived_from_domain(isolated_demo) -> None:
    """When no url is supplied, build_capture synthesizes one from the domain."""
    cap = build_capture(title="t", text="The Fed raised rates 25bps", domain="reuters.com")
    assert cap.url == "https://reuters.com/demo"
    assert cap.canonical_url == "https://reuters.com/demo"


def test_run_demo_is_idempotent_for_identical_content(isolated_demo) -> None:
    """Same content → same capture_id, so storage holds exactly one record.

    Each call writes its own timestamped JSONL file and spins up a transient
    Supervisor, so the row is re-processed (``processed==1`` both times); the
    deterministic capture_id means the second pass *replaces* rather than
    duplicates the stored record.
    """
    a = run_demo(title="x", text=FED_ARTICLE, domain="reuters.com")
    b = run_demo(title="x", text=FED_ARTICLE, domain="reuters.com")
    assert a.capture_id == b.capture_id
    assert a.processed == 1
    assert b.record["capture_id"] == a.record["capture_id"]


def test_run_demo_with_custom_settings(isolated_demo) -> None:
    """run_demo accepts a custom settings object, exercising the branch where settings is not None."""
    from catchem.settings import load_settings

    custom_settings = load_settings()
    result = run_demo(
        title="Custom settings test",
        text=FED_ARTICLE,
        domain="custom.settings.local",
        settings=custom_settings,
    )
    assert result.processed == 1
    assert result.record["domain"] == "custom.settings.local"
