"""Guarded NewsImpact diagnostic adapter.

This module implements *one* very narrow capability: in research_diagnostic mode
only, load read-only NewsImpact governance/score artifacts and emit a
clearly-labeled `diagnostic_multimodal_result` payload alongside a catchem
record. It does **not**:

  * train anything
  * write to the NewsImpact repo
  * load or modify final_best.pt
  * relax any threshold
  * publish artifacts
  * override catchem's own is_finance_relevant decision

The adapter refuses to activate unless three independent conditions hold:
  1. catchem is configured for research_diagnostic mode
  2. the guards.newsimpact_diagnostic_enabled flag is True
  3. governance_index.json shows release_gate_passed = False (the expected state)

If any of these is wrong the adapter raises ``NewsImpactGuardError`` with a
specific reason code. Production_safe and other modes never construct this
adapter.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .logging import get_logger

logger = get_logger("catchem.newsimpact_guard")


GOVERNANCE_INDEX_REL = "models/governance_index/governance_index.json"
PROTECTED_GLOBS = (
    "final_best.pt",
    "models/release_candidates/**/*",
    "models/manifests/**/*",
    "models/governance_index/**/*",
    "governance/**/*",
)


class NewsImpactGuardError(RuntimeError):
    """Raised whenever a guarded operation is attempted from a wrong context."""


@dataclass(frozen=True)
class GuardSnapshot:
    governance_index_path: Path
    governance_index_sha256: str
    release_gate_passed: bool
    quarantine_state: str
    safe_to_publish: bool
    safe_to_promote: bool
    fusion_verdict_class: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "governance_index_path": str(self.governance_index_path),
            "governance_index_sha256": self.governance_index_sha256,
            "release_gate_passed": self.release_gate_passed,
            "quarantine_state": self.quarantine_state,
            "safe_to_publish": self.safe_to_publish,
            "safe_to_promote": self.safe_to_promote,
            "fusion_verdict_class": self.fusion_verdict_class,
        }


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot_guard_state(newsimpact_root: Path) -> GuardSnapshot:
    """Read the governance index and report the current guard state.

    Raises ``NewsImpactGuardError`` only when the index file is missing or
    malformed. A *passed* release gate is itself reported back to the caller
    so the caller can refuse to enable diagnostic mode.
    """
    idx_path = newsimpact_root / GOVERNANCE_INDEX_REL
    if not idx_path.exists():
        raise NewsImpactGuardError(f"governance_index.json missing at {idx_path}")
    try:
        data = json.loads(idx_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise NewsImpactGuardError(f"governance_index.json unreadable: {exc}") from exc
    cands = data.get("candidates") or []
    if not cands:
        raise NewsImpactGuardError("governance_index.json contains no candidates")
    c = cands[0]
    gate = c.get("gate_failure_status", {}) or {}
    return GuardSnapshot(
        governance_index_path=idx_path,
        governance_index_sha256=_sha256_file(idx_path),
        release_gate_passed=bool(gate.get("release_gate_passed", True)),
        quarantine_state=str(c.get("governance_status", "UNKNOWN")),
        # `safe_to_publish`/`safe_to_promote` are inferred from forbidden_operations
        safe_to_publish="export" not in (c.get("forbidden_operations") or []),
        safe_to_promote="promotion" not in (c.get("forbidden_operations") or []),
        fusion_verdict_class=str(c.get("fusion_verdict_class", "UNKNOWN")),
    )


def assert_protected_artifacts_unmodified(newsimpact_root: Path, baseline: Mapping[str, str]) -> None:
    """Verify a set of (path → sha256) baselines is still intact.

    The bootstrap captures the baseline on first run and re-checks on subsequent
    runs. If anything in the protected list has been touched, we abort.
    """
    for rel, expected in baseline.items():
        p = newsimpact_root / rel
        if not p.exists():
            # missing protected artifact is itself suspicious
            raise NewsImpactGuardError(f"protected_artifact_missing: {rel}")
        actual = _sha256_file(p)
        if actual != expected:
            raise NewsImpactGuardError(f"protected_artifact_modified: {rel}")


class NewsImpactGuardedAdapter:
    """Lazy, read-only adapter. Constructor enforces guard preconditions."""

    DIAGNOSTIC_LABEL = "newsimpact_diagnostic_v0"

    def __init__(
        self,
        newsimpact_root: Path,
        mode: str,
        diagnostic_flag: bool,
        allow_modes: Iterable[str] = ("research_diagnostic",),
    ) -> None:
        self.newsimpact_root = Path(newsimpact_root)
        if mode == "production_safe":
            raise NewsImpactGuardError("production_safe_mode_refuses_diagnostic_adapter")
        if not diagnostic_flag:
            raise NewsImpactGuardError("guards.newsimpact_diagnostic_enabled=False")
        if mode not in set(allow_modes):
            raise NewsImpactGuardError(f"mode_not_in_allow_list: {mode}")
        self.snapshot = snapshot_guard_state(self.newsimpact_root)
        if self.snapshot.release_gate_passed:
            raise NewsImpactGuardError(
                "release_gate_passed_unexpectedly_true — refusing to load diagnostic adapter"
            )
        logger.warning(
            "newsimpact_diagnostic_loaded",
            mode=mode,
            quarantine=self.snapshot.quarantine_state,
            verdict=self.snapshot.fusion_verdict_class,
            note="newsimpact diagnostic path available only for research; production override forbidden",
        )

    def diagnostic_payload(self, capture_id: str, text: str | None) -> dict[str, Any]:
        """Read-only payload composed entirely from local NewsImpact metadata.

        We do not run the multimodal model. We return what governance says about
        the candidate so the user can see the diagnostic context next to each
        record. This is intentionally minimal — it's a *label*, not an inference.
        """
        return {
            "label": self.DIAGNOSTIC_LABEL,
            "is_research_diagnostic": True,
            "release_gate_passed": self.snapshot.release_gate_passed,
            "quarantine_state": self.snapshot.quarantine_state,
            "fusion_verdict_class": self.snapshot.fusion_verdict_class,
            "governance_index_sha256": self.snapshot.governance_index_sha256,
            "may_override_finance_relevance": False,
            "capture_id": capture_id,
            "note": "diagnostic only — do not treat as validated news-impact",
        }
