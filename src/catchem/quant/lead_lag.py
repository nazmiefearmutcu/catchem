"""Lead/lag attribution for catchem event clusters.

Given a set of :class:`EventCluster`-like objects (anything exposing
``cluster_id`` and ``capture_ids``) plus the underlying records, this
module determines which news domain consistently publishes **first** on
shared events and quantifies the typical gap between the leader and the
followers.

The output is a deterministic, read-only :class:`LeadLagReport` that the
quant supervisor can cache and the UI can render as a leaderboard.

Algorithm summary:

    1. Build a ``capture_id -> record`` lookup.
    2. For each cluster:
         * resolve every member record's timestamp (``published_ts`` with
           ``created_at`` as a soft fallback);
         * leader = earliest timestamp, ties broken by ``capture_id`` sort
           so the result is stable;
         * follower lag = ``member_ts - leader_ts`` in whole seconds,
           grouped per domain (multiple followers from the same domain
           collapse to their minimum lag — we reward "domain X showed up
           fast", not "domain X spammed five identical re-publishes").
    3. Aggregate per-source totals — participation, leadership, mean lag
       when following, mean gap to nearest follower when leading.
    4. Score each source with
       ``lead_rate * 0.6 + min(1.0, log10(events_led + 1) / 2.0) * 0.4``
       so a domain that leads once and a domain that leads 50x are not
       weighted identically.

Design constraints honoured:
  * stdlib-only (``datetime``, ``math``);
  * duck-typed on the cluster object (``.cluster_id``, ``.capture_ids``);
  * never mutates inputs.
"""

from __future__ import annotations

import functools
import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "LeadLagReport",
    "PerEventLeadLag",
    "SourceLeadLagScore",
    "attribute_lead_lag",
]


# ---------------------------------------------------------------------------
# Public shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PerEventLeadLag:
    """Lead/lag breakdown for a single event cluster."""

    cluster_id: str
    leader_domain: str | None
    leader_capture_id: str | None
    leader_ts: str | None
    member_count: int
    # Sorted ascending by lag; one entry per non-leader domain (min over
    # that domain's members so duplicate captures from the same outlet
    # don't double-count).
    follower_lag_seconds: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class SourceLeadLagScore:
    """Aggregated leadership/follower stats for one domain."""

    domain: str
    events_participated: int
    events_led: int
    lead_rate: float
    mean_lag_seconds_when_following: float | None
    mean_lead_seconds_when_leading: float | None
    composite_score: float


@dataclass(frozen=True)
class LeadLagReport:
    total_events: int
    total_sources: int
    per_event: tuple[PerEventLeadLag, ...]
    per_source: tuple[SourceLeadLagScore, ...]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=8192)
def _parse_ts_cached(raw: str) -> datetime | None:
    if raw.endswith("Z"):
        normalized = raw[:-1] + "+00:00"
    else:
        normalized = raw
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    if dt.tzinfo is UTC:
        return dt
    return dt.astimezone(UTC)


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp string into a tz-aware UTC datetime.

    Accepts the trailing ``Z`` shortcut and naive strings (assumed UTC).
    Returns ``None`` if the value is missing, blank, or unparseable —
    callers handle that as "no timestamp, drop this member".
    """
    if not isinstance(value, str) or not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    return _parse_ts_cached(raw)


def _record_timestamp(record: dict[str, Any]) -> tuple[datetime | None, str | None]:
    """Pick the best available timestamp for a record.

    Prefers ``published_ts``; falls back to ``created_at`` if the
    publication time is absent (this is a soft fallback, not a panic
    path — the cluster member still counts).

    Returns ``(datetime_or_None, original_iso_string_or_None)`` so the
    report can echo the exact string the caller provided.
    """
    pub_raw = record.get("published_ts")
    pub_dt = _parse_ts(pub_raw)
    if pub_dt is not None:
        return pub_dt, pub_raw if isinstance(pub_raw, str) else (
            str(pub_raw) if pub_raw is not None else None
        )
    created_raw = record.get("created_at")
    created_dt = _parse_ts(created_raw)
    if created_dt is not None:
        return created_dt, created_raw if isinstance(created_raw, str) else (
            str(created_raw) if created_raw is not None else None
        )
    return None, None


def _domain_of(record: dict[str, Any]) -> str:
    """Pull the domain string, defaulting to ``"unknown"`` so a missing
    or blank value still produces a deterministic group key."""
    raw = record.get("domain")
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped:
            return stripped.lower()
    return "unknown"


def _composite_score(events_led: int, lead_rate: float) -> float:
    """Blend lead_rate (quality) with volume of leadership.

    ``lead_rate`` is in ``[0, 1]``. The volume term saturates at 100
    leaderships (``log10(100 + 1) / 2.0 ~= 1.004``) and is clipped to 1.0
    so the score stays bounded in ``[0, 1]``.
    """
    volume_term = min(1.0, math.log10(events_led + 1) / 2.0) if events_led >= 0 else 0.0
    return lead_rate * 0.6 + volume_term * 0.4


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def attribute_lead_lag(
    clusters: Iterable[Any],
    records: list[dict[str, Any]],
) -> LeadLagReport:
    """Compute per-event and per-source lead/lag attribution.

    Parameters
    ----------
    clusters:
        Iterable of cluster-like objects. Each must expose ``cluster_id``
        and ``capture_ids``. The real ``EventCluster`` dataclass from
        :mod:`catchem.quant.event_clustering` fits transparently.
    records:
        List of ``FinancialImpactRecord``-style dicts. Only ``capture_id``,
        ``domain``, ``published_ts`` and ``created_at`` are consulted.
    """
    # Build the capture lookup once.
    # We pre-resolve the timestamp and domain for each record to avoid looking up
    # and parsing inside the inner cluster loop.
    by_capture_resolved: dict[str, tuple[datetime, str | None, str]] = {}
    for rec in records:
        cap = rec.get("capture_id")
        if isinstance(cap, str) and cap:
            ts_dt, ts_raw = _record_timestamp(rec)
            if ts_dt is not None:
                by_capture_resolved[cap] = (ts_dt, ts_raw, _domain_of(rec))

    per_event: list[PerEventLeadLag] = []

    # Per-source accumulators. Use defaultdict for O(1) creation and accumulation.
    participated = defaultdict(int)
    led = defaultdict(int)
    lag_when_following = defaultdict(list)
    lead_gap_when_leading = defaultdict(list)

    for cluster in clusters:
        cluster_id = getattr(cluster, "cluster_id", None)
        capture_ids = tuple(getattr(cluster, "capture_ids", ()) or ())
        if not isinstance(cluster_id, str) or not capture_ids:
            # Skip malformed entries rather than crashing the report.
            continue

        # Pull (ts_dt, capture_id, ts_raw, domain) for every resolvable member.
        # We store ts_dt first to allow natural sorting without python lambda key function.
        members: list[tuple[datetime, str, str | None, str]] = []
        for cap_id in capture_ids:
            resolved = by_capture_resolved.get(cap_id)
            if resolved is None:
                continue
            ts_dt, ts_raw, domain = resolved
            members.append((ts_dt, cap_id, ts_raw, domain))

        if not members:
            # Cluster references no resolvable record — emit an empty
            # placeholder so consumers see the cluster_id, but skip all
            # per-source accounting.
            per_event.append(
                PerEventLeadLag(
                    cluster_id=cluster_id,
                    leader_domain=None,
                    leader_capture_id=None,
                    leader_ts=None,
                    member_count=0,
                    follower_lag_seconds=(),
                )
            )
            continue

        # Earliest timestamp wins; ties broken by capture_id sort so the
        # leader pick is stable across runs.
        # Natural sorting compares ts_dt first, then cap_id, which matches the key=lambda m: (m[1], m[0]).
        members_sorted = sorted(members)
        leader_dt, leader_cap, leader_iso, leader_domain = members_sorted[0]

        # Per-domain lag — take the minimum lag across multiple captures
        # from the same outlet so spammy re-publishes don't dilute the
        # signal.
        domain_min_lag: dict[str, int] = {}
        member_domains: set[str] = set()
        for dt, _cap_id, _iso, domain in members_sorted:
            member_domains.add(domain)
            # Skip every capture from the LEADER domain.
            # Since members_sorted is sorted ascending by timestamp, the first time
            # we see any follower domain, it must have the minimum lag.
            if domain != leader_domain and domain not in domain_min_lag:
                domain_min_lag[domain] = int((dt - leader_dt).total_seconds())

        # Sort the per-event follower entries by lag asc, then by domain
        # so equal-lag rows are deterministic.
        follower_pairs: tuple[tuple[str, int], ...] = tuple(
            sorted(domain_min_lag.items(), key=lambda kv: (kv[1], kv[0]))
        )

        per_event.append(
            PerEventLeadLag(
                cluster_id=cluster_id,
                leader_domain=leader_domain,
                leader_capture_id=leader_cap,
                leader_ts=leader_iso,
                member_count=len(members_sorted),
                follower_lag_seconds=follower_pairs,
            )
        )

        # ---- accumulate per-source stats ----
        for domain in member_domains:
            participated[domain] += 1

        led[leader_domain] += 1

        # Gap to nearest follower from the leader's perspective. None when
        # the cluster has no followers (e.g. singleton, or only same-domain
        # members which collapsed away above).
        if follower_pairs:
            nearest_follower_lag = follower_pairs[0][1]
            lead_gap_when_leading[leader_domain].append(nearest_follower_lag)

        for domain, lag in domain_min_lag.items():
            lag_when_following[domain].append(lag)

    # ---- build per-source scores ----
    # All domains that ever participated/led are keys of the participated dict
    # because the leader must also have participated.
    per_source_unsorted: list[SourceLeadLagScore] = []
    for domain in participated:
        n_part = participated[domain]
        n_led = led[domain]
        lead_rate = (n_led / n_part) if n_part > 0 else 0.0

        follow_lags = lag_when_following.get(domain)
        mean_follow = (sum(follow_lags) / len(follow_lags)) if follow_lags else None

        lead_gaps = lead_gap_when_leading.get(domain)
        mean_lead = (sum(lead_gaps) / len(lead_gaps)) if lead_gaps else None

        per_source_unsorted.append(
            SourceLeadLagScore(
                domain=domain,
                events_participated=n_part,
                events_led=n_led,
                lead_rate=lead_rate,
                mean_lag_seconds_when_following=mean_follow,
                mean_lead_seconds_when_leading=mean_lead,
                composite_score=_composite_score(n_led, lead_rate),
            )
        )

    # Rank by composite_score DESC. Tiebreakers (events_led DESC then
    # domain ASC) keep the order stable when scores collide.
    per_source = tuple(
        sorted(
            per_source_unsorted,
            key=lambda s: (-s.composite_score, -s.events_led, s.domain),
        )
    )

    return LeadLagReport(
        total_events=len(per_event),
        total_sources=len(per_source),
        per_event=tuple(per_event),
        per_source=per_source,
    )
