"""Dedicated unit tests for :mod:`catchem.service`.

These complement the integration/smoke suites
(``test_service_replay_mode.py`` / ``test_service_live_mode_smoke.py``) which
drive the pipeline through :class:`~catchem.supervisor.Supervisor`. Here we
construct :class:`~catchem.service.CatchemService` directly with deterministic,
fully-offline stub models and pin the wiring + branch behaviour that the
integration tests skip:

  * ``build_service`` loads the bundled taxonomy and returns a usable service.
  * ``model_versions`` advertises the stub model versions.
  * the cashtag/ticker → equities asset-class bridge (BUG-BB / BUG-BB.1).
  * the optional ``VectorIndex`` save path (happy + failure-swallowed).
  * the guarded NewsImpact diagnostic adapter (construct + refuse).
  * the pure helper functions ``_looks_like_equity_ticker`` /
    ``_horizons_from_reasons`` / ``_horizon_buckets``.

All models run as stubs (``settings.models.use_ml_stubs=True``) so there is no
network access and the output is deterministic. The conftest ``isolated_env``
fixture already scrubs env + sets ``CATCHEM_MODELS__USE_ML_STUBS=true``; we also
build explicit Settings objects where a non-default mode is required.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import numpy as np
import pytest

from catchem.embeddings import VectorIndex
from catchem.schemas import FinancialImpactRecord, ProcessingMode, SentimentLabel
from catchem.service import (
    CatchemService,
    _horizon_buckets,
    _horizons_from_reasons,
    _looks_like_equity_ticker,
    build_service,
)
from catchem.settings import (
    CatchemMode,
    GuardConfig,
    ModelConfig,
    Settings,
    load_settings,
)
from catchem.taxonomy import default_taxonomy_path, load_taxonomy


# ── shared helpers ──────────────────────────────────────────────────────────
def _offline_settings(
    *,
    mode: CatchemMode = CatchemMode.PRODUCTION_SAFE,
    newsimpact_repo: Path | None = None,
    diagnostic_enabled: bool = False,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> Settings:
    """A deterministic, offline Settings: stub models, no real repos.

    The conftest ``isolated_env`` fixture exports ``CATCHEM_PATHS__NEWSIMPACT_REPO``
    into the process env, and pydantic-settings ranks env ABOVE constructor
    kwargs for nested fields — so a ``PathConfig(newsimpact_repo=...)`` init arg
    is silently shadowed. When a test needs a specific repo it passes a
    ``monkeypatch`` and we set the env var (the idiomatic override in this
    codebase, mirroring ``test_service_replay_mode.py``).
    """
    if newsimpact_repo is not None and monkeypatch is not None:
        monkeypatch.setenv("CATCHEM_PATHS__NEWSIMPACT_REPO", str(newsimpact_repo))
    return Settings(
        mode=mode,
        models=ModelConfig(use_ml_stubs=True),
        guards=GuardConfig(newsimpact_diagnostic_enabled=diagnostic_enabled),
    )


def _write_governance_index(root: Path, *, release_gate_passed: bool) -> Path:
    """Write a minimal NewsImpact governance index the guard adapter can read."""
    idx = root / "models" / "governance_index" / "governance_index.json"
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_text(
        json.dumps(
            {
                "candidates": [
                    {
                        "governance_status": "QUARANTINED",
                        "fusion_verdict_class": "FUSION_REGRESSIVE",
                        "forbidden_operations": ["export", "promotion"],
                        "gate_failure_status": {"release_gate_passed": release_gate_passed},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return idx


# ── build_service factory ───────────────────────────────────────────────────
def test_build_service_returns_wired_service(tmp_settings: Settings) -> None:
    """build_service loads the bundled taxonomy and wires every stage."""
    svc = build_service(tmp_settings)
    assert isinstance(svc, CatchemService)
    assert svc.taxonomy.asset_class_ids, "taxonomy must be populated"
    # Component stages are all present.
    assert svc.prefilter is not None
    assert svc.zero_shot is not None
    assert svc.sentiment is not None
    assert svc.embedder is not None
    assert svc.reranker is not None
    assert svc.symbol_mapper is not None
    assert svc.entity_linker is not None
    assert svc.chart_reader is not None
    # No vector index passed → stays None; no diagnostic in production_safe.
    assert svc.vector_index is None
    assert svc.diagnostic_enabled is False


def test_build_service_threads_vector_index(tmp_path: Path, tmp_settings: Settings) -> None:
    """A VectorIndex passed to build_service is threaded onto the service."""
    vi = VectorIndex(tmp_path / "vec")
    svc = build_service(tmp_settings, vector_index=vi)
    assert svc.vector_index is vi


def test_model_versions_advertise_stub_versions(tmp_settings: Settings) -> None:
    svc = build_service(tmp_settings)
    mv = svc.model_versions
    assert mv["zero_shot"].startswith("stub-")
    assert mv["sentiment"].startswith("stub-")
    assert mv["embedding"].startswith("stub-")
    assert mv["reranker"].startswith("stub-")
    assert mv["prefilter"] == "rule:v1"
    assert mv["scoring"] == "rule:v1"


# ── process() pipeline output ───────────────────────────────────────────────
def test_process_produces_record_with_expected_shape(synth_capture) -> None:
    """A finance-relevant capture yields a populated FinancialImpactRecord."""
    svc = build_service(_offline_settings())
    rec = svc.process(synth_capture())
    assert isinstance(rec, FinancialImpactRecord)
    assert rec.capture_id == "cap-001"
    assert rec.processing_mode == ProcessingMode.PRODUCTION_SAFE
    assert rec.is_finance_relevant is True
    assert rec.model_versions["zero_shot"].startswith("stub-")
    assert rec.diagnostic_multimodal_enabled is False
    assert rec.diagnostic_multimodal_result is None


def test_process_non_finance_capture_marked_not_relevant(synth_non_finance_capture) -> None:
    """A sports story still produces a record, flagged not finance-relevant."""
    svc = build_service(_offline_settings())
    rec = svc.process(synth_non_finance_capture)
    assert isinstance(rec, FinancialImpactRecord)
    assert rec.is_finance_relevant is False


def test_negative_class_capture_records_neg_max(synth_capture) -> None:
    """A capture scoring a negative class surfaces a ``neg_max`` component
    score (covers the ``if neg_scores`` transparency branch in process)."""
    svc = build_service(_offline_settings())
    cap = synth_capture(
        capture_id="c-sports",
        doc_id="d-sports",
        domain="espn.com",
        title="Sports roundup: championship final recap",
        text="A sports story about the championship match and athletic competition.",
    )
    rec = svc.process(cap)
    assert "neg_max" in rec.component_scores
    assert rec.component_scores["neg_max"] > 0.0


def test_no_equity_hit_leaves_asset_classes_unbridged(synth_capture) -> None:
    """When equities is in the taxonomy but the capture carries no cashtag /
    equity ticker, the BUG-BB bridge must NOT inject 'equities' (covers the
    has_equity_hit=False branch, 141->151)."""
    svc = build_service(_offline_settings())
    assert "equities" in svc.taxonomy.asset_class_ids
    cap = synth_capture(
        capture_id="c-noeq",
        doc_id="d-noeq",
        title="The Federal Reserve raised interest rates by 25 basis points",
        text="Policymakers cited persistent inflation; Treasury yields rose.",
    )
    rec = svc.process(cap)
    # No cashtag / bare-equity ticker in the text → equities not force-added by
    # the bridge. (Macro/central-bank story, no specific tradeable equity.)
    assert not any(h.kind == "cashtag" for h in svc.entity_linker.extract(cap.title, cap.text or "").hits)
    assert "equities" not in rec.asset_classes


def test_bridge_skipped_when_taxonomy_lacks_equities(synth_capture) -> None:
    """If the taxonomy has no 'equities' asset class, the BUG-BB bridge block
    is skipped entirely even for a cashtag capture (covers the 141->151 branch
    where the outer ``if 'equities' in asset_class_ids`` is False)."""
    tax = load_taxonomy(default_taxonomy_path())
    no_equities = dataclasses.replace(
        tax, asset_classes=tuple(d for d in tax.asset_classes if d.id != "equities")
    )
    assert "equities" not in no_equities.asset_class_ids
    svc = CatchemService(settings=_offline_settings(), taxonomy=no_equities)
    cap = synth_capture(
        capture_id="c-noeqtax",
        doc_id="d-noeqtax",
        title="$AAPL rose 4% in after-hours trading on the news",
        text="Shares moved sharply after the announcement.",
    )
    rec = svc.process(cap)
    # The symbol still resolves, but the asset class can never be 'equities'
    # because the taxonomy doesn't define it — the bridge had nothing to add to.
    assert "AAPL" in rec.candidate_symbols
    assert "equities" not in rec.asset_classes


def test_cashtag_bridges_into_equities_asset_class(synth_capture) -> None:
    """BUG-BB: a $TICKER cashtag forces the equities asset class even when no
    equities alias word appears in the text."""
    svc = build_service(_offline_settings())
    cap = synth_capture(
        capture_id="c-cash",
        doc_id="d-cash",
        title="$AAPL rose 4% in after-hours trading on the news",
        text="Shares of the company moved sharply after the announcement.",
    )
    rec = svc.process(cap)
    assert "equities" in rec.asset_classes
    assert "AAPL" in rec.candidate_symbols


def test_reranker_path_runs_with_multiple_symbols(synth_capture) -> None:
    """>1 symbol candidates routes through the reranker (covers the branch)."""
    svc = build_service(_offline_settings())
    cap = synth_capture(
        capture_id="c-multi",
        doc_id="d-multi",
        title="$AAPL and $MSFT both rallied as $GOOGL reported earnings",
        text="Megacap technology stocks advanced after a strong session.",
    )
    rec = svc.process(cap)
    # All three cashtags resolve; reranked list is non-empty and unique.
    assert len(rec.candidate_symbols) >= 2
    assert len(set(rec.candidate_symbols)) == len(rec.candidate_symbols)


def test_process_saves_embedding_when_vector_index_present(
    tmp_path: Path, synth_capture
) -> None:
    """The optional embedding-save path runs and persists a vector."""
    vi = VectorIndex(tmp_path / "vec")
    svc = build_service(_offline_settings(), vector_index=vi)
    rec = svc.process(synth_capture(capture_id="c-emb", doc_id="d-emb"))
    saved = vi.load("c-emb")
    assert saved is not None
    assert saved.shape[0] > 0
    assert rec.capture_id == "c-emb"


def test_process_swallows_embedding_failure(
    tmp_path: Path, synth_capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An embedder/index failure is logged-and-swallowed — process still
    returns a record (covers the try/except around vector_index.save)."""
    vi = VectorIndex(tmp_path / "vec")

    def boom(_cid: str, _vec: np.ndarray) -> None:
        raise RuntimeError("synthetic vector-store failure")

    monkeypatch.setattr(vi, "save", boom)
    svc = build_service(_offline_settings(), vector_index=vi)
    rec = svc.process(synth_capture(capture_id="c-fail", doc_id="d-fail"))
    assert isinstance(rec, FinancialImpactRecord)
    assert rec.capture_id == "c-fail"
    # Nothing was persisted because the save raised.
    assert vi.load("c-fail") is None


# ── guarded NewsImpact diagnostic adapter ───────────────────────────────────
def test_diagnostic_adapter_constructs_in_research_mode(
    tmp_path: Path, synth_capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """research_diagnostic mode + flag on + gate-not-passed → adapter wired,
    and process() attaches the diagnostic payload."""
    repo = tmp_path / "merged_news"
    _write_governance_index(repo, release_gate_passed=False)
    settings = _offline_settings(
        mode=CatchemMode.RESEARCH_DIAGNOSTIC,
        newsimpact_repo=repo,
        diagnostic_enabled=True,
        monkeypatch=monkeypatch,
    )
    assert settings.diagnostic_allowed() is True

    svc = build_service(settings)
    assert svc.diagnostic_enabled is True

    rec = svc.process(synth_capture(capture_id="c-diag", doc_id="d-diag"))
    assert rec.diagnostic_multimodal_enabled is True
    payload = rec.diagnostic_multimodal_result
    assert payload is not None
    assert payload["is_research_diagnostic"] is True
    assert payload["may_override_finance_relevance"] is False
    assert payload["capture_id"] == "c-diag"
    assert rec.processing_mode == ProcessingMode.RESEARCH_DIAGNOSTIC


def test_diagnostic_adapter_refused_when_gate_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the governance gate reports *passed* (the unexpected state), the
    adapter raises internally and the service degrades to diagnostic-off
    rather than crashing (covers the except NewsImpactGuardError branch)."""
    repo = tmp_path / "merged_news"
    _write_governance_index(repo, release_gate_passed=True)
    settings = _offline_settings(
        mode=CatchemMode.RESEARCH_DIAGNOSTIC,
        newsimpact_repo=repo,
        diagnostic_enabled=True,
        monkeypatch=monkeypatch,
    )
    svc = build_service(settings)
    # Construction did not raise; the adapter just refused to attach.
    assert svc.diagnostic_enabled is False


def test_diagnostic_not_allowed_in_production_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with the flag on, production_safe never builds the adapter."""
    repo = tmp_path / "merged_news"
    _write_governance_index(repo, release_gate_passed=False)
    settings = _offline_settings(
        mode=CatchemMode.PRODUCTION_SAFE,
        newsimpact_repo=repo,
        diagnostic_enabled=True,
        monkeypatch=monkeypatch,
    )
    assert settings.diagnostic_allowed() is False
    svc = build_service(settings)
    assert svc.diagnostic_enabled is False


# ── pure helpers ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("ticker", "expected"),
    [
        ("AAPL", True),
        ("MSFT", True),
        ("BRK.B", True),
        ("GOOGL", True),
        ("BTC-USD", False),   # crypto suffix
        ("EURUSD=X", False),  # fx suffix
        ("GC=F", False),      # commodity suffix
        ("^GSPC", False),     # index prefix
        ("", False),          # empty
        ("ABCDEFGHI", False), # >8 chars
        ("aapl", False),      # lowercase
    ],
)
def test_looks_like_equity_ticker(ticker: str, expected: bool) -> None:
    assert _looks_like_equity_ticker(ticker) is expected


def test_horizons_from_reasons_buckets() -> None:
    assert _horizons_from_reasons(()) == []
    assert _horizons_from_reasons(("earnings",)) == ["intraday", "one_day"]
    # BUG-AA: product_launch must map to the short-term horizon, not [].
    assert _horizons_from_reasons(("product_launch",)) == ["intraday", "one_day"]
    assert _horizons_from_reasons(("m_and_a",)) == ["one_week"]
    assert _horizons_from_reasons(("inflation",)) == ["structural"]
    # A reason in no bucket contributes nothing.
    assert _horizons_from_reasons(("totally_unknown_reason",)) == []
    # Multiple reasons union + sort.
    multi = _horizons_from_reasons(("earnings", "m_and_a", "inflation"))
    assert multi == sorted(set(multi))
    assert {"intraday", "one_day", "one_week", "structural"} == set(multi)


def test_horizon_buckets_mirror_mapping() -> None:
    short_term, one_week, structural = _horizon_buckets()
    assert "product_launch" in short_term
    assert "m_and_a" in one_week
    assert "inflation" in structural
    # The three buckets are disjoint.
    assert short_term.isdisjoint(one_week)
    assert short_term.isdisjoint(structural)
    assert one_week.isdisjoint(structural)


# ── direct constructor (no factory) ─────────────────────────────────────────
def test_constructor_accepts_prebuilt_taxonomy(synth_capture) -> None:
    """CatchemService can be constructed directly with an explicit taxonomy —
    the path build_service wraps."""
    tax = load_taxonomy(default_taxonomy_path())
    svc = CatchemService(settings=_offline_settings(), taxonomy=tax)
    rec = svc.process(synth_capture())
    assert rec.sentiment_label in set(SentimentLabel)
    assert isinstance(rec.finance_relevance_score, float)


def test_load_settings_smoke_build(tmp_settings: Settings) -> None:
    """Sanity: the cached load_settings() path also feeds build_service."""
    # tmp_settings already reloaded the cache via conftest; this exercises the
    # public load_settings() accessor → build_service round-trip.
    svc = build_service(load_settings())
    assert isinstance(svc, CatchemService)
