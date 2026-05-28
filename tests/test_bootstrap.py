from __future__ import annotations

from pathlib import Path

import pytest

from catchem import bootstrap as bootstrap_module
from catchem.bootstrap import (
    _count_finalized_jsonl,
    _kaggle_skip_reason,
    _run_guard_verifier,
    _warm_hf_models,
    bootstrap,
)


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


@pytest.mark.guard
def test_bootstrap_is_idempotent_on_second_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running bootstrap twice against the same dir is a no-op the second time."""
    from catchem.settings import reload_settings

    out = tmp_path / "out"
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(out))
    reload_settings()

    first = bootstrap(skip_warm=True)
    # All subdirs exist after the first call.
    subs = ("results", "cache", "db", "logs", "vector_index", "golden", "kaggle", "replay")
    assert all((out / s).is_dir() for s in subs)

    # Second call must not raise (mkdir(exist_ok=True)) and yields an equivalent summary.
    second = bootstrap(skip_warm=True)
    assert all((out / s).is_dir() for s in subs)
    assert second["mode"] == first["mode"]
    assert second["catchem_output_dir"] == first["catchem_output_dir"] == str(out)


@pytest.mark.guard
def test_bootstrap_summary_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The returned summary exposes the documented keys."""
    from catchem.settings import reload_settings

    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "out"))
    reload_settings()
    summary = bootstrap(skip_warm=True)
    for key in (
        "mode",
        "catchem_output_dir",
        "awareness_repo_exists",
        "newsimpact_repo_exists",
        "awareness_jsonl_seen",
        "newsimpact_guard",
        "models_warmed",
        "kaggle_attempted",
    ):
        assert key in summary, f"missing summary key {key}"
    assert summary["models_warmed"] is False  # skip_warm short-circuits warming
    assert isinstance(summary["awareness_repo_exists"], bool)
    assert isinstance(summary["awareness_jsonl_seen"], int)


@pytest.mark.guard
def test_bootstrap_returns_early_when_guard_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing guard short-circuits before warming/kaggle keys are populated."""
    from catchem.settings import reload_settings

    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "out"))
    reload_settings()

    def _fake_guard(_root: Path) -> dict[str, object]:
        return {"status": "fail", "returncode": 1, "stdout": "", "stderr": "tampered"}

    monkeypatch.setattr(bootstrap_module, "_run_guard_verifier", _fake_guard)
    summary = bootstrap(skip_warm=True)

    assert summary["newsimpact_guard"]["status"] == "fail"
    # Dirs are still created (step 1 runs before the guard check).
    assert (tmp_path / "out" / "db").is_dir()
    # Early return: the warm/kaggle keys (steps 4-5) were never reached.
    assert "models_warmed" not in summary
    assert "kaggle_attempted" not in summary


@pytest.mark.guard
def test_bootstrap_counts_finalized_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """awareness_jsonl_seen reflects finalized (non-.tmp) JSONL captures."""
    from catchem.settings import reload_settings

    aw = tmp_path / "aw"
    jsonl_root = aw / "jsonl" / "2026" / "05" / "28"
    jsonl_root.mkdir(parents=True)
    (jsonl_root / "a.jsonl").write_text('{"x":1}\n', encoding="utf-8")
    (jsonl_root / "b.jsonl").write_text('{"x":2}\n', encoding="utf-8")
    (jsonl_root / "partial.jsonl.tmp").write_text("{", encoding="utf-8")  # excluded

    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("CATCHEM_PATHS__AWARENESS_DATA_DIR", str(aw))
    reload_settings()
    summary = bootstrap(skip_warm=True)
    assert summary["awareness_jsonl_seen"] == 2


@pytest.mark.guard
def test_bootstrap_warms_models_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """skip_warm=False with ML enabled drives the warm path (step 4)."""
    from catchem.settings import reload_settings

    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("CATCHEM_MODELS__USE_ML_STUBS", "false")
    reload_settings()

    monkeypatch.setattr(bootstrap_module, "_warm_hf_models", lambda _s: True)
    summary = bootstrap(skip_warm=False)
    assert summary["models_warmed"] is True


@pytest.mark.guard
def test_bootstrap_kaggle_attempted_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enabling downloads flips kaggle_attempted and records a skip reason."""
    from catchem.settings import reload_settings

    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("CATCHEM_KAGGLE__ENABLE_DOWNLOADS", "true")
    monkeypatch.delenv("KAGGLE_USERNAME", raising=False)
    monkeypatch.delenv("KAGGLE_KEY", raising=False)
    reload_settings()
    summary = bootstrap(skip_warm=True)
    assert summary["kaggle_attempted"] is True
    assert summary["kaggle_skipped_reason"] == "no_credentials"


# --- helper-level unit tests (cover the script-launching branches) -----------


def test_count_finalized_jsonl_missing_root(tmp_path: Path) -> None:
    """No jsonl/ dir → zero, no error."""
    assert _count_finalized_jsonl(tmp_path / "nope") == 0


def test_run_guard_verifier_missing_script(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A missing verifier script reports skip rather than crashing."""
    missing = tmp_path / "no_such_script.py"
    monkeypatch.setattr(bootstrap_module.Path, "exists", lambda self: False)
    result = _run_guard_verifier(tmp_path)
    assert result["status"] == "skip"
    assert result["reason"] == "verifier_missing"
    assert not missing.exists()


def test_run_guard_verifier_exec_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An OSError/SubprocessError from the run is caught and reported as skip."""
    monkeypatch.setattr(bootstrap_module.Path, "exists", lambda self: True)

    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(bootstrap_module.subprocess, "run", _boom)
    result = _run_guard_verifier(tmp_path)
    assert result["status"] == "skip"
    assert result["reason"].startswith("verifier_exec_error:")


def test_run_guard_verifier_ok_and_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """returncode 0 → ok; non-zero → fail, both carry stdout/stderr."""
    monkeypatch.setattr(bootstrap_module.Path, "exists", lambda self: True)

    class _Res:
        def __init__(self, rc: int) -> None:
            self.returncode = rc
            self.stdout = " out \n"
            self.stderr = " err \n"

    monkeypatch.setattr(bootstrap_module.subprocess, "run", lambda *a, **k: _Res(0))
    ok = _run_guard_verifier(tmp_path)
    assert ok["status"] == "ok"
    assert ok["returncode"] == 0
    assert ok["stdout"] == "out"
    assert ok["stderr"] == "err"

    monkeypatch.setattr(bootstrap_module.subprocess, "run", lambda *a, **k: _Res(3))
    fail = _run_guard_verifier(tmp_path)
    assert fail["status"] == "fail"
    assert fail["returncode"] == 3


def test_warm_hf_models_missing_script(monkeypatch: pytest.MonkeyPatch) -> None:
    """No warm script → returns False without invoking subprocess."""
    from catchem.settings import load_settings

    monkeypatch.setattr(bootstrap_module.Path, "exists", lambda self: False)
    assert _warm_hf_models(load_settings()) is False


def test_warm_hf_models_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Script present + subprocess succeeds → True."""
    from catchem.settings import load_settings

    monkeypatch.setattr(bootstrap_module.Path, "exists", lambda self: True)
    monkeypatch.setattr(bootstrap_module.subprocess, "run", lambda *a, **k: None)
    assert _warm_hf_models(load_settings()) is True


def test_warm_hf_models_subprocess_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A subprocess error is swallowed and reported as False."""
    import subprocess

    from catchem.settings import load_settings

    monkeypatch.setattr(bootstrap_module.Path, "exists", lambda self: True)

    def _boom(*_a: object, **_k: object) -> None:
        raise subprocess.CalledProcessError(returncode=1, cmd="warm")

    monkeypatch.setattr(bootstrap_module.subprocess, "run", _boom)
    assert _warm_hf_models(load_settings()) is False


def test_kaggle_skip_reason_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KAGGLE_USERNAME", raising=False)
    monkeypatch.delenv("KAGGLE_KEY", raising=False)
    assert _kaggle_skip_reason() == "no_credentials"


def test_kaggle_skip_reason_with_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAGGLE_USERNAME", "alice")
    monkeypatch.setenv("KAGGLE_KEY", "secret")
    assert _kaggle_skip_reason() == ""
