"""Golden benchmark: schema version, generated_at, and loud failures on
malformed extended JSONL.

These guard rails matter because the synthetic set can score 100% — without
a schema-versioned output, silent drift in payload shape would be invisible
to any history/diff tooling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from catchem.golden import (
    GOLDEN_SCHEMA_VERSION,
    REQUIRED_GOLDEN_FIELDS,
    SYNTHETIC,
    BenchmarkReport,
    GoldenItem,
    LabelStats,
    _stats_for_set,
    load_extended,
    run_benchmark,
    validate_golden_row,
)
from catchem.schemas import FinancialImpactRecord, SentimentLabel


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


def test_stats_for_set_fn() -> None:
    bucket = {}
    _stats_for_set(predicted={"a"}, expected={"a", "b"}, bucket=bucket)
    assert "b" in bucket
    assert bucket["b"].fn == 1
    assert bucket["b"].tp == 0
    assert bucket["b"].fp == 0


def test_load_extended_with_whitespace_and_blank_lines(tmp_path: Path) -> None:
    p = tmp_path / "extended.jsonl"
    p.write_text(
        "\n"
        "   \n"
        '{"capture_id": "ok", "title": "t", "text": "b", "expected_finance_relevant": true}\n'
        "\t\n",
        encoding="utf-8"
    )
    items = load_extended(p, strict=True)
    assert len(items) == 1
    assert items[0].capture_id == "ok"


def test_load_extended_with_null_and_missing_optional_fields(tmp_path: Path) -> None:
    p = tmp_path / "extended.jsonl"
    p.write_text(
        '{"capture_id": "ok", "title": "t", "text": "b", "expected_finance_relevant": true, "expected_asset_classes": null}\n',
        encoding="utf-8"
    )
    items = load_extended(p, strict=True)
    assert len(items) == 1
    assert items[0].expected_asset_classes == ()


def test_label_stats_division_by_zero() -> None:
    stats = LabelStats(tp=0, fp=0, fn=0)
    assert stats.precision == 0.0
    assert stats.recall == 0.0
    assert stats.f1 == 0.0

    stats2 = LabelStats(tp=0, fp=1, fn=1)
    assert stats2.precision == 0.0
    assert stats2.recall == 0.0
    assert stats2.f1 == 0.0


class MockService:
    def __init__(self, callback):
        self.callback = callback

    def process(self, cap):
        return self.callback(cap)


def test_run_benchmark_mismatches() -> None:
    item = GoldenItem(
        capture_id="mock-1",
        title="Mock Title",
        text="Mock Text",
        domain="mock.com",
        source_type="rss",
        expected_finance_relevant=True,
        expected_asset_classes=("equities",),
        expected_reason_codes=("earnings",),
        expected_symbols=("AAPL",),
        expected_sentiment="positive",
    )

    def process_1(cap):
        return FinancialImpactRecord(
            capture_id=cap.capture_id,
            doc_id=cap.doc_id,
            title=cap.title,
            text_excerpt=cap.text,
            domain=cap.domain,
            language=cap.language,
            url=cap.url,
            is_finance_relevant=False,
            finance_relevance_score=0.1,
            asset_classes=["rates"],
            impact_reason_codes=["inflation"],
            candidate_symbols=["MSFT"],
            sentiment_label=SentimentLabel.NEGATIVE,
            sentiment_score=0.9,
            processing_mode="research_diagnostic",
        )

    svc = MockService(process_1)
    rep = run_benchmark(svc, [item])
    assert rep.relevance.fn == 1
    assert rep.relevance.tp == 0
    assert rep.relevance.fp == 0
    assert rep.symbol_recall_hits == 0
    assert rep.symbol_recall_total == 1
    assert rep.sentiment_correct == 0
    assert rep.sentiment_total == 1

    item_non_fin = GoldenItem(
        capture_id="mock-2",
        title="Non Fin",
        text="Non Fin Text",
        domain="mock.com",
        source_type="rss",
        expected_finance_relevant=False,
    )

    def process_2(cap):
        return FinancialImpactRecord(
            capture_id=cap.capture_id,
            doc_id=cap.doc_id,
            title=cap.title,
            text_excerpt=cap.text,
            domain=cap.domain,
            language=cap.language,
            url=cap.url,
            is_finance_relevant=True,
            finance_relevance_score=0.9,
            processing_mode="research_diagnostic",
        )

    svc2 = MockService(process_2)
    rep2 = run_benchmark(svc2, [item_non_fin])
    assert rep2.relevance.fp == 1
    assert rep2.relevance.tp == 0
    assert rep2.relevance.fn == 0


def test_benchmark_to_dict_fully_populated() -> None:
    rep = BenchmarkReport()
    rep.relevance.tp = 1
    rep.relevance.fp = 1
    rep.relevance.fn = 1
    rep.asset_class["equities"] = LabelStats(tp=1, fp=0, fn=0)
    rep.reason_code["earnings"] = LabelStats(tp=1, fp=0, fn=0)
    rep.symbol_recall_hits = 1
    rep.symbol_recall_total = 2
    rep.sentiment_correct = 1
    rep.sentiment_total = 2
    rep.per_item.append({"capture_id": "test"})

    out = rep.to_dict()
    assert out["relevance"]["f1"] == 0.5
    assert out["asset_class_f1"]["equities"] == 1.0
    assert out["reason_code_f1"]["earnings"] == 1.0
    assert out["symbol_recall"] == 0.5
    assert out["sentiment_accuracy"] == 0.5
    assert out["n"] == 1
