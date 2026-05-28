"""Lock the env > YAML > defaults precedence for nested settings.

Background: pydantic-settings v2 defaults to init-kwargs *overriding* env. We
flipped that order via `settings_customise_sources` so YAML (which we pass as
init kwargs from `_yaml_overrides`) becomes the LOWEST priority and env wins.

If anyone ever resets `settings_customise_sources` to the default, these tests
fail loudly. The CLI relies on this for `CATCHEM_MODE`,
`CATCHEM_MODELS__USE_ML_STUBS`, `CATCHEM_LIVE__POLL_SECONDS`, etc.
"""

from __future__ import annotations

import os

import pytest

from catchem.settings import load_settings, reload_settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every CATCHEM_* var so each test runs from defaults+YAML."""
    for k in list(os.environ.keys()):
        if k.startswith("CATCHEM_"):
            monkeypatch.delenv(k, raising=False)
    reload_settings()


def test_yaml_default_is_baseline() -> None:
    """With no env set, settings.live.poll_seconds reflects configs/catchem.yaml."""
    s = load_settings()
    # configs/catchem.yaml ships live.poll_seconds: 10.0
    assert s.live.poll_seconds == 10.0


def test_nested_env_overrides_yaml_for_poll_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATCHEM_LIVE__POLL_SECONDS", "1.5")
    reload_settings()
    s = load_settings()
    assert s.live.poll_seconds == 1.5, (
        f"env did not override YAML — got {s.live.poll_seconds}. "
        "Check Settings.settings_customise_sources ordering."
    )


def test_nested_env_overrides_yaml_for_tail_max(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATCHEM_LIVE__TAIL_MAX_PER_TICK", "7")
    reload_settings()
    s = load_settings()
    assert s.live.tail_max_per_tick == 7


def test_nested_env_overrides_yaml_for_replay_batch_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATCHEM_REPLAY__BATCH_SIZE", "9")
    reload_settings()
    s = load_settings()
    assert s.replay.batch_size == 9


def test_top_level_env_overrides_yaml_for_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATCHEM_MODE", "research_diagnostic")
    reload_settings()
    s = load_settings()
    assert s.mode.value == "research_diagnostic"


def test_top_level_env_overrides_yaml_for_use_ml_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented CATCHEM_USE_ML_STUBS flat var must flow into ModelConfig.

    This guards against the regression where the flat var lived on Settings
    but nothing read it; the runtime read `models_.use_ml_stubs` instead.
    """
    monkeypatch.setenv("CATCHEM_USE_ML_STUBS", "false")
    reload_settings()
    s = load_settings()
    assert s.models.use_ml_stubs is False, (
        "CATCHEM_USE_ML_STUBS=false did not flow into ModelConfig.use_ml_stubs"
    )


def test_invalid_env_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Garbage in nested env should fail loudly, not silently revert."""
    monkeypatch.setenv("CATCHEM_LIVE__POLL_SECONDS", "definitely-not-a-number")
    reload_settings()
    # B017 noqa: pydantic-settings can surface the coercion failure as either
    # a pydantic ValidationError or, depending on the version's source-order
    # validator chain, a generic ValueError. Anchoring on the base
    # Exception keeps the contract about *failure*, not the specific
    # exception class — the *which-class* detail is pydantic-version churn.
    with pytest.raises(Exception):  # noqa: B017
        load_settings()


def test_unknown_top_level_env_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """`extra="ignore"` means unknown CATCHEM_* keys do not break loading."""
    monkeypatch.setenv("CATCHEM_NOT_A_REAL_FIELD", "hello")
    reload_settings()
    s = load_settings()  # must not raise
    assert s.mode.value in ("production_safe", "replay_existing", "live_tail", "research_diagnostic")


def test_two_env_vars_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATCHEM_LIVE__POLL_SECONDS", "2.5")
    monkeypatch.setenv("CATCHEM_REPLAY__BATCH_SIZE", "11")
    reload_settings()
    s = load_settings()
    assert s.live.poll_seconds == 2.5
    assert s.replay.batch_size == 11


def test_diagnostic_allowed_only_in_research_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity: even with diagnostic flag on, production_safe is hard-refused."""
    monkeypatch.setenv("CATCHEM_GUARDS__NEWSIMPACT_DIAGNOSTIC_ENABLED", "true")
    monkeypatch.setenv("CATCHEM_MODE", "production_safe")
    reload_settings()
    s = load_settings()
    assert s.diagnostic_allowed() is False

    monkeypatch.setenv("CATCHEM_MODE", "research_diagnostic")
    reload_settings()
    s = load_settings()
    assert s.diagnostic_allowed() is True


# ── Catchem release-mode env contract ───────────────────────────────────────
#
# The Tauri shell (`desktop/catchem/src-tauri/src/sidecar.rs`) injects
# `CATCHEM_PATHS__CATCHEM_OUTPUT_DIR` and `CATCHEM_PATHS__AWARENESS_DATA_DIR`
# when `SidecarConfig.release_mode == true`, pointing at
# `~/Library/Application Support/Catchem/{data,awareness-data}/`.
#
# These tests pin the Python side of that contract. If pydantic-settings'
# `env_nested_delimiter='__'` is ever flipped or `Paths.catchem_output_dir`
# is renamed, the Rust shell's release build would silently fall through
# to the default `project_root() / data` and start writing inside the .app
# bundle again. We want a CI-visible failure in that case, not a runtime
# write attempt to a Gatekeeper-protected directory.


def test_catchem_paths_catchem_output_dir_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Rust release-mode contract: CATCHEM_PATHS__CATCHEM_OUTPUT_DIR wins."""
    target = tmp_path / "Library" / "Application Support" / "Catchem" / "data"
    target.mkdir(parents=True)
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(target))
    reload_settings()
    s = load_settings()
    assert str(s.paths.catchem_output_dir) == str(target), (
        "CATCHEM_PATHS__CATCHEM_OUTPUT_DIR did not override Paths.catchem_output_dir. "
        "The Rust shell's release-mode env injection (sidecar.rs cfg.release_mode) "
        "depends on this contract."
    )


def test_catchem_paths_awareness_data_dir_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Rust release-mode contract: CATCHEM_PATHS__AWARENESS_DATA_DIR wins."""
    target = tmp_path / "Library" / "Application Support" / "Catchem" / "awareness-data"
    target.mkdir(parents=True)
    monkeypatch.setenv("CATCHEM_PATHS__AWARENESS_DATA_DIR", str(target))
    reload_settings()
    s = load_settings()
    assert str(s.paths.awareness_data_dir) == str(target), (
        "CATCHEM_PATHS__AWARENESS_DATA_DIR did not override Paths.awareness_data_dir. "
        "Rust shell relies on this in release mode."
    )


def test_catchem_api_host_and_port_use_nested_delim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rust shell contract: `CATCHEM_API__HOST` / `CATCHEM_API__PORT`
    (DOUBLE underscore) configures the bind, not `CATCHEM_API_HOST` /
    `CATCHEM_API_PORT` (single).

    The Tauri shell (desktop/catchem/src-tauri/src/sidecar.rs) passes
    these env vars to the spawned Python sidecar. With the single-
    underscore variant pydantic-settings can't navigate into ApiConfig
    and silently keeps the YAML/default value — which broke any
    attempt to move the port (multiple sidecar instances, port-
    conflict avoidance, test isolation).

    Pin both directions so a future contributor renaming or flipping
    the delimiter sees the test fail in CI before the runtime
    regression hits an operator.
    """
    monkeypatch.setenv("CATCHEM_API__HOST", "127.0.0.99")
    monkeypatch.setenv("CATCHEM_API__PORT", "58088")
    reload_settings()
    s = load_settings()
    assert s.api.host == "127.0.0.99"
    assert s.api.port == 58088


def test_catchem_api_single_underscore_is_silently_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The single-underscore form is NOT honored by pydantic-settings
    when the field lives under a nested model. This test exists so a
    future contributor who notices both forms in env-var lists doesn't
    'fix' it by adding alias support — that would mask the contract
    the Rust shell now follows.
    """
    monkeypatch.setenv("CATCHEM_API_HOST", "127.0.0.99")
    monkeypatch.setenv("CATCHEM_API_PORT", "58088")
    reload_settings()
    s = load_settings()
    # Defaults from configs/catchem.yaml (or ApiConfig defaults) win —
    # the single-underscore env vars do nothing for nested models.
    assert s.api.host != "127.0.0.99", (
        "single-underscore CATCHEM_API_HOST was honored — that breaks the "
        "Rust shell's contract and means the regression that motivated "
        "this test has been re-introduced."
    )
    assert s.api.port != 58088


# ── Aliased nested env vars (regression: CATCHEM_MODELS__* / CATCHEM_LOGGING__*) ──
#
# Pre-fix bug: Settings used `models_` (alias="models") and `logging_`
# (alias="logging") for fields whose name conflicted with Python keywords/stdlib.
# pydantic-settings derives the nested env path from the FIELD NAME, not the
# alias — so `CATCHEM_MODELS__USE_ML_STUBS` was silently ignored even though
# conftest.py, ci.yml, multiple test setUps, and several scripts set it.
#
# The fix renames the fields to `models` / `logging` (no alias needed) so the
# documented env-var contract works. These tests pin that contract.


def test_catchem_models_use_ml_stubs_env_overrides_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    """`CATCHEM_MODELS__USE_ML_STUBS=false` MUST land on Settings.models.use_ml_stubs."""
    monkeypatch.setenv("CATCHEM_MODELS__USE_ML_STUBS", "false")
    reload_settings()
    s = load_settings()
    assert s.models.use_ml_stubs is False, (
        "CATCHEM_MODELS__USE_ML_STUBS=false did not flow into Settings.models.use_ml_stubs. "
        "This is the env-var contract that conftest.py, .github/workflows/ci.yml, "
        "and several scripts/test setUps rely on."
    )


def test_catchem_models_sentiment_default_env_overrides_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    """`CATCHEM_MODELS__SENTIMENT_DEFAULT=X` MUST land on Settings.models.sentiment_default."""
    monkeypatch.setenv("CATCHEM_MODELS__SENTIMENT_DEFAULT", "ProsusAI/finbert-tone-test")
    reload_settings()
    s = load_settings()
    assert s.models.sentiment_default == "ProsusAI/finbert-tone-test"


def test_catchem_logging_level_env_overrides_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    """`CATCHEM_LOGGING__LEVEL=ERROR` MUST land on Settings.logging.level.

    The autouse `isolated_env` fixture sets this to WARNING. If env wiring is
    broken, level stays at INFO (default) regardless of what conftest sets.
    """
    monkeypatch.setenv("CATCHEM_LOGGING__LEVEL", "ERROR")
    reload_settings()
    s = load_settings()
    assert s.logging.level == "ERROR"


def test_catchem_release_mode_both_paths_together(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Both Catchem release-mode paths are read independently in the same boot."""
    out_dir = tmp_path / "AppSupport" / "Catchem" / "data"
    aw_dir = tmp_path / "AppSupport" / "Catchem" / "awareness-data"
    out_dir.mkdir(parents=True)
    aw_dir.mkdir(parents=True)
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(out_dir))
    monkeypatch.setenv("CATCHEM_PATHS__AWARENESS_DATA_DIR", str(aw_dir))
    # Plus the production_safe + stubs combination the shell always pins.
    monkeypatch.setenv("CATCHEM_MODE", "production_safe")
    monkeypatch.setenv("CATCHEM_GUARDS__NEWSIMPACT_DIAGNOSTIC_ENABLED", "false")
    monkeypatch.setenv("CATCHEM_USE_ML_STUBS", "true")
    reload_settings()
    s = load_settings()
    assert str(s.paths.catchem_output_dir) == str(out_dir)
    assert str(s.paths.awareness_data_dir) == str(aw_dir)
    assert s.mode == "production_safe"
    assert s.diagnostic_allowed() is False
    assert s.use_ml_stubs is True
