"""Synthetic golden-set evaluation harness.

Ships a small, curated set of finance / non-finance items with multi-label
expectations. The harness scores the live pipeline against the labels and
reports precision/recall + per-label F1. Used by CLI `catchem benchmark`
and by the regression test suite.

The set is intentionally synthetic so it can ride in the repo without licensing
issues. When Kaggle assets are downloaded, the user can extend the golden set
manually under ``data/golden/extended.jsonl``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .schemas import AwarenessCaptureView
from .service import CatchemService


@dataclass(frozen=True)
class GoldenItem:
    """One golden capture with expected labels."""

    capture_id: str
    title: str
    text: str
    domain: str
    source_type: str
    expected_finance_relevant: bool
    expected_asset_classes: tuple[str, ...] = ()
    expected_reason_codes: tuple[str, ...] = ()
    expected_symbols: tuple[str, ...] = ()
    expected_sentiment: str | None = None   # "positive" | "negative" | "neutral"

    def as_capture(self) -> AwarenessCaptureView:
        now = datetime.now(UTC)
        return AwarenessCaptureView(
            capture_id=self.capture_id,
            doc_id=f"golden-{self.capture_id}",
            title=self.title,
            text=self.text,
            domain=self.domain,
            source_type=self.source_type,
            discovery_channel=f"golden:{self.source_type}",
            url=f"https://{self.domain}/{self.capture_id}",
            language="en",
            fetch_ts=now,
            observed_ts=now,
            published_ts=now,
            content_hash=f"gold-{self.capture_id}",
            robots_decision="not_applicable",
        )


# ── built-in synthetic set ──────────────────────────────────────────────────
# Pinned, hand-checked. When tuning the pipeline these are the contracts.
SYNTHETIC: tuple[GoldenItem, ...] = (
    # finance — central bank / rates
    GoldenItem(
        "g-fed-hike",
        "Federal Reserve raises rates by 25 bps amid sticky inflation",
        "The Federal Reserve raised its benchmark interest rate by 25 basis points on Wednesday, citing persistent inflation. Treasury yields jumped after the decision and Chair Powell said the central bank remains data-dependent.",
        "reuters.com", "rss",
        expected_finance_relevant=True,
        expected_asset_classes=("rates", "macro"),
        expected_reason_codes=("central_bank", "inflation"),
        expected_sentiment=None,
    ),
    GoldenItem(
        "g-fomc-minutes",
        "Minutes of the Federal Open Market Committee, March 17-18, 2026",
        "The Federal Open Market Committee released minutes from its March meeting. Officials discussed monetary policy, inflation risks, Treasury yields, and the path for interest rates.",
        "federalreserve.gov", "rss",
        expected_finance_relevant=True,
        expected_asset_classes=("rates", "macro"),
        expected_reason_codes=("central_bank", "inflation"),
        expected_sentiment=None,
    ),
    # finance — earnings + guidance + cashtag
    GoldenItem(
        "g-aapl-beat",
        "Apple beats earnings expectations and raises full-year guidance",
        "Apple Inc reported Q4 revenue above consensus and raised its full-year guidance. $AAPL rose 4% in after-hours trading on the news.",
        "wsj.com", "rss",
        expected_finance_relevant=True,
        expected_asset_classes=("equities",),
        expected_reason_codes=("earnings", "guidance"),
        expected_symbols=("AAPL",),
        expected_sentiment="positive",
    ),
    # finance — m_and_a
    GoldenItem(
        "g-msft-acq",
        "Microsoft to acquire a cybersecurity startup in a $4 billion all-cash deal",
        "Microsoft announced a definitive agreement to acquire the cybersecurity company in a $4bn all-cash takeover. The board has approved the merger and the deal is expected to close in Q2.",
        "ft.com", "rss",
        expected_finance_relevant=True,
        expected_asset_classes=("equities",),
        expected_reason_codes=("m_and_a",),
        expected_symbols=("MSFT",),
    ),
    # finance — energy / geopolitics
    GoldenItem(
        "g-oil-supply",
        "Oil prices surge as OPEC announces unexpected production cut",
        "Brent crude climbed above $90 a barrel after OPEC+ announced a surprise production cut. Energy stocks rallied while airlines fell on higher fuel cost expectations.",
        "bloomberg.com", "rss",
        expected_finance_relevant=True,
        expected_asset_classes=("commodities", "equities"),
        expected_reason_codes=("energy",),
        expected_sentiment=None,
    ),
    # finance — crypto
    GoldenItem(
        "g-btc-rally",
        "Bitcoin rallies past $80,000 amid ETF inflows",
        "Bitcoin pushed past the $80,000 mark as spot ETF flows accelerated. Ether and other altcoins also rose. Analysts attribute the move to institutional demand.",
        "coindesk.com", "rss",
        expected_finance_relevant=True,
        expected_asset_classes=("crypto",),
        expected_reason_codes=(),
        expected_symbols=("BTC-USD",),
        expected_sentiment="positive",
    ),
    # finance — regulation
    GoldenItem(
        "g-sec-fine",
        "SEC imposes record fine on bank for AML compliance failures",
        "The Securities and Exchange Commission fined a major US bank a record amount over anti-money-laundering compliance failures. The bank's shares fell on the news.",
        "wsj.com", "rss",
        expected_finance_relevant=True,
        expected_asset_classes=("equities",),
        expected_reason_codes=("regulation",),
        expected_sentiment="negative",
    ),
    # ─── non-finance (negative class) ──────────────────────────────────────
    # sports
    GoldenItem(
        "g-sports-final",
        "Local football team wins championship with last-minute goal",
        "The scoreboard told the story: a dramatic last-minute goal sealed the championship. Players celebrated with the trophy as fans rushed the field. The coach praised his squad.",
        "espn.com", "rss",
        expected_finance_relevant=False,
    ),
    # celebrity
    GoldenItem(
        "g-celeb-wedding",
        "Hollywood actor announces surprise wedding in Tuscany",
        "The actor shared photos from a private ceremony in Tuscany. Friends and family attended the celebrity wedding under tight security.",
        "tmz.com", "rss",
        expected_finance_relevant=False,
    ),
    # art / history (the historical false-positive class)
    GoldenItem(
        "g-art-restitution",
        "Painting looted by Nazis found in home of descendants of an SS officer",
        "A 17th-century portrait that was looted during the Nazi occupation has been recovered. The painting will be returned to the heirs of its original Jewish owners.",
        "bbc.com", "rss",
        expected_finance_relevant=False,
    ),
    # recipe
    GoldenItem(
        "g-recipe",
        "A perfect slow-cooked beef stew recipe for autumn evenings",
        "This recipe yields tender beef in a rich red-wine gravy. Serve with mashed potatoes and crusty bread.",
        "bbc.com", "sitemap",
        expected_finance_relevant=False,
    ),
    # human interest
    GoldenItem(
        "g-human-rescue",
        "Volunteers rescue stranded dog from icy river in dramatic operation",
        "Local volunteers worked together to free a dog that had been stuck on ice in a river. The dog is recovering well after the rescue.",
        "bbc.com", "rss",
        expected_finance_relevant=False,
    ),
    # tech product (borderline — release announcement w/o ticker)
    GoldenItem(
        "g-launch-generic",
        "Indie game studio releases new sci-fi adventure to positive reviews",
        "The small studio launched its long-awaited sci-fi adventure to positive reviews. Players praised the storytelling and visual design.",
        "polygon.com", "rss",
        expected_finance_relevant=False,
    ),
)


@dataclass
class LabelStats:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


GOLDEN_SCHEMA_VERSION = 1
"""Bump this when the golden output shape changes in a breaking way."""

REQUIRED_GOLDEN_FIELDS = (
    "capture_id",
    "title",
    "text",
    "expected_finance_relevant",
)


def validate_golden_row(row: object) -> dict:
    """Validate a raw dict from extended.jsonl. Raises ValueError on failure.

    We want loud failures on malformed extended golden sets so a stale or
    accidentally-truncated row doesn't silently destroy the metric.
    """
    if not isinstance(row, dict):
        raise ValueError(f"golden row must be a JSON object, got {type(row).__name__}")
    missing = [k for k in REQUIRED_GOLDEN_FIELDS if k not in row]
    if missing:
        raise ValueError(f"golden row missing required fields {missing}: {row.get('capture_id', '<no-id>')}")
    if not isinstance(row["expected_finance_relevant"], bool):
        raise ValueError(
            f"expected_finance_relevant must be bool, got {type(row['expected_finance_relevant']).__name__}"
        )
    for k in ("expected_asset_classes", "expected_reason_codes", "expected_symbols"):
        v = row.get(k)
        if v is not None and not isinstance(v, list):
            raise ValueError(f"{k} must be a list when present (golden id={row['capture_id']!r})")
    return row


@dataclass
class BenchmarkReport:
    relevance: LabelStats = field(default_factory=LabelStats)
    asset_class: dict[str, LabelStats] = field(default_factory=dict)
    reason_code: dict[str, LabelStats] = field(default_factory=dict)
    symbol_recall_hits: int = 0
    symbol_recall_total: int = 0
    sentiment_correct: int = 0
    sentiment_total: int = 0
    per_item: list[dict] = field(default_factory=list)
    dataset_name: str = "synthetic_v1"

    def to_dict(self) -> dict:
        return {
            "schema_version": GOLDEN_SCHEMA_VERSION,
            "dataset_name": self.dataset_name,
            "generated_at": datetime.now(UTC).isoformat(),
            "relevance": {"precision": self.relevance.precision, "recall": self.relevance.recall, "f1": self.relevance.f1},
            "asset_class_f1": {k: s.f1 for k, s in self.asset_class.items()},
            "reason_code_f1": {k: s.f1 for k, s in self.reason_code.items()},
            "symbol_recall": self.symbol_recall_hits / self.symbol_recall_total if self.symbol_recall_total else None,
            "sentiment_accuracy": self.sentiment_correct / self.sentiment_total if self.sentiment_total else None,
            "n": len(self.per_item),
            "per_item": self.per_item,
        }


def _stats_for_set(predicted: set[str], expected: set[str], bucket: dict[str, LabelStats]) -> None:
    for k in predicted & expected:
        bucket.setdefault(k, LabelStats()).tp += 1
    for k in predicted - expected:
        bucket.setdefault(k, LabelStats()).fp += 1
    for k in expected - predicted:
        bucket.setdefault(k, LabelStats()).fn += 1


def load_extended(path: Path, *, strict: bool = True) -> list[GoldenItem]:
    """Read additional golden items from a JSONL file. Optional.

    By default (`strict=True`), malformed rows raise ValueError loudly so a
    truncated or stale extended file does not silently degrade the metric.
    Pass `strict=False` to skip bad rows and continue.
    """
    if not path.exists():
        return []
    out: list[GoldenItem] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            if strict:
                raise ValueError(f"{path}:{lineno} — invalid JSON: {exc}") from exc
            continue
        try:
            data = validate_golden_row(data)
        except ValueError:
            if strict:
                raise
            continue
        out.append(GoldenItem(
            capture_id=str(data["capture_id"]),
            title=str(data["title"]),
            text=str(data["text"]),
            domain=str(data.get("domain", "unknown")),
            source_type=str(data.get("source_type", "rss")),
            expected_finance_relevant=bool(data["expected_finance_relevant"]),
            # `or []` collapses BOTH a missing key AND an explicit JSON null into
            # () — validate_golden_row permits `None` for these fields, so a bare
            # `tuple(data.get(k, []))` would crash with TypeError on `tuple(None)`
            # for a key-present/value-null row, defeating the "loud failures only"
            # contract by producing an opaque, location-less downstream crash.
            expected_asset_classes=tuple(data.get("expected_asset_classes") or []),
            expected_reason_codes=tuple(data.get("expected_reason_codes") or []),
            expected_symbols=tuple(data.get("expected_symbols") or []),
            expected_sentiment=data.get("expected_sentiment"),
        ))
    return out


def run_benchmark(svc: CatchemService, items: Iterable[GoldenItem] | None = None) -> BenchmarkReport:
    items = list(items if items is not None else SYNTHETIC)
    rep = BenchmarkReport()
    for it in items:
        cap = it.as_capture()
        rec = svc.process(cap)
        # relevance
        if it.expected_finance_relevant and rec.is_finance_relevant:
            rep.relevance.tp += 1
        elif it.expected_finance_relevant and not rec.is_finance_relevant:
            rep.relevance.fn += 1
        elif not it.expected_finance_relevant and rec.is_finance_relevant:
            rep.relevance.fp += 1
        # asset class / reason code
        _stats_for_set(set(rec.asset_classes), set(it.expected_asset_classes), rep.asset_class)
        _stats_for_set(set(rec.impact_reason_codes), set(it.expected_reason_codes), rep.reason_code)
        # symbol recall (per-expected-symbol)
        for sym in it.expected_symbols:
            rep.symbol_recall_total += 1
            if sym in set(rec.candidate_symbols):
                rep.symbol_recall_hits += 1
        # sentiment accuracy (only when expected_sentiment is set)
        if it.expected_sentiment is not None:
            rep.sentiment_total += 1
            if rec.sentiment_label and rec.sentiment_label.value == it.expected_sentiment:
                rep.sentiment_correct += 1
        rep.per_item.append({
            "capture_id": it.capture_id,
            "expected_finance_relevant": it.expected_finance_relevant,
            "predicted_finance_relevant": rec.is_finance_relevant,
            "score": rec.finance_relevance_score,
            "expected_asset_classes": list(it.expected_asset_classes),
            "predicted_asset_classes": rec.asset_classes,
            "expected_reason_codes": list(it.expected_reason_codes),
            "predicted_reason_codes": rec.impact_reason_codes,
        })
    return rep
