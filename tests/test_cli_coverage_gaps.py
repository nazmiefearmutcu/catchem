"""Coverage for ``catchem coverage-gaps`` — the terminal twin of
/api/news/coverage-gaps (the awareness BLIND-SPOT detector).

Offline by design: opens Storage directly (same pattern as ``catchem
top-recent``), so no sidecar / network is touched. Built on the autouse
``isolated_env`` fixture in conftest.py — the SQLite path lives under
``tmp_path/data/db/`` per test, and the watchlist is pinned via
``CATCHEM_NEWS__PRIORITY_TICKERS`` so the covered/gap split is deterministic
regardless of the mega-cap fallback.

Seeds two records (an AAPL headline + an MSFT headline) and asserts:
  * a mentioned term (AAPL) lands in ``covered`` with a mention count,
  * an unmentioned term (TSLA) lands in ``gaps``,
  * ``--json`` emits a parseable envelope with the stable shape.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from catchem.cli import app
from catchem.schemas import (
    FinancialImpactRecord,
    ProcessingMode,
    SentimentLabel,
)
from catchem.settings import load_settings, reload_settings
from catchem.storage import load_storage_from_settings

runner = CliRunner()


def _make_record(
    capture_id: str,
    title: str,
    domain: str,
    *,
    symbols: tuple[str, ...] = ("AAPL",),
    score: float = 0.85,
) -> FinancialImpactRecord:
    return FinancialImpactRecord(
        capture_id=capture_id,
        doc_id=f"doc-{capture_id}",
        title=title,
        text_excerpt=f"Body of {title}.",
        domain=domain,
        url=f"https://{domain}/{capture_id}",
        language="en",
        is_finance_relevant=True,
        finance_relevance_score=score,
        asset_classes=["equities"],
        impact_reason_codes=["earnings"],
        candidate_symbols=list(symbols),
        candidate_entities=[],
        impact_horizons=["short_term"],
        sentiment_label=SentimentLabel.POSITIVE,
        sentiment_score=0.7,
        evidence_sentences=[f"Sentence about {title}."],
        reason_text="matched on earnings",
        component_scores={"finance_relevance_score": score},
        diagnostic_multimodal_enabled=False,
        diagnostic_multimodal_result=None,
        processing_mode=ProcessingMode.PRODUCTION_SAFE,
        model_versions={"stub": "v0"},
    )


@pytest.fixture
def seeded_storage(monkeypatch: pytest.MonkeyPatch):
    """Seed two records + pin the watchlist to AAPL/MSFT/TSLA.

    ``created_at`` is stamped at insert time (now), so both rows fall well
    inside the default 24h window. The pinned watchlist makes the covered/gap
    split deterministic: AAPL + MSFT are mentioned, TSLA never is.
    """
    monkeypatch.setenv("CATCHEM_NEWS__PRIORITY_TICKERS", '["AAPL", "MSFT", "TSLA"]')
    reload_settings()

    settings = load_settings()
    storage = load_storage_from_settings(settings)
    storage.insert_record(
        _make_record("c-aapl", "Apple beats earnings expectations", "wsj.com", symbols=("AAPL",))
    )
    storage.insert_record(
        _make_record("c-msft", "Microsoft cloud growth accelerates", "reuters.com", symbols=("MSFT",))
    )
    storage.close()
    return settings


def test_coverage_gaps_text_splits_covered_and_gap(seeded_storage) -> None:
    """Mentioned term (AAPL) shows covered; unmentioned (TSLA) shows as a gap."""
    result = runner.invoke(app, ["coverage-gaps"])
    assert result.exit_code == 0, result.output
    assert "catchem coverage-gaps" in result.output

    out = result.output
    gaps_section = out[out.index("gaps ("):out.index("covered (")]
    covered_section = out[out.index("covered ("):]

    # TSLA was never mentioned → must appear in the gaps block.
    assert "TSLA" in gaps_section
    # AAPL was mentioned → must appear in the covered block, not the gaps block.
    assert "AAPL" in covered_section
    assert "AAPL" not in gaps_section
    assert "mention(s)" in covered_section


def test_coverage_gaps_json_envelope(seeded_storage) -> None:
    """--json emits a parseable envelope mirroring the HTTP twin's shape."""
    result = runner.invoke(app, ["coverage-gaps", "--json"])
    assert result.exit_code == 0, result.output
    body = result.output.strip().split("\n")[-1]
    payload = json.loads(body)

    for key in ("schema_version", "window_hours", "limit", "watch_terms",
                "generated_at", "window_seconds", "covered", "gaps"):
        assert key in payload, f"missing {key} in JSON payload"

    assert payload["watch_terms"] == ["AAPL", "MSFT", "TSLA"]
    assert payload["window_seconds"] == pytest.approx(24.0 * 3600.0)

    covered_terms = {c["term"] for c in payload["covered"]}
    assert "AAPL" in covered_terms
    assert "MSFT" in covered_terms
    assert "TSLA" in payload["gaps"]

    aapl = next(c for c in payload["covered"] if c["term"] == "AAPL")
    assert aapl["mention_count"] >= 1
    assert aapl["last_seen_age_seconds"] is not None


def test_coverage_gaps_window_hours_threads_through(seeded_storage) -> None:
    """--window-hours is honoured end-to-end (hours → seconds conversion).

    A 0.1h flag must surface as 360s in ``window_seconds`` (not the 24h
    default), proving the hours→seconds math is wired into find_coverage_gaps.
    The covered set can never exceed the watchlist size regardless of window.
    """
    result = runner.invoke(app, ["coverage-gaps", "--window-hours", "0.1", "--json"])
    assert result.exit_code == 0, result.output
    body = result.output.strip().split("\n")[-1]
    payload = json.loads(body)
    assert payload["window_seconds"] == pytest.approx(0.1 * 3600.0)
    assert payload["window_hours"] == pytest.approx(0.1)
    assert len(payload["covered"]) <= len(payload["watch_terms"])
