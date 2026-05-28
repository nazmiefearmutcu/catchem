"""Awareness Quant Lens.

Post-processing layer that adds high-depth analytics on top of catchem's
primary `FinancialImpactRecord` stream. Each submodule owns one signal;
this package's `__init__` re-exports the public surface so the supervisor
+ API + UI can wire to a single import.

Design priorities:
  * read-only against catchem storage — never mutates `records`
  * idempotent: same inputs → same outputs (caching is safe)
  * cheap-by-default: heavy ops gated behind explicit calls; nothing
    fires on every ingestion unless explicitly opted in
  * fail-soft: a quant signal that crashes is logged + skipped, not
    propagated back to the primary pipeline
"""

from __future__ import annotations

from .anomaly import (
    AnomalyReport,
    SentimentShock,
    SymbolBurst,
    VolumeAnomaly,
    detect_anomalies,
)
from .co_occurrence import (
    AssetConcentration,
    AssetReasonCell,
    CoOccurrenceReport,
    SymbolEdge,
    compute_co_occurrence,
)
from .engine import QuantEngine
from .event_clustering import EventCluster, cluster_records
from .lead_lag import (
    LeadLagReport,
    PerEventLeadLag,
    SourceLeadLagScore,
    attribute_lead_lag,
)
from .market_reaction import HorizonReturn, ReactionReport, compute_reaction
from .novelty import NoveltyResult, compute_novelty, score_corpus
from .sentiment_momentum import (
    SentimentBucket,
    SentimentMomentumReport,
    TickerMomentum,
    compute_sentiment_momentum,
)
from .source_reliability import SourceLeaderboard, SourceScore, compute_source_scores
from .spillover import SpilloverEdge, SpilloverReport, compute_spillover
from .topic_regime import RegimeBucket, RegimeReport, detect_regime_shifts

__all__ = [
    "AnomalyReport",
    "AssetConcentration",
    "AssetReasonCell",
    "CoOccurrenceReport",
    "EventCluster",
    "HorizonReturn",
    "LeadLagReport",
    "NoveltyResult",
    "PerEventLeadLag",
    "QuantEngine",
    "ReactionReport",
    "RegimeBucket",
    "RegimeReport",
    "SentimentBucket",
    "SentimentMomentumReport",
    "SentimentShock",
    "SourceLeaderboard",
    "SourceLeadLagScore",
    "SourceScore",
    "SpilloverEdge",
    "SpilloverReport",
    "SymbolBurst",
    "SymbolEdge",
    "TickerMomentum",
    "VolumeAnomaly",
    "attribute_lead_lag",
    "cluster_records",
    "compute_co_occurrence",
    "compute_novelty",
    "compute_reaction",
    "compute_sentiment_momentum",
    "compute_source_scores",
    "compute_spillover",
    "detect_anomalies",
    "detect_regime_shifts",
    "score_corpus",
]
