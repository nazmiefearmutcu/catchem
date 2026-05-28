"""Coverage for the tag CRUD CLI subcommands added under task #186 (v56).

Mirrors the /api/records/{id}/tags + /api/tags endpoints, but operates
directly on Storage (no sidecar needed). All cases run through
``typer.testing.CliRunner`` so option parsing + exit codes are exercised
the same way real shell invocations are.

The ``isolated_env`` autouse fixture in conftest.py points the SQLite
path at ``tmp_path/data/db/`` per test, so seed and assertion live in
their own DB.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from catchem.cli import app
from catchem.schemas import (
    FinancialImpactRecord,
    ProcessingMode,
    SentimentLabel,
)
from catchem.settings import load_settings
from catchem.storage import load_storage_from_settings

runner = CliRunner()


def _make_record(
    capture_id: str,
    title: str,
    domain: str = "wsj.com",
    *,
    score: float = 0.75,
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
        candidate_symbols=["AAPL"],
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
def seeded_storage(tmp_path: Path):
    """Insert a couple of records so tag-add / tag-by have targets."""
    settings = load_settings()
    storage = load_storage_from_settings(settings)
    storage.insert_record(_make_record("c-aapl", "Apple beats earnings"))
    storage.insert_record(_make_record("c-msft", "Microsoft cloud growth"))
    storage.close()
    return settings


# ── tag-add ─────────────────────────────────────────────────────────────────


def test_tag_add_inserts_then_reports_already_present(seeded_storage) -> None:
    first = runner.invoke(app, ["tag-add", "c-aapl", "watchlist"])
    assert first.exit_code == 0, first.output
    assert "+" in first.output
    assert "added" in first.output
    assert "watchlist" in first.output

    # repeat — INSERT OR IGNORE should report "already present"
    second = runner.invoke(app, ["tag-add", "c-aapl", "watchlist"])
    assert second.exit_code == 0, second.output
    assert "already present" in second.output


def test_tag_add_rejects_invalid_tag(seeded_storage) -> None:
    # Whitespace + slashes are blocked by _validate_tag (storage layer).
    result = runner.invoke(app, ["tag-add", "c-aapl", "has spaces"])
    assert result.exit_code == 1
    combined = result.output + (result.stderr or "")
    assert "invalid tag" in combined


# ── tag-remove ──────────────────────────────────────────────────────────────


def test_tag_remove_drops_then_reports_was_not_present(seeded_storage) -> None:
    runner.invoke(app, ["tag-add", "c-aapl", "alpha"])

    first = runner.invoke(app, ["tag-remove", "c-aapl", "alpha"])
    assert first.exit_code == 0, first.output
    assert "-" in first.output
    assert "removed" in first.output

    second = runner.invoke(app, ["tag-remove", "c-aapl", "alpha"])
    assert second.exit_code == 0, second.output
    assert "was not present" in second.output


# ── tag-list ────────────────────────────────────────────────────────────────


def test_tag_list_empty_then_populated(seeded_storage) -> None:
    empty = runner.invoke(app, ["tag-list", "c-aapl"])
    assert empty.exit_code == 0, empty.output
    assert "no tags" in empty.output

    runner.invoke(app, ["tag-add", "c-aapl", "watchlist"])
    runner.invoke(app, ["tag-add", "c-aapl", "earnings-2026"])

    populated = runner.invoke(app, ["tag-list", "c-aapl"])
    assert populated.exit_code == 0, populated.output
    assert "watchlist" in populated.output
    assert "earnings-2026" in populated.output


# ── tag-top ─────────────────────────────────────────────────────────────────


def test_tag_top_orders_by_count_desc(seeded_storage) -> None:
    # 'beta' on both records, 'alpha' on one
    runner.invoke(app, ["tag-add", "c-aapl", "beta"])
    runner.invoke(app, ["tag-add", "c-msft", "beta"])
    runner.invoke(app, ["tag-add", "c-aapl", "alpha"])

    result = runner.invoke(app, ["tag-top", "--limit", "10"])
    assert result.exit_code == 0, result.output
    output = result.output
    # 'beta' (count 2) must appear before 'alpha' (count 1)
    assert output.index("beta") < output.index("alpha")
    # counts surfaced
    assert "2" in output
    assert "1" in output


def test_tag_top_empty_state(seeded_storage) -> None:
    result = runner.invoke(app, ["tag-top"])
    assert result.exit_code == 0, result.output
    assert "no tags yet" in result.output


# ── tag-by ──────────────────────────────────────────────────────────────────


def test_tag_by_finds_matching_records(seeded_storage) -> None:
    runner.invoke(app, ["tag-add", "c-aapl", "watchlist"])
    runner.invoke(app, ["tag-add", "c-msft", "watchlist"])

    result = runner.invoke(app, ["tag-by", "watchlist", "--limit", "10"])
    assert result.exit_code == 0, result.output
    # Both records' capture_id prefixes should appear; both titles too.
    assert "c-aapl"[:8] in result.output
    assert "c-msft"[:8] in result.output
    assert "Apple" in result.output
    assert "Microsoft" in result.output


def test_tag_by_empty_when_no_matches(seeded_storage) -> None:
    result = runner.invoke(app, ["tag-by", "ghost-tag"])
    assert result.exit_code == 0, result.output
    assert "no records with tag" in result.output
