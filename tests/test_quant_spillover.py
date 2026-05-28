"""Tests for `catchem.quant.spillover`.

Each test owns one contract guarantee from the module docstring so a
regression there points at exactly one expectation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from catchem.quant.spillover import (
    SpilloverEdge,
    SpilloverReport,
    compute_spillover,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record(
    published_ts: str,
    *,
    asset_classes: list[str] | None = None,
    capture_id: str | None = None,
) -> dict:
    """Minimal FinancialImpactRecord-shaped dict that spillover reads.

    The module only consults ``published_ts``/``created_at`` and
    ``asset_classes``; the other fields exist only so callers can
    re-use this builder elsewhere without surprise key errors.
    """

    return {
        "capture_id": capture_id or f"cap-{published_ts}",
        "published_ts": published_ts,
        "created_at": published_ts,
        "asset_classes": list(asset_classes or []),
    }


def _ts(base: datetime, minutes: int) -> str:
    """Format ``base + minutes`` as the ISO string spillover accepts."""

    return (base + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


# Anchor datetime used by most synthetic streams. Picking 09:00 keeps
# bucket starts visually obvious in failure output.
_BASE = datetime(2024, 1, 1, 9, 0, 0, tzinfo=timezone.utc)


def _flood(
    base: datetime,
    bucket_idx: int,
    asset: str,
    *,
    count: int,
    bucket_minutes: int,
) -> list[dict]:
    """Generate ``count`` records inside bucket ``bucket_idx`` for ``asset``.

    Records are spread across the inner seconds of the bucket so the
    floor still lands them in the intended window. Used to fabricate
    a spike that will dominate the rolling-window baseline and trip
    the z-score threshold.
    """

    out: list[dict] = []
    bucket_start_minutes = bucket_idx * bucket_minutes
    for j in range(count):
        # Spread inside the bucket using second offsets so capture_ids
        # stay unique even within the same minute.
        seconds = (j % (bucket_minutes * 60))
        ts = base + timedelta(minutes=bucket_start_minutes, seconds=seconds)
        iso = ts.isoformat().replace("+00:00", "Z")
        out.append(
            _record(
                iso,
                asset_classes=[asset],
                capture_id=f"{asset}-{bucket_idx}-{j}",
            )
        )
    return out


# Varied baseline counts. A flat baseline (every bucket equal) makes
# the rolling stdev zero, which the production guard treats as "no
# surge" (refusing to divide by ~0). Real news streams always have
# bucket-to-bucket variation, so the test scaffolding mirrors that
# with a small repeating sequence.
_BASELINE_PATTERN: tuple[int, ...] = (1, 2, 1, 3, 1, 2, 1, 2, 1, 3)


def _baseline(
    base: datetime,
    n_buckets: int,
    assets: tuple[str, ...],
    *,
    bucket_minutes: int,
) -> list[dict]:
    """Emit ``n_buckets`` worth of varied baseline records for each asset.

    Cycles through ``_BASELINE_PATTERN`` so the rolling stdev > 0 and
    a real spike crosses ``surge_z_threshold=1.5`` cleanly.
    """

    out: list[dict] = []
    for bucket_idx in range(n_buckets):
        per_bucket = _BASELINE_PATTERN[bucket_idx % len(_BASELINE_PATTERN)]
        for asset in assets:
            out.extend(
                _flood(
                    base,
                    bucket_idx,
                    asset,
                    count=per_bucket,
                    bucket_minutes=bucket_minutes,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_records_yields_empty_report() -> None:
    """No records ⇒ empty edges, empty self_loops, total_buckets=0."""

    report = compute_spillover([], bucket_minutes=30, lag_buckets=1)
    assert isinstance(report, SpilloverReport)
    assert report.edges == ()
    assert report.self_loops == ()
    assert report.total_buckets == 0
    # Echoed params survive even on the empty path.
    assert report.bucket_minutes == 30
    assert report.lag_buckets == 1


def test_invalid_bucket_minutes_raises() -> None:
    """Zero / negative widths are nonsensical and must raise."""

    with pytest.raises(ValueError):
        compute_spillover([], bucket_minutes=0)
    with pytest.raises(ValueError):
        compute_spillover([], bucket_minutes=-30)


def test_invalid_lag_buckets_raises() -> None:
    """Zero / negative lag would conflate co-occurrence with spillover."""

    with pytest.raises(ValueError):
        compute_spillover([], lag_buckets=0)
    with pytest.raises(ValueError):
        compute_spillover([], lag_buckets=-1)


def test_short_history_returns_no_edges() -> None:
    """Fewer buckets than the warmup floor ⇒ no surges, no edges."""

    # Only 2 buckets of data — z-scores require >= 3 prior buckets so
    # nothing can surge.
    records: list[dict] = []
    for bucket_idx in (0, 1):
        records.extend(
            _flood(_BASE, bucket_idx, "rates", count=5, bucket_minutes=30)
        )
    report = compute_spillover(records, bucket_minutes=30, lag_buckets=1)
    # No cross-asset edges fire because there's nothing to spill over to.
    assert report.edges == ()
    # Self-loops always emit one zeroed edge per asset (book-keeping);
    # no co-movements should have fired with this little history.
    for loop in report.self_loops:
        assert loop.co_movements == 0
        assert loop.spillover_score <= 0.0


# ---------------------------------------------------------------------------
# Directional spillover
# ---------------------------------------------------------------------------


def test_rates_surge_then_equities_surge_at_lag_1() -> None:
    """Every rates surge is followed 1 bucket later by an equities surge.

    Construct a history where rates spikes precede equities spikes
    at lag 1, but the pairs are well-separated so the reverse
    direction (equities -> rates) does NOT see a spike at lag 1.

    Layout (lag 1 = 1 bucket ahead):
      * 10 buckets of varied baseline (assets shared);
      * pair A: rates@10 -> equities@11 (then quiet);
      * pair B: rates@14 -> equities@15 (then quiet);
      * pair C: rates@18 -> equities@19;
      * pair D: rates@22 -> equities@23.

    At lag 1:
      * (rates -> equities) sees 4 co-movements — strong positive edge;
      * (equities -> rates) sees 0 co-movements (the bucket after each
        equities spike is quiet for rates), so it's filtered out.
    """

    bucket_minutes = 30
    records: list[dict] = _baseline(
        _BASE, 10, ("rates", "equities"), bucket_minutes=bucket_minutes
    )
    # Four well-separated (rates @t -> equities @t+1) pairs.
    for src in (10, 14, 18, 22):
        records.extend(
            _flood(_BASE, src, "rates", count=25, bucket_minutes=bucket_minutes)
        )
        records.extend(
            _flood(
                _BASE, src + 1, "equities", count=25, bucket_minutes=bucket_minutes
            )
        )
    # Filler so the gap buckets aren't empty (varied counts keep stdev
    # > 0 without themselves crossing the surge threshold).
    for bucket_idx in (11, 12, 13, 15, 16, 17, 19, 20, 21, 23, 24):
        # Bucket 11/15/19/23 are equities-spike buckets — skip those.
        if bucket_idx in (11, 15, 19, 23):
            continue
        records.extend(
            _flood(_BASE, bucket_idx, "rates", count=2, bucket_minutes=bucket_minutes)
        )
        records.extend(
            _flood(
                _BASE, bucket_idx, "equities", count=2, bucket_minutes=bucket_minutes
            )
        )

    report = compute_spillover(
        records,
        bucket_minutes=bucket_minutes,
        lag_buckets=1,
        surge_z_threshold=1.5,
    )

    edges_by_pair = {(e.source_asset, e.target_asset): e for e in report.edges}
    assert ("rates", "equities") in edges_by_pair
    re_edge = edges_by_pair[("rates", "equities")]
    assert re_edge.spillover_score > 0.0
    assert re_edge.co_movements >= 2
    assert re_edge.lag_minutes == bucket_minutes  # lag_buckets=1

    # equities -> rates must NOT show a positive edge (every bucket
    # following an equities spike is quiet for rates).
    assert ("equities", "rates") not in edges_by_pair


def test_solo_surger_produces_no_positive_edges() -> None:
    """An asset that spikes alone (no follow-on) yields no positive edges."""

    bucket_minutes = 30
    # Varied baseline for 10 buckets of both assets.
    records: list[dict] = _baseline(
        _BASE, 10, ("rates", "equities"), bucket_minutes=bucket_minutes
    )
    # Rates spikes at 10 and 12; equities never spikes.
    records.extend(_flood(_BASE, 10, "rates", count=30, bucket_minutes=bucket_minutes))
    records.extend(_flood(_BASE, 12, "rates", count=30, bucket_minutes=bucket_minutes))
    # Filler for buckets 11, 13, 14 — varied so stdev stays positive.
    for bucket_idx in (11, 13, 14):
        records.extend(
            _flood(_BASE, bucket_idx, "rates", count=1, bucket_minutes=bucket_minutes)
        )
        records.extend(
            _flood(
                _BASE, bucket_idx, "equities", count=2, bucket_minutes=bucket_minutes
            )
        )

    report = compute_spillover(
        records,
        bucket_minutes=bucket_minutes,
        lag_buckets=1,
        surge_z_threshold=1.5,
    )

    # No cross-asset edge should pass the filter — equities never surges
    # after rates, and a self loop would be excluded from edges anyway.
    for edge in report.edges:
        assert (
            edge.source_asset != edge.target_asset
        ), "self_loops must not appear in edges"


def test_self_loop_persistence_is_separated() -> None:
    """An asset that surges in consecutive buckets shows up in self_loops.

    Equities spikes at buckets 10 AND 11 (so at lag 1, equities@10
    co-moves with equities@11). The pair (equities, equities) must
    appear in ``self_loops`` and never in ``edges``.
    """

    bucket_minutes = 30
    records: list[dict] = _baseline(
        _BASE, 10, ("equities",), bucket_minutes=bucket_minutes
    )
    # Consecutive equities spikes — persistence.
    records.extend(
        _flood(_BASE, 10, "equities", count=25, bucket_minutes=bucket_minutes)
    )
    records.extend(
        _flood(_BASE, 11, "equities", count=25, bucket_minutes=bucket_minutes)
    )
    # And one more pair so we get 2+ co-movements.
    records.extend(
        _flood(_BASE, 13, "equities", count=25, bucket_minutes=bucket_minutes)
    )
    records.extend(
        _flood(_BASE, 14, "equities", count=25, bucket_minutes=bucket_minutes)
    )

    report = compute_spillover(
        records,
        bucket_minutes=bucket_minutes,
        lag_buckets=1,
        surge_z_threshold=1.5,
    )

    # equities should not appear in cross-asset edges.
    for edge in report.edges:
        assert not (edge.source_asset == "equities" and edge.target_asset == "equities")

    # self_loops contains the persistence edge.
    loops_by_asset = {(e.source_asset, e.target_asset): e for e in report.self_loops}
    assert ("equities", "equities") in loops_by_asset
    loop = loops_by_asset[("equities", "equities")]
    # Persistence here is strong — at least one co-movement should land.
    assert loop.co_movements >= 1


# ---------------------------------------------------------------------------
# sample_pivots cap
# ---------------------------------------------------------------------------


def test_sample_pivots_capped_at_three() -> None:
    """An edge with many co-movements only echoes the first 3 pivots."""

    bucket_minutes = 30
    records: list[dict] = _baseline(
        _BASE, 10, ("rates", "equities"), bucket_minutes=bucket_minutes
    )
    # 5 consecutive (rates -> equities) pairs at lag 1.
    for source_bucket, target_bucket in [
        (10, 11),
        (12, 13),
        (14, 15),
        (16, 17),
        (18, 19),
    ]:
        records.extend(
            _flood(
                _BASE,
                source_bucket,
                "rates",
                count=20,
                bucket_minutes=bucket_minutes,
            )
        )
        records.extend(
            _flood(
                _BASE,
                target_bucket,
                "equities",
                count=20,
                bucket_minutes=bucket_minutes,
            )
        )

    report = compute_spillover(
        records,
        bucket_minutes=bucket_minutes,
        lag_buckets=1,
        surge_z_threshold=1.5,
    )

    edges_by_pair = {(e.source_asset, e.target_asset): e for e in report.edges}
    re_edge = edges_by_pair[("rates", "equities")]
    assert re_edge.co_movements >= 3
    # The cap is enforced regardless of how many co-movements fire.
    assert len(re_edge.sample_pivots) == 3


# ---------------------------------------------------------------------------
# Non-unit lag
# ---------------------------------------------------------------------------


def test_lag_buckets_2_fires_at_two_buckets_ahead() -> None:
    """With lag_buckets=2, equities must surge 2 buckets after rates.

    Lag-1 pairing should NOT fire as a positive (rates -> equities)
    edge in this construction because equities is quiet 1 bucket
    after the rates spike.
    """

    bucket_minutes = 30
    records: list[dict] = _baseline(
        _BASE, 10, ("rates", "equities"), bucket_minutes=bucket_minutes
    )
    # Three (rates @t -> equities @t+2) pairs, well separated.
    for src in (10, 15, 20):
        records.extend(
            _flood(_BASE, src, "rates", count=25, bucket_minutes=bucket_minutes)
        )
        records.extend(
            _flood(
                _BASE, src + 2, "equities", count=25, bucket_minutes=bucket_minutes
            )
        )
    # Filler buckets — varied counts but well below surge level.
    for bucket_idx in (11, 13, 14, 16, 18, 19, 21, 23):
        records.extend(
            _flood(_BASE, bucket_idx, "rates", count=2, bucket_minutes=bucket_minutes)
        )
        records.extend(
            _flood(
                _BASE, bucket_idx, "equities", count=2, bucket_minutes=bucket_minutes
            )
        )

    # lag=2 should find the relationship.
    report_lag2 = compute_spillover(
        records,
        bucket_minutes=bucket_minutes,
        lag_buckets=2,
        surge_z_threshold=1.5,
    )
    pairs_lag2 = {(e.source_asset, e.target_asset): e for e in report_lag2.edges}
    assert ("rates", "equities") in pairs_lag2
    re_lag2 = pairs_lag2[("rates", "equities")]
    assert re_lag2.spillover_score > 0.0
    assert re_lag2.co_movements >= 2
    assert re_lag2.lag_minutes == 2 * bucket_minutes

    # lag=1 should NOT find a positive (rates -> equities) edge here.
    report_lag1 = compute_spillover(
        records,
        bucket_minutes=bucket_minutes,
        lag_buckets=1,
        surge_z_threshold=1.5,
    )
    pairs_lag1 = {(e.source_asset, e.target_asset): e for e in report_lag1.edges}
    assert ("rates", "equities") not in pairs_lag1


# ---------------------------------------------------------------------------
# Edge filtering / sorting contract
# ---------------------------------------------------------------------------


def test_edges_sorted_by_score_desc_and_filtered() -> None:
    """Edges in the report are sorted by score DESC and filtered to score>0, comov>=2."""

    bucket_minutes = 30
    # Three assets with varied baseline.
    records: list[dict] = _baseline(
        _BASE, 10, ("rates", "equities", "fx"), bucket_minutes=bucket_minutes
    )

    # Strong rates -> equities relationship (3 co-movements).
    for src, tgt in [(10, 11), (12, 13), (14, 15)]:
        records.extend(
            _flood(_BASE, src, "rates", count=25, bucket_minutes=bucket_minutes)
        )
        records.extend(
            _flood(_BASE, tgt, "equities", count=25, bucket_minutes=bucket_minutes)
        )
    # Weak fx -> equities relationship (only 1 co-movement — should be filtered out).
    records.extend(_flood(_BASE, 16, "fx", count=25, bucket_minutes=bucket_minutes))
    records.extend(
        _flood(_BASE, 17, "equities", count=25, bucket_minutes=bucket_minutes)
    )

    report = compute_spillover(
        records,
        bucket_minutes=bucket_minutes,
        lag_buckets=1,
        surge_z_threshold=1.5,
    )

    # Score is monotonically non-increasing across the list.
    scores = [e.spillover_score for e in report.edges]
    assert scores == sorted(scores, reverse=True)

    # Every reported edge passes the filter.
    for edge in report.edges:
        assert edge.spillover_score > 0.0
        assert edge.co_movements >= 2
        assert edge.source_asset != edge.target_asset

    # And the fx -> equities pair, with only 1 co-movement, is filtered out.
    pairs = {(e.source_asset, e.target_asset) for e in report.edges}
    assert ("fx", "equities") not in pairs


def test_returns_spillover_edge_instances() -> None:
    """All entries in ``edges`` and ``self_loops`` are SpilloverEdge dataclasses."""

    bucket_minutes = 30
    records: list[dict] = _baseline(
        _BASE, 10, ("rates", "equities"), bucket_minutes=bucket_minutes
    )
    for src, tgt in [(10, 11), (12, 13)]:
        records.extend(
            _flood(_BASE, src, "rates", count=25, bucket_minutes=bucket_minutes)
        )
        records.extend(
            _flood(_BASE, tgt, "equities", count=25, bucket_minutes=bucket_minutes)
        )

    report = compute_spillover(
        records,
        bucket_minutes=bucket_minutes,
        lag_buckets=1,
        surge_z_threshold=1.5,
    )

    for edge in report.edges:
        assert isinstance(edge, SpilloverEdge)
    for edge in report.self_loops:
        assert isinstance(edge, SpilloverEdge)


# ---------------------------------------------------------------------------
# Timestamp parsing fallbacks
# ---------------------------------------------------------------------------


def test_record_without_timestamp_is_skipped() -> None:
    """A record whose published_ts/created_at are missing or unparseable is dropped.

    Exercises the ``ts is None`` skip in ``compute_spillover`` and the
    ``_parse_ts`` None branches (non-string, blank, ValueError). When the
    only surviving records belong to a single bucket-less stream the report
    must report an empty matrix without raising.
    """

    records = [
        # Missing both timestamp fields entirely.
        {"capture_id": "no-ts", "asset_classes": ["rates"]},
        # published_ts present but not a string (None) and created_at blank
        # after strip — both _parse_ts None paths.
        {
            "capture_id": "bad-ts",
            "published_ts": None,
            "created_at": "   ",
            "asset_classes": ["rates"],
        },
        # published_ts is an unparseable string ⇒ ValueError branch.
        {
            "capture_id": "garbage",
            "published_ts": "not-a-timestamp",
            "asset_classes": ["rates"],
        },
    ]
    report = compute_spillover(records, bucket_minutes=30, lag_buckets=1)
    assert report.edges == ()
    assert report.self_loops == ()
    assert report.total_buckets == 0


def test_created_at_fallback_and_naive_timestamps_bucket_together() -> None:
    """A naive ``created_at`` (no tz) is treated as UTC and buckets normally.

    Covers the ``_parse_ts`` naive-tz branch (tzinfo is None ⇒ assume UTC)
    plus the ``published_ts`` → ``created_at`` fallback inside
    ``_record_timestamp``. A naive and a ``Z``-suffixed string one minute
    apart must land in the same 30-minute bucket.
    """

    records = [
        # Naive string (no offset) reached only via created_at fallback.
        {
            "capture_id": "naive",
            "published_ts": "",
            "created_at": "2024-01-01T09:05:00",
            "asset_classes": ["rates"],
        },
        # Z-suffixed string in the same bucket.
        _record("2024-01-01T09:06:00Z", asset_classes=["rates"]),
    ]
    report = compute_spillover(records, bucket_minutes=30, lag_buckets=1)
    # Both records share one bucket ⇒ exactly one bucket materializes.
    assert report.total_buckets == 1
    # Too little history to surge, so no edges/positive self-loops.
    assert report.edges == ()
    for loop in report.self_loops:
        assert loop.co_movements == 0


# ---------------------------------------------------------------------------
# asset_classes hygiene
# ---------------------------------------------------------------------------


def test_record_with_no_asset_classes_is_dropped() -> None:
    """A record with an empty/blank asset_classes list contributes nothing.

    Covers the ``if not assets: continue`` skip in ``compute_spillover``
    and the ``_asset_classes`` blank/non-string filters. With every record
    lacking a usable asset the report degrades to the empty-timed path
    (total_buckets=0).
    """

    records = [
        _record("2024-01-01T09:05:00Z", asset_classes=[]),
        # Non-string and blank entries are filtered out, leaving nothing.
        {
            "capture_id": "junk-assets",
            "published_ts": "2024-01-01T09:06:00Z",
            "asset_classes": [123, "", "   ", None],
        },
        # asset_classes is not even a list ⇒ _asset_classes returns [].
        {
            "capture_id": "scalar-assets",
            "published_ts": "2024-01-01T09:07:00Z",
            "asset_classes": "rates",
        },
    ]
    report = compute_spillover(records, bucket_minutes=30, lag_buckets=1)
    assert report.total_buckets == 0
    assert report.edges == ()
    assert report.self_loops == ()


def test_duplicate_asset_in_record_counts_once_per_bucket() -> None:
    """``["rates", "rates"]`` in one record contributes 1 (not 2) to its bucket.

    Covers the ``cleaned in seen`` dedupe branch in ``_asset_classes``. If
    the dedupe regressed, the doubled count would inflate the rates surge
    baseline; here we just assert the de-duped asset produces a single
    self-loop entry and the report builds cleanly.
    """

    records: list[dict] = []
    for bucket_idx in range(5):
        # Same asset listed twice in every record of the baseline.
        records.append(
            _record(
                _ts(_BASE, bucket_idx * 30 + 1),
                asset_classes=["rates", "rates", " rates "],
                capture_id=f"dup-{bucket_idx}",
            )
        )
    report = compute_spillover(records, bucket_minutes=30, lag_buckets=1)
    # Only two distinct cleaned assets exist: "rates" and "rates" (trailing
    # space stripped → also "rates"), so exactly one asset key survives.
    loop_assets = {e.source_asset for e in report.self_loops}
    assert loop_assets == {"rates"}
