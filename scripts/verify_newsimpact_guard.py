#!/usr/bin/env python3
"""Standalone guard verifier. Exits 0 if NewsImpact is in the expected
quarantine state and exits non-zero if anything has been silently flipped.

Usage:
    python scripts/verify_newsimpact_guard.py /path/to/merged_news

Designed to be called from the bootstrap script and from CI. Never imports
catchem source code itself so it can run before the package is installed.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


REL = "models/governance_index/governance_index.json"
EXPECTED_QUARANTINE = "QUARANTINED_REGRESSIVE_MULTIMODAL"
EXPECTED_VERDICT = "FUSION_REGRESSIVE"
EXPECTED_GATE_PASSED = False
EXPECTED_FORBIDDEN = {"benchmark", "export", "promotion", "training"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fail(reason: str, code: int = 2) -> None:
    sys.stderr.write(f"[guard] FAIL: {reason}\n")
    sys.exit(code)


def main() -> None:
    if len(sys.argv) < 2:
        fail("usage: verify_newsimpact_guard.py <newsimpact_root>")
    root = Path(sys.argv[1]).expanduser().resolve()
    if not root.exists():
        # Soft warning: in dev some users may not have newsimpact at all.
        sys.stderr.write(f"[guard] WARN: newsimpact_root not found at {root}; skipping (no guarded operation possible).\n")
        sys.exit(0)
    idx_path = root / REL
    if not idx_path.exists():
        fail(f"missing governance index at {idx_path}")
    try:
        data = json.loads(idx_path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"unreadable governance index: {exc}")
        return
    candidates = data.get("candidates") or []
    if not candidates:
        fail("no candidates in governance index")
    c = candidates[0]
    quarantine = str(c.get("governance_status"))
    verdict = str(c.get("fusion_verdict_class"))
    gate = bool((c.get("gate_failure_status") or {}).get("release_gate_passed", True))
    forbidden = set(c.get("forbidden_operations") or [])
    if quarantine != EXPECTED_QUARANTINE:
        fail(f"unexpected quarantine state: {quarantine!r} (expected {EXPECTED_QUARANTINE})")
    if verdict != EXPECTED_VERDICT:
        fail(f"unexpected fusion verdict: {verdict!r} (expected {EXPECTED_VERDICT})")
    if gate is not EXPECTED_GATE_PASSED:
        fail(f"release_gate_passed flipped to {gate!r} — refusing to proceed")
    if not EXPECTED_FORBIDDEN.issubset(forbidden):
        fail(f"forbidden_operations missing entries: have {sorted(forbidden)}")

    sha = sha256_file(idx_path)
    print(f"[guard] OK: NewsImpact quarantine intact "
          f"(state={quarantine}, gate=False, sha256={sha[:16]}…)")
    sys.exit(0)


if __name__ == "__main__":
    main()
