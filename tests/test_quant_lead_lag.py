"""Tests for :mod:`catchem.quant.lead_lag`.

The fixtures use minimal record dicts and a hand-rolled cluster stub to
prove the API really is duck-typed against ``.cluster_id`` /
``.capture_ids`` — no dependency on the real ``EventCluster`` dataclass.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from catchem.quant.lead_lag import (
    LeadLagReport,
    attribute_lead_lag,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ClusterStub:
    """Mimics :class:`catchem.quant.event_clustering.EventCluster` for the
    fields the lead/lag attribution actually reads."""

    cluster_id: str
    capture_ids: tuple[str, ...]


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _rec(capture_id: str, domain: str, published_ts: str, **extra) -> dict:
    base = {
        "capture_id": capture_id,
        "domain": domain,
        "published_ts": published_ts,
        "created_at": published_ts,
        "title": f"news from {domain}",
    }
    base.update(extra)
    return base


def _build_chain_event(
    cluster_id: str,
    base_dt: datetime,
    domains_in_order: list[str],
    gap_seconds: int = 30,
) -> tuple[_ClusterStub, list[dict]]:
    """Build a cluster where ``domains_in_order[0]`` publishes at ``base_dt``,
    each subsequent domain ``gap_seconds`` later."""
    capture_ids: list[str] = []
    records: list[dict] = []
    for idx, domain in enumerate(domains_in_order):
        cap = f"{cluster_id}-{domain}"
        ts = _iso(base_dt + timedelta(seconds=idx * gap_seconds))
        capture_ids.append(cap)
        records.append(_rec(cap, domain, ts))
    return _ClusterStub(cluster_id=cluster_id, capture_ids=tuple(capture_ids)), records


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_two_clusters_consistent_leader_reuters_wins() -> None:
    """Two clusters, reuters always 30s ahead of ft, ft 30s ahead of bloomberg.

    Expect reuters lead_rate == 1.0 on 2 events, bloomberg's mean follow
    lag = 60s (always 60s behind the leader), ft's = 30s.
    """
    base_a = datetime(2026, 5, 27, 10, 0, 0, tzinfo=UTC)
    base_b = datetime(2026, 5, 27, 14, 30, 0, tzinfo=UTC)
    cluster_a, recs_a = _build_chain_event(
        "evt-A", base_a, ["reuters.com", "ft.com", "bloomberg.com"], gap_seconds=30
    )
    cluster_b, recs_b = _build_chain_event(
        "evt-B", base_b, ["reuters.com", "ft.com", "bloomberg.com"], gap_seconds=30
    )

    report = attribute_lead_lag([cluster_a, cluster_b], recs_a + recs_b)

    assert isinstance(report, LeadLagReport)
    assert report.total_events == 2
    assert report.total_sources == 3

    # ---- per-event ----
    by_id = {pe.cluster_id: pe for pe in report.per_event}
    pe_a = by_id["evt-A"]
    assert pe_a.leader_domain == "reuters.com"
    assert pe_a.leader_capture_id == "evt-A-reuters.com"
    assert pe_a.member_count == 3
    assert pe_a.follower_lag_seconds == (("ft.com", 30), ("bloomberg.com", 60))

    pe_b = by_id["evt-B"]
    assert pe_b.leader_domain == "reuters.com"
    assert pe_b.follower_lag_seconds == (("ft.com", 30), ("bloomberg.com", 60))

    # ---- per-source ----
    by_dom = {s.domain: s for s in report.per_source}

    reuters = by_dom["reuters.com"]
    assert reuters.events_participated == 2
    assert reuters.events_led == 2
    assert reuters.lead_rate == 1.0
    assert reuters.mean_lag_seconds_when_following is None  # never followed
    # Gap to nearest follower (ft.com) is 30s on both events.
    assert reuters.mean_lead_seconds_when_leading == pytest.approx(30.0)

    bloomberg = by_dom["bloomberg.com"]
    assert bloomberg.events_participated == 2
    assert bloomberg.events_led == 0
    assert bloomberg.lead_rate == 0.0
    assert bloomberg.mean_lag_seconds_when_following == pytest.approx(60.0)
    assert bloomberg.mean_lead_seconds_when_leading is None

    ft = by_dom["ft.com"]
    assert ft.events_led == 0
    assert ft.mean_lag_seconds_when_following == pytest.approx(30.0)

    # Ranking: reuters first.
    assert report.per_source[0].domain == "reuters.com"
    # Reuters should dominate; composite_score strictly higher than the others.
    assert reuters.composite_score > ft.composite_score
    assert reuters.composite_score > bloomberg.composite_score


def test_singleton_cluster_has_leader_and_no_followers() -> None:
    base = datetime(2026, 5, 27, 9, 0, 0, tzinfo=UTC)
    cap = "solo-1"
    rec = _rec(cap, "reuters.com", _iso(base))
    cluster = _ClusterStub(cluster_id="solo", capture_ids=(cap,))

    report = attribute_lead_lag([cluster], [rec])

    assert report.total_events == 1
    (pe,) = report.per_event
    assert pe.cluster_id == "solo"
    assert pe.leader_capture_id == "solo-1"
    assert pe.leader_domain == "reuters.com"
    assert pe.member_count == 1
    assert pe.follower_lag_seconds == ()

    (src,) = report.per_source
    assert src.domain == "reuters.com"
    assert src.events_led == 1
    assert src.lead_rate == 1.0
    # No follower → cannot compute a lead gap.
    assert src.mean_lead_seconds_when_leading is None


def test_empty_inputs_return_empty_report() -> None:
    report = attribute_lead_lag([], [])
    assert report.total_events == 0
    assert report.total_sources == 0
    assert report.per_event == ()
    assert report.per_source == ()


def test_missing_published_ts_falls_back_to_created_at() -> None:
    """A record without ``published_ts`` should still place into the
    timeline via ``created_at`` instead of crashing or being dropped."""
    base = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)
    # Reuters has no published_ts but does have created_at, and it's the
    # earliest of the three, so it should still be picked as leader.
    rec_reuters = {
        "capture_id": "fb-reuters",
        "domain": "reuters.com",
        "published_ts": None,
        "created_at": _iso(base),
    }
    rec_ft = _rec("fb-ft", "ft.com", _iso(base + timedelta(seconds=45)))
    rec_bbg = _rec("fb-bbg", "bloomberg.com", _iso(base + timedelta(seconds=120)))

    cluster = _ClusterStub(
        cluster_id="fb-evt",
        capture_ids=("fb-reuters", "fb-ft", "fb-bbg"),
    )

    report = attribute_lead_lag([cluster], [rec_reuters, rec_ft, rec_bbg])
    (pe,) = report.per_event
    assert pe.leader_capture_id == "fb-reuters"
    assert pe.leader_domain == "reuters.com"
    assert pe.member_count == 3
    assert pe.follower_lag_seconds == (("ft.com", 45), ("bloomberg.com", 120))


def test_same_domain_dupes_collapse_to_minimum_lag() -> None:
    """Two ft.com captures inside the same event must contribute one
    follower entry (the faster one), not two."""
    base = datetime(2026, 5, 27, 11, 0, 0, tzinfo=UTC)
    rec_leader = _rec("d-reuters", "reuters.com", _iso(base))
    rec_ft_fast = _rec("d-ft-1", "ft.com", _iso(base + timedelta(seconds=20)))
    rec_ft_slow = _rec("d-ft-2", "ft.com", _iso(base + timedelta(seconds=400)))
    cluster = _ClusterStub(
        cluster_id="dupe",
        capture_ids=("d-reuters", "d-ft-1", "d-ft-2"),
    )

    report = attribute_lead_lag([cluster], [rec_leader, rec_ft_fast, rec_ft_slow])
    (pe,) = report.per_event
    # One follower row for ft.com, holding the 20s value (not 400s, not both).
    assert pe.follower_lag_seconds == (("ft.com", 20),)

    by_dom = {s.domain: s for s in report.per_source}
    # ft.com counts as participating in one event, with mean follow lag = 20s.
    assert by_dom["ft.com"].events_participated == 1
    assert by_dom["ft.com"].mean_lag_seconds_when_following == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# Malformed clusters / records
# ---------------------------------------------------------------------------


def test_malformed_clusters_are_skipped() -> None:
    """Clusters with a non-string id or no capture_ids are skipped silently.

    Covers the ``not isinstance(cluster_id, str) or not capture_ids``
    guard in ``attribute_lead_lag``. The one well-formed cluster still
    produces a report; the malformed siblings vanish without raising.
    """
    base = datetime(2026, 5, 27, 8, 0, 0, tzinfo=UTC)
    good_cluster, good_recs = _build_chain_event(
        "ok", base, ["reuters.com", "ft.com"], gap_seconds=30
    )
    bad_id = _ClusterStub(cluster_id=None, capture_ids=("x",))  # non-str id
    bad_empty = _ClusterStub(cluster_id="empty", capture_ids=())  # no captures

    report = attribute_lead_lag([bad_id, good_cluster, bad_empty], good_recs)
    assert report.total_events == 1
    (pe,) = report.per_event
    assert pe.cluster_id == "ok"


def test_records_without_capture_id_are_not_indexed() -> None:
    """Records lacking a usable ``capture_id`` never enter the lookup.

    Covers the ``isinstance(cap, str) and cap`` filter when building
    ``by_capture``. A cluster pointing only at such records resolves no
    members and falls through to the empty-placeholder path.
    """
    base = datetime(2026, 5, 27, 7, 0, 0, tzinfo=UTC)
    # capture_id is None / blank / non-string ⇒ all skipped during indexing.
    rec_none = {"capture_id": None, "domain": "x.com", "published_ts": _iso(base)}
    rec_blank = {"capture_id": "", "domain": "y.com", "published_ts": _iso(base)}
    rec_int = {"capture_id": 42, "domain": "z.com", "published_ts": _iso(base)}
    cluster = _ClusterStub(cluster_id="ghost", capture_ids=("real-1",))

    report = attribute_lead_lag([cluster], [rec_none, rec_blank, rec_int])
    (pe,) = report.per_event
    # Cluster references "real-1" which is not in the lookup ⇒ no members.
    assert pe.cluster_id == "ghost"
    assert pe.member_count == 0
    assert pe.leader_domain is None
    # No per-source accounting happened.
    assert report.per_source == ()


def test_cluster_with_no_resolvable_members_emits_placeholder() -> None:
    """A cluster whose members all lack timestamps yields an empty placeholder.

    Covers the ``if not members`` branch (lines 223-237): the cluster_id
    is echoed with member_count=0 and a None leader, but contributes
    nothing to the per-source totals. Also exercises ``_record_timestamp``
    returning ``(None, None)`` when neither timestamp field parses.
    """
    # Member exists in the lookup but has no parseable timestamp at all.
    rec_no_ts = {
        "capture_id": "nt-1",
        "domain": "reuters.com",
        "published_ts": None,
        "created_at": "garbage-string",
    }
    cluster = _ClusterStub(cluster_id="no-ts-evt", capture_ids=("nt-1",))

    report = attribute_lead_lag([cluster], [rec_no_ts])
    assert report.total_events == 1
    (pe,) = report.per_event
    assert pe.cluster_id == "no-ts-evt"
    assert pe.leader_domain is None
    assert pe.leader_capture_id is None
    assert pe.leader_ts is None
    assert pe.member_count == 0
    assert pe.follower_lag_seconds == ()
    # No domain ever participated/led ⇒ empty per_source.
    assert report.per_source == ()


def test_missing_domain_defaults_to_unknown() -> None:
    """A record with no (or blank) ``domain`` is grouped under ``"unknown"``.

    Covers the ``_domain_of`` default branch. The blank-domain record is
    the earliest, so ``unknown`` becomes the leader domain.
    """
    base = datetime(2026, 5, 27, 6, 0, 0, tzinfo=UTC)
    rec_blank_domain = {
        "capture_id": "u-1",
        "domain": "   ",  # whitespace-only ⇒ falls back to "unknown"
        "published_ts": _iso(base),
    }
    rec_named = _rec("u-2", "ft.com", _iso(base + timedelta(seconds=30)))
    cluster = _ClusterStub(cluster_id="u-evt", capture_ids=("u-1", "u-2"))

    report = attribute_lead_lag([cluster], [rec_blank_domain, rec_named])
    (pe,) = report.per_event
    assert pe.leader_domain == "unknown"
    assert pe.follower_lag_seconds == (("ft.com", 30),)
    by_dom = {s.domain: s for s in report.per_source}
    assert "unknown" in by_dom
    assert by_dom["unknown"].events_led == 1


def test_naive_and_blank_timestamps_in_parse_ts() -> None:
    """A naive (offset-less) timestamp is read as UTC; a blank one is dropped.

    Covers ``_parse_ts`` branches: ``raw`` empty after strip (whitespace-only
    string ⇒ None) and the ``dt.tzinfo is None`` naive→UTC assumption. The
    leader is the naive-timestamped record; the blank-timestamp member is
    dropped from the timeline but a later sibling still follows.
    """
    # Leader: naive ISO (no Z, no offset) ⇒ assumed UTC.
    rec_leader = {
        "capture_id": "n-lead",
        "domain": "reuters.com",
        "published_ts": "2026-05-27T03:00:00",
        "created_at": "2026-05-27T03:00:00",
    }
    # Whitespace-only published_ts ⇒ _parse_ts None; created_at rescues it.
    rec_follow = {
        "capture_id": "n-follow",
        "domain": "ft.com",
        "published_ts": "   ",
        "created_at": "2026-05-27T03:00:40",
    }
    cluster = _ClusterStub(cluster_id="naive-evt", capture_ids=("n-lead", "n-follow"))

    report = attribute_lead_lag([cluster], [rec_leader, rec_follow])
    (pe,) = report.per_event
    assert pe.leader_capture_id == "n-lead"
    assert pe.leader_domain == "reuters.com"
    assert pe.follower_lag_seconds == (("ft.com", 40),)


def test_cluster_referencing_unknown_capture_skips_that_member() -> None:
    """A capture_id in the cluster but absent from records is skipped.

    Covers the ``rec is None: continue`` branch in the member-resolution
    loop. The surviving (resolvable) member still drives a valid event.
    """
    base = datetime(2026, 5, 27, 5, 0, 0, tzinfo=UTC)
    rec = _rec("present", "reuters.com", _iso(base))
    cluster = _ClusterStub(
        cluster_id="partial",
        capture_ids=("present", "missing-from-records"),
    )

    report = attribute_lead_lag([cluster], [rec])
    (pe,) = report.per_event
    # Only the one present capture resolved.
    assert pe.member_count == 1
    assert pe.leader_capture_id == "present"
    assert pe.follower_lag_seconds == ()
