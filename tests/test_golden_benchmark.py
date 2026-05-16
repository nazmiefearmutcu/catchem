"""Golden-set regression: pin the stub-pipeline's minimum quality bar.

If these floors get harder to meet, that's a signal to tune the taxonomy or
the scoring weights — not to lower the floors. Update with care.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fusion_stack.golden import SYNTHETIC, load_extended, run_benchmark
from fusion_stack.service import build_service
from fusion_stack.settings import load_settings, reload_settings


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FUSION_MODELS__USE_ML_STUBS", "true")
    reload_settings()
    return build_service(load_settings())


@pytest.mark.regression
def test_golden_set_relevance_precision_and_recall_floors(svc) -> None:
    rep = run_benchmark(svc, SYNTHETIC)
    # Stubs aren't a real model, but on the curated set we expect strong numbers.
    assert rep.relevance.recall >= 0.83, f"recall too low: {rep.relevance}"
    assert rep.relevance.precision >= 0.83, f"precision too low: {rep.relevance}"
    assert rep.relevance.f1 >= 0.83, f"f1 too low: {rep.relevance}"


@pytest.mark.regression
def test_golden_set_symbol_recall(svc) -> None:
    rep = run_benchmark(svc, SYNTHETIC)
    if rep.symbol_recall_total:
        ratio = rep.symbol_recall_hits / rep.symbol_recall_total
        assert ratio >= 0.66, f"symbol recall too low: {ratio:.2f}"


@pytest.mark.regression
def test_golden_set_sentiment_accuracy(svc) -> None:
    rep = run_benchmark(svc, SYNTHETIC)
    if rep.sentiment_total:
        acc = rep.sentiment_correct / rep.sentiment_total
        assert acc >= 0.5, f"sentiment accuracy too low: {acc:.2f}"


@pytest.mark.regression
def test_negatives_are_rejected(svc) -> None:
    """Every expected-non-finance item must come out as not relevant."""
    rep = run_benchmark(svc, SYNTHETIC)
    failures = [
        item for item in rep.per_item
        if not item["expected_finance_relevant"] and item["predicted_finance_relevant"]
    ]
    assert not failures, f"non-finance leaked through: {failures}"


@pytest.mark.regression
def test_load_extended_tolerates_missing_file(tmp_path: Path) -> None:
    assert load_extended(tmp_path / "missing.jsonl") == []
