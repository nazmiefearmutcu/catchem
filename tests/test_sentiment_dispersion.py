"""Tests for ``catchem.quant.sentiment_dispersion``.

Each test pins one contract guarantee from the module docstring so a
regression there points at exactly one expectation. The endpoint test
exercises the FastAPI envelope shape end-to-end via the TestClient.
"""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem.quant.sentiment_dispersion import (
    DispersionResult,
    _dominant,
    compute_by_scope,
    compute_dispersion,
)
from catchem.settings import load_settings, reload_settings

MAX_H = math.log2(3)  # ≈ 1.585


# ---------------------------------------------------------------------------
# compute_dispersion
# ---------------------------------------------------------------------------


def test_empty_input_returns_zero_entropy() -> None:
    """No labelled sentiments → zero entropy, "tied" dominant, canonical counts."""

    r = compute_dispersion([])
    assert isinstance(r, DispersionResult)
    assert r.sample_size == 0
    assert r.entropy == 0.0
    assert r.normalized_entropy == 0.0
    assert r.dominant_label == "tied"
    # Canonical counts shape — UI indexes without defensive checks.
    assert r.counts == {"positive": 0, "neutral": 0, "negative": 0}
    assert r.max_entropy == pytest.approx(MAX_H)


def test_unanimous_labels_yield_zero_entropy() -> None:
    """One sentiment dominating 100 % → H = 0 and that label is dominant."""

    r = compute_dispersion(["positive"] * 10)
    assert r.sample_size == 10
    assert r.entropy == pytest.approx(0.0, abs=1e-12)
    assert r.normalized_entropy == pytest.approx(0.0, abs=1e-12)
    assert r.dominant_label == "positive"
    assert r.counts == {"positive": 10, "neutral": 0, "negative": 0}


def test_perfect_three_way_split_hits_max_entropy() -> None:
    """1/3 each → entropy ≈ log2(3) ≈ 1.585 and normalized ≈ 1.0."""

    r = compute_dispersion(["positive", "neutral", "negative"] * 5)
    assert r.sample_size == 15
    assert r.entropy == pytest.approx(MAX_H)
    assert r.normalized_entropy == pytest.approx(1.0)
    # All three tied — every count is 5, so dominant must be "tied".
    assert r.dominant_label == "tied"


def test_two_thirds_one_third_split() -> None:
    """2/3 vs 1/3 → H(2/3, 1/3, 0) ≈ 0.918, positive dominates."""

    r = compute_dispersion(["positive", "positive", "negative"])
    assert r.sample_size == 3
    assert r.entropy == pytest.approx(0.9182958340544896, rel=1e-6)
    # Normalized = 0.918... / 1.585 ≈ 0.579
    assert r.normalized_entropy == pytest.approx(0.5793801642856951, rel=1e-6)
    assert r.dominant_label == "positive"


def test_tied_dominant_label_when_two_top_counts_match() -> None:
    """Two labels tied for first place → dominant = "tied"."""

    r = compute_dispersion(["positive", "positive", "negative", "negative"])
    assert r.counts["positive"] == 2
    assert r.counts["negative"] == 2
    assert r.counts["neutral"] == 0
    assert r.dominant_label == "tied"


def test_invalid_labels_are_dropped() -> None:
    """None, "mixed", "" and non-string junk are silently skipped."""

    r = compute_dispersion([None, "mixed", "", 1, "positive", "negative"])  # type: ignore[list-item]
    assert r.sample_size == 2
    assert r.counts == {"positive": 1, "neutral": 0, "negative": 1}
    assert r.dominant_label == "tied"


# ---------------------------------------------------------------------------
# compute_by_scope
# ---------------------------------------------------------------------------


def test_compute_by_scope_buckets_by_asset_class_and_uses_singular_prefix() -> None:
    """A list-valued scope_key lifts every member; prefix is the singular form."""

    records = [
        {"sentiment_label": "positive", "asset_classes": ["equities"]},
        {"sentiment_label": "negative", "asset_classes": ["equities"]},
        {"sentiment_label": "positive", "asset_classes": ["crypto"]},
        {"sentiment_label": "positive", "asset_classes": ["crypto", "equities"]},
    ]
    out = compute_by_scope(records, scope_key="asset_classes")
    by_scope = {r.scope: r for r in out}
    # Equities saw 3 votes (1 pos + 1 neg + 1 pos), crypto saw 2 (1 pos + 1 pos).
    assert by_scope["asset_class:equities"].sample_size == 3
    assert by_scope["asset_class:crypto"].sample_size == 2
    # Sorted by sample size DESC.
    assert out[0].scope == "asset_class:equities"
    # Crypto unanimous → entropy 0; equities mixed → > 0.
    assert by_scope["asset_class:crypto"].entropy == pytest.approx(0.0, abs=1e-12)
    assert by_scope["asset_class:equities"].entropy > 0.0


def test_compute_by_scope_handles_scalar_and_falsy_values() -> None:
    """Scalar scope values still bucket; None / "" are skipped, not bucketed."""

    records = [
        {"sentiment_label": "positive", "actor_types": "regulator"},
        {"sentiment_label": "negative", "actor_types": "regulator"},
        {"sentiment_label": "positive", "actor_types": None},
        {"sentiment_label": "positive", "actor_types": ""},
        {"sentiment_label": "neutral", "actor_types": "corporate"},
    ]
    out = compute_by_scope(records, scope_key="actor_types")
    scopes = {r.scope for r in out}
    assert "actor:regulator" in scopes
    assert "actor:corporate" in scopes
    # Falsy buckets are not created.
    assert not any(":None" in s or s.endswith(":") for s in scopes)


def test_compute_by_scope_empty_input_returns_empty_list() -> None:
    """No records → empty list, never a fake "overall" entry."""

    assert compute_by_scope([], scope_key="asset_classes") == []
    assert compute_by_scope([{}], scope_key="asset_classes") == []


def test_dominant_empty_counter_is_tied() -> None:
    """An empty Counter falls through to "tied" (the not-counts guard)."""

    assert _dominant(Counter()) == "tied"


def test_dominant_single_label_returns_that_label() -> None:
    """A counter with a clear top label returns it (single-entry path)."""

    assert _dominant(Counter(["positive"])) == "positive"


def test_dominant_label_edge_cases() -> None:
    """Exercise all dominant label decision tree branches and _dominant tie path."""

    # 1. _dominant with tie
    assert _dominant(Counter(["positive", "negative"])) == "tied"

    # 2. pos > neu, pos < neg => dominant = "negative"
    r = compute_dispersion(["positive"] * 2 + ["neutral"] * 1 + ["negative"] * 3)
    assert r.dominant_label == "negative"

    # 3. pos < neu, neu < neg => dominant = "negative"
    r = compute_dispersion(["positive"] * 1 + ["neutral"] * 2 + ["negative"] * 3)
    assert r.dominant_label == "negative"

    # 4. pos < neu, neu == neg => dominant = "tied"
    r = compute_dispersion(["positive"] * 1 + ["neutral"] * 2 + ["negative"] * 2)
    assert r.dominant_label == "tied"

    # 5. pos == neu, pos < neg => dominant = "negative"
    r = compute_dispersion(["positive"] * 1 + ["neutral"] * 1 + ["negative"] * 2)
    assert r.dominant_label == "negative"


def test_compute_by_scope_skips_non_dict_records() -> None:
    """Junk list members (str / int / None) don't crash the bucketer."""

    records = [
        "not a record",
        42,
        None,
        {"sentiment_label": "positive", "asset_classes": ["equities"]},
        {"sentiment_label": "negative", "asset_classes": ["equities"]},
    ]
    out = compute_by_scope(records, scope_key="asset_classes")  # type: ignore[arg-type]
    by_scope = {r.scope: r for r in out}
    assert by_scope["asset_class:equities"].sample_size == 2


def test_compute_by_scope_skips_non_string_scope_members() -> None:
    """List members that are falsy / non-string are skipped (line 155 path)."""

    records = [
        {"sentiment_label": "positive", "asset_classes": ["equities", None, "", 42]},
        {"sentiment_label": "neutral", "asset_classes": ["equities"]},
    ]
    out = compute_by_scope(records, scope_key="asset_classes")
    scopes = {r.scope for r in out}
    # Only the valid "equities" string produced a bucket.
    assert scopes == {"asset_class:equities"}
    assert {r.scope: r for r in out}["asset_class:equities"].sample_size == 2


def test_compute_by_scope_all_invalid_sentiments_yield_tied_empty_bucket() -> None:
    """A bucket whose sentiments are all non-canonical reports the empty shape.

    This drives compute_dispersion's empty-input path from *inside*
    compute_by_scope: the scope still appears (the records exist) but the
    entropy result is the zero/"tied" canonical shape.
    """

    records = [
        {"sentiment_label": None, "asset_classes": ["equities"]},
        {"sentiment_label": "mixed", "asset_classes": ["equities"]},
    ]
    out = compute_by_scope(records, scope_key="asset_classes")
    assert len(out) == 1
    bucket = out[0]
    assert bucket.scope == "asset_class:equities"
    assert bucket.sample_size == 0
    assert bucket.entropy == 0.0
    assert bucket.dominant_label == "tied"
    assert bucket.counts == {"positive": 0, "neutral": 0, "negative": 0}


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
    # Use the context-manager form so FastAPI lifespan hooks run and the
    # supervisor is fully initialized before any request lands.
    with TestClient(app) as tc:
        yield tc


def test_endpoint_overall_returns_valid_envelope(client: TestClient) -> None:
    """``scope=overall`` envelope: ``result`` populated, ``buckets`` null."""

    res = client.get("/api/quant/sentiment-dispersion?scope=overall&limit=50")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["schema_version"] == 1
    assert body["scope"] == "overall"
    assert body["buckets"] is None
    assert body["result"] is not None
    r = body["result"]
    # Canonical counts dict.
    assert set(r["counts"].keys()) == {"positive", "neutral", "negative"}
    assert r["dominant_label"] in {"positive", "neutral", "negative", "tied"}
    assert 0.0 <= r["normalized_entropy"] <= 1.0 + 1e-9


def test_endpoint_buckets_scope_returns_list(client: TestClient) -> None:
    """``scope=asset_classes`` envelope: ``buckets`` is a list (possibly empty)."""

    res = client.get("/api/quant/sentiment-dispersion?scope=asset_classes&limit=50")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["scope"] == "asset_classes"
    assert body["result"] is None
    assert isinstance(body["buckets"], list)
    # Cap respected even if storage is small.
    assert len(body["buckets"]) <= 30


def test_endpoint_rejects_unknown_scope(client: TestClient) -> None:
    """Unknown scope values are rejected by the regex validator."""

    res = client.get("/api/quant/sentiment-dispersion?scope=garbage")
    assert res.status_code == 422
