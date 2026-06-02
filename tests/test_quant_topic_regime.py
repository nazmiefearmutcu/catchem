"""Tests for `catchem.quant.topic_regime`.

Each test owns one contract guarantee from the module docstring so a
regression there points at exactly one expectation.
"""

from __future__ import annotations

import math

import pytest

from catchem.quant.topic_regime import (
    RegimeReport,
    detect_regime_shifts,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record(
    published_ts: str,
    *,
    asset_classes: list[str] | None = None,
    impact_reason_codes: list[str] | None = None,
    finance_relevance_score: float = 0.5,
    sentiment_label: str | None = "neutral",
    capture_id: str | None = None,
) -> dict:
    """Build a minimal FinancialImpactRecord-shaped dict.

    Only the fields topic_regime actually reads are populated; the rest
    intentionally stay absent so we exercise the ``record.get`` paths.
    """

    return {
        "capture_id": capture_id or f"cap-{published_ts}",
        "published_ts": published_ts,
        "created_at": published_ts,
        "asset_classes": list(asset_classes or []),
        "impact_reason_codes": list(impact_reason_codes or []),
        "finance_relevance_score": finance_relevance_score,
        "sentiment_label": sentiment_label,
    }


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_records_yields_empty_report() -> None:
    """No records ⇒ no buckets, no shifts, but params still echoed."""

    report = detect_regime_shifts([], bucket_minutes=30, shift_threshold=0.4)
    assert isinstance(report, RegimeReport)
    assert report.buckets == ()
    assert report.detected_shifts == ()
    assert report.bucket_minutes == 30
    assert report.shift_threshold == 0.4


def test_invalid_bucket_minutes_raises() -> None:
    """Zero / negative widths are nonsensical and must raise."""

    with pytest.raises(ValueError):
        detect_regime_shifts([], bucket_minutes=0)
    with pytest.raises(ValueError):
        detect_regime_shifts([], bucket_minutes=-15)


# ---------------------------------------------------------------------------
# Single-bucket invariants
# ---------------------------------------------------------------------------


def test_single_bucket_has_no_kl_and_no_shift() -> None:
    """First bucket has no previous to compare to ⇒ KL is None, shift False."""

    records = [
        _record("2024-01-01T09:05:00Z", asset_classes=["equities"]),
        _record("2024-01-01T09:12:00Z", asset_classes=["equities"]),
    ]
    report = detect_regime_shifts(records, bucket_minutes=60)
    assert len(report.buckets) == 1
    bucket = report.buckets[0]
    assert bucket.kl_divergence_from_prev is None
    assert bucket.is_regime_shift is False
    assert report.detected_shifts == ()


# ---------------------------------------------------------------------------
# Stable / shifting regimes
# ---------------------------------------------------------------------------


def test_identical_distributions_produce_near_zero_kl() -> None:
    """Two buckets with the same asset+reason mix ⇒ KL ≈ 0."""

    records = [
        # Bucket 1 (09:00-10:00)
        _record(
            "2024-01-01T09:10:00Z",
            asset_classes=["equities"],
            impact_reason_codes=["earnings"],
        ),
        _record(
            "2024-01-01T09:45:00Z",
            asset_classes=["equities"],
            impact_reason_codes=["earnings"],
        ),
        # Bucket 2 (10:00-11:00) — same mix
        _record(
            "2024-01-01T10:05:00Z",
            asset_classes=["equities"],
            impact_reason_codes=["earnings"],
        ),
        _record(
            "2024-01-01T10:30:00Z",
            asset_classes=["equities"],
            impact_reason_codes=["earnings"],
        ),
    ]
    # min_records_per_bucket=1 here because the synthetic case is exactly
    # 2 records per bucket — we want the math (KL on disjoint distributions
    # exceeding the threshold) to fire without the sparse-bucket gate
    # kicking in. Production callers leave the default 3 so 1-2-record
    # buckets don't dominate the shift list (see module docstring).
    report = detect_regime_shifts(records, bucket_minutes=60, shift_threshold=0.4, min_records_per_bucket=1)
    assert len(report.buckets) == 2
    kl = report.buckets[1].kl_divergence_from_prev
    assert kl is not None
    assert kl == pytest.approx(0.0, abs=1e-9)
    assert report.buckets[1].is_regime_shift is False
    assert report.detected_shifts == ()


def test_disjoint_distributions_trigger_shift() -> None:
    """Bucket 1 = all equities/earnings, Bucket 2 = all rates/central_bank ⇒ KL high."""

    records = [
        # Bucket 1 — equities + earnings
        _record(
            "2024-01-01T09:10:00Z",
            asset_classes=["equities"],
            impact_reason_codes=["earnings"],
        ),
        _record(
            "2024-01-01T09:30:00Z",
            asset_classes=["equities"],
            impact_reason_codes=["earnings"],
        ),
        # Bucket 2 — rates + central_bank
        _record(
            "2024-01-01T10:05:00Z",
            asset_classes=["rates"],
            impact_reason_codes=["central_bank"],
        ),
        _record(
            "2024-01-01T10:40:00Z",
            asset_classes=["rates"],
            impact_reason_codes=["central_bank"],
        ),
    ]
    # min_records_per_bucket=1 here because the synthetic case is exactly
    # 2 records per bucket — we want the math (KL on disjoint distributions
    # exceeding the threshold) to fire without the sparse-bucket gate
    # kicking in. Production callers leave the default 3 so 1-2-record
    # buckets don't dominate the shift list (see module docstring).
    report = detect_regime_shifts(records, bucket_minutes=60, shift_threshold=0.4, min_records_per_bucket=1)
    assert len(report.buckets) == 2
    second = report.buckets[1]
    assert second.kl_divergence_from_prev is not None
    # Concrete sanity: with epsilon=1e-3 on disjoint vocab, KL is large.
    assert second.kl_divergence_from_prev > 0.4
    assert second.is_regime_shift is True
    assert report.detected_shifts == (second.bucket_start,)


# ---------------------------------------------------------------------------
# Per-bucket math
# ---------------------------------------------------------------------------


def test_mean_relevance_arithmetic() -> None:
    """Mean is plain arithmetic mean of the finance_relevance_score field."""

    records = [
        _record("2024-01-01T09:01:00Z", finance_relevance_score=0.20),
        _record("2024-01-01T09:02:00Z", finance_relevance_score=0.80),
        _record("2024-01-01T09:03:00Z", finance_relevance_score=0.50),
    ]
    report = detect_regime_shifts(records, bucket_minutes=60)
    assert len(report.buckets) == 1
    assert report.buckets[0].mean_relevance == pytest.approx(0.5, abs=1e-9)


def test_asset_distribution_uses_mass_spreading_and_top_n() -> None:
    """One record with two asset_classes contributes 0.5 to each, top-N capped."""

    records = [
        # 1 record splits 50/50 between equities and rates
        _record(
            "2024-01-01T09:01:00Z",
            asset_classes=["equities", "rates"],
        ),
        # 1 record fully on equities
        _record(
            "2024-01-01T09:02:00Z",
            asset_classes=["equities"],
        ),
    ]
    report = detect_regime_shifts(records, bucket_minutes=60)
    asset_dist = dict(report.buckets[0].asset_distribution)
    # equities: 0.5 + 1.0 = 1.5 ; rates: 0.5 ; total = 2.0
    assert asset_dist["equities"] == pytest.approx(0.75, abs=1e-9)
    assert asset_dist["rates"] == pytest.approx(0.25, abs=1e-9)
    # Sorted DESC by probability
    assert report.buckets[0].asset_distribution[0][0] == "equities"


def test_sentiment_distribution_skips_none_in_denominator() -> None:
    """Records with sentiment_label=None must not dilute the labelled ones."""

    records = [
        _record("2024-01-01T09:01:00Z", sentiment_label="positive"),
        _record("2024-01-01T09:02:00Z", sentiment_label="positive"),
        _record("2024-01-01T09:03:00Z", sentiment_label=None),
    ]
    report = detect_regime_shifts(records, bucket_minutes=60)
    sentiment = dict(report.buckets[0].sentiment_distribution)
    # 2 labelled, both positive ⇒ positive=1.0, others=0.0
    assert sentiment["positive"] == pytest.approx(1.0, abs=1e-9)
    assert sentiment["neutral"] == pytest.approx(0.0, abs=1e-9)
    assert sentiment["negative"] == pytest.approx(0.0, abs=1e-9)


def test_empty_asset_classes_contribute_no_mass() -> None:
    """Records with no asset_classes must not dilute the distribution."""

    records = [
        _record("2024-01-01T09:01:00Z", asset_classes=[]),
        _record("2024-01-01T09:02:00Z", asset_classes=["equities"]),
    ]
    report = detect_regime_shifts(records, bucket_minutes=60)
    asset_dist = dict(report.buckets[0].asset_distribution)
    assert asset_dist == {"equities": pytest.approx(1.0, abs=1e-9)}


# ---------------------------------------------------------------------------
# Bucket-boundary anchoring
# ---------------------------------------------------------------------------


def test_bucket_boundaries_floor_to_bucket_minutes() -> None:
    """Boundaries must align to the floor of bucket_minutes, not to the first ts."""

    # First record at 09:17 with bucket_minutes=15 should floor to 09:15:00,
    # NOT 09:17:00. This is the contract that lets two overlapping reports
    # share boundaries.
    records = [
        _record(
            "2024-01-01T09:17:00Z",
            asset_classes=["equities"],
        ),
        _record(
            "2024-01-01T09:22:00Z",
            asset_classes=["equities"],
        ),
        # New bucket starts at 09:30
        _record(
            "2024-01-01T09:31:00Z",
            asset_classes=["rates"],
        ),
    ]
    report = detect_regime_shifts(records, bucket_minutes=15)
    assert len(report.buckets) == 2
    assert report.buckets[0].bucket_start == "2024-01-01T09:15:00+00:00"
    assert report.buckets[0].bucket_end == "2024-01-01T09:30:00+00:00"
    assert report.buckets[1].bucket_start == "2024-01-01T09:30:00+00:00"
    assert report.buckets[1].bucket_end == "2024-01-01T09:45:00+00:00"


def test_falls_back_to_created_at_when_published_ts_missing() -> None:
    """If published_ts is unparseable, created_at is used for bucketing."""

    record = {
        "capture_id": "x",
        "published_ts": None,
        "created_at": "2024-01-01T09:20:00Z",
        "asset_classes": ["equities"],
        "impact_reason_codes": ["earnings"],
        "finance_relevance_score": 0.5,
        "sentiment_label": "neutral",
    }
    report = detect_regime_shifts([record], bucket_minutes=60)
    assert len(report.buckets) == 1
    assert report.buckets[0].bucket_start == "2024-01-01T09:00:00+00:00"


def test_records_without_any_timestamp_are_dropped() -> None:
    """Records with neither published_ts nor created_at must be skipped silently."""

    records = [
        {"capture_id": "ghost", "asset_classes": ["equities"]},
        _record("2024-01-01T09:01:00Z", asset_classes=["equities"]),
    ]
    report = detect_regime_shifts(records, bucket_minutes=60)
    assert len(report.buckets) == 1
    assert report.buckets[0].record_count == 1


# ---------------------------------------------------------------------------
# KL-math sanity
# ---------------------------------------------------------------------------


def test_kl_is_natural_log_and_uses_combined_keyspace() -> None:
    """For two single-key disjoint distributions, the closed form KL holds.

    Bucket 1 asset = {equities: 1.0}, reason = {earnings: 1.0}
    Bucket 2 asset = {rates: 1.0},    reason = {central_bank: 1.0}

    Combined key space (post-prefix) = {asset:equities, asset:rates,
                                        reason:earnings, reason:central_bank}.
    After epsilon=1e-3 smoothing each side and renormalizing each to 1.0,
    the KL is finite and identical for both directions (since the shape
    is symmetric). We assert it matches the closed form to rule out a
    base-2-log regression.
    """

    eps = 1e-3
    # Combined raw mass per side = 2.0 (one for asset, one for reason).
    # After smoothing, p has 2 keys with mass 1.0 and 2 keys with mass eps;
    # total = 2 + 2*eps; renormalized:
    p_high = 1.0 / (2.0 + 2.0 * eps)
    p_low = eps / (2.0 + 2.0 * eps)
    # q is the mirror image (swap which keys carry the heavy mass).
    expected_kl = 2.0 * p_high * math.log(p_high / p_low) + 2.0 * p_low * math.log(
        p_low / p_high
    )

    records = [
        _record(
            "2024-01-01T09:10:00Z",
            asset_classes=["equities"],
            impact_reason_codes=["earnings"],
        ),
        _record(
            "2024-01-01T10:05:00Z",
            asset_classes=["rates"],
            impact_reason_codes=["central_bank"],
        ),
    ]
    report = detect_regime_shifts(records, bucket_minutes=60, shift_threshold=10.0)
    second = report.buckets[1]
    assert second.kl_divergence_from_prev is not None
    assert second.kl_divergence_from_prev == pytest.approx(expected_kl, rel=1e-6)
    # shift_threshold=10.0 ⇒ even huge KL should NOT count as a shift here.
    assert second.is_regime_shift is False
    assert report.detected_shifts == ()


# ---------------------------------------------------------------------------
# Distribution-source hygiene
# ---------------------------------------------------------------------------


def test_naive_timestamp_buckets_as_utc() -> None:
    """A naive (offset-less) ISO timestamp is interpreted as UTC for bucketing.

    Covers the ``_parse_ts`` no-``Z`` path and the ``parsed.tzinfo is None``
    naive→UTC branch. 09:20 with bucket_minutes=60 floors to 09:00 UTC.
    """

    record = {
        "capture_id": "naive",
        "published_ts": "2024-01-01T09:20:00",  # no Z, no offset
        "created_at": "2024-01-01T09:20:00",
        "asset_classes": ["equities"],
        "impact_reason_codes": ["earnings"],
        "finance_relevance_score": 0.5,
        "sentiment_label": "neutral",
    }
    report = detect_regime_shifts([record], bucket_minutes=60)
    assert len(report.buckets) == 1
    assert report.buckets[0].bucket_start == "2024-01-01T09:00:00+00:00"


def test_unparseable_timestamp_record_is_dropped() -> None:
    """A record whose only timestamp is an unparseable string is skipped.

    Covers the ``_parse_ts`` ValueError branch (fromisoformat raising). The
    garbage-timestamp record drops out, leaving the one valid record.
    """

    records = [
        {
            "capture_id": "garbage",
            "published_ts": "definitely-not-iso",
            "created_at": "also-bad",
            "asset_classes": ["rates"],
        },
        _record("2024-01-01T09:01:00Z", asset_classes=["equities"]),
    ]
    report = detect_regime_shifts(records, bucket_minutes=60)
    assert len(report.buckets) == 1
    assert report.buckets[0].record_count == 1
    asset_dist = dict(report.buckets[0].asset_distribution)
    assert asset_dist == {"equities": pytest.approx(1.0, abs=1e-9)}


def test_non_list_distribution_fields_are_ignored() -> None:
    """``asset_classes``/``impact_reason_codes`` that aren't lists contribute nothing.

    Covers the ``not isinstance(values, (list, tuple))`` guard in
    ``_mass_spread``. A scalar string in those fields must NOT be iterated
    character-by-character; the bucket's asset distribution should ignore it
    entirely and the only valid record drives the result.
    """

    records = [
        {
            "capture_id": "scalar",
            "published_ts": "2024-01-01T09:01:00Z",
            "created_at": "2024-01-01T09:01:00Z",
            # Both fields are scalars, not lists — must be skipped wholesale.
            "asset_classes": "equities",
            "impact_reason_codes": "earnings",
            "finance_relevance_score": 0.5,
            "sentiment_label": "neutral",
        },
        _record(
            "2024-01-01T09:02:00Z",
            asset_classes=["rates"],
            impact_reason_codes=["central_bank"],
        ),
    ]
    report = detect_regime_shifts(records, bucket_minutes=60)
    assert len(report.buckets) == 1
    asset_dist = dict(report.buckets[0].asset_distribution)
    # The scalar "equities" string was ignored ⇒ only "rates" survives.
    assert asset_dist == {"rates": pytest.approx(1.0, abs=1e-9)}
    reason_dist = dict(report.buckets[0].reason_distribution)
    assert reason_dist == {"central_bank": pytest.approx(1.0, abs=1e-9)}


def test_unlabelled_bucket_yields_zeroed_sentiment() -> None:
    """A bucket where no record carries a known sentiment label ⇒ all zeros.

    Covers the ``labelled == 0`` early return in ``_sentiment_distribution``.
    An unrecognized label ("mixed") is not in the canonical vocab and must
    not be counted, leaving the distribution at the all-zero baseline.
    """

    records = [
        _record("2024-01-01T09:01:00Z", sentiment_label=None),
        _record("2024-01-01T09:02:00Z", sentiment_label="mixed"),
    ]
    report = detect_regime_shifts(records, bucket_minutes=60)
    sentiment = dict(report.buckets[0].sentiment_distribution)
    assert sentiment == {
        "positive": pytest.approx(0.0),
        "neutral": pytest.approx(0.0),
        "negative": pytest.approx(0.0),
    }


def test_mean_relevance_skips_bool_and_non_numeric() -> None:
    """``finance_relevance_score`` that is bool or non-numeric is excluded.

    Covers the bool guard (bool subclasses int) and the implicit skip of
    non-(int|float) values in ``_mean_relevance``. Only the genuine numeric
    score should drive the mean; ``True`` must NOT be read as ``1.0``.
    """

    records = [
        # bool — must be skipped despite isinstance(True, int) being True.
        _record("2024-01-01T09:01:00Z", finance_relevance_score=True),
        # non-numeric string — skipped.
        _record("2024-01-01T09:02:00Z", finance_relevance_score="high"),
        # the only real number ⇒ mean must equal exactly this.
        _record("2024-01-01T09:03:00Z", finance_relevance_score=0.42),
    ]
    report = detect_regime_shifts(records, bucket_minutes=60)
    assert report.buckets[0].mean_relevance == pytest.approx(0.42, abs=1e-9)


def test_mean_relevance_zero_when_no_numeric_scores() -> None:
    """A bucket with zero usable scores reports mean_relevance 0.0.

    Covers the ``if not scores: return 0.0`` branch in ``_mean_relevance``.
    """

    records = [
        _record("2024-01-01T09:01:00Z", finance_relevance_score=None),
        _record("2024-01-01T09:02:00Z", finance_relevance_score=False),
    ]
    report = detect_regime_shifts(records, bucket_minutes=60)
    assert report.buckets[0].mean_relevance == 0.0


# ---------------------------------------------------------------------------
# Empty-distribution KL path
# ---------------------------------------------------------------------------


def test_consecutive_topicless_buckets_have_zero_kl_no_shift() -> None:
    """Two buckets that carry records but no asset/reason topics ⇒ KL 0, no shift.

    Both buckets' asset and reason distributions normalize to ``{}`` (every
    record has empty list fields). ``_combine_for_kl`` then yields empty
    dicts, ``_smooth_pair`` hits its empty-keyspace return ``({}, {})``, and
    ``_kl_divergence`` loops over nothing ⇒ 0.0. The shift must NOT fire even
    though both buckets are well-populated.
    """

    records = [
        # Bucket 1 (09:00-10:00) — 3 records, no asset/reason topics.
        _record("2024-01-01T09:05:00Z", asset_classes=[], impact_reason_codes=[]),
        _record("2024-01-01T09:15:00Z", asset_classes=[], impact_reason_codes=[]),
        _record("2024-01-01T09:25:00Z", asset_classes=[], impact_reason_codes=[]),
        # Bucket 2 (10:00-11:00) — same shape.
        _record("2024-01-01T10:05:00Z", asset_classes=[], impact_reason_codes=[]),
        _record("2024-01-01T10:15:00Z", asset_classes=[], impact_reason_codes=[]),
        _record("2024-01-01T10:25:00Z", asset_classes=[], impact_reason_codes=[]),
    ]
    report = detect_regime_shifts(
        records, bucket_minutes=60, shift_threshold=0.4, min_records_per_bucket=3
    )
    assert len(report.buckets) == 2
    second = report.buckets[1]
    # Both distributions empty ⇒ KL computed over empty keyspace is 0.0.
    assert second.kl_divergence_from_prev == pytest.approx(0.0, abs=1e-12)
    assert second.is_regime_shift is False
    assert report.detected_shifts == ()


def test_sparse_buckets_report_kl_but_do_not_fire_shift() -> None:
    """Below ``min_records_per_bucket`` the KL is reported but no shift fires.

    Covers the ``adequate`` quality gate: disjoint topics across two
    1-record buckets give a large KL, yet with the default
    ``min_records_per_bucket=3`` the shift is suppressed. This pins the
    sparse-tail false-positive guard described in the module docstring.
    """

    records = [
        _record(
            "2024-01-01T09:10:00Z",
            asset_classes=["equities"],
            impact_reason_codes=["earnings"],
        ),
        _record(
            "2024-01-01T10:05:00Z",
            asset_classes=["rates"],
            impact_reason_codes=["central_bank"],
        ),
    ]
    report = detect_regime_shifts(
        records, bucket_minutes=60, shift_threshold=0.4, min_records_per_bucket=3
    )
    second = report.buckets[1]
    # KL is large (disjoint vocab) yet the shift is gated off by sparsity.
    assert second.kl_divergence_from_prev is not None
    assert second.kl_divergence_from_prev > 0.4
    assert second.is_regime_shift is False
    assert report.detected_shifts == ()


def test_topic_regime_internal_helpers_coverage() -> None:
    from catchem.quant.topic_regime import _smooth_pair, _kl_divergence

    # 1. _smooth_pair with negative values to cause total_p <= 0.0
    sp_p, sp_q = _smooth_pair({"a": -1.0}, {"b": -1.0})
    assert sp_p == {}
    assert sp_q == {}

    # 2. _kl_divergence with p_i <= 0.0
    assert _kl_divergence({"a": -0.5}, {"a": 0.5}) == 0.0

    # 3. _kl_divergence with q_i <= 0.0
    assert _kl_divergence({"a": 0.5}, {"a": -0.5}) == 0.0

