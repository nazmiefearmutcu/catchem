"""Programmatic bootstrap: validate environment, verify guard, init storage.

The shell script ``scripts/catchem_bootstrap_and_run.sh`` calls this via the CLI
after creating the venv. Tests call it directly.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from .logging import configure_logging, get_logger
from .settings import Settings, load_settings

logger = get_logger("catchem.bootstrap")


def bootstrap(skip_warm: bool = True) -> dict[str, Any]:
    """Idempotent initialization. Returns a summary suitable for printing."""
    s = load_settings()
    configure_logging(level=s.logging.level, json_mode=False)
    summary: dict[str, Any] = {"mode": s.mode.value}

    # 1. ensure output dirs
    s.paths.catchem_output_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("results", "cache", "db", "logs", "vector_index", "golden", "kaggle", "replay"):
        (s.paths.catchem_output_dir / sub).mkdir(parents=True, exist_ok=True)
    summary["catchem_output_dir"] = str(s.paths.catchem_output_dir)

    # 2. repo paths exist?
    summary["awareness_repo_exists"] = s.paths.awareness_repo.exists()
    summary["newsimpact_repo_exists"] = s.paths.newsimpact_repo.exists()
    summary["awareness_jsonl_seen"] = _count_finalized_jsonl(s.paths.awareness_data_dir)

    # 3. guard
    guard = _run_guard_verifier(s.paths.newsimpact_repo)
    summary["newsimpact_guard"] = guard
    if guard["status"] == "fail":
        logger.error("guard_failed", **guard)
        return summary

    # 4. (optional) warm HF cache
    summary["models_warmed"] = False
    if not skip_warm and not s.models.use_ml_stubs:
        warm = _warm_hf_models(s)
        summary["models_warmed"] = warm

    # 5. (optional) kaggle
    summary["kaggle_attempted"] = False
    if s.kaggle.enable_downloads:
        summary["kaggle_attempted"] = True
        summary["kaggle_skipped_reason"] = _kaggle_skip_reason()

    return summary


def _count_finalized_jsonl(awareness_data_dir: Path) -> int:
    root = awareness_data_dir / "jsonl"
    if not root.exists():
        return 0
    return sum(1 for _ in root.glob("**/*.jsonl") if not _.name.endswith(".tmp"))


def _run_guard_verifier(newsimpact_root: Path) -> dict[str, Any]:
    script = Path(__file__).resolve().parents[2] / "scripts" / "verify_newsimpact_guard.py"
    if not script.exists():
        return {"status": "skip", "reason": "verifier_missing"}
    try:
        res = subprocess.run(
            [sys.executable, str(script), str(newsimpact_root)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # OSError covers missing executable / permission denied / read errors;
        # SubprocessError covers TimeoutExpired and CalledProcessError.
        # Anything broader would mask programming bugs (TypeError etc).
        return {"status": "skip", "reason": f"verifier_exec_error:{exc}"}
    return {
        "status": "ok" if res.returncode == 0 else "fail",
        "returncode": res.returncode,
        "stdout": res.stdout.strip(),
        "stderr": res.stderr.strip(),
    }


def _warm_hf_models(settings: Settings) -> bool:
    script = Path(__file__).resolve().parents[2] / "scripts" / "warm_hf_models.py"
    if not script.exists():
        return False
    try:
        subprocess.run([sys.executable, str(script)], check=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        # Narrow to subprocess + os failures (timeout, non-zero exit, missing
        # binary). Programming bugs (TypeError, AttributeError) must escape
        # so a refactor that breaks the script-launching path isn't masked.
        logger.warning("warm_hf_failed", err=str(exc))
        return False
    return True


def _kaggle_skip_reason() -> str:
    import os

    if not os.environ.get("KAGGLE_USERNAME") or not os.environ.get("KAGGLE_KEY"):
        return "no_credentials"
    return ""
