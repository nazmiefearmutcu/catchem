from __future__ import annotations

from unittest.mock import patch

import pytest

from catchem.reviewers import ReviewerError
from catchem.schemas import AwarenessCaptureView
from catchem.settings import Settings
from catchem.supervisor import Supervisor


@pytest.fixture
def temp_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CATCHEM_NEWS__POLLER_ENABLED", "false")
    monkeypatch.setenv("CATCHEM_ARCHIVE__ENABLED", "false")
    s = Settings()
    s.paths.catchem_output_dir = tmp_path
    s.storage.sqlite_url = f"sqlite:///{tmp_path}/catchem.sqlite3"
    return s


@pytest.fixture
def supervisor(temp_settings):
    sup = Supervisor(temp_settings)
    yield sup
    sup.close()


def test_registry_stub_caching_and_lock(supervisor) -> None:
    # 1. stub() caching (lines 87-90)
    registry = supervisor.reviewers
    stub_1 = registry.stub()
    stub_2 = registry.stub()
    assert stub_1 is stub_2


def test_registry_deepseek_missing_api_key(supervisor) -> None:
    # 2. deepseek() returns None when enabled but api_key is blank/None (line 99)
    supervisor.settings.reviewers.deepseek.enabled = True
    supervisor.settings.reviewers.deepseek.api_key = ""
    registry = supervisor.reviewers
    assert registry.deepseek() is None


def test_registry_deepseek_init_reviewer_error(supervisor) -> None:
    # 3. deepseek() handles ReviewerError on initialization (lines 110-112)
    supervisor.settings.reviewers.deepseek.enabled = True
    supervisor.settings.reviewers.deepseek.api_key = "dummy-key"
    
    registry = supervisor.reviewers

    # Mock DeepSeekReviewer init to raise ReviewerError
    def mock_init(*args, **kwargs):
        raise ReviewerError("init_failure", "Failed to init DeepSeek")

    with patch("catchem.reviewers.registry.DeepSeekReviewer", side_effect=mock_init):
        assert registry.deepseek() is None


def test_registry_add_spend_zero_or_negative(supervisor) -> None:
    # 4. add_spend with amount <= 0 does not upsert but bumps cache (lines 172->187)
    registry = supervisor.reviewers
    # Initialize cache first
    registry.add_spend(0.0)
    assert registry._cached_spent_usd == 0.0

    # Test adding negative amount (clamped to 0.0)
    registry.add_spend(-5.0)
    assert registry._cached_spent_usd == 0.0


def test_registry_add_spend_already_cached(supervisor) -> None:
    # 5. add_spend when cache is not None (line 190)
    registry = supervisor.reviewers
    registry.add_spend(0.5)
    assert registry._cached_spent_usd == 0.5
    # Call again to hit the 'else' branch (line 190)
    registry.add_spend(0.3)
    assert registry._cached_spent_usd == 0.8


def test_registry_bump_cache_only_when_empty(supervisor) -> None:
    # 6. _bump_cache_only when cache is None (line 208)
    registry = supervisor.reviewers
    assert registry._cached_spent_usd is None
    registry._bump_cache_only(0.2)
    assert registry._cached_spent_usd == 0.0
    # Second call uses cache, so it is bumped by the amount
    registry._bump_cache_only(0.3)
    assert registry._cached_spent_usd == 0.3



def test_registry_run_and_persist_deepseek_disabled(supervisor) -> None:
    # 7. run_and_persist_deepseek returns None when DeepSeek is disabled (line 230)
    supervisor.settings.reviewers.deepseek.enabled = False
    registry = supervisor.reviewers
    cap = AwarenessCaptureView(capture_id="cap-1", doc_id="doc-1", text="text")
    assert registry.run_and_persist_deepseek(cap) is None


def test_registry_run_and_persist_deepseek_budget_exhausted(supervisor) -> None:
    # 8. run_and_persist_deepseek returns persisted error when budget exhausted (line 232)
    supervisor.settings.reviewers.deepseek.enabled = True
    supervisor.settings.reviewers.deepseek.api_key = "dummy-key"
    supervisor.settings.reviewers.deepseek.usd_cap = 0.1
    
    registry = supervisor.reviewers
    registry.add_spend(0.2) # exhaust budget

    cap = AwarenessCaptureView(capture_id="cap-2", doc_id="doc-2", text="text")
    res = registry.run_and_persist_deepseek(cap)
    assert res is not None
    assert res.sentiment_label is None  # fallback label for error
    
    # Verify the error was persisted in storage
    rows = supervisor.storage.get_reviews_for_capture(cap.capture_id)
    assert len(rows) == 1
    assert rows[0]["reviewer_id"] == "deepseek"
    assert rows[0]["error_code"] == "budget_exceeded"
