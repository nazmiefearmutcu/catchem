"""Co-occurrence graph over catchem's `FinancialImpactRecord` stream.

Three views fused into one report:

  (a) ``asset_class`` × ``impact_reason_code`` — counts plus a *lift*
      score (observed / expected under independence). Lift > 1 means the
      pair shows up more often than independent margins would predict.
  (b) ``candidate_symbols`` ↔ ``candidate_symbols`` — undirected edges
      between symbols co-mentioned in the same record. Edge weight is the
      number of records mentioning both endpoints.
  (c) Per-``asset_class`` reason-mix concentration via the Herfindahl
      index over reason-share probabilities. 1.0 = one reason dominates;
      a low value = the asset's news is reason-diverse.

The module is pure-function and stdlib-only. It never mutates the input
records and produces deterministic output for a given input.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import combinations
from typing import Any

__all__ = [
    "AssetConcentration",
    "AssetReasonCell",
    "CoOccurrenceReport",
    "SymbolEdge",
    "compute_co_occurrence",
]


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssetReasonCell:
    """One cell of the asset×reason contingency table."""

    asset_class: str
    reason_code: str
    count: int
    lift: float
    mean_relevance: float


@dataclass(frozen=True)
class SymbolEdge:
    """One undirected co-mention edge between two ``candidate_symbols``."""

    symbol_a: str
    symbol_b: str
    weight: int
    sample_capture_ids: tuple[str, ...]


@dataclass(frozen=True)
class AssetConcentration:
    """How concentrated an asset_class's reason-mix is (Herfindahl)."""

    asset_class: str
    record_count: int
    reason_count: int
    herfindahl_index: float
    top_reasons: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class CoOccurrenceReport:
    """The fused report returned by :func:`compute_co_occurrence`."""

    total_records: int
    distinct_assets: int
    distinct_reasons: int
    distinct_symbols: int
    asset_reason_cells: tuple[AssetReasonCell, ...]
    strong_edges: tuple[SymbolEdge, ...]
    asset_concentration: tuple[AssetConcentration, ...]


# ---------------------------------------------------------------------------
# Helpers — input coercion (mirrors event_clustering's defensive style)
# ---------------------------------------------------------------------------


def _as_iter(value: Any) -> Iterable[Any]:
    """Coerce list-ish fields into a safe iterable. None → empty."""
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return value
    return (value,)


def _clean_set(values: Any) -> frozenset[str]:
    """Return a frozenset of non-empty, stripped strings."""
    out: set[str] = set()
    for v in _as_iter(values):
        if v is None:
            continue
        s = str(v).strip()
        if s:
            out.add(s)
    return frozenset(out)


def _clean_symbols(values: Any) -> frozenset[str]:
    """Symbols are case-insensitive — normalize to upper for matching."""
    return frozenset(s.upper() for s in _clean_set(values))


def _relevance(record: dict) -> float | None:
    """Return finance_relevance_score as a float, or None when missing/bad."""
    v = record.get("finance_relevance_score")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _capture_id(record: dict) -> str:
    """Stringified capture_id (defensive against ints / None)."""
    cid = record.get("capture_id", "")
    return str(cid) if cid is not None else ""


# ---------------------------------------------------------------------------
# (a) asset × reason cells with lift
# ---------------------------------------------------------------------------


def _build_asset_reason_cells(
    records: list[dict],
    *,
    min_pair_count: int,
    top_n: int,
) -> tuple[AssetReasonCell, ...]:
    """Tally (asset, reason) pairs and score each cell by lift."""
    # Each record contributes the cartesian product of asset×reason,
    # but only once per (record, asset, reason) — so duplicates inside a
    # single record's lists do not double-count.
    pair_counts: Counter[tuple[str, str]] = Counter()
    pair_relevance: dict[tuple[str, str], list[float]] = {}
    row_total: Counter[str] = Counter()
    col_total: Counter[str] = Counter()

    for rec in records:
        assets = _clean_set(rec.get("asset_classes"))
        reasons = _clean_set(rec.get("impact_reason_codes"))
        if not assets or not reasons:
            continue
        rel = _relevance(rec)
        for asset in assets:
            for reason in reasons:
                key = (asset, reason)
                pair_counts[key] += 1
                row_total[asset] += 1
                col_total[reason] += 1
                if rel is not None:
                    pair_relevance.setdefault(key, []).append(rel)

    total = sum(pair_counts.values())
    if total == 0:
        return ()

    cells: list[AssetReasonCell] = []
    for (asset, reason), count in pair_counts.items():
        if count < min_pair_count:
            continue
        r_tot = row_total.get(asset, 0)
        c_tot = col_total.get(reason, 0)
        # Empty margin => can't form an "expected" — treat as neutral.
        if r_tot == 0 or c_tot == 0:
            lift = 1.0
        else:
            # lift = (count * total) / (row_total * col_total)
            lift = (count * total) / (r_tot * c_tot)
        rels = pair_relevance.get((asset, reason), [])
        mean_rel = sum(rels) / len(rels) if rels else 0.0
        cells.append(
            AssetReasonCell(
                asset_class=asset,
                reason_code=reason,
                count=count,
                lift=lift,
                mean_relevance=mean_rel,
            )
        )

    # Deterministic sort: lift DESC, then count DESC, then alpha asset, reason.
    cells.sort(
        key=lambda c: (-c.lift, -c.count, c.asset_class, c.reason_code)
    )
    return tuple(cells[:top_n])


# ---------------------------------------------------------------------------
# (b) symbol ↔ symbol edges
# ---------------------------------------------------------------------------


def _build_symbol_edges(
    records: list[dict],
    *,
    min_edge_weight: int,
    top_n: int,
) -> tuple[SymbolEdge, ...]:
    """Tally undirected co-mention edges between candidate_symbols."""
    edge_weight: Counter[tuple[str, str]] = Counter()
    # Up-to-3 sample capture_ids per edge, recorded in encounter order so
    # tests can pin "first three records I saw this edge in".
    edge_samples: dict[tuple[str, str], list[str]] = {}

    for rec in records:
        symbols = _clean_symbols(rec.get("candidate_symbols"))
        if len(symbols) < 2:
            continue
        cid = _capture_id(rec)
        # Sort once so combinations() produces lex-ordered pairs directly.
        ordered = sorted(symbols)
        for sym_a, sym_b in combinations(ordered, 2):
            key = (sym_a, sym_b)
            edge_weight[key] += 1
            if cid:
                bucket = edge_samples.setdefault(key, [])
                if len(bucket) < 3 and cid not in bucket:
                    bucket.append(cid)

    edges: list[SymbolEdge] = []
    for (sym_a, sym_b), weight in edge_weight.items():
        if weight < min_edge_weight:
            continue
        edges.append(
            SymbolEdge(
                symbol_a=sym_a,
                symbol_b=sym_b,
                weight=weight,
                sample_capture_ids=tuple(edge_samples.get((sym_a, sym_b), [])),
            )
        )

    # Deterministic sort: weight DESC, then alpha by (symbol_a, symbol_b).
    edges.sort(key=lambda e: (-e.weight, e.symbol_a, e.symbol_b))
    return tuple(edges[:top_n])


# ---------------------------------------------------------------------------
# (c) per-asset reason concentration (Herfindahl)
# ---------------------------------------------------------------------------


def _build_asset_concentration(
    records: list[dict],
) -> tuple[AssetConcentration, ...]:
    """Per-asset reason-mix concentration via Herfindahl index."""
    # For each asset, count both records (for record_count) and (asset,reason)
    # pair occurrences (for share probabilities). Per record, an asset and a
    # reason each contribute once even if duplicated in the list.
    asset_records: Counter[str] = Counter()
    asset_reason_counts: dict[str, Counter[str]] = {}

    for rec in records:
        assets = _clean_set(rec.get("asset_classes"))
        if not assets:
            continue
        reasons = _clean_set(rec.get("impact_reason_codes"))
        for asset in assets:
            asset_records[asset] += 1
            if not reasons:
                continue
            bucket = asset_reason_counts.setdefault(asset, Counter())
            for reason in reasons:
                bucket[reason] += 1

    out: list[AssetConcentration] = []
    for asset, rec_count in asset_records.items():
        reason_counter = asset_reason_counts.get(asset, Counter())
        reason_total = sum(reason_counter.values())
        if reason_total == 0:
            # No reasons ever co-occurred with this asset — concentration
            # is undefined; report neutral defaults.
            out.append(
                AssetConcentration(
                    asset_class=asset,
                    record_count=rec_count,
                    reason_count=0,
                    herfindahl_index=0.0,
                    top_reasons=(),
                )
            )
            continue

        # Share probabilities, then sum-of-squares = Herfindahl.
        shares: list[tuple[str, float]] = [
            (reason, count / reason_total)
            for reason, count in reason_counter.items()
        ]
        # float clamp to [0,1] — a single reason yields exactly 1.0 by math
        # but rounding could nudge it; defensive clamp keeps the contract.
        hhi = sum(p * p for _, p in shares)
        if hhi > 1.0:
            hhi = 1.0
        elif hhi < 0.0:
            hhi = 0.0

        # Top 5 reasons: share DESC, then alpha.
        shares.sort(key=lambda kv: (-kv[1], kv[0]))
        top_reasons = tuple(shares[:5])

        out.append(
            AssetConcentration(
                asset_class=asset,
                record_count=rec_count,
                reason_count=len(reason_counter),
                herfindahl_index=hhi,
                top_reasons=top_reasons,
            )
        )

    # Sort by record_count DESC, then alpha asset for determinism.
    out.sort(key=lambda a: (-a.record_count, a.asset_class))
    return tuple(out)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compute_co_occurrence(
    records: list[dict],
    *,
    min_pair_count: int = 1,
    min_edge_weight: int = 2,
    top_n_cells: int = 50,
    top_n_edges: int = 60,
) -> CoOccurrenceReport:
    """Build the three-view co-occurrence report.

    Parameters
    ----------
    records:
        Sequence of FinancialImpactRecord-shaped dicts. Read-only.
    min_pair_count:
        Drop asset×reason cells whose raw co-occurrence count is below
        this. Default 1 (keep everything).
    min_edge_weight:
        Drop symbol edges weaker than this. Default 2 (singletons hidden).
    top_n_cells:
        Cap on the number of cells reported. Default 50.
    top_n_edges:
        Cap on the number of edges reported. Default 60.

    Returns
    -------
    CoOccurrenceReport
        With ``asset_reason_cells`` sorted by lift DESC, ``strong_edges``
        by weight DESC, and ``asset_concentration`` by record_count DESC.
        Empty inputs yield an all-zero report.
    """
    if not records:
        return CoOccurrenceReport(
            total_records=0,
            distinct_assets=0,
            distinct_reasons=0,
            distinct_symbols=0,
            asset_reason_cells=(),
            strong_edges=(),
            asset_concentration=(),
        )

    # Pre-pass for the "distinct" summary counters. We do this once rather
    # than threading sets through every builder.
    distinct_assets: set[str] = set()
    distinct_reasons: set[str] = set()
    distinct_symbols: set[str] = set()
    for rec in records:
        distinct_assets.update(_clean_set(rec.get("asset_classes")))
        distinct_reasons.update(_clean_set(rec.get("impact_reason_codes")))
        distinct_symbols.update(_clean_symbols(rec.get("candidate_symbols")))

    cells = _build_asset_reason_cells(
        records, min_pair_count=min_pair_count, top_n=top_n_cells
    )
    edges = _build_symbol_edges(
        records, min_edge_weight=min_edge_weight, top_n=top_n_edges
    )
    concentration = _build_asset_concentration(records)

    return CoOccurrenceReport(
        total_records=len(records),
        distinct_assets=len(distinct_assets),
        distinct_reasons=len(distinct_reasons),
        distinct_symbols=len(distinct_symbols),
        asset_reason_cells=cells,
        strong_edges=edges,
        asset_concentration=concentration,
    )
