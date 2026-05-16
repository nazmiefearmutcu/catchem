"""Regression test for the asset-class gate.

A capture with no asset-class hits and only a moderately-strong reason code
should NOT be flagged finance-relevant. Specifically pins the BBC "Nazi looted
portrait" case that snuck through in the v1 scoring.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fusion_stack.schemas import AwarenessCaptureView
from fusion_stack.service import build_service
from fusion_stack.settings import load_settings, reload_settings


@pytest.mark.regression
def test_nazi_looted_portrait_is_not_finance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Historical false-positive: a human-interest art story scored 0.355 — just
    above the floor — because reason_code_max(geopolitics) = 0.73 dragged the
    weighted sum over the line. The asset-class gate fixes this."""
    monkeypatch.setenv("FUSION_MODELS__USE_ML_STUBS", "true")
    reload_settings()

    svc = build_service(load_settings())
    cap = AwarenessCaptureView(
        capture_id="nazi-portrait",
        doc_id="nazi-portrait-doc",
        title="Portrait looted by Nazis found in home of Dutch SS leader's descendants",
        text=(
            "A 17th-century portrait that was looted during the Nazi occupation of the "
            "Netherlands has been found in the home of descendants of a Dutch SS leader. "
            "The painting will be returned to the heirs of its original Jewish owners."
        ),
        domain="bbc.com",
        source_type="rss",
        url="https://bbc.com/news/nazi-portrait",
        language="en",
        fetch_ts=datetime.now(timezone.utc),
        observed_ts=datetime.now(timezone.utc),
    )
    rec = svc.process(cap)
    assert rec.is_finance_relevant is False, (
        f"art-restitution story leaked through finance gate. components={rec.component_scores}"
    )
    assert rec.asset_classes == []


@pytest.mark.regression
def test_strong_geopolitics_without_assets_only_if_very_strong() -> None:
    """If geopolitics scores extremely high (≥0.85) we *do* allow relevance —
    a real geopolitical-shock headline like an invasion can move markets even
    without an explicit asset class in the title."""
    from fusion_stack.scoring import ScoringInputs, score
    from fusion_stack.taxonomy import default_taxonomy_path, load_taxonomy

    tax = load_taxonomy(default_taxonomy_path())
    out = score(
        ScoringInputs(
            prefilter_rule_score=0.6,
            domain_prior=0.9,
            source_type_prior=0.55,
            asset_class_scores={},
            reason_code_scores={"geopolitics": 0.90},
            negative_class_scores={},
            sentiment_confidence=0.5,
            entity_density=0.3,
        ),
        taxonomy=tax,
    )
    assert out.is_finance_relevant is True
    assert "geopolitics" in out.reason_codes_passed


@pytest.mark.regression
def test_finance_story_with_weak_asset_score_still_passes(synth_capture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUSION_MODELS__USE_ML_STUBS", "true")
    reload_settings()
    svc = build_service(load_settings())
    cap = synth_capture()  # Fed/rates story
    rec = svc.process(cap)
    assert rec.is_finance_relevant is True
