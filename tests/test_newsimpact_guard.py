"""Hard guards. These tests MUST pass; never silently skip."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from catchem.newsimpact_guarded_adapter import (
    NewsImpactGuardedAdapter,
    NewsImpactGuardError,
    snapshot_guard_state,
)
from catchem.settings import load_settings, reload_settings

NEWSIMPACT_REAL = Path("/Users/nazmi/Desktop/Projeler/proje/merged_news")


@pytest.mark.guard
def test_real_newsimpact_quarantine_state_is_expected() -> None:
    if not (NEWSIMPACT_REAL / "models/governance_index/governance_index.json").exists():
        pytest.skip("real merged_news not present on this machine")
    snap = snapshot_guard_state(NEWSIMPACT_REAL)
    assert snap.release_gate_passed is False
    assert snap.quarantine_state == "QUARANTINED_REGRESSIVE_MULTIMODAL"
    assert snap.fusion_verdict_class == "FUSION_REGRESSIVE"
    assert snap.safe_to_publish is False
    assert snap.safe_to_promote is False


@pytest.mark.guard
def test_production_safe_refuses_diagnostic_adapter(tmp_path: Path) -> None:
    # Even if someone passes diagnostic_flag=True, production_safe mode must refuse.
    fake_root = _make_fake_quarantined_root(tmp_path)
    with pytest.raises(NewsImpactGuardError):
        NewsImpactGuardedAdapter(
            newsimpact_root=fake_root,
            mode="production_safe",
            diagnostic_flag=True,
        )


@pytest.mark.guard
def test_research_diagnostic_with_flag_off_refuses(tmp_path: Path) -> None:
    fake_root = _make_fake_quarantined_root(tmp_path)
    with pytest.raises(NewsImpactGuardError):
        NewsImpactGuardedAdapter(
            newsimpact_root=fake_root,
            mode="research_diagnostic",
            diagnostic_flag=False,
        )


@pytest.mark.guard
def test_research_diagnostic_with_flag_on_and_quarantined_works(tmp_path: Path) -> None:
    fake_root = _make_fake_quarantined_root(tmp_path)
    adapter = NewsImpactGuardedAdapter(
        newsimpact_root=fake_root,
        mode="research_diagnostic",
        diagnostic_flag=True,
    )
    out = adapter.diagnostic_payload("cap-1", "any text")
    assert out["label"] == "newsimpact_diagnostic_v0"
    assert out["release_gate_passed"] is False
    assert out["may_override_finance_relevance"] is False
    assert "do not treat as validated" in out["note"]


@pytest.mark.guard
def test_release_gate_flip_refuses_loading(tmp_path: Path) -> None:
    fake_root = _make_fake_quarantined_root(tmp_path)
    # Flip the gate to True — simulating an unsafe mutation
    idx_path = fake_root / "models/governance_index/governance_index.json"
    data = json.loads(idx_path.read_text())
    data["candidates"][0]["gate_failure_status"]["release_gate_passed"] = True
    idx_path.write_text(json.dumps(data))
    with pytest.raises(NewsImpactGuardError, match="release_gate_passed_unexpectedly_true"):
        NewsImpactGuardedAdapter(
            newsimpact_root=fake_root,
            mode="research_diagnostic",
            diagnostic_flag=True,
        )


@pytest.mark.guard
def test_verify_script_returns_zero_on_real_repo() -> None:
    if not NEWSIMPACT_REAL.exists():
        pytest.skip("real merged_news not present")
    script = Path(__file__).resolve().parents[1] / "scripts" / "verify_newsimpact_guard.py"
    res = subprocess.run([sys.executable, str(script), str(NEWSIMPACT_REAL)], capture_output=True, text=True)
    assert res.returncode == 0, f"verifier failed: {res.stderr}"


@pytest.mark.guard
def test_verify_script_fails_on_flipped_gate(tmp_path: Path) -> None:
    fake_root = _make_fake_quarantined_root(tmp_path)
    idx_path = fake_root / "models/governance_index/governance_index.json"
    data = json.loads(idx_path.read_text())
    data["candidates"][0]["gate_failure_status"]["release_gate_passed"] = True
    idx_path.write_text(json.dumps(data))
    script = Path(__file__).resolve().parents[1] / "scripts" / "verify_newsimpact_guard.py"
    res = subprocess.run([sys.executable, str(script), str(fake_root)], capture_output=True, text=True)
    assert res.returncode != 0


@pytest.mark.guard
def test_service_in_production_safe_never_loads_diagnostic_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATCHEM_MODE", "production_safe")
    # Even if the operator forces the diagnostic flag to true, prod-safe must refuse.
    monkeypatch.setenv("CATCHEM_GUARDS__NEWSIMPACT_DIAGNOSTIC_ENABLED", "true")
    reload_settings()
    s = load_settings()
    from catchem.service import build_service
    svc = build_service(s)
    assert svc.diagnostic_enabled is False


def _make_fake_quarantined_root(tmp_path: Path) -> Path:
    root = tmp_path / "fake_newsimpact"
    (root / "models/governance_index").mkdir(parents=True)
    idx = {
        "candidates": [
            {
                "candidate_id": "fake",
                "governance_status": "QUARANTINED_REGRESSIVE_MULTIMODAL",
                "fusion_verdict_class": "FUSION_REGRESSIVE",
                "forbidden_operations": ["benchmark", "export", "promotion", "training"],
                "allowed_operations": ["eval", "diagnostic"],
                "gate_failure_status": {
                    "release_gate_passed": False,
                    "candidate_status": "failed_gate_diagnostic",
                    "failure_codes": ["PERMUTED_LABEL_TOO_CLOSE_TO_CHART_ONLY"],
                },
            }
        ],
        "deterministic": True,
        "safeguards": {"no_external_publish": True, "no_governance_mutation": True},
    }
    (root / "models/governance_index/governance_index.json").write_text(json.dumps(idx))
    return root
