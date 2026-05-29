"""Regression tests for the bug-hunt group E (text_extract + reviewers/registry).

Each test FAILS on the pre-fix code and PASSES after the fix:

  * E1 — non-string JSON `title` must be coerced to str|None so the upload
         route can safely `.strip()` the returned title hint (no HTTP 500).
  * E2 — HTML heading hint must NOT bleed preceding inline text, and must not
         fold a multi-line heading down to its last line.
  * E3 — DeepSeek narrative/stream spend booked via `add_spend()` must be
         durable: it must survive `invalidate_budget_cache()` so the usd_cap
         guard cannot be bypassed.
"""

from __future__ import annotations

import pytest

from catchem.reviewers.registry import REVIEWER_DEEPSEEK_NARRATIVE
from catchem.settings import Settings
from catchem.supervisor import Supervisor
from catchem.text_extract import extract_text


# ── E1: non-string JSON title -------------------------------------------------
class TestJsonTitleCoercion:
    def test_numeric_title_returned_as_str(self):
        """`{"title": 12345}` must yield a *string* title hint, not an int."""
        title_hint, body = extract_text("x.json", b'{"text":"hi","title":12345}')
        assert body == "hi"
        assert isinstance(title_hint, str)
        assert title_hint == "12345"

    def test_list_title_does_not_crash_caller_strip(self):
        """The sole caller does `(title or title_hint or "...").strip()`.

        Before the fix the raw list leaked out and `.strip()` raised
        AttributeError → unhandled 500. Now the hint is a plain string.
        """
        title_hint, _ = extract_text("x.json", b'{"text":"hi","title":[1,2,3]}')
        # Mirror api.ui_demo_upload's coercion — must not raise.
        effective_title = (None or title_hint or "(untitled upload)").strip()
        assert isinstance(effective_title, str)
        assert effective_title  # non-empty

    def test_missing_title_still_none(self):
        """No `title` key → hint stays None (caller falls back to default)."""
        title_hint, body = extract_text("x.json", b'{"text":"hello world"}')
        assert title_hint is None
        assert body == "hello world"

    def test_string_title_preserved(self):
        title_hint, body = extract_text("x.json", b'{"text":"hi","title":"Real Title"}')
        assert title_hint == "Real Title"
        assert body == "hi"


# ── E2: HTML heading hint isolation -------------------------------------------
class TestHtmlHeadingHint:
    def test_inline_text_before_heading_not_bled(self):
        html = (
            b"<html><body><span>Advertisement </span>"
            b"<h1>Real Title</h1><p>Body paragraph here.</p></body></html>"
        )
        title_hint, body = extract_text("x.html", html)
        assert title_hint == "Real Title"
        # Body still contains everything (no regression to body extraction).
        assert "Body paragraph here." in body

    def test_multiline_heading_not_truncated_to_last_line(self):
        html = b"<html><body><h1>Line one<br>Line two</h1><p>x</p></body></html>"
        title_hint, _ = extract_text("x.html", html)
        # Both lines must survive (collapsed to one line), not just "Line two".
        assert "Line one" in title_hint
        assert "Line two" in title_hint

    def test_clean_heading_still_works(self):
        html = b"<html><body><h1>Just A Title</h1><p>body</p></body></html>"
        title_hint, _ = extract_text("x.html", html)
        assert title_hint == "Just A Title"


# ── E3: durable DeepSeek narrative spend --------------------------------------
@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    monkeypatch.setenv("CATCHEM_NEWS__POLLER_ENABLED", "false")
    monkeypatch.setenv("CATCHEM_ARCHIVE__ENABLED", "false")
    s = Settings()
    s.paths.catchem_output_dir = tmp_path
    s.storage.sqlite_url = f"sqlite:///{tmp_path}/catchem.sqlite3"
    sup = Supervisor(s)
    yield sup
    sup.close()


class TestNarrativeSpendDurability:
    def test_add_spend_survives_cache_invalidation(self, supervisor):
        """The bug: narrative spend is only in-memory, so a settings PATCH
        (which calls invalidate_budget_cache) discards it and the cap under-
        counts. After the fix, add_spend writes a durable ledger row."""
        supervisor.settings.reviewers.deepseek.enabled = True
        supervisor.settings.reviewers.deepseek.api_key = "test-key"
        supervisor.settings.reviewers.deepseek.usd_cap = 1.0

        # Simulate three live-read / stream / narrative spends.
        supervisor.reviewers.add_spend(0.30)
        supervisor.reviewers.add_spend(0.30)
        supervisor.reviewers.add_spend(0.30)
        assert supervisor.reviewers.budget_state().spent_usd == pytest.approx(0.90, rel=1e-6)

        # Operator toggles a reviewer setting → cache is dropped.
        supervisor.reviewers.invalidate_budget_cache()

        # Pre-fix: this rebuilt from the reviews table only → 0.0 (spend lost).
        state = supervisor.reviewers.budget_state()
        assert state.spent_usd == pytest.approx(0.90, rel=1e-6)

    def test_cap_not_bypassable_after_invalidation(self, supervisor):
        """Cumulative narrative spend past the cap must keep the budget
        exhausted across an invalidation, so the guard can't be bypassed."""
        supervisor.settings.reviewers.deepseek.enabled = True
        supervisor.settings.reviewers.deepseek.api_key = "test-key"
        supervisor.settings.reviewers.deepseek.usd_cap = 1.0

        supervisor.reviewers.add_spend(0.60)
        supervisor.reviewers.add_spend(0.60)  # total 1.20 > cap
        assert supervisor.reviewers.budget_state().exhausted

        supervisor.reviewers.invalidate_budget_cache()
        assert supervisor.reviewers.budget_state().exhausted

    def test_ledger_rows_accumulate_not_overwrite(self, supervisor):
        """Distinct synthetic capture_id per call → rows accumulate."""
        supervisor.reviewers.add_spend(0.10)
        supervisor.reviewers.add_spend(0.10)
        supervisor.reviewers.add_spend(0.10)
        ledger_total = supervisor.storage.sum_review_cost(REVIEWER_DEEPSEEK_NARRATIVE)
        assert ledger_total == pytest.approx(0.30, rel=1e-6)

    def test_zero_spend_writes_no_row(self, supervisor):
        """A zero / negative spend should not pollute the ledger."""
        supervisor.reviewers.add_spend(0.0)
        supervisor.reviewers.add_spend(-5.0)
        assert supervisor.storage.sum_review_cost(REVIEWER_DEEPSEEK_NARRATIVE) == 0.0

    def test_narrative_ledger_excluded_from_deepseek_compare_id(self, supervisor):
        """Ledger rows use a distinct reviewer_id so they don't leak into the
        canonical `deepseek` reviewer rows feeding the compare page."""
        from catchem.reviewers.base import REVIEWER_DEEPSEEK

        supervisor.reviewers.add_spend(0.25)
        assert supervisor.storage.sum_review_cost(REVIEWER_DEEPSEEK) == 0.0
        assert supervisor.storage.sum_review_cost(REVIEWER_DEEPSEEK_NARRATIVE) == pytest.approx(
            0.25, rel=1e-6
        )
