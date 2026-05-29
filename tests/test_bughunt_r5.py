"""Regression tests for round-5 bug-hunt fixes.

Covers:
  * symbol_mapper fuzzy fallback RESTORED (round-4 guard had made it dead) while
    the substring false-positive stays blocked.
  * CLI `export csv` neutralizes spreadsheet formula injection (parity with the
    HTTP /api/export/records endpoint).
  * `replay --path FILE` escapes glob metacharacters in the filename.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from catchem.cli import app
from catchem.schemas import FinancialImpactRecord, ProcessingMode, SentimentLabel
from catchem.settings import load_settings
from catchem.storage import load_storage_from_settings
from catchem.symbol_mapper import SymbolMapper

runner = CliRunner()


# ── symbol_mapper fuzzy fallback restored ────────────────────────────────────


def test_fuzzy_fallback_resolves_spelling_drift() -> None:
    sm = SymbolMapper()
    # Genuine typos must resolve via the fuzzy path (round-4 guard killed this).
    assert "MSFT" in {m.symbol for m in sm.map_text("Microsft reports record cloud revenue")}
    assert "GS" in {m.symbol for m in sm.map_text("Goldmann Sachs upgrades its outlook")}


def test_fuzzy_substring_false_positive_still_blocked() -> None:
    sm = SymbolMapper()
    assert "BZ=F" not in {m.symbol for m in sm.map_text("Brentwood real estate prices climb")}
    assert "DIS" not in {m.symbol for m in sm.map_text("Disneyland Paris reopens its gates")}


# ── CLI export csv formula-injection escaping ────────────────────────────────


def _insert_record(capture_id: str, title: str) -> None:
    storage = load_storage_from_settings(load_settings())
    storage.insert_record(
        FinancialImpactRecord(
            capture_id=capture_id, doc_id="d", title=title, text_excerpt="x",
            domain="reuters.com", language="en", is_finance_relevant=True,
            finance_relevance_score=0.9, asset_classes=["equities"], impact_reason_codes=[],
            candidate_symbols=[], candidate_entities=[], impact_horizons=[],
            sentiment_label=SentimentLabel.NEUTRAL, sentiment_score=0.0,
            evidence_sentences=[], reason_text=None, component_scores={},
            diagnostic_multimodal_enabled=False, diagnostic_multimodal_result=None,
            processing_mode=ProcessingMode.LIVE_TAIL, model_versions={},
            published_ts=datetime.now(UTC), created_at=datetime.now(UTC), url=None,
        )
    )
    storage.close()


def test_cli_export_csv_neutralizes_formula_injection(tmp_path: Path) -> None:
    _insert_record("evil-1", '=HYPERLINK("http://evil","click")')
    target = tmp_path / "out.csv"
    result = runner.invoke(app, ["export", "csv", "--output", str(target)])
    assert result.exit_code == 0, result.output
    text = target.read_text(encoding="utf-8")
    # The dangerous leading '=' must be neutralized (archive._csv_safe prefixes
    # a guard char); the raw "=HYPERLINK(" cell must NOT appear unescaped.
    assert "=HYPERLINK" not in text or "'=HYPERLINK" in text
    # Stronger: no CSV field may start a line with a formula trigger.
    for line in text.splitlines()[1:]:
        first_cell = line.split(",")[0].strip().strip('"')
        assert not first_cell[:1] in ("=", "+", "@") or first_cell.startswith("'"), line


# ── replay --path glob-metacharacter escaping ────────────────────────────────


def test_run_replay_single_file_with_glob_chars(tmp_path: Path, write_jsonl) -> None:
    """A filename containing glob metacharacters must replay exactly that file,
    not be interpreted as a pattern (which would match the wrong files/none)."""
    from catchem.supervisor import Supervisor

    # Two sibling files; the target's name contains '[' ']' glob metacharacters.
    rows = [{
        "capture_id": "cap-bracket", "doc_id": "d1",
        "title": "Fed raises rates 25bps", "text": "The Federal Reserve raised rates.",
        "domain": "reuters.com", "url": "https://reuters.com/x",
        "source_type": "rss", "language": "en",
    }]
    target_dir = tmp_path / "jsonl"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "captures[2026].jsonl"
    import json as _json
    target.write_text(_json.dumps(rows[0]) + "\n", encoding="utf-8")
    # A decoy sibling that a broken glob could accidentally sweep.
    (target_dir / "other.jsonl").write_text(
        _json.dumps({**rows[0], "capture_id": "cap-decoy"}) + "\n", encoding="utf-8"
    )

    sup = Supervisor(load_settings())
    try:
        sup.run_replay(file=target)
        # Exactly the bracketed file's record was ingested; the decoy was not.
        assert sup.storage.get_record("cap-bracket") is not None
        assert sup.storage.get_record("cap-decoy") is None
    finally:
        sup.close()
