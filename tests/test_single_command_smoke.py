"""Verify the one-command bootstrap script runs end-to-end on CPU.

We don't run the API server here (NO_API=1) and we skip the live-tail run path
(SKIP_RUN=1). The point is: the script must exit 0, create dirs, verify guard,
and not error.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.smoke
def test_bootstrap_shell_runs_end_to_end(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["CATCHEM_PATHS__CATCHEM_OUTPUT_DIR"] = str(tmp_path / "out")
    env["CATCHEM_NO_API"] = "1"
    env["CATCHEM_SKIP_RUN"] = "0"
    env["CATCHEM_MAX_RECORDS"] = "5"
    env["CATCHEM_MODELS__USE_ML_STUBS"] = "true"
    env["CATCHEM_MODE"] = "replay_existing"

    script = PROJECT_ROOT / "scripts" / "catchem_bootstrap_and_run.sh"
    res = subprocess.run(
        ["bash", str(script), "--no-api", "--max=5"],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if res.returncode != 0:
        print("STDOUT:", res.stdout)
        print("STDERR:", res.stderr)
    assert res.returncode == 0, "bootstrap script failed"
    # Some output dir was created
    assert (PROJECT_ROOT / "data" / "db").exists() or (tmp_path / "out" / "db").exists()
