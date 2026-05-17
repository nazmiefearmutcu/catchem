"""Golden benchmark: schema version, generated_at, and loud failures on
malformed extended JSONL.

These guard rails matter because the synthetic set can score 100% — without
a schema-versioned output, silent drift in payload shape would be invisible
to any history/diff tooling.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fusion_stack.golden import (
    GOLDEN_SCHEMA_VERSION,
    REQUIRED_GOLDEN_FIELDS,
    BenchmarkReport,
    SYNTHETIC,
    load_extended,
    run_benchmark,
    validate_golden_row,
)


def test_required_golden_fields_constant_exposes_contract() -> None:
    # Lock the documented required set so downstream tooling can rely on it.
    assert set(REQUIRED_GOLDEN_FIELDS) == {"capture_id", "title", "text", "expected_finance_relevant"}


def test_validate_golden_row_passes_well_formed_row() -> None:
    row = {
        "capture_id": "x",
        "title": "hello",
        "text": "world",
        "expected_finance_relevant": True,
        "expected_asset_classes": ["equities"],
        "expected_reason_codes": ["earnings"],
        "expected_symbols": ["AAPL"],
    }
    assert validate_golden_row(row) is row


@pytest.mark.parametrize("bad", [
    {"title": "no id"},
    {"capture_id": "x", "title": "t"},     # missing text, expected_finance_relevant
    {"capture_id": "x", "title": "t", "text": "b", "expected_finance_relevant": "yes"},  # wrong type
    {"capture_id": "x", "title": "t", "text": "b", "expected_finance_relevant": True,
     "expected_asset_classes": "not a list"},
    "not even a dict",
    None,
    42,
])
def test_validate_golden_row_rejects_bad_rows(bad: object) -> None:
    with pytest.raises(ValueError):
        validate_golden_row(bad)


def test_load_extended_strict_raises_on_malformed_jsonl(tmp_path: Path) -> None:
    p = tmp_path / "extended.jsonl"
    p.write_text("{\"capture_id\": \"ok\", \"title\": \"t\", \"text\": \"b\", \"expected_finance_relevant\": true}\n"
                 "{this is not json}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_extended(p, strict=True)


def test_load_extended_strict_raises_on_missing_fields(tmp_path: Path) -> None:
    p = tmp_path / "extended.jsonl"
    p.write_text("{\"title\": \"missing-id\"}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing required fields"):
        load_extended(p, strict=True)


def test_load_extended_lax_mode_skips_bad_rows(tmp_path: Path) -> None:
    p = tmp_path / "extended.jsonl"
    p.write_text(
        "{\"capture_id\": \"ok\", \"title\": \"t\", \"text\": \"b\", \"expected_finance_relevant\": true}\n"
        "{garbage}\n"
        "{\"title\": \"missing\"}\n",
        encoding="utf-8",
    )
    items = load_extended(p, strict=False)
    assert len(items) == 1
    assert items[0].capture_id == "ok"


def test_load_extended_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_extended(tmp_path / "nope.jsonl") == []


def test_benchmark_output_has_schema_version_and_generated_at() -> None:
    rep = BenchmarkReport()
    out = rep.to_dict()
    assert out["schema_version"] == GOLDEN_SCHEMA_VERSION
    assert isinstance(out["generated_at"], str) and "T" in out["generated_at"]
    assert out["dataset_name"] == "synthetic_v1"


def test_synthetic_golden_set_includes_hard_negatives() -> None:
    """The synthetic set MUST include the kinds of items that snuck through v1
    of the scoring (art-restitution, sports finals, recipes, celebrity).
    Without them the benchmark would silently drift back to false positives."""
    ids = {it.capture_id for it in SYNTHETIC}
    expected_negatives = {"g-art-restitution", "g-sports-final", "g-celeb-wedding",
                          "g-recipe", "g-human-rescue", "g-launch-generic"}
    missing = expected_negatives - ids
    assert not missing, f"missing hard-negative golden items: {missing}"


def test_synthetic_golden_set_covers_diverse_asset_classes() -> None:
    """At least one positive per major asset class."""
    positive_ac = set()
    for it in SYNTHETIC:
        if it.expected_finance_relevant:
            positive_ac.update(it.expected_asset_classes)
    assert "rates" in positive_ac or "macro" in positive_ac
    assert "equities" in positive_ac
    assert "crypto" in positive_ac
    assert "commodities" in positive_ac
