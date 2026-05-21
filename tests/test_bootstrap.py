from __future__ import annotations

import os
from pathlib import Path

import pytest

from catchem.bootstrap import bootstrap


@pytest.mark.guard
def test_bootstrap_creates_required_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from catchem.settings import reload_settings

    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "out"))
    reload_settings()
    summary = bootstrap(skip_warm=True)
    out = tmp_path / "out"
    for sub in ("results", "cache", "db", "logs", "vector_index", "golden", "kaggle", "replay"):
        assert (out / sub).is_dir(), f"missing {sub}"
    assert summary["mode"] in ("production_safe", "replay_existing", "live_tail", "research_diagnostic")


@pytest.mark.guard
def test_bootstrap_runs_guard_verifier_against_real_repo() -> None:
    summary = bootstrap(skip_warm=True)
    g = summary["newsimpact_guard"]
    # In this environment the real merged_news repo exists; the verifier must report OK.
    if g["status"] == "skip":
        pytest.skip(f"guard verifier skipped: {g}")
    assert g["status"] == "ok", f"guard failed: {g}"


@pytest.mark.guard
def test_bootstrap_kaggle_skipped_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KAGGLE_USERNAME", raising=False)
    monkeypatch.delenv("KAGGLE_KEY", raising=False)
    summary = bootstrap(skip_warm=True)
    # We don't enable downloads by default; if we did, skip reason would be set.
    assert summary["kaggle_attempted"] is False
