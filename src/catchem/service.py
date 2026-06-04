"""Pipeline orchestration: takes one AwarenessCaptureView, runs all stages,
produces one FinancialImpactRecord. The supervisor wraps this for batch/live."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from .chart_context import ChartContextReader
from .embeddings import Embedder, VectorIndex, make_embedder
from .entity_linker import EntityLinker
from .evidence import build_reason_text, clean_boilerplate_text, extract_evidence
from .finance_filter import FastPrefilter
from .logging import get_logger
from .newsimpact_guarded_adapter import NewsImpactGuardedAdapter, NewsImpactGuardError
from .reranker import Reranker, make_reranker
from .schemas import (
    AwarenessCaptureView,
    FinancialImpactRecord,
    ProcessingMode,
    SentimentLabel,
)
from .scoring import ScoringInputs, estimate_entity_density, score
from .sentiment import SentimentClassifier, make_sentiment
from .settings import CatchemMode, Settings
from .symbol_mapper import SymbolMapper
from .taxonomy import Taxonomy, default_taxonomy_path, load_taxonomy
from .zero_shot_classifier import ZeroShot, make_zero_shot

logger = get_logger("catchem.service")


_MODE_MAP = {
    CatchemMode.PRODUCTION_SAFE: ProcessingMode.PRODUCTION_SAFE,
    CatchemMode.REPLAY_EXISTING: ProcessingMode.REPLAY_EXISTING,
    CatchemMode.LIVE_TAIL: ProcessingMode.LIVE_TAIL,
    CatchemMode.RESEARCH_DIAGNOSTIC: ProcessingMode.RESEARCH_DIAGNOSTIC,
}


class CatchemService:
    """Stateful pipeline. Construct once per process; ``process`` per capture."""

    def __init__(
        self,
        settings: Settings,
        taxonomy: Taxonomy,
        vector_index: VectorIndex | None = None,
    ) -> None:
        self.settings = settings
        self.taxonomy = taxonomy

        use_stubs = bool(settings.models.use_ml_stubs)
        self.prefilter = FastPrefilter(taxonomy=taxonomy)
        self.zero_shot: ZeroShot = make_zero_shot(taxonomy, settings.models.zero_shot, use_stubs)
        self.sentiment: SentimentClassifier = make_sentiment(settings.models.sentiment_default, use_stubs)
        self.embedder: Embedder = make_embedder(settings.models.embedding, use_stubs)
        self.reranker: Reranker = make_reranker(settings.models.reranker, use_stubs)
        config_path = Path(__file__).resolve().parents[2] / "configs" / "symbols.yaml"
        self.symbol_mapper = SymbolMapper(
            config_path=config_path if config_path.exists() else None,
            newsimpact_root=settings.paths.newsimpact_repo
        )
        self.entity_linker = EntityLinker(company_aliases=self.symbol_mapper.alias_dict())
        self.chart_reader = ChartContextReader(settings.paths.newsimpact_repo)
        self.vector_index = vector_index

        # Diagnostic adapter is constructed lazily — only if both mode and flag agree.
        self._diagnostic_adapter: NewsImpactGuardedAdapter | None = None
        if settings.diagnostic_allowed():
            try:
                self._diagnostic_adapter = NewsImpactGuardedAdapter(
                    newsimpact_root=settings.paths.newsimpact_repo,
                    mode=settings.mode.value,
                    diagnostic_flag=settings.guards.newsimpact_diagnostic_enabled,
                    allow_modes=settings.guards.allow_research_diagnostic_in_modes,
                )
            except NewsImpactGuardError as exc:
                logger.warning("diagnostic_adapter_refused", reason=str(exc))
                self._diagnostic_adapter = None

    @property
    def diagnostic_enabled(self) -> bool:
        return self._diagnostic_adapter is not None

    @property
    def model_versions(self) -> Mapping[str, str]:
        return {
            "zero_shot": self.zero_shot.model_version,
            "sentiment": self.sentiment.model_version,
            "embedding": self.embedder.model_version,
            "reranker": self.reranker.model_version,
            "prefilter": "rule:v1",
            "scoring": "rule:v1",
        }

    # ── single-capture pipeline ─────────────────────────────────────────────
    def process(self, cap: AwarenessCaptureView) -> FinancialImpactRecord:
        # Stage A
        pre = self.prefilter.evaluate(cap)

        # Short-circuit: clear-non-finance items still get a record (with is_finance_relevant=False),
        # so the dashboard can show what was filtered out.
        zs = self.zero_shot.classify(cap)
        sent = self.sentiment.classify(cap)
        clean_text = clean_boilerplate_text(cap.text or "")
        ents = self.entity_linker.extract(cap.title, clean_text)

        # Symbol mapping over title (title is more discriminative)
        symbol_matches = self.symbol_mapper.map_text((cap.title or "") + "\n" + clean_text[:600])
        candidate_symbols = [m.symbol for m in symbol_matches]
        candidate_entities = ents.unique_texts()

        # Light reranking only when there are >1 symbol candidates
        if len(candidate_symbols) > 1:
            ranked = self.reranker.rank(cap.title or "", candidate_symbols)
            candidate_symbols = [c for c, _ in ranked]

        ac_scores = {k: v for k, v in zs.label_scores.items() if k in self.taxonomy.asset_class_ids}
        rc_scores = {k: v for k, v in zs.label_scores.items() if k in self.taxonomy.reason_code_ids}
        neg_scores = {k: v for k, v in zs.label_scores.items() if k in self.taxonomy.negative_class_ids}

        # BUG-BB: cashtag/ticker hits are a high-confidence "this is about a
        # specific tradeable equity" signal. Pre-fix the zero-shot stub
        # picked up "equities" only when the taxonomy alias set
        # (`stocks`/`shares`/`equity`) happened to appear in the text — a
        # press release saying `$AAPL rose 4% in after-hours trading on the
        # news` carried the ticker but none of the aliases, so `asset=[]`
        # came out of an obviously equities-relevant story. Bridge symbol
        # detection back into the asset-class layer so the channel mapping
        # and downstream consumers see equities.
        #
        # BUG-BB.1: the first cut treated ANY ticker hit as equity, but
        # EntityLinker also resolves crypto/FX/commodity/index aliases into
        # the ticker kind (Bitcoin → BTC-USD, EUR/USD → EURUSD=X, gold →
        # GC=F, S&P 500 → ^GSPC). A BTC-only headline then showed up with
        # asset_classes=['crypto', 'equities'] — a false equity tag. The
        # ticker format unambiguously encodes the asset class:
        #   - equity:   AAPL, MSFT, BRK.B  (plain uppercase, optional .X)
        #   - crypto:   *-USD              (suffix)
        #   - fx:       *=X                (suffix)
        #   - commodity:*=F                (suffix)
        #   - index:    ^*                 (prefix)
        # Cashtags ($TICKER) are unambiguous — $ is the equity convention.
        if "equities" in self.taxonomy.asset_class_ids:
            has_equity_hit = any(h.kind == "cashtag" for h in ents.hits) or any(
                h.kind == "ticker" and _looks_like_equity_ticker(h.text)
                for h in ents.hits
            )
            if has_equity_hit:
                ac_scores["equities"] = max(ac_scores.get("equities", 0.0), 0.6)

        # entity_density should only count finance-grounded hit kinds — generic
        # proper-noun runs ("Nazi", "Dutch SS leader") must not inflate it.
        finance_hit_kinds = {"cashtag", "ticker", "currency", "central_bank", "index", "commodity", "crypto"}
        finance_hits = sum(1 for h in ents.hits if h.kind in finance_hit_kinds)
        density = estimate_entity_density(num_hits=finance_hits, text_length=len(cap.text or ""))
        # Sentiment "non-neutralness": helpful for non-neutral signals.
        non_neutral = sent.score if sent.label in (SentimentLabel.POSITIVE, SentimentLabel.NEGATIVE) else 0.0

        scoring_outputs = score(
            ScoringInputs(
                prefilter_rule_score=pre.rule_score,
                domain_prior=pre.domain_prior,
                source_type_prior=pre.source_type_prior,
                asset_class_scores=ac_scores,
                reason_code_scores=rc_scores,
                negative_class_scores=neg_scores,
                sentiment_confidence=non_neutral,
                entity_density=density,
            ),
            taxonomy=self.taxonomy,
        )

        # Horizons: simple heuristic from reason codes.
        impact_horizons = _horizons_from_reasons(scoring_outputs.reason_codes_passed)

        # Evidence
        label_terms = (
            list(scoring_outputs.asset_classes_passed)
            + list(scoring_outputs.reason_codes_passed)
        )
        entity_terms = candidate_entities[:8]
        evidence = extract_evidence(cap, label_terms, entity_terms, top_k=self.taxonomy.threshold("evidence_top_k", 3))

        reason_text = build_reason_text(
            scoring_outputs.asset_classes_passed,
            scoring_outputs.reason_codes_passed,
            sent.label.value if sent.label != SentimentLabel.UNKNOWN else None,
        )

        # Optionally store embedding
        if self.vector_index is not None:
            try:
                vec = self.embedder.encode((cap.title or "") + "\n" + (cap.text or "")[:1500])
                self.vector_index.save(cap.capture_id, vec)
            except Exception as exc:
                logger.warning("embedding_save_failed", err=str(exc))

        # Diagnostic (research mode only)
        diag_payload = None
        if self._diagnostic_adapter is not None:
            diag_payload = self._diagnostic_adapter.diagnostic_payload(
                capture_id=cap.capture_id, text=cap.text
            )

        # Component scores from zero-shot too (keep top-3 per group for transparency)
        comp = dict(scoring_outputs.component_scores)
        for k, v in sorted(ac_scores.items(), key=lambda kv: -kv[1])[:3]:
            comp[f"ac_{k}"] = float(v)
        for k, v in sorted(rc_scores.items(), key=lambda kv: -kv[1])[:3]:
            comp[f"rc_{k}"] = float(v)
        if neg_scores:
            comp["neg_max"] = float(max(neg_scores.values()))

        text_excerpt = (cap.text or "")[: self.settings.replay.text_excerpt_chars] or (cap.title or "(no body)")

        return FinancialImpactRecord(
            capture_id=cap.capture_id,
            doc_id=cap.doc_id,
            title=cap.title,
            text_excerpt=text_excerpt,
            published_ts=cap.published_ts,
            domain=cap.domain,
            language=cap.language,
            url=cap.url,
            is_finance_relevant=bool(scoring_outputs.is_finance_relevant and pre.keep),
            finance_relevance_score=float(scoring_outputs.finance_relevance_score),
            asset_classes=list(scoring_outputs.asset_classes_passed),
            impact_reason_codes=list(scoring_outputs.reason_codes_passed),
            candidate_symbols=candidate_symbols[:8],
            candidate_entities=candidate_entities[:12],
            impact_horizons=impact_horizons,
            sentiment_label=sent.label,
            sentiment_score=float(sent.score),
            evidence_sentences=evidence,
            reason_text=reason_text,
            component_scores=comp,
            diagnostic_multimodal_enabled=self.diagnostic_enabled,
            diagnostic_multimodal_result=diag_payload,
            processing_mode=_MODE_MAP[self.settings.mode],
            model_versions=dict(self.model_versions),
            created_at=datetime.now(UTC),
        )


def _looks_like_equity_ticker(text: str) -> bool:
    """Return True when `text` matches the bare-uppercase equity ticker
    convention (AAPL, MSFT, BRK.B, GOOGL). Rejects crypto (`-USD`),
    FX (`=X`), commodity (`=F`), and index (`^TICKER`) formats — see
    `_INTERNAL_REGISTRY` in symbol_mapper for the canonical examples of
    each asset class.

    Equity tickers are 1-8 chars, uppercase, optionally with a dot-suffix
    for share classes. Empty / lowercase / suffixed / prefixed → False.
    """
    if not text or len(text) > 8:
        return False
    if text.startswith("^") or "=" in text or "-" in text:
        return False
    return all(c.isupper() or c == "." for c in text)


def _horizons_from_reasons(reasons: tuple[str, ...]) -> list[str]:
    if not reasons:
        return []
    out: set[str] = set()
    # BUG-AA: `product_launch` was the only reason code in the taxonomy
    # (28 total) silently dropped by this mapping — product-launch records
    # surfaced with `impact_horizons=[]`. Launches typically move the stock
    # intraday/one_day on the announcement.
    short_term = {"central_bank", "earnings", "guidance", "cyber_outage", "natural_disaster",
                  "fraud_governance", "litigation", "product_launch"}
    one_week = {"m_and_a", "regulation", "sanctions_trade", "supply_chain", "energy", "metals"}
    structural = {"inflation", "growth_recession", "employment", "esg_reputation", "funding_liquidity",
                  "geopolitics"}
    for r in reasons:
        if r in short_term:
            out.update({"intraday", "one_day"})
        if r in one_week:
            out.add("one_week")
        if r in structural:
            out.add("structural")
    return sorted(out)


def _horizon_buckets() -> tuple[set[str], set[str], set[str]]:
    """Exposed for test access. Mirrors the buckets in `_horizons_from_reasons`."""
    short_term = {"central_bank", "earnings", "guidance", "cyber_outage", "natural_disaster",
                  "fraud_governance", "litigation", "product_launch"}
    one_week = {"m_and_a", "regulation", "sanctions_trade", "supply_chain", "energy", "metals"}
    structural = {"inflation", "growth_recession", "employment", "esg_reputation", "funding_liquidity",
                  "geopolitics"}
    return short_term, one_week, structural


def build_service(settings: Settings, vector_index: VectorIndex | None = None) -> CatchemService:
    taxonomy = load_taxonomy(default_taxonomy_path())
    return CatchemService(settings=settings, taxonomy=taxonomy, vector_index=vector_index)
