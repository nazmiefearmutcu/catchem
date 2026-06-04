"""Awareness BLIND-SPOT detector — invert the firehose question.

With hundreds of sources flooding in, "what just arrived?" is cheap and the
*valuable* question flips to "what am I **not** seeing?". This pure module
answers it: given a batch of ingested records and a watchlist of terms
(tickers / keywords / sectors), it reports which watched terms have **no**
recent coverage inside a rolling window — the analyst's blind spots — and how
fresh the coverage is for the ones that are covered.

Design notes
------------
* **Pure + deterministic.** No clock reads, no storage, no I/O. The caller
  injects ``now`` so the same inputs always produce the same output (the API
  layer + tests pin behaviour by passing a fixed ``now``).
* **Tolerant of messy records.** Records are plain dicts straight off storage
  (or replay / demo fixtures). Any field may be missing, ``None``, or the
  wrong type; nothing here raises on a malformed record — it simply doesn't
  contribute a match.
* **Case-insensitive whole-corpus match.** A term is "covered" if it appears,
  case-insensitively, in a record's title, its text/excerpt body, OR any entry
  of a symbols-style list field. Symbols match exactly (case-insensitive)
  because a ticker like ``T`` should not be flagged as covered just because the
  letter *t* appears in some headline.

Output shape (stable contract)::

    {
        "generated_at": "<now ISO8601>",
        "window_seconds": <float>,
        "covered": [
            {"term": "AAPL", "last_seen_age_seconds": 42.0, "mention_count": 3},
            ...  # sorted freshest-first (smallest age first)
        ],
        "gaps": ["TSLA", ...],  # watched terms with zero in-window mentions
    }
"""

from __future__ import annotations

import functools
import re
from datetime import UTC, datetime
from typing import Any

__all__ = ["find_coverage_gaps"]


@functools.lru_cache(maxsize=4096)
def _get_word_boundary_pattern(term: str) -> re.Pattern[str]:
    """Compile and cache regex word boundary pattern for a term."""
    return re.compile(rf"\b{re.escape(term)}\b")


# Record fields scanned for free-text term matches (substring, case-insensitive).
_TEXT_FIELDS: tuple[str, ...] = ("title", "text", "text_excerpt", "summary", "body")

# Record fields treated as symbol lists (exact token match, case-insensitive).
_SYMBOL_FIELDS: tuple[str, ...] = (
    "symbols",
    "candidate_symbols",
    "tickers",
    "candidate_entities",
)

# Record fields that may carry an event timestamp, in age-preference order.
_TS_FIELDS: tuple[str, ...] = ("published_ts", "created_at", "timestamp", "ts")


def _coerce_dt(value: Any) -> datetime | None:
    """Best-effort parse of a record timestamp into an aware ``datetime``.

    Accepts ``datetime`` objects (naive ones are assumed UTC) and ISO-8601
    strings (including a trailing ``Z``). Anything unparseable → ``None`` so a
    record with a junk timestamp is simply ignored rather than blowing up the
    whole gap scan.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith(("Z", "z")):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _record_event_dt(record: dict[str, Any]) -> datetime | None:
    """Pick the most relevant timestamp from a record (published > created)."""
    for field in _TS_FIELDS:
        dt = _coerce_dt(record.get(field))
        if dt is not None:
            return dt
    return None


def _record_haystack(record: dict[str, Any]) -> tuple[str, set[str]]:
    """Build the searchable surface of a record.

    Returns a ``(text_blob, symbol_set)`` pair where ``text_blob`` is the
    lower-cased concatenation of all free-text fields and ``symbol_set`` is the
    set of lower-cased symbol tokens. Both are tolerant of missing / non-string
    fields.
    """
    parts: list[str] = []
    for field in _TEXT_FIELDS:
        val = record.get(field)
        if isinstance(val, str) and val:
            parts.append(val.lower())
    text_blob = "\n".join(parts)

    symbols: set[str] = set()
    for field in _SYMBOL_FIELDS:
        val = record.get(field)
        if isinstance(val, str):
            token = val.strip().lower()
            if token:
                symbols.add(token)
        elif isinstance(val, (list, tuple, set)):
            for item in val:
                if isinstance(item, str):
                    token = item.strip().lower()
                    if token:
                        symbols.add(token)
    return text_blob, symbols


def find_coverage_gaps(
    records: list[dict],
    watch_terms: list[str],
    *,
    window_seconds: float = 86400.0,
    now: datetime | None = None,
) -> dict:
    """Classify each watched term as covered (with freshness) or a blind spot.

    Parameters
    ----------
    records:
        Ingested record dicts (storage rows, replay, or demo fixtures). Each
        may carry ``title``/``text``/``text_excerpt`` text fields, a symbols
        list field (``symbols`` / ``candidate_symbols`` / …), and a timestamp
        (``published_ts`` preferred, else ``created_at``). All fields optional.
    watch_terms:
        Tickers / keywords / sectors the analyst is watching. De-duplicated
        case-insensitively while preserving first-seen order; blank / non-str
        entries are dropped.
    window_seconds:
        Coverage horizon. A mention older than this (or with no usable
        timestamp) does not count toward "covered".
    now:
        Reference instant for age math. Injected for determinism; defaults to
        ``datetime.now(UTC)`` only when omitted. Naive values are read as UTC.

    Returns
    -------
    dict
        ``{"generated_at", "window_seconds", "covered": [...], "gaps": [...]}``
        — see module docstring for the exact shape. ``covered`` is sorted
        freshest-first; ``gaps`` preserves watch-term input order.
    """
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    try:
        window = float(window_seconds)
    except (TypeError, ValueError):
        window = 86400.0

    # De-dupe watch terms case-insensitively, keep first-seen casing + order.
    ordered_terms: list[str] = []
    term_lc: list[str] = []
    seen_lc: set[str] = set()
    for raw in watch_terms or []:
        if not isinstance(raw, str):
            continue
        term = raw.strip()
        if not term:
            continue
        lc = term.lower()
        if lc in seen_lc:
            continue
        seen_lc.add(lc)
        ordered_terms.append(term)
        term_lc.append(lc)

    # Per-term running state: in-window mention count + freshest age (seconds).
    mention_count: dict[str, int] = {lc: 0 for lc in term_lc}
    best_age: dict[str, float | None] = {lc: None for lc in term_lc}
    # Free-text matching is on WORD BOUNDARIES, not bare substring: a short
    # ticker like ``T``/``ON``/``GE`` must not be flagged covered just because
    # the letters appear inside an unrelated word ("Boston", "change"). This
    # restores the module's documented exact-symbol contract for the free-text
    # branch while still catching multi-word keyword terms. The exact
    # set-membership test against the symbol fields is kept unchanged.
    text_pat: dict[str, re.Pattern[str]] = {lc: _get_word_boundary_pattern(lc) for lc in term_lc}

    for record in records or []:
        if not isinstance(record, dict):
            continue
        event_dt = _record_event_dt(record)
        if event_dt is None:
            continue
        age = (now - event_dt).total_seconds()
        # Only mentions inside the window contribute. Future-dated records
        # (negative age, e.g. clock skew) are kept — they are "recent".
        if age > window:
            continue
        text_blob, symbols = _record_haystack(record)
        if not text_blob and not symbols:
            continue
        for lc in term_lc:
            matched = (lc in symbols) or bool(text_pat[lc].search(text_blob))
            if not matched:
                continue
            mention_count[lc] += 1
            prev = best_age[lc]
            if prev is None or age < prev:
                best_age[lc] = age

    covered: list[dict[str, Any]] = []
    gaps: list[str] = []
    for term, lc in zip(ordered_terms, term_lc, strict=True):
        if mention_count[lc] > 0:
            covered.append(
                {
                    "term": term,
                    "last_seen_age_seconds": best_age[lc],
                    "mention_count": mention_count[lc],
                }
            )
        else:
            gaps.append(term)

    # Freshest coverage first; ties broken by term for stable output.
    covered.sort(key=lambda c: (c["last_seen_age_seconds"], c["term"]))

    return {
        "generated_at": now.isoformat(),
        "window_seconds": window,
        "covered": covered,
        "gaps": gaps,
    }
