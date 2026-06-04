"""Event clustering for catchem's `FinancialImpactRecord` stream.

Groups multiple news captures that all describe the same underlying
real-world happening (e.g. five outlets covering a Fed rate cut within
half an hour) into a single :class:`EventCluster`.

The algorithm is intentionally simple and dependency-free:

  1. Sort the records by ``published_ts`` (falling back to ``created_at``).
  2. Score every pair with :func:`pairwise_similarity` — a weighted sum
     of Jaccard overlaps across symbols, reason codes, asset classes,
     entities and (stopword-filtered) title tokens.
  3. Greedy single-linkage: each new record attaches to the best-matching
     existing cluster whose max-member similarity clears the threshold
     AND whose timestamps fall within the rolling time window. Otherwise
     a new singleton is created.
  4. Drop singletons smaller than ``min_cluster_size`` (default 2).

This module is pure-function and read-only: it never mutates the input
records and produces deterministic output for a given input (the
``cluster_id`` is a SHA-1 over the sorted capture_ids inside the
cluster, so two runs on the same data yield identical IDs).
"""

from __future__ import annotations

import functools
import hashlib
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

__all__ = ["EventCluster", "cluster_records", "pairwise_similarity"]


# Small, intentionally inline stopword set — keeps the module self-contained
# and avoids pulling in nltk / sklearn-stopwords for a 15-word filter.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "in",
        "on",
        "to",
        "for",
        "is",
        "are",
        "was",
        "were",
        "and",
        "or",
        "but",
    }
)


# Title tokenization: lowercase word characters. Anything non-alphanumeric
# splits — keeps the filter language-agnostic for the small set we care about.
_TOKEN_RE = re.compile(r"[A-Za-z0-9']+")


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventCluster:
    """An emergent group of news captures describing the same event."""

    cluster_id: str
    capture_ids: tuple[str, ...]
    first_seen_ts: str
    last_seen_ts: str
    dominant_symbols: tuple[str, ...]
    dominant_reasons: tuple[str, ...]
    dominant_assets: tuple[str, ...]
    member_domains: tuple[str, ...]
    size: int
    mean_relevance: float
    coherence: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_iter(value: Any) -> Iterable[Any]:
    """Coerce list-ish fields into a safe iterable. None → empty."""
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return value
    return (value,)


def _normalize_set(values: Any) -> frozenset[str]:
    """Turn a possibly-None/list field into a frozenset of non-empty strings."""
    out: set[str] = set()
    for v in _as_iter(values):
        if v is None:
            continue
        s = str(v).strip()
        if s:
            out.add(s)
    return frozenset(out)


def _normalize_symbols(values: Any) -> frozenset[str]:
    """Symbols are case-insensitive; normalize to upper for matching."""
    return frozenset(s.upper() for s in _normalize_set(values))


def _title_tokens(title: Any) -> frozenset[str]:
    """Lowercase, alphanumeric, stopword-filtered title tokens."""
    if not title:
        return frozenset()
    raw = _TOKEN_RE.findall(str(title).lower())
    return frozenset(tok for tok in raw if tok and tok not in _STOPWORDS)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Standard Jaccard. Empty-vs-empty defined as 0 (no shared signal)."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    union = len(a | b)
    return inter / union


@functools.lru_cache(maxsize=8192)
def _parse_ts_cached(s: str) -> datetime | None:
    """Cached fast-path parser for ISO-8601 strings."""
    # Python 3.11+ handles `Z` natively only since 3.11; be defensive.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 string; returns None on failure / missing."""
    if value is None:
        return None
    if isinstance(value, datetime):
        # Treat naive datetimes as UTC for ordering consistency.
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    return _parse_ts_cached(s)


def _record_ts(record: dict) -> datetime | None:
    """Resolve a record's effective timestamp (published > created)."""
    return _parse_ts(record.get("published_ts")) or _parse_ts(record.get("created_at"))


def _record_ts_iso(record: dict) -> str | None:
    """Return the ISO string we actually used for sorting / window math.

    Important for stable ``first_seen_ts`` / ``last_seen_ts`` reporting.
    """
    pub = record.get("published_ts")
    if _parse_ts(pub) is not None:
        return str(pub)
    created = record.get("created_at")
    if _parse_ts(created) is not None:
        return str(created)
    return None


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------

# Field weights — sum to 1.0 by construction so the output stays in [0,1].
_W_SYMBOLS = 0.35
_W_REASONS = 0.20
_W_ASSETS = 0.15
_W_ENTITIES = 0.10
_W_TITLE = 0.20


def pairwise_similarity(a: dict, b: dict, *, _cache: dict[int, Any] | None = None) -> float:
    """Weighted Jaccard similarity over five signal categories.

    Returns 0 when the two records have no overlapping signal in any
    category — empty set vs empty set is defined as 0 (see :func:`_jaccard`),
    so a record with only a title will still match another record with
    only a title via the title channel.
    """
    if a is b:
        # Same dict reference → identical input, identical signal.
        return 1.0

    if _cache is not None:
        if id(a) in _cache:
            a_syms, a_reasons, a_assets, a_entities, a_title = _cache[id(a)]
        else:
            a_syms = _normalize_symbols(a.get("candidate_symbols"))
            a_reasons = _normalize_set(a.get("impact_reason_codes"))
            a_assets = _normalize_set(a.get("asset_classes"))
            a_entities = _normalize_set(a.get("candidate_entities"))
            a_title = _title_tokens(a.get("title"))
            _cache[id(a)] = (a_syms, a_reasons, a_assets, a_entities, a_title)

        if id(b) in _cache:
            b_syms, b_reasons, b_assets, b_entities, b_title = _cache[id(b)]
        else:
            b_syms = _normalize_symbols(b.get("candidate_symbols"))
            b_reasons = _normalize_set(b.get("impact_reason_codes"))
            b_assets = _normalize_set(b.get("asset_classes"))
            b_entities = _normalize_set(b.get("candidate_entities"))
            b_title = _title_tokens(b.get("title"))
            _cache[id(b)] = (b_syms, b_reasons, b_assets, b_entities, b_title)
    else:
        a_syms = _normalize_symbols(a.get("candidate_symbols"))
        a_reasons = _normalize_set(a.get("impact_reason_codes"))
        a_assets = _normalize_set(a.get("asset_classes"))
        a_entities = _normalize_set(a.get("candidate_entities"))
        a_title = _title_tokens(a.get("title"))

        b_syms = _normalize_symbols(b.get("candidate_symbols"))
        b_reasons = _normalize_set(b.get("impact_reason_codes"))
        b_assets = _normalize_set(b.get("asset_classes"))
        b_entities = _normalize_set(b.get("candidate_entities"))
        b_title = _title_tokens(b.get("title"))

    sym_score = _jaccard(a_syms, b_syms)
    reason_score = _jaccard(a_reasons, b_reasons)
    asset_score = _jaccard(a_assets, b_assets)
    entity_score = _jaccard(a_entities, b_entities)
    title_score = _jaccard(a_title, b_title)

    total = (
        _W_SYMBOLS * sym_score
        + _W_REASONS * reason_score
        + _W_ASSETS * asset_score
        + _W_ENTITIES * entity_score
        + _W_TITLE * title_score
    )

    # All categories scored 0 → no overlapping signal anywhere.
    if total == 0.0:
        return 0.0

    # Clamp defensively; weights already sum to 1.0 but float math is float math.
    if total > 1.0:
        return 1.0
    return total


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def _cluster_id(capture_ids: Iterable[str]) -> str:
    """Deterministic SHA-1 over sorted capture_ids joined by '|'."""
    payload = "|".join(sorted(str(c) for c in capture_ids))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _ranked_dominant(
    members: list[dict], key: str, top_n: int = 5, *, normalize_upper: bool = False
) -> tuple[str, ...]:
    """Return list-field tokens appearing in >=2 members, top-N by frequency.

    Ties broken alphabetically so the result is deterministic.
    """
    counter: Counter[str] = Counter()
    for rec in members:
        seen: set[str] = set()
        for raw in _as_iter(rec.get(key)):
            if raw is None:
                continue
            s = str(raw).strip()
            if not s:
                continue
            if normalize_upper:
                s = s.upper()
            seen.add(s)
        for tok in seen:
            counter[tok] += 1

    repeated = [(tok, n) for tok, n in counter.items() if n >= 2]
    repeated.sort(key=lambda item: (-item[1], item[0]))
    return tuple(tok for tok, _ in repeated[:top_n])


def _member_domains(members: list[dict]) -> tuple[str, ...]:
    """Unique, sorted, non-empty domains."""
    domains: set[str] = set()
    for rec in members:
        d = rec.get("domain")
        if d is None:
            continue
        s = str(d).strip()
        if s:
            domains.add(s)
    return tuple(sorted(domains))


def _mean_relevance(members: list[dict]) -> float:
    """Average of finance_relevance_score; missing entries treated as 0.0."""
    if not members:
        return 0.0
    total = 0.0
    for rec in members:
        v = rec.get("finance_relevance_score")
        if v is None:
            continue
        try:
            total += float(v)
        except (TypeError, ValueError):
            continue
    return total / len(members)


def _coherence(
    members: list[dict],
    sim_cache: dict[tuple[int, int], float],
    feature_cache: dict[int, Any] | None = None,
) -> float:
    """Mean pairwise similarity across all member pairs."""
    n = len(members)
    if n < 2:
        # Singletons that survived to the output (min_cluster_size <= 1) are
        # perfectly self-coherent.
        return 1.0
    pair_count = 0
    total = 0.0
    for i in range(n):
        for j in range(i + 1, n):
            key = (id(members[i]), id(members[j]))
            if key in sim_cache:
                total += sim_cache[key]
            else:
                # Fallback (shouldn't happen if cache is primed during clustering).
                total += pairwise_similarity(members[i], members[j], _cache=feature_cache)
            pair_count += 1
    return total / pair_count if pair_count else 1.0


def _first_last_ts(
    members: list[dict],
    ts_cache: dict[int, tuple[datetime | None, str | None]] | None = None,
) -> tuple[str, str]:
    """Earliest and latest effective ISO timestamps in the cluster.

    Falls back to the empty string only if literally nothing is parseable.
    """
    candidates: list[tuple[datetime, str]] = []
    for rec in members:
        if ts_cache is not None and id(rec) in ts_cache:
            ts, iso = ts_cache[id(rec)]
        else:
            ts = _record_ts(rec)
            iso = _record_ts_iso(rec) if ts is not None else None
        if ts is None or iso is None:
            continue
        candidates.append((ts, iso))
    if not candidates:
        return ("", "")
    candidates.sort(key=lambda kv: kv[0])
    return (candidates[0][1], candidates[-1][1])


def cluster_records(
    records: list[dict],
    *,
    window_seconds: int = 1800,
    similarity_threshold: float = 0.35,
    min_cluster_size: int = 2,
    min_distinct_domains: int = 1,
) -> list[EventCluster]:
    """Cluster ``records`` into :class:`EventCluster` events.

    Parameters
    ----------
    records:
        Sequence of FinancialImpactRecord-shaped dicts. Read-only.
    window_seconds:
        Two records cluster only if their effective timestamps differ by
        at most this many seconds. Defaults to 30 minutes.
    similarity_threshold:
        Minimum weighted-Jaccard similarity required for attachment to
        an existing cluster. Defaults to 0.35.
    min_cluster_size:
        Clusters smaller than this are dropped from the return list.
        Defaults to 2 (singletons hidden).
    min_distinct_domains:
        Drop clusters whose captures all come from the same domain. A
        "cluster" of two captures from cnbc.com isn't a multi-source
        event — it's the same outlet republishing. Defaults to 1
        (everything kept) for backward compat; the engine pins this to
        2 so cross-source corroboration is required.

    Returns
    -------
    list[EventCluster]
        Clusters sorted by ``first_seen_ts`` ascending (deterministic).
    """
    if not records:
        return []

    # Pre-parse timestamps and create feature cache to avoid redundant work in nested loops
    ts_cache: dict[int, tuple[datetime | None, str | None]] = {}
    feature_cache: dict[int, Any] = {}
    for rec in records:
        ts = _record_ts(rec)
        iso = _record_ts_iso(rec) if ts is not None else None
        ts_cache[id(rec)] = (ts, iso)

    def get_rec_ts(r: dict) -> datetime | None:
        return ts_cache[id(r)][0]

    # --- 1. Sort by effective timestamp. Records without any timestamp
    #     sort to the end so they still participate in clustering but
    #     don't anchor the window.
    def _sort_key(rec: dict) -> tuple[int, datetime]:
        ts = get_rec_ts(rec)
        if ts is None:
            return (1, datetime.max.replace(tzinfo=UTC))
        return (0, ts)

    ordered = sorted(records, key=_sort_key)

    # --- 2. Greedy single-linkage with rolling time window.
    #     Each cluster keeps a list of member dicts (preserving insertion
    #     order). ``sim_cache`` memoizes the pair similarities so we can
    #     reuse them when computing coherence at the end.
    clusters: list[list[dict]] = []
    cluster_last_ts: dict[int, datetime | None] = {}
    sim_cache: dict[tuple[int, int], float] = {}

    for rec in ordered:
        rec_ts = get_rec_ts(rec)

        best_cluster: list[dict] | None = None
        best_score = similarity_threshold  # strict ≥ threshold below.

        for cluster in clusters:
            # Time-window check uses the cluster's latest timestamp — that's
            # what "rolling window" means here. A burst of stories within
            # `window_seconds` of any prior member extends the window.
            within_window = True
            if rec_ts is not None:
                last_in_cluster = cluster_last_ts[id(cluster)]
                delta = abs((rec_ts - last_in_cluster).total_seconds())
                if delta > window_seconds:
                    within_window = False

            if not within_window:
                continue

            # Max-link similarity to any cluster member.
            cluster_max = 0.0
            for member in cluster:
                key = (id(member), id(rec))
                if key in sim_cache:
                    score = sim_cache[key]
                else:
                    score = pairwise_similarity(member, rec, _cache=feature_cache)
                    sim_cache[key] = score
                    sim_cache[(id(rec), id(member))] = score
                if score > cluster_max:
                    cluster_max = score

            if cluster_max >= best_score:
                best_score = cluster_max
                best_cluster = cluster

        if best_cluster is None:
            new_cluster = [rec]
            clusters.append(new_cluster)
            if rec_ts is not None:
                cluster_last_ts[id(new_cluster)] = rec_ts
        else:
            best_cluster.append(rec)
            if rec_ts is not None:
                prior_last = cluster_last_ts.get(id(best_cluster))
                if prior_last is None or rec_ts > prior_last:
                    cluster_last_ts[id(best_cluster)] = rec_ts

    # --- 3. Drop too-small clusters.
    surviving = [c for c in clusters if len(c) >= min_cluster_size]
    if min_distinct_domains > 1:
        surviving = [c for c in surviving if len(set(_member_domains(c))) >= min_distinct_domains]

    # --- 4. Build the public EventCluster objects.
    out: list[EventCluster] = []
    for members in surviving:
        capture_ids = tuple(str(rec.get("capture_id", "")) for rec in members)
        # Compute coherence using the pair cache populated during clustering.
        # For size-2 the spec says "the single pair score" — _coherence
        # handles that naturally (mean of one pair == that pair).
        coh = _coherence(members, sim_cache, feature_cache)

        first_ts, last_ts = _first_last_ts(members, ts_cache)

        cluster = EventCluster(
            cluster_id=_cluster_id(capture_ids),
            capture_ids=capture_ids,
            first_seen_ts=first_ts,
            last_seen_ts=last_ts,
            dominant_symbols=_ranked_dominant(members, "candidate_symbols", normalize_upper=True),
            dominant_reasons=_ranked_dominant(members, "impact_reason_codes"),
            dominant_assets=_ranked_dominant(members, "asset_classes"),
            member_domains=_member_domains(members),
            size=len(members),
            mean_relevance=_mean_relevance(members),
            coherence=coh,
        )
        out.append(cluster)

    # Deterministic order: earliest event first, then by cluster_id.
    out.sort(key=lambda c: (c.first_seen_ts or "", c.cluster_id))
    return out
