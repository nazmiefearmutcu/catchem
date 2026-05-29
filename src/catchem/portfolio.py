"""READ-ONLY portfolio enrichment — join holdings to the awareness layer.

Given a list of analyst-entered holdings (plain dicts, each carrying at least
a ``symbol``), this pure module annotates every holding with the context the
awareness/quant pipeline already produced for that symbol:

  * **recent_news_count** — how many in-window records mention the symbol,
  * **recent_top** — up to three highest finance-relevance matching records,
  * **coverage** — covered / blind-spot freshness via
    :func:`catchem.awareness_gaps.find_coverage_gaps`,
  * **quote** — the latest price snapshot, fetched through an injected
    ``quote_fn`` so the join stays testable and side-effect-free here.

Design notes (mirrors :mod:`catchem.awareness_gaps`)
----------------------------------------------------
* **Pure + deterministic.** No clock reads, no storage, no network. The
  caller injects ``now`` and ``quote_fn``; identical inputs always produce
  identical output. There is NO order execution and NO money movement — this
  is purely tracking + awareness enrichment.
* **Tolerant of messy records.** Records are plain dicts off storage / replay
  / demo fixtures. Any field may be missing, ``None``, or the wrong type;
  nothing here raises on a malformed record — it just doesn't contribute a
  match. A holding with no ``symbol`` gets empty enrichment rather than an
  exception.
* **Case-insensitive matching.** A record "mentions" the symbol when the
  symbol token appears (case-insensitive) in any of its free-text fields
  (title / text / excerpt / summary / body) OR as an exact token in any of
  its symbol-list fields (symbols / candidate_symbols / tickers /
  candidate_entities).

Output shape (stable contract): every input holding dict is shallow-copied
and extended with::

    {
        ...original holding fields...,
        "recent_news_count": <int>,
        "recent_top": [{"title", "url", "score"}, ...],   # up to 3, score desc
        "coverage": {"covered": <bool>,
                     "last_seen_age_seconds": <float|None>,
                     "mention_count": <int>},
        "quote": {"last", "prev_close", "change_pct"} | None,
        "sentiment_label": "positive"|"negative"|"neutral"|"unknown"|None,
    }
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any

from .awareness_gaps import (
    _record_event_dt,
    _record_haystack,
    find_coverage_gaps,
)

__all__ = ["enrich_holdings"]

# Reused from awareness_gaps so the match surface is identical to the
# blind-spot detector: free-text substring fields + exact-token symbol fields.


def _symbol_matches(symbol_lc: str, text_blob: str, symbols: set[str]) -> bool:
    """True when the (lower-cased) symbol appears in the record's surface.

    Word-boundary match against the free-text blob OR exact token match against
    the symbol set — the same rule :func:`find_coverage_gaps` applies, so a
    holding's news count and its coverage flag never disagree. The boundary
    test (not bare substring) keeps a short ticker like ``T``/``ON``/``GE``
    from counting an unrelated word ("Boston", "change") as a mention.
    """
    if not symbol_lc:
        return False
    if symbol_lc in symbols:
        return True
    return bool(re.search(rf"\b{re.escape(symbol_lc)}\b", text_blob))


def _quote_payload(raw: Any) -> dict[str, Any] | None:
    """Normalize a ``quote_fn`` result to ``{last, prev_close, change_pct}``.

    Accepts either a mapping (dict-like) or an object exposing the fields as
    attributes (e.g. a ``MarketQuote`` pydantic model). Returns ``None`` when
    the raw value is ``None`` or carries no usable price at all. A quote that
    is present but priced ``None`` (unknown symbol) still returns the shaped
    dict so the caller can distinguish "no quote_fn answer" from "symbol
    unknown to provider".
    """
    if raw is None:
        return None

    def _get(field: str) -> Any:
        if isinstance(raw, dict):
            return raw.get(field)
        return getattr(raw, field, None)

    last = _coerce_float(_get("last"))
    prev_close = _coerce_float(_get("prev_close"))
    change_pct = _coerce_float(_get("change_pct"))
    # Derive change_pct if the provider omitted it but gave both prices.
    if change_pct is None and last is not None and prev_close:
        change_pct = (last - prev_close) / prev_close
    return {"last": last, "prev_close": prev_close, "change_pct": change_pct}


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    # Reject NaN / ±inf — a non-finite price would poison the derived
    # change_pct ((last - prev_close) / prev_close) and silently propagate a
    # NaN into the enrichment output. Mirrors quant.global_tone._coerce_value.
    if not math.isfinite(f):
        return None
    return f


def _record_score(record: dict[str, Any]) -> float:
    """Pull the finance-relevance score off a record (0.0 when absent/junk)."""
    val = record.get("finance_relevance_score")
    score = _coerce_float(val)
    return score if score is not None else 0.0


# The four labels the sentiment model emits (mirrors schemas.SentimentLabel),
# which is also exactly what the frontend's `sentiment_label` union accepts.
_SENTIMENT_LABELS: frozenset[str] = frozenset(
    {"positive", "negative", "neutral", "unknown"}
)


def _record_sentiment(record: dict[str, Any]) -> str | None:
    """Pull a normalized sentiment label off a record.

    Returns one of ``positive`` / ``negative`` / ``neutral`` / ``unknown`` (the
    values the UI's SentimentChip understands), or ``None`` when the field is
    absent or carries something outside that set.
    """
    val = record.get("sentiment_label")
    if isinstance(val, str):
        lc = val.strip().lower()
        if lc in _SENTIMENT_LABELS:
            return lc
    return None


def enrich_holdings(
    holdings: list[dict],
    *,
    records: list[dict],
    quote_fn: Any,
    now: datetime | None = None,
    window_seconds: float = 86400.0,
) -> list[dict]:
    """Annotate each holding with awareness + quote context. Pure.

    Parameters
    ----------
    holdings:
        Analyst-entered position dicts. Each should carry a ``symbol`` (any
        other fields are preserved untouched). A blank / missing symbol yields
        empty-but-well-shaped enrichment rather than an error.
    records:
        Ingested record dicts (storage rows / replay / demo). Same tolerant
        shape as :func:`catchem.awareness_gaps.find_coverage_gaps`.
    quote_fn:
        Callable ``symbol -> quote`` injected for testability. Its result is
        normalized to ``{last, prev_close, change_pct}``; any exception or a
        ``None`` return collapses the holding's ``quote`` to ``None``.
    now:
        Reference instant for age / window math. Injected for determinism;
        defaults to :func:`datetime.now(UTC)` only when omitted.
    window_seconds:
        Coverage / news-count horizon. Mentions older than this (or with no
        usable timestamp) don't count toward ``recent_news_count`` or
        ``coverage``.

    Returns
    -------
    list[dict]
        One shallow-copied + enriched dict per input holding, in input order.
        See the module docstring for the exact added-field shape.
    """
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    try:
        window = float(window_seconds)
    except (TypeError, ValueError):
        window = 86400.0

    safe_records = [r for r in (records or []) if isinstance(r, dict)]

    # Distinct watch terms for the coverage pass (one find_coverage_gaps call
    # covers every holding's symbol → freshness in a single sweep).
    watch_terms: list[str] = []
    seen_terms: set[str] = set()
    for h in holdings or []:
        if not isinstance(h, dict):
            continue
        sym = str(h.get("symbol") or "").strip()
        if not sym:
            continue
        lc = sym.lower()
        if lc in seen_terms:
            continue
        seen_terms.add(lc)
        watch_terms.append(sym)

    coverage_report = find_coverage_gaps(
        safe_records, watch_terms, window_seconds=window, now=now
    )
    coverage_by_term: dict[str, dict[str, Any]] = {
        str(c["term"]).lower(): c for c in coverage_report.get("covered", [])
    }

    enriched: list[dict] = []
    for holding in holdings or []:
        if not isinstance(holding, dict):
            continue
        out = dict(holding)
        sym = str(holding.get("symbol") or "").strip()
        sym_lc = sym.lower()

        # ── news count + top matching records ────────────────────────────
        news_count = 0
        matches: list[dict[str, Any]] = []
        if sym_lc:
            for record in safe_records:
                event_dt = _record_event_dt(record)
                if event_dt is None:
                    continue
                age = (now - event_dt).total_seconds()
                if age > window:
                    continue
                text_blob, symbols = _record_haystack(record)
                if not text_blob and not symbols:
                    continue
                if not _symbol_matches(sym_lc, text_blob, symbols):
                    continue
                news_count += 1
                matches.append(record)

        matches.sort(key=lambda r: _record_score(r), reverse=True)
        recent_top = [
            {
                "title": rec.get("title"),
                "url": rec.get("url"),
                "score": _record_score(rec),
            }
            for rec in matches[:3]
        ]

        # Sentiment of the freshest-relevance coverage. The frontend's
        # PortfolioEnrichedHolding declares `sentiment_label` and renders a
        # SentimentChip from it; deriving it from the top matching record means
        # the column reflects how recent coverage skews rather than rendering a
        # permanent "—". None when there is no matching record at all.
        sentiment_label = _record_sentiment(matches[0]) if matches else None

        # ── coverage (covered / blind-spot freshness) ────────────────────
        cov = coverage_by_term.get(sym_lc)
        if cov is not None:
            coverage = {
                "covered": True,
                "last_seen_age_seconds": cov.get("last_seen_age_seconds"),
                "mention_count": int(cov.get("mention_count") or 0),
            }
        else:
            coverage = {
                "covered": False,
                "last_seen_age_seconds": None,
                "mention_count": 0,
            }

        # ── quote (injected, failure-tolerant) ───────────────────────────
        quote: dict[str, Any] | None = None
        if sym and quote_fn is not None:
            try:
                quote = _quote_payload(quote_fn(sym))
            except Exception:
                # Never let a flaky provider break enrichment — a failed
                # quote collapses to None, the rest of the join still stands.
                quote = None

        out["recent_news_count"] = news_count
        out["recent_top"] = recent_top
        out["coverage"] = coverage
        out["quote"] = quote
        out["sentiment_label"] = sentiment_label
        enriched.append(out)

    return enriched
