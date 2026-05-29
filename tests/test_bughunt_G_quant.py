"""Regression tests for bug-hunt group G-quant.

Each test FAILS on the pre-fix code and PASSES after the fix:

  F1 (lead_lag): a leader's own domain must never appear as a follower of
     itself, and the leader's mean_lag_seconds_when_following must not be
     polluted by a self-follow lag.
  F2 (novelty): compute_novelty must not over-exclude id-less corpus rows;
     a genuine near-duplicate stays a near-duplicate (parity with
     score_corpus).
  F3 (global_tone): a series with >= 2 valid points but < 2 DATED points
     yields tone_trend 0.0 that must NOT be folded into the overall trend.
  F4 (persistence): a naive timestamp must be interpreted as UTC, not the
     host's local timezone, so day bucketing is deterministic.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass

from catchem.quant.global_tone import compute_global_tone, summarize_tone
from catchem.quant.lead_lag import attribute_lead_lag
from catchem.quant.novelty import compute_novelty, score_corpus
from catchem.quant.persistence import _parse_day


@dataclass
class _Cluster:
    cluster_id: str
    capture_ids: tuple


# --------------------------------------------------------------------------- #
# F1 — lead_lag self-follow                                                    #
# --------------------------------------------------------------------------- #


def test_f1_leader_domain_never_follows_itself() -> None:
    """A second (later) capture from the leader domain must not be counted as
    a follower of the leader."""
    records = [
        {"capture_id": "a", "domain": "x.com", "published_ts": "2026-05-27T00:00:00Z"},
        {"capture_id": "b", "domain": "x.com", "published_ts": "2026-05-27T00:10:00Z"},
        {"capture_id": "c", "domain": "y.com", "published_ts": "2026-05-27T00:05:00Z"},
    ]
    report = attribute_lead_lag([_Cluster("cl1", ("a", "b", "c"))], records)

    pe = report.per_event[0]
    assert pe.leader_domain == "x.com"
    follower_domains = {d for d, _lag in pe.follower_lag_seconds}
    # The leader's own domain must NOT appear in follower_lag_seconds.
    assert "x.com" not in follower_domains
    assert follower_domains == {"y.com"}
    assert pe.follower_lag_seconds == (("y.com", 300),)

    by_domain = {s.domain: s for s in report.per_source}
    # The leading domain must have no follower-latency stat (it never followed).
    assert by_domain["x.com"].mean_lag_seconds_when_following is None
    assert by_domain["y.com"].mean_lag_seconds_when_following == 300.0


# --------------------------------------------------------------------------- #
# F2 — novelty over-exclusion on empty/missing capture_id                      #
# --------------------------------------------------------------------------- #


def test_f2_compute_novelty_keeps_idless_near_duplicates() -> None:
    """compute_novelty must find a near-duplicate even when neither the target
    nor the corpus rows carry a capture_id, matching score_corpus."""
    target = {
        "title": "Fed raises rates sharply markets tumble",
        "text_excerpt": "central bank inflation tightening",
    }
    dup = {
        "title": "Fed raises rates sharply markets tumble",
        "text_excerpt": "central bank inflation tightening",
    }

    result = compute_novelty(target, [target, dup])
    parity = score_corpus([target, dup])[0]

    # The near-duplicate must be detected, NOT reported as fully novel.
    assert result.max_similarity_to_corpus > 0.5
    assert result.novelty_score < 0.5
    assert result.explanation != "first of kind in corpus"
    # Parity with the index-based timeline path on the same input.
    assert abs(result.max_similarity_to_corpus - parity.max_similarity_to_corpus) < 1e-9


def test_f2_compute_novelty_still_excludes_one_self_with_real_id() -> None:
    """The existing single-self-exclusion contract for real ids is preserved:
    the target must not match itself."""
    target = {
        "capture_id": "cap-1",
        "title": "Unique headline about a rare event",
        "text_excerpt": "no other record covers this topic at all",
    }
    other = {
        "capture_id": "cap-2",
        "title": "Completely different sports recap",
        "text_excerpt": "the home team won the championship game",
    }
    result = compute_novelty(target, [target, other])
    # Only the target row is excluded (by id); it is genuinely novel vs. cap-2.
    assert result.nearest_capture_id == "cap-2"
    assert result.novelty_score > 0.5


# --------------------------------------------------------------------------- #
# F3 — global_tone spurious 0.0 trend from undated-heavy series                #
# --------------------------------------------------------------------------- #


def test_f3_summarize_tone_signals_few_dated_points() -> None:
    """A series with many valid points but < 2 dated points reports a real
    n_points but exposes n_dated_points so the trend can be skipped."""
    timeline = [
        {"value": 1.0},
        {"value": 5.0},
        {"value": -3.0},
        {"value": 8.0},
        {"date": "20260528T120000Z", "value": 2.0},
    ]
    s = summarize_tone(timeline)
    assert s["n_points"] == 5
    assert s["n_dated_points"] == 1
    # tone_trend is undefined here (only one positioned point) → 0.0.
    assert s["tone_trend"] == 0.0


def test_f3_compute_global_tone_skips_undated_only_trend() -> None:
    """compute_global_tone must NOT fold a theme's spurious 0.0 trend into the
    overall rollup when that theme has < 2 dated points."""

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def _series(points):
        return {"timeline": [{"series": "tone", "data": points}]}

    # Theme A: strongly improving with FULLY dated points → real positive trend.
    improving = [
        {"date": "20260528T000000Z", "value": -5.0},
        {"date": "20260528T060000Z", "value": -4.0},
        {"date": "20260528T120000Z", "value": 6.0},
        {"date": "20260528T180000Z", "value": 8.0},
    ]
    # Theme B: many undated points + a single dated point → spurious 0.0 trend.
    undated_heavy = [
        {"value": 1.0},
        {"value": 2.0},
        {"value": 3.0},
        {"value": 4.0},
        {"date": "20260528T120000Z", "value": 2.0},
    ]

    class _Client:
        async def get(self, url):
            # The improving theme is "markets"; map by query substring.
            if "stock" in url:
                return _Resp(_series(improving))
            return _Resp(_series(undated_heavy))

    themes = {"markets": "stock market", "fed": "federal reserve"}
    result = asyncio.run(compute_global_tone(themes, client=_Client()))

    # Only the improving theme's trend should count; the undated-heavy theme's
    # fake 0.0 must be excluded, so the overall state reflects the real signal.
    assert result["by_theme"]["fed"]["n_dated_points"] == 1
    assert result["by_theme"]["markets"]["n_dated_points"] == 4
    assert result["overall_state"] == "improving"


# --------------------------------------------------------------------------- #
# F4 — persistence naive timestamp must be UTC, not host-local                 #
# --------------------------------------------------------------------------- #


def test_f4_parse_day_treats_naive_as_utc_under_local_tz() -> None:
    """A naive timestamp must bucket to its UTC day regardless of the host's
    local timezone."""
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        # 22:00 naive: in America/New_York astimezone() would push it to the
        # next UTC day (2026-05-28); treated as UTC it must stay 2026-05-27.
        assert _parse_day("2026-05-27T22:00:00") == "2026-05-27"
        # An explicit-offset timestamp is unaffected and must agree.
        assert _parse_day("2026-05-27T22:00:00Z") == "2026-05-27"
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()
