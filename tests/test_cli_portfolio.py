"""Coverage for the ``catchem portfolio`` command group — the offline twin of
/api/portfolio + /api/portfolio/enriched (the READ-ONLY holdings tracker).

Offline by design: every verb opens Storage directly (same pattern as
``catchem top-recent`` / ``catchem coverage-gaps``), so no sidecar / network
is touched. Built on the autouse ``isolated_env`` fixture in conftest.py — the
SQLite path lives under ``tmp_path/data/db/`` per test, so a fresh DB starts
with zero holdings and zero records.

Asserts the full CRUD round-trip:
  * ``add`` creates a holding and ``list`` then shows it,
  * ``remove`` deletes it (and a bad id → exit 1),
  * ``--json`` envelopes parse with the stable shape,
  * ``show`` / ``list --enriched`` run without a sidecar — records may be
    empty, so enrichment yields each holding with empty news + None quote,
    still exit 0.
"""

from __future__ import annotations

import json

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


def _seed_record(symbol: str = "AAPL", *, title: str = "Apple beats earnings") -> None:
    """Insert one in-window record mentioning ``symbol`` so enrichment has a hit."""
    settings = load_settings()
    storage = load_storage_from_settings(settings)
    storage.insert_record(
        FinancialImpactRecord(
            capture_id=f"c-{symbol.lower()}",
            doc_id=f"doc-{symbol.lower()}",
            title=title,
            text_excerpt=f"Body about {symbol}.",
            domain="wsj.com",
            url=f"https://wsj.com/{symbol.lower()}",
            language="en",
            is_finance_relevant=True,
            finance_relevance_score=0.9,
            asset_classes=["equities"],
            impact_reason_codes=["earnings"],
            candidate_symbols=[symbol],
            candidate_entities=[],
            impact_horizons=["short_term"],
            sentiment_label=SentimentLabel.POSITIVE,
            sentiment_score=0.7,
            evidence_sentences=[f"Sentence about {symbol}."],
            reason_text="matched on earnings",
            component_scores={"finance_relevance_score": 0.9},
            diagnostic_multimodal_enabled=False,
            diagnostic_multimodal_result=None,
            processing_mode=ProcessingMode.PRODUCTION_SAFE,
            model_versions={"stub": "v0"},
        )
    )
    storage.close()


def test_add_then_list_shows_holding() -> None:
    """add creates a holding; list (explicit + default) surfaces it."""
    add = runner.invoke(
        app,
        ["portfolio", "add", "AAPL", "--shares", "10", "--label", "Apple", "--cost-basis", "150"],
    )
    assert add.exit_code == 0, add.output
    assert "AAPL" in add.output

    # Explicit `list`
    listed = runner.invoke(app, ["portfolio", "list"])
    assert listed.exit_code == 0, listed.output
    assert "AAPL" in listed.output
    assert "Apple" in listed.output

    # Default (no subcommand) == list
    default = runner.invoke(app, ["portfolio"])
    assert default.exit_code == 0, default.output
    assert "AAPL" in default.output


def test_empty_list_message() -> None:
    """A fresh DB has no holdings — list prints the empty hint, exit 0."""
    result = runner.invoke(app, ["portfolio", "list"])
    assert result.exit_code == 0, result.output
    assert "no holdings" in result.output


def test_remove_deletes_and_bad_id_exits_1() -> None:
    """remove deletes a real holding; a non-existent id → exit 1."""
    add = runner.invoke(app, ["portfolio", "add", "MSFT", "--json"])
    assert add.exit_code == 0, add.output
    holding_id = json.loads(add.output.strip().split("\n")[-1])["id"]

    # Bad id first — nothing to delete yet at id+999.
    bad = runner.invoke(app, ["portfolio", "remove", str(holding_id + 999)])
    assert bad.exit_code == 1, bad.output
    assert "no holding" in bad.output

    # Real delete succeeds and the holding disappears from list.
    ok = runner.invoke(app, ["portfolio", "remove", str(holding_id)])
    assert ok.exit_code == 0, ok.output
    assert f"holding {holding_id} removed" in ok.output

    listed = runner.invoke(app, ["portfolio", "list"])
    assert listed.exit_code == 0, listed.output
    assert "MSFT" not in listed.output


def test_list_json_envelope_parses() -> None:
    """--json on list emits a parseable envelope mirroring the HTTP twin."""
    runner.invoke(app, ["portfolio", "add", "NVDA"])
    result = runner.invoke(app, ["portfolio", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().split("\n")[-1])

    for key in ("schema_version", "generated_at", "holdings"):
        assert key in payload, f"missing {key}"
    assert isinstance(payload["holdings"], list)
    symbols = {h["symbol"] for h in payload["holdings"]}
    assert "NVDA" in symbols
    nvda = next(h for h in payload["holdings"] if h["symbol"] == "NVDA")
    for field in ("id", "symbol", "label", "shares", "cost_basis", "added_at"):
        assert field in nvda, f"holding missing {field}"


def test_show_runs_without_sidecar_empty_records() -> None:
    """show enriches with no records present — empty news + None quote, exit 0."""
    runner.invoke(app, ["portfolio", "add", "ZZZZ"])  # unknown to fixture provider
    result = runner.invoke(app, ["portfolio", "show", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().split("\n")[-1])

    assert payload["schema_version"] == 1
    holdings = payload["holdings"]
    assert len(holdings) == 1
    h = holdings[0]
    assert h["symbol"] == "ZZZZ"
    # Enrichment shape present even with zero records.
    assert h["recent_news_count"] == 0
    assert h["recent_top"] == []
    assert h["coverage"]["covered"] is False
    # Unknown symbol → fixture provider returns a shaped quote priced None.
    assert h["quote"] is None or h["quote"]["last"] is None


def test_show_enriches_with_records_and_quote() -> None:
    """With a matching record + a fixture-known symbol, show fills news + quote."""
    _seed_record("AAPL")
    runner.invoke(app, ["portfolio", "add", "AAPL", "--label", "Apple"])

    result = runner.invoke(app, ["portfolio", "show", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().split("\n")[-1])
    aapl = next(h for h in payload["holdings"] if h["symbol"] == "AAPL")

    assert aapl["recent_news_count"] >= 1
    assert aapl["coverage"]["covered"] is True
    # AAPL is in the fixture quote table → a real last price.
    assert aapl["quote"] is not None
    assert aapl["quote"]["last"] is not None

    # Text mode renders the headline + coverage label without crashing.
    text = runner.invoke(app, ["portfolio", "show"])
    assert text.exit_code == 0, text.output
    assert "AAPL" in text.output
    assert "covered" in text.output


def test_list_enriched_flag_matches_show() -> None:
    """`list --enriched` is an alias for `show` — same enriched shape."""
    runner.invoke(app, ["portfolio", "add", "AAPL"])
    result = runner.invoke(app, ["portfolio", "list", "--enriched", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().split("\n")[-1])
    assert payload["schema_version"] == 1
    h = payload["holdings"][0]
    # Enrichment-only fields prove the --enriched path ran, not plain list.
    assert "recent_news_count" in h
    assert "coverage" in h
    assert "quote" in h
