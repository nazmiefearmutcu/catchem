"""Tests for ``catchem.quant.market_time``.

Covers the contract:
  * known timestamps map to the correct US-market session label
  * Saturday (any hour) and Sunday before 18:00 ET → weekend
  * Sunday at/after 18:00 ET → overnight (futures reopen)
  * empty input → 7 zero-buckets, canonical order preserved
  * mixed timestamps → correct grouping, avg_score + relevant_count
  * malformed / missing timestamps are silently skipped
  * DST handled — same wall-clock time in summer vs. winter both work
"""

from __future__ import annotations

from datetime import UTC, datetime

from catchem.quant.market_time import (
    SESSIONS,
    SessionBucket,
    aggregate_by_session,
    classify_session,
)


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# ── classify_session ─────────────────────────────────────────────────────


def test_classify_known_weekday_timestamps() -> None:
    # 2026-05-27 is a Wednesday. ET is UTC-4 in May (DST in effect).
    # 08:30 ET == 12:30 UTC  → pre_open
    assert classify_session(_utc(2026, 5, 27, 12, 30)) == "pre_open"
    # 10:00 ET == 14:00 UTC  → open (09:30 - 11:00)
    assert classify_session(_utc(2026, 5, 27, 14, 0)) == "open"
    # 12:00 ET == 16:00 UTC  → lunch
    assert classify_session(_utc(2026, 5, 27, 16, 0)) == "lunch"
    # 15:00 ET == 19:00 UTC  → close (14:00 - 16:30)
    assert classify_session(_utc(2026, 5, 27, 19, 0)) == "close"
    # 17:00 ET == 21:00 UTC  → after_hours
    assert classify_session(_utc(2026, 5, 27, 21, 0)) == "after_hours"
    # 02:00 ET == 06:00 UTC  → overnight
    assert classify_session(_utc(2026, 5, 27, 6, 0)) == "overnight"
    # 22:00 ET == 02:00 UTC next day → overnight
    assert classify_session(_utc(2026, 5, 28, 2, 0)) == "overnight"


def test_weekend_saturday_all_day_and_sunday_before_18et() -> None:
    # 2026-05-30 is a Saturday. Every hour of the day is weekend.
    # 09:00 ET == 13:00 UTC
    assert classify_session(_utc(2026, 5, 30, 13, 0)) == "weekend"
    # Saturday 23:00 ET == 03:00 UTC Sunday — but the EASTERN-time
    # weekday is still Saturday at 23:00, so still weekend.
    assert classify_session(_utc(2026, 5, 31, 3, 0)) == "weekend"
    # Sunday 14:00 ET == 18:00 UTC → weekend (Sunday before 18:00 ET).
    assert classify_session(_utc(2026, 5, 31, 18, 0)) == "weekend"
    # Sunday 18:00 ET == 22:00 UTC → no longer weekend; falls to the
    # regular minute-bucket rules → 18:00 lives in after_hours.
    assert classify_session(_utc(2026, 5, 31, 22, 0)) == "after_hours"
    # Sunday 21:00 ET == 01:00 UTC Monday → overnight (after 20:00 ET).
    assert classify_session(_utc(2026, 6, 1, 1, 0)) == "overnight"


def test_classify_dst_winter_still_aligns_with_session_wall_clock() -> None:
    # 2026-01-07 is a Wednesday in standard time (ET is UTC-5).
    # 09:30 ET == 14:30 UTC → open.
    assert classify_session(_utc(2026, 1, 7, 14, 30)) == "open"
    # 16:00 ET == 21:00 UTC → close.
    assert classify_session(_utc(2026, 1, 7, 21, 0)) == "close"


# ── aggregate_by_session ─────────────────────────────────────────────────


def test_aggregate_empty_returns_all_zero_buckets_in_order() -> None:
    out = aggregate_by_session([])
    assert [b.session for b in out] == list(SESSIONS)
    assert all(isinstance(b, SessionBucket) for b in out)
    assert all(b.volume == 0 and b.avg_score == 0.0 and b.relevant_count == 0 for b in out)


def test_aggregate_groups_records_and_computes_avg_and_relevant_count() -> None:
    # Three records — two land in `open` (14:00 UTC on a Wed) and one
    # in `weekend` (Saturday). avg_score for `open` = (0.8+0.4)/2 = 0.6,
    # relevant_count = 1 (only the 0.8 is ≥ 0.5).
    recs = [
        {
            "published_ts": "2026-05-27T14:00:00Z",
            "finance_relevance_score": 0.8,
        },
        {
            "published_ts": "2026-05-27T14:30:00+00:00",
            "finance_relevance_score": 0.4,
        },
        {
            "published_ts": "2026-05-30T15:00:00Z",
            "finance_relevance_score": 0.9,
        },
    ]
    out = aggregate_by_session(recs)
    by_session = {b.session: b for b in out}
    assert by_session["open"].volume == 2
    assert abs(by_session["open"].avg_score - 0.6) < 1e-9
    assert by_session["open"].relevant_count == 1
    assert by_session["weekend"].volume == 1
    assert abs(by_session["weekend"].avg_score - 0.9) < 1e-9
    assert by_session["weekend"].relevant_count == 1
    # All other sessions are empty.
    for s in SESSIONS:
        if s in {"open", "weekend"}:
            continue
        assert by_session[s].volume == 0


def test_aggregate_falls_back_to_created_at_when_published_ts_missing() -> None:
    recs = [
        {
            "published_ts": None,
            "created_at": "2026-05-27T14:30:00Z",
            "finance_relevance_score": 0.7,
        },
    ]
    out = aggregate_by_session(recs)
    by_session = {b.session: b for b in out}
    assert by_session["open"].volume == 1
    assert by_session["open"].relevant_count == 1


def test_aggregate_skips_records_with_bad_or_missing_timestamps() -> None:
    recs = [
        {"published_ts": "not-a-date", "finance_relevance_score": 0.9},
        {"finance_relevance_score": 0.9},
        {"published_ts": None, "created_at": None, "finance_relevance_score": 0.9},
        {"published_ts": "", "finance_relevance_score": 0.9},
        # Invalid timestamp with timezone suffix to trigger ValueError in parser
        {"published_ts": "2026-99-99T99:99:99Z", "finance_relevance_score": 0.9},
        # A naive (no timezone) ISO string is rejected — _parse_ts
        # requires tz-aware values to keep classification correct.
        {"published_ts": "2026-05-27T14:00:00", "finance_relevance_score": 0.9},
        # One good record to prove the bad ones didn't crash the aggregate.
        {"published_ts": "2026-05-27T14:30:00Z", "finance_relevance_score": 0.6},
    ]
    out = aggregate_by_session(recs)
    total = sum(b.volume for b in out)
    assert total == 1
    by_session = {b.session: b for b in out}
    assert by_session["open"].volume == 1


def test_aggregate_handles_missing_or_invalid_finance_relevance_score() -> None:
    recs = [
        {"published_ts": "2026-05-27T14:00:00Z", "finance_relevance_score": None},
        {"published_ts": "2026-05-27T14:05:00Z", "finance_relevance_score": "bogus"},
        {"published_ts": "2026-05-27T14:10:00Z", "finance_relevance_score": 0.9},
    ]
    out = aggregate_by_session(recs)
    by_session = {b.session: b for b in out}
    assert by_session["open"].volume == 3
    assert by_session["open"].relevant_count == 1  # only the 0.9
    assert abs(by_session["open"].avg_score - (0.9 / 3)) < 1e-9


def test_aggregate_relevance_score_types() -> None:
    recs = [
        {"published_ts": "2026-05-27T14:00:00Z", "finance_relevance_score": 1},
        {"published_ts": "2026-05-27T14:05:00Z", "finance_relevance_score": "0.85"},
        {"published_ts": "2026-05-27T14:10:00Z", "finance_relevance_score": []},
    ]
    out = aggregate_by_session(recs)
    by_session = {b.session: b for b in out}
    assert by_session["open"].volume == 3
    assert by_session["open"].relevant_count == 2  # 1.0 and 0.85 are >= 0.5
    assert abs(by_session["open"].avg_score - (1.85 / 3)) < 1e-9
