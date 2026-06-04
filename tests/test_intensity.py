"""Tests for ``catchem.quant.intensity``.

Each test pins one contract guarantee from the module docstring so a
regression points at exactly one expectation. The endpoint test
exercises the FastAPI envelope shape end-to-end via the TestClient.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem.quant.intensity import (
    IntensityBucket,
    _record_intensity,
    compute_by_scope,
    compute_overall,
)
from catchem.settings import load_settings, reload_settings

# ---------------------------------------------------------------------------
# _record_intensity
# ---------------------------------------------------------------------------


def test_record_intensity_multiplies_relevance_by_abs_sentiment() -> None:
    """intensity = relevance * |sentiment_score| — both halves contribute."""

    r = {"finance_relevance_score": 0.8, "sentiment_score": 0.6}
    assert _record_intensity(r) == pytest.approx(0.48)


def test_record_intensity_uses_absolute_sentiment() -> None:
    """A strongly bearish read scores the same magnitude as a bullish one."""

    pos = {"finance_relevance_score": 0.7, "sentiment_score": 0.9}
    neg = {"finance_relevance_score": 0.7, "sentiment_score": -0.9}
    assert _record_intensity(pos) == pytest.approx(_record_intensity(neg))


def test_record_intensity_missing_fields_are_zero() -> None:
    """Missing or None values coerce to 0.0 instead of raising."""

    assert _record_intensity({}) == 0.0
    assert _record_intensity({"finance_relevance_score": None, "sentiment_score": None}) == 0.0
    assert _record_intensity({"finance_relevance_score": "junk", "sentiment_score": "x"}) == 0.0
    # Bools must NOT silently coerce to 1.0 — guards against
    # sentiment flags accidentally becoming intensity inputs.
    assert _record_intensity({"finance_relevance_score": True, "sentiment_score": True}) == 0.0
    # Non-finite values must coerce to 0.0
    assert _record_intensity({"finance_relevance_score": float("nan"), "sentiment_score": 0.8}) == 0.0
    assert _record_intensity({"finance_relevance_score": 0.8, "sentiment_score": float("inf")}) == 0.0



# ---------------------------------------------------------------------------
# compute_overall
# ---------------------------------------------------------------------------


def test_compute_overall_empty_returns_zero_bucket() -> None:
    """No records → zero-valued bucket, canonical shape."""

    out = compute_overall([])
    assert isinstance(out, IntensityBucket)
    assert out.scope == "overall"
    assert out.sample_size == 0
    assert out.mean_intensity == 0.0
    assert out.max_intensity == 0.0
    assert out.count_high_intensity == 0
    assert out.top_records == []


def test_compute_overall_skips_non_dict_records() -> None:
    """Junk list members are skipped; only real dicts are aggregated."""

    records = [
        "not a record",
        42,
        None,
        {"finance_relevance_score": 0.9, "sentiment_score": 0.9},  # 0.81
    ]
    out = compute_overall(records)  # type: ignore[arg-type]
    assert out.sample_size == 1
    assert out.max_intensity == pytest.approx(0.81)


def test_compute_overall_all_non_dict_returns_zero_bucket() -> None:
    """A list with zero usable dicts hits the empty-pairs zero-bucket path."""

    out = compute_overall(["x", 1, None])  # type: ignore[arg-type]
    assert isinstance(out, IntensityBucket)
    assert out.scope == "overall"
    assert out.sample_size == 0
    assert out.mean_intensity == 0.0
    assert out.max_intensity == 0.0
    assert out.count_high_intensity == 0
    assert out.top_records == []


def test_compute_overall_counts_high_intensity_above_half() -> None:
    """count_high_intensity = number of records with intensity > 0.5."""

    records = [
        {"finance_relevance_score": 0.9, "sentiment_score": 0.9},   # 0.81 → high
        {"finance_relevance_score": 0.8, "sentiment_score": -0.8},  # 0.64 → high
        {"finance_relevance_score": 0.5, "sentiment_score": 0.5},   # 0.25 → not
        {"finance_relevance_score": 0.3, "sentiment_score": 0.1},   # 0.03 → not
        {"finance_relevance_score": 0.5, "sentiment_score": 0.0},   # 0.00 (neutral) → not
    ]
    out = compute_overall(records)
    assert out.sample_size == 5
    assert out.count_high_intensity == 2
    assert out.max_intensity == pytest.approx(0.81)
    # Mean is the simple arithmetic mean of all intensities.
    assert out.mean_intensity == pytest.approx((0.81 + 0.64 + 0.25 + 0.03 + 0.0) / 5)


# ---------------------------------------------------------------------------
# compute_by_scope
# ---------------------------------------------------------------------------


def test_compute_by_scope_buckets_and_sorts_by_mean_desc() -> None:
    """List-valued scope lifts every bucket; results sort by mean DESC."""

    records = [
        # equities: 0.81, 0.10  mean 0.455
        {"asset_classes": ["equities"], "finance_relevance_score": 0.9, "sentiment_score": 0.9},
        {"asset_classes": ["equities"], "finance_relevance_score": 0.5, "sentiment_score": 0.2},
        # crypto: 0.04  mean 0.04
        {"asset_classes": ["crypto"], "finance_relevance_score": 0.2, "sentiment_score": 0.2},
        # multi-asset story lifts BOTH equities and bonds.
        # equities + 0.49 → equities mean still highest
        # bonds 0.49 alone → mean 0.49 < equities new mean (0.81+0.10+0.49)/3 ≈ 0.466
        {
            "asset_classes": ["equities", "bonds"],
            "finance_relevance_score": 0.7,
            "sentiment_score": -0.7,
        },
    ]
    out = compute_by_scope(records, scope_key="asset_classes")
    by_scope = {b.scope: b for b in out}
    # Singular-prefix matches sentiment_dispersion convention.
    assert "asset_class:equities" in by_scope
    assert "asset_class:crypto" in by_scope
    assert "asset_class:bonds" in by_scope
    # Multi-asset story lifted equities to 3 samples, bonds to 1, crypto to 1.
    assert by_scope["asset_class:equities"].sample_size == 3
    assert by_scope["asset_class:bonds"].sample_size == 1
    assert by_scope["asset_class:crypto"].sample_size == 1
    # bonds has the highest mean (0.49 single record) since equities pulls down
    # via the 0.10 record. The first entry has the highest mean.
    assert out[0].mean_intensity >= out[1].mean_intensity >= out[2].mean_intensity


def test_compute_by_scope_top_records_capped_at_five() -> None:
    """top_records never exceeds 5 even with many high-intensity members."""

    records = [
        {
            "asset_classes": ["equities"],
            "finance_relevance_score": 0.9,
            "sentiment_score": 0.9,
            "capture_id": f"c{i}",
            "title": f"story {i}",
            "sentiment_label": "positive",
        }
        for i in range(20)
    ]
    out = compute_by_scope(records, scope_key="asset_classes")
    assert len(out) == 1
    assert out[0].sample_size == 20
    # Cap is hardcoded to 5 in _build_top_records.
    assert len(out[0].top_records) == 5
    # Each entry exposes the contract fields the UI consumes.
    sample = out[0].top_records[0]
    assert "capture_id" in sample
    assert "title" in sample
    assert "intensity" in sample
    assert "score" in sample
    assert "sentiment_label" in sample
    assert "sentiment_score" in sample


def test_compute_by_scope_skips_falsy_and_non_string_buckets() -> None:
    """None / "" / non-string scope values are skipped, never bucketed."""

    records = [
        {"asset_classes": "equities", "finance_relevance_score": 0.7, "sentiment_score": 0.6},
        {"asset_classes": None, "finance_relevance_score": 0.7, "sentiment_score": 0.6},
        {"asset_classes": "", "finance_relevance_score": 0.7, "sentiment_score": 0.6},
        # Non-strings (ints, dicts) are silently skipped to avoid garbage scopes.
        {"asset_classes": [42], "finance_relevance_score": 0.7, "sentiment_score": 0.6},
    ]
    out = compute_by_scope(records, scope_key="asset_classes")
    scopes = {b.scope for b in out}
    assert scopes == {"asset_class:equities"}
    assert not any(":None" in s or s.endswith(":") for s in scopes)


def test_compute_by_scope_skips_non_dict_records() -> None:
    """Junk list members don't crash the per-scope bucketer (line 197 path)."""

    records = [
        "not a record",
        42,
        None,
        {"asset_classes": ["equities"], "finance_relevance_score": 0.9, "sentiment_score": 0.9},
        {"asset_classes": ["equities"], "finance_relevance_score": 0.8, "sentiment_score": 0.8},
    ]
    out = compute_by_scope(records, scope_key="asset_classes")  # type: ignore[arg-type]
    by_scope = {b.scope: b for b in out}
    assert by_scope["asset_class:equities"].sample_size == 2


def test_compute_by_scope_empty_input_returns_empty_list() -> None:
    """No records → empty list (never a fake bucket)."""

    assert compute_by_scope([], scope_key="asset_classes") == []
    assert compute_by_scope([{}], scope_key="asset_classes") == []


def test_compute_by_scope_unknown_scope_key_falls_back_to_raw_prefix() -> None:
    """A scope_key not in _SCOPE_LABELS uses the raw key as the prefix."""

    records = [
        {"sectors": ["tech"], "finance_relevance_score": 0.9, "sentiment_score": 0.9},
    ]
    out = compute_by_scope(records, scope_key="sectors")
    assert out[0].scope == "sectors:tech"


# ---------------------------------------------------------------------------
# Endpoint envelope
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Boot the FastAPI app rooted at a fresh ``tmp_path`` so the
    supervisor's storage initializes cleanly and we don't touch real data.
    """

    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    reload_settings()
    from catchem.api import create_app

    app = create_app(load_settings())
    with TestClient(app) as tc:
        yield tc


def test_endpoint_overall_returns_valid_envelope(client: TestClient) -> None:
    """``scope=overall`` envelope: ``result`` populated, ``buckets`` null."""

    res = client.get("/api/quant/intensity?scope=overall&limit=50")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["schema_version"] == 1
    assert body["scope"] == "overall"
    assert body["buckets"] is None
    assert body["result"] is not None
    r = body["result"]
    for key in (
        "scope",
        "sample_size",
        "mean_intensity",
        "max_intensity",
        "count_high_intensity",
        "top_records",
    ):
        assert key in r, f"missing key {key}"
    assert isinstance(r["top_records"], list)
    assert len(r["top_records"]) <= 5


def test_endpoint_buckets_scope_returns_capped_list(client: TestClient) -> None:
    """``scope=asset_classes`` returns a list capped at 20 entries."""

    res = client.get("/api/quant/intensity?scope=asset_classes&limit=50")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["scope"] == "asset_classes"
    assert body["result"] is None
    assert isinstance(body["buckets"], list)
    assert len(body["buckets"]) <= 20


def test_endpoint_rejects_unknown_scope(client: TestClient) -> None:
    """Unknown scope values are rejected by the regex validator."""

    res = client.get("/api/quant/intensity?scope=garbage")
    assert res.status_code == 422


def test_finite_or_none_coverage() -> None:
    from catchem.quant.intensity import _finite_or_none

    assert _finite_or_none(None) is None
    assert _finite_or_none(True) is None
    assert _finite_or_none(False) is None
    assert _finite_or_none("not-a-float") is None
    assert _finite_or_none([1.2]) is None
    assert _finite_or_none(float("inf")) is None
    assert _finite_or_none(float("-inf")) is None
    assert _finite_or_none(float("nan")) is None
    assert _finite_or_none(1.23) == pytest.approx(1.23)


def test_compute_scope_buckets_empty_item_continue(monkeypatch) -> None:
    from collections import defaultdict

    import catchem.quant.intensity as intensity_mod

    def mock_defaultdict(*args, **kwargs):
        d = defaultdict(*args, **kwargs)
        d["empty_key"] = []
        return d

    monkeypatch.setattr(intensity_mod, "defaultdict", mock_defaultdict)

    records = [{"asset_classes": ["equities"], "finance_relevance_score": 0.5, "sentiment_score": 0.8}]
    buckets = intensity_mod.compute_by_scope(records, "asset_classes")
    scopes = [b.scope for b in buckets]
    assert "asset_class:empty_key" not in scopes
    assert "asset_class:equities" in scopes


def test_additional_coverage_for_optimization() -> None:
    from catchem.quant.intensity import _coerce_float, _finite_or_none, compute_by_scope, compute_overall

    # Coerce float with int
    assert _coerce_float(42) == 42.0
    assert _coerce_float("inf") == 0.0
    assert _coerce_float("nan") == 0.0
    assert _coerce_float("0.8") == 0.8

    # Finite or none with int
    assert _finite_or_none(42) == 42.0
    assert _finite_or_none("inf") is None
    assert _finite_or_none("nan") is None
    assert _finite_or_none("0.8") == 0.8

    # Compute by scope where subsequent item has higher intensity
    records = [
        {"asset_classes": ["equities"], "finance_relevance_score": 0.2, "sentiment_score": 0.2}, # intensity = 0.04
        {"asset_classes": ["equities"], "finance_relevance_score": 0.8, "sentiment_score": 0.8}, # intensity = 0.64
    ]
    buckets = compute_by_scope(records, "asset_classes")
    assert len(buckets) == 1
    assert buckets[0].max_intensity == pytest.approx(0.64)

    # compute_overall where subsequent item has higher intensity
    overall_records = [
        {"finance_relevance_score": 0.2, "sentiment_score": 0.2}, # 0.04
        {"finance_relevance_score": 0.8, "sentiment_score": 0.8}, # 0.64
    ]
    out = compute_overall(overall_records)
    assert out.max_intensity == pytest.approx(0.64)



