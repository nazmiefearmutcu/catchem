"""Source-repo regression safety: assert we have not mutated Awareness or NewsImpact."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest


AWARENESS = Path("/Users/nazmi/Desktop/Projeler/proje/awareness")
NEWSIMPACT = Path("/Users/nazmi/Desktop/Projeler/proje/merged_news")


@pytest.mark.regression
def test_awareness_doccapture_schema_unmodified() -> None:
    p = AWARENESS / "src/awareness/schemas/doc.py"
    if not p.exists():
        pytest.skip("awareness repo not on this machine")
    txt = p.read_text(encoding="utf-8")
    # Spot-check: the field-list tuple must still be a 29-name tuple as committed.
    assert "DOC_FIELDS_ORDERED" in txt
    assert "capture_id" in txt and "doc_id" in txt and "near_dup_hash" in txt


@pytest.mark.regression
def test_awareness_jsonl_writer_signature_unmodified() -> None:
    p = AWARENESS / "src/awareness/storage/jsonl.py"
    if not p.exists():
        pytest.skip("awareness repo not on this machine")
    txt = p.read_text(encoding="utf-8")
    assert "class JsonlStagingWriter" in txt
    assert "max_records_per_file" in txt
    assert "def write" in txt and "def flush" in txt


@pytest.mark.regression
def test_newsimpact_final_best_pt_not_modified_by_fusion_run(tmp_path: Path) -> None:
    """fusion_stack must never write to final_best.pt.

    We capture a sha256 of the file (if present), run a fusion replay, and re-check.
    """
    # Locate any final_best.pt under merged_news (none expected on this machine,
    # but fail-loudly if one appears.)
    if not NEWSIMPACT.exists():
        pytest.skip("merged_news not on this machine")
    candidates = list(NEWSIMPACT.glob("**/final_best.pt"))
    if not candidates:
        pytest.skip("no final_best.pt present (expected; nothing to verify)")
    baselines = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in candidates}

    # Run a tiny replay through the supervisor
    from fusion_stack.settings import load_settings, reload_settings
    from fusion_stack.supervisor import Supervisor

    reload_settings()
    sup = Supervisor(load_settings())
    try:
        sup.run_replay(max_records=5)
    finally:
        sup.close()

    for p, sha in baselines.items():
        assert hashlib.sha256(p.read_bytes()).hexdigest() == sha, f"final_best.pt mutated at {p}"


@pytest.mark.regression
def test_no_fusion_call_into_v7_runner_training_path() -> None:
    """Imports of training/runner modules from fusion_stack should be impossible.

    Inspect every fusion_stack module's source for imports of merged_news training files.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "fusion_stack"
    forbidden = ("v7_runner", "v6_runner", "v5_runner", "pipeline_v7", "v4_runner", "v3_runner")
    for py in src.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for word in forbidden:
            assert word not in text, f"{py} references forbidden training module {word!r}"
