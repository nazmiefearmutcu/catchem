"""Pin the source-of-truth YAML to the documented invariants."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


SOT_PATH = Path(__file__).resolve().parents[1] / "configs" / "source_of_truth.yaml"
SOT_DOC = Path(__file__).resolve().parents[1] / "docs" / "SOURCE_OF_TRUTH.md"


@pytest.mark.guard
def test_source_of_truth_yaml_invariants() -> None:
    data = yaml.safe_load(SOT_PATH.read_text(encoding="utf-8"))
    assert data["newsimpact"]["release_gate_passed"] is False
    assert data["newsimpact"]["safe_to_publish"] is False
    assert data["newsimpact"]["safe_to_promote"] is False
    assert "training" in data["newsimpact"]["forbidden_operations"]
    assert "promotion" in data["newsimpact"]["forbidden_operations"]
    assert "benchmark" in data["newsimpact"]["forbidden_operations"]
    assert "export" in data["newsimpact"]["forbidden_operations"]
    assert data["awareness"]["consumption_strategy"] == "post_commit_jsonl"
    modes = data["mode_invariants"]
    assert modes["production_safe"]["enable_newsimpact_diagnostic"] is False
    assert modes["research_diagnostic"]["enable_newsimpact_diagnostic"] is True


@pytest.mark.guard
def test_source_of_truth_doc_references_yaml() -> None:
    txt = SOT_DOC.read_text(encoding="utf-8")
    assert "source_of_truth.yaml" in txt
    assert "release_gate" in txt
    assert "QUARANTINED_REGRESSIVE_MULTIMODAL" in txt
    assert "final_best.pt" in txt
