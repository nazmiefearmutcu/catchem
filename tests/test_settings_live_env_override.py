"""Lock the env > YAML > defaults precedence for nested settings.

Background: pydantic-settings v2 defaults to init-kwargs *overriding* env. We
flipped that order via `settings_customise_sources` so YAML (which we pass as
init kwargs from `_yaml_overrides`) becomes the LOWEST priority and env wins.

If anyone ever resets `settings_customise_sources` to the default, these tests
fail loudly. The CLI relies on this for `FUSION_MODE`,
`FUSION_MODELS__USE_ML_STUBS`, `FUSION_LIVE__POLL_SECONDS`, etc.
"""

from __future__ import annotations

import os

import pytest

from fusion_stack.settings import load_settings, reload_settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every FUSION_* var so each test runs from defaults+YAML."""
    for k in list(os.environ.keys()):
        if k.startswith("FUSION_"):
            monkeypatch.delenv(k, raising=False)
    reload_settings()


def test_yaml_default_is_baseline() -> None:
    """With no env set, settings.live.poll_seconds reflects configs/fusion.yaml."""
    s = load_settings()
    # configs/fusion.yaml ships live.poll_seconds: 10.0
    assert s.live.poll_seconds == 10.0


def test_nested_env_overrides_yaml_for_poll_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUSION_LIVE__POLL_SECONDS", "1.5")
    reload_settings()
    s = load_settings()
    assert s.live.poll_seconds == 1.5, (
        f"env did not override YAML — got {s.live.poll_seconds}. "
        "Check Settings.settings_customise_sources ordering."
    )


def test_nested_env_overrides_yaml_for_tail_max(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUSION_LIVE__TAIL_MAX_PER_TICK", "7")
    reload_settings()
    s = load_settings()
    assert s.live.tail_max_per_tick == 7


def test_nested_env_overrides_yaml_for_replay_batch_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUSION_REPLAY__BATCH_SIZE", "9")
    reload_settings()
    s = load_settings()
    assert s.replay.batch_size == 9


def test_top_level_env_overrides_yaml_for_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUSION_MODE", "research_diagnostic")
    reload_settings()
    s = load_settings()
    assert s.mode.value == "research_diagnostic"


def test_top_level_env_overrides_yaml_for_use_ml_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented FUSION_USE_ML_STUBS flat var must flow into ModelConfig.

    This guards against the regression where the flat var lived on Settings
    but nothing read it; the runtime read `models_.use_ml_stubs` instead.
    """
    monkeypatch.setenv("FUSION_USE_ML_STUBS", "false")
    reload_settings()
    s = load_settings()
    assert s.models_.use_ml_stubs is False, (
        "FUSION_USE_ML_STUBS=false did not flow into ModelConfig.use_ml_stubs"
    )


def test_invalid_env_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Garbage in nested env should fail loudly, not silently revert."""
    monkeypatch.setenv("FUSION_LIVE__POLL_SECONDS", "definitely-not-a-number")
    reload_settings()
    with pytest.raises(Exception):
        load_settings()


def test_unknown_top_level_env_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """`extra="ignore"` means unknown FUSION_* keys do not break loading."""
    monkeypatch.setenv("FUSION_NOT_A_REAL_FIELD", "hello")
    reload_settings()
    s = load_settings()  # must not raise
    assert s.mode.value in ("production_safe", "replay_existing", "live_tail", "research_diagnostic")


def test_two_env_vars_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUSION_LIVE__POLL_SECONDS", "2.5")
    monkeypatch.setenv("FUSION_REPLAY__BATCH_SIZE", "11")
    reload_settings()
    s = load_settings()
    assert s.live.poll_seconds == 2.5
    assert s.replay.batch_size == 11


def test_diagnostic_allowed_only_in_research_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity: even with diagnostic flag on, production_safe is hard-refused."""
    monkeypatch.setenv("FUSION_GUARDS__NEWSIMPACT_DIAGNOSTIC_ENABLED", "true")
    monkeypatch.setenv("FUSION_MODE", "production_safe")
    reload_settings()
    s = load_settings()
    assert s.diagnostic_allowed() is False

    monkeypatch.setenv("FUSION_MODE", "research_diagnostic")
    reload_settings()
    s = load_settings()
    assert s.diagnostic_allowed() is True
