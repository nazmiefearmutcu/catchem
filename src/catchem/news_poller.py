"""Background RSS/Atom poller — keeps the Live Feed actually live.

Catchem ships standalone (no Awareness sidecar producing JSONL), so without
a self-contained source the feed sits at whatever the bootstrap seeded.
This module pulls a handful of public RSS feeds on a fixed interval, dedups
by URL, and routes each new article through the same
`build_capture → JSONL → run_replay` path the paste-demo uses. The result
is that `count_records()["total"]` grows over time, which causes the SSE
`summary` event to fire, which causes the UI to refresh.

Design notes:
  * Pure-stdlib RSS/Atom parsing (`xml.etree.ElementTree`) — no feedparser.
  * httpx for fetching (already a dep).
  * Asyncio task started in FastAPI `lifespan`; cancelled cleanly on
    shutdown.
  * Defensive: any feed-fetch error logs and moves on. One bad feed never
    stops the poller.
  * Dedup: a process-local LRU of recently-seen URLs PLUS storage.get_record
    by deterministic capture_id (text+url hashed). Re-runs are no-ops.
  * Off by default in tests (`CATCHEM_NEWS__POLLER_ENABLED=false`).
  * UA strings on every fetch — many feeds 403 the default httpx UA.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import inspect
import re
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from functools import lru_cache
from urllib.parse import urlparse

import httpx

from .demo import build_capture, write_jsonl
from .logging import get_logger
from .settings import Settings
from .supervisor import Supervisor

logger = get_logger("catchem.news_poller")

# Pre-flight checks: keep UA distinctive so admins can spot us in their
# server logs. macOS Catchem identifies itself as Catchem-News-Poller/0.1.
_USER_AGENT = "Catchem-News-Poller/0.1 (+local catchem sidecar)"

# Explicit per-feed HTTP timeout. httpx's default (5s for everything) silently
# became the contract; pin it so an upstream change can't extend a stalled
# tick beyond the poller's own poll interval. connect/read tight enough that
# six feeds in parallel still finish well under the 10s default poll cycle.
_HTTPX_TIMEOUT = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0)

# Atom/RSS namespace bag — defensive over many feed variants.
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "media": "http://search.yahoo.com/mrss/",
}


@dataclass(frozen=True)
class FeedSpec:
    """One configured feed source.

    `parser` selects how the fetched body is turned into ParsedItems. The
    default "rss" handles RSS/Atom XML (the original behavior). Source packs
    under `catchem.news_sources` can register additional parsers (e.g.
    "gdelt", "reddit") via `register_parser` so non-XML firehoses plug into
    the exact same fetch → dedup → ingest pipeline without touching the loop.
    """

    name: str
    url: str
    # Default domain to attribute when an item lacks one (rare but happens).
    fallback_domain: str = ""
    # Body parser key — see `register_parser`. "rss" = RSS/Atom XML.
    parser: str = "rss"


# ── Pluggable parser registry ────────────────────────────────────────────
# A parser turns a fetched response body (bytes) into a list[ParsedItem].
# Source packs register non-RSS parsers (GDELT/Reddit JSON, etc.) here so a
# single generic GET path can feed many heterogeneous firehoses. The "rss"
# parser is registered at import time below, after parse_feed is defined.
# `ParsedItem` is forward-referenced (defined further down), hence the string
# annotation; `Callable` is imported at module top.
_PARSERS: dict[str, Callable[[bytes, str], list[ParsedItem]]] = {}


def register_parser(name: str, fn: Callable[[bytes, str], list[ParsedItem]]) -> None:
    """Register a body parser under `name` (idempotent — last write wins)."""
    _PARSERS[name] = fn


def get_parser(name: str) -> Callable[[bytes, str], list[ParsedItem]]:
    """Return the parser for `name`, falling back to the rss parser."""
    return _PARSERS.get(name) or _PARSERS["rss"]


# ── Pluggable feed-provider registry ─────────────────────────────────────
# Source packs (catchem/news_sources/*.py) call `register_feed_provider` at
# import time to contribute extra FeedSpecs (more wires, GDELT, Reddit, per-
# sector Google News queries, …). `assemble_feeds()` merges DEFAULT_FEEDS +
# every provider's output, so a pack only ever ADDS its own new module — it
# never edits the shared DEFAULT_FEEDS tuple, which keeps parallel authorship
# collision-free.
_FEED_PROVIDERS: list[Callable[[], Iterable[FeedSpec]]] = []


def register_feed_provider(fn: Callable[[], Iterable[FeedSpec]]) -> Callable[[], Iterable[FeedSpec]]:
    """Register a zero-arg callable returning extra FeedSpecs. Returns the
    callable so it can be used as a decorator."""
    _FEED_PROVIDERS.append(fn)
    return fn


def assemble_feeds() -> tuple[FeedSpec, ...]:
    """DEFAULT_FEEDS + every registered provider's feeds, de-duped by name AND URL.

    Importing `catchem.news_sources` triggers auto-discovery of all source
    packs (each self-registers). De-dup keeps the FIRST occurrence so
    DEFAULT_FEEDS always wins over a pack that reuses a name/URL by accident.

    Two distinct collisions are both dropped (and logged so the drop isn't
    silent):
      * **name collision** — a later spec reusing a name would otherwise share
        `feed_health`/cooldown state with the first under the name key.
      * **URL collision** — two specs with different names but the same URL
        would fetch the SAME endpoint twice every poll (pure wasted bandwidth +
        double rate-limit pressure); item-level canonical-URL dedup hides the
        duplicate content but not the redundant request.
    """
    with contextlib.suppress(Exception):
        import catchem.news_sources  # noqa: F401  (import side-effect: registration)

    seen_names: set[str] = set()
    seen_urls: set[str] = set()
    out: list[FeedSpec] = []

    def _admit(spec: FeedSpec, *, origin: str) -> None:
        if spec.name in seen_names:
            logger.warning("feed_dropped_duplicate_name", name=spec.name, url=spec.url, origin=origin)
            return
        if spec.url in seen_urls:
            logger.warning("feed_dropped_duplicate_url", name=spec.name, url=spec.url, origin=origin)
            return
        seen_names.add(spec.name)
        seen_urls.add(spec.url)
        out.append(spec)

    for spec in DEFAULT_FEEDS:
        _admit(spec, origin="default")
    for provider in _FEED_PROVIDERS:
        try:
            for spec in provider():
                _admit(spec, origin=getattr(provider, "__name__", "?"))
        except Exception as exc:  # one bad pack never breaks the rest
            logger.warning(
                "feed_provider_failed", provider=getattr(provider, "__name__", "?"), error=str(exc)
            )
    return tuple(out)


# Curated default set — public, no-auth, stable over years. Each one is a
# clean RSS/Atom endpoint with a sensible Title + Description + Link triple.
# Operators can override via CATCHEM_NEWS__FEEDS but the defaults Just Work.
#
# Coverage strategy: broad enough that *every* poll has a non-trivial chance
# of surfacing a new URL (mainstream + financial + tech + crypto + regulator).
# Each candidate was live-tested 2026-05-17; sources known to 404 or 403 the
# common UA are intentionally absent. If you add new sources, run
#   python -c "import asyncio, httpx; from catchem.news_poller import \
#     fetch_feed, FeedSpec; ..."
# before checking in.
DEFAULT_FEEDS: tuple[FeedSpec, ...] = (
    # ── Mainstream business (5-15 items/hr each)
    FeedSpec("bbc-business", "https://feeds.bbci.co.uk/news/business/rss.xml", "bbc.com"),
    FeedSpec("bbc-tech", "https://feeds.bbci.co.uk/news/technology/rss.xml", "bbc.com"),
    FeedSpec("reuters-business", "https://feeds.feedburner.com/reuters/businessNews", "reuters.com"),
    FeedSpec("reuters-tech", "https://feeds.feedburner.com/reuters/technologyNews", "reuters.com"),
    FeedSpec("guardian-business", "https://www.theguardian.com/uk/business/rss", "theguardian.com"),
    FeedSpec("nytimes-business", "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml", "nytimes.com"),
    # ── Financial press
    FeedSpec("cnbc-top", "https://www.cnbc.com/id/100003114/device/rss/rss.html", "cnbc.com"),
    FeedSpec("cnbc-business", "https://www.cnbc.com/id/10001147/device/rss/rss.html", "cnbc.com"),
    FeedSpec("cnbc-economy", "https://www.cnbc.com/id/20910258/device/rss/rss.html", "cnbc.com"),
    FeedSpec("cnbc-finance", "https://www.cnbc.com/id/15839069/device/rss/rss.html", "cnbc.com"),
    FeedSpec("cnbc-markets", "https://www.cnbc.com/id/15839135/device/rss/rss.html", "cnbc.com"),
    FeedSpec(
        "marketwatch-top", "https://feeds.content.dowjones.io/public/rss/mw_topstories", "marketwatch.com"
    ),
    FeedSpec(
        "marketwatch-marketpulse",
        "https://feeds.content.dowjones.io/public/rss/mw_marketpulse",
        "marketwatch.com",
    ),
    FeedSpec(
        "marketwatch-bulletins",
        "https://feeds.content.dowjones.io/public/rss/mw_bulletins",
        "marketwatch.com",
    ),
    FeedSpec(
        "marketwatch-realtime",
        "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines",
        "marketwatch.com",
    ),
    FeedSpec("yahoo-finance", "https://finance.yahoo.com/news/rssindex", "finance.yahoo.com"),
    FeedSpec("seekingalpha", "https://seekingalpha.com/feed.xml", "seekingalpha.com"),
    FeedSpec("benzinga-news", "https://www.benzinga.com/news/feed", "benzinga.com"),
    FeedSpec("investing-news", "https://www.investing.com/rss/news.rss", "investing.com"),
    FeedSpec("zerohedge", "https://feeds.feedburner.com/zerohedge/feed", "zerohedge.com"),
    # ── Press wires (high volume — many per hour)
    FeedSpec("prnewswire-all", "https://www.prnewswire.com/rss/all-news-releases-list.rss", "prnewswire.com"),
    FeedSpec(
        "prnewswire-financial",
        "https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss",
        "prnewswire.com",
    ),
    FeedSpec("ap-business", "https://feedx.net/rss/ap.xml", "apnews.com"),
    # ── Bloomberg
    FeedSpec("bloomberg-markets", "https://feeds.bloomberg.com/markets/news.rss", "bloomberg.com"),
    FeedSpec("bloomberg-tech", "https://feeds.bloomberg.com/technology/news.rss", "bloomberg.com"),
    # ── WSJ + Barron's + Forbes + FT (paywall-marked but RSS is open)
    FeedSpec("wsj-markets", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml", "wsj.com"),
    FeedSpec("wsj-business", "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml", "wsj.com"),
    FeedSpec("barrons-recent", "https://feeds.content.dowjones.io/public/rss/RSSWSJD", "barrons.com"),
    FeedSpec("forbes-business", "https://www.forbes.com/business/feed/", "forbes.com"),
    FeedSpec("financial-times", "https://www.ft.com/rss/home", "ft.com"),
    FeedSpec("aljazeera", "https://www.aljazeera.com/xml/rss/all.xml", "aljazeera.com"),
    # ── Regulators / central banks
    FeedSpec("fed-press-all", "https://www.federalreserve.gov/feeds/press_all.xml", "federalreserve.gov"),
    FeedSpec(
        "sec-edgar-current",
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=&output=atom",
        "sec.gov",
    ),
    FeedSpec(
        "sec-8k", "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom", "sec.gov"
    ),
    FeedSpec(
        "sec-10q",
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=10-Q&output=atom",
        "sec.gov",
    ),
    FeedSpec("ecb-press", "https://www.ecb.europa.eu/rss/press.html", "ecb.europa.eu"),
    # ── Crypto
    FeedSpec(
        "coindesk-business", "https://www.coindesk.com/arc/outboundfeeds/rss?outputType=xml", "coindesk.com"
    ),
    FeedSpec("decrypt-crypto", "https://decrypt.co/feed", "decrypt.co"),
    FeedSpec("theblock", "https://www.theblock.co/rss.xml", "theblockcrypto.com"),
    FeedSpec("cointelegraph", "https://cointelegraph.com/rss", "cointelegraph.com"),
    FeedSpec("bitcoinmagazine", "https://bitcoinmagazine.com/feed", "bitcoinmagazine.com"),
    # ── Tech-adjacent (HN regularly covers fintech, regulation, market moves)
    FeedSpec("hackernews", "https://news.ycombinator.com/rss", "news.ycombinator.com"),
    FeedSpec("hn-frontpage", "https://hnrss.org/frontpage", "news.ycombinator.com"),
    FeedSpec("hn-newest", "https://hnrss.org/newest", "news.ycombinator.com"),
    # ── Google News searches — near-real-time aggregation of every major
    # publisher's homepage. Each query returns ~100 fresh items and updates
    # within minutes of publication, which sidesteps the RSS publisher lag
    # (Yahoo/Forbes/etc. refresh their RSS every 10-15 min; their articles
    # appear in Google News within seconds).
    FeedSpec(
        "gnews-finance",
        "https://news.google.com/rss/search?q=finance+markets&hl=en-US&gl=US&ceid=US:en",
        "news.google.com",
    ),
    FeedSpec(
        "gnews-stocks",
        "https://news.google.com/rss/search?q=stocks+earnings&hl=en-US&gl=US&ceid=US:en",
        "news.google.com",
    ),
    FeedSpec(
        "gnews-bitcoin",
        "https://news.google.com/rss/search?q=bitcoin+OR+ethereum&hl=en-US&gl=US&ceid=US:en",
        "news.google.com",
    ),
    FeedSpec(
        "gnews-fed",
        "https://news.google.com/rss/search?q=federal+reserve+OR+FOMC&hl=en-US&gl=US&ceid=US:en",
        "news.google.com",
    ),
    FeedSpec(
        "gnews-inflation",
        "https://news.google.com/rss/search?q=inflation+OR+CPI+OR+PPI&hl=en-US&gl=US&ceid=US:en",
        "news.google.com",
    ),
    FeedSpec(
        "gnews-recession",
        "https://news.google.com/rss/search?q=recession+OR+unemployment&hl=en-US&gl=US&ceid=US:en",
        "news.google.com",
    ),
    FeedSpec(
        "gnews-mna",
        "https://news.google.com/rss/search?q=merger+OR+acquisition+OR+IPO&hl=en-US&gl=US&ceid=US:en",
        "news.google.com",
    ),
    FeedSpec(
        "gnews-energy",
        "https://news.google.com/rss/search?q=oil+OR+OPEC+OR+gas+prices&hl=en-US&gl=US&ceid=US:en",
        "news.google.com",
    ),
    FeedSpec(
        "gnews-geo",
        "https://news.google.com/rss/search?q=sanctions+OR+geopolitics+OR+trade+war&hl=en-US&gl=US&ceid=US:en",
        "news.google.com",
    ),
)


# Tracking-param prefixes stripped before dedup. The set matches the de-facto
# convention used by every analytics platform that adds query params to
# article URLs — utm_*, gclid, fbclid, mc_*. Adding more here is safe; the
# only requirement is that none of these prefixes can be a legitimate
# article-identifier query param the publisher relies on.
_TRACKING_PREFIXES: tuple[str, ...] = ("utm_", "gclid", "fbclid", "mc_", "_ga", "ref_src")


# Circuit-breaker ladder — when a feed crosses CIRCUIT_BREAKER_THRESHOLD
# consecutive failed fetches, we put it on a cooldown timer instead of
# hammering it every poll cycle. The ladder is climbed each additional
# failure; once at the top step further failures keep the cooldown at the
# maximum step. A single successful fetch clears the cooldown immediately.
#
# Picked empirically: 60s shoulders intermittent 5xx, 5min handles routine
# operator outages (Cloudflare/Fastly rolling restarts), 15-30min covers
# the typical publisher maintenance window, 60min is the cap so we still
# probe the feed every hour rather than going silent until restart.
CIRCUIT_BREAKER_THRESHOLD: int = 5
BACKOFF_LADDER_SECONDS: tuple[int, ...] = (60, 300, 900, 1800, 3600)


def _compute_cooldown_until(consecutive_errors: int, now: datetime) -> datetime | None:
    """Compute the next probe time given a feed's consecutive-error count.

    Below CIRCUIT_BREAKER_THRESHOLD: returns None (no backoff). At the
    threshold and beyond, indexes into BACKOFF_LADDER_SECONDS with the
    excess (capped at the last rung). Pure + side-effect free so the test
    suite can pin every step without touching the poller loop.
    """
    if consecutive_errors < CIRCUIT_BREAKER_THRESHOLD:
        return None
    idx = min(consecutive_errors - CIRCUIT_BREAKER_THRESHOLD, len(BACKOFF_LADDER_SECONDS) - 1)
    return now + timedelta(seconds=BACKOFF_LADDER_SECONDS[idx])


# Adaptive per-source polling ladder — SEPARATE from the error circuit
# breaker above. The breaker reacts to *failures* (5xx, timeouts); this
# ladder reacts to persistent *emptiness* — a feed that fetches fine (HTTP
# 200) but yields zero NEW items cycle after cycle (think-tanks, podcasts,
# quiet regulators). Polling those every 10s wastes bandwidth and drags the
# median publisher-lag window, so we stretch their cadence the longer they
# stay dry, while high-yield firehoses (GDELT/Google News/squawk) keep
# fetching every cycle.
#
# Mapping (consecutive zero-new-item successful fetches → cycle multiplier):
#     0-2  empties → 1  (every cycle)
#     3-5  empties → 3  (every 3rd cycle)
#     6-10 empties → 6  (every 6th cycle)
#     >10  empties → 12 (every 12th cycle — the cap)
# A cycle that yields >=1 NEW item resets consecutive_empty to 0, snapping the
# feed straight back to every-cycle. Errors do NOT count as emptiness; the
# circuit breaker owns those and an errored fetch never advances this ladder.
ADAPTIVE_CADENCE_LADDER: tuple[tuple[int, int], ...] = (
    (2, 1),  # <=2 empties → every cycle
    (5, 3),  # <=5 empties → every 3rd cycle
    (10, 6),  # <=10 empties → every 6th cycle
)
ADAPTIVE_CADENCE_MAX: int = 12  # >10 empties → every 12th cycle (cap)


def _adaptive_cadence(consecutive_empty: int) -> int:
    """Map a feed's consecutive-empty count to its poll-cycle multiplier.

    Pure + side-effect free so the ladder can be pinned in isolation. A
    multiplier of N means "fetch this feed once every N cycles". Negative
    inputs are clamped to 0 (treated as freshly-yielding). See
    ADAPTIVE_CADENCE_LADDER for the rungs.
    """
    n = max(0, consecutive_empty)
    for threshold, multiplier in ADAPTIVE_CADENCE_LADDER:
        if n <= threshold:
            return multiplier
    return ADAPTIVE_CADENCE_MAX


def _canonical_url(url: str) -> str:
    """Return a normalized form of `url` suitable for dedup-keying.

    Strips: `www.` host prefix, trailing slash on non-root paths, common
    analytics tracking params (utm_*, gclid, fbclid, mc_*). Lowercases the
    host (paths are case-sensitive on most CDNs, so they're preserved).

    Falls back to the raw string on any parse failure — the cache still
    benefits from literal-key dedup in that case.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        parts = urlsplit(raw)
        if not parts.scheme or not parts.netloc:
            return raw  # not a URL we can confidently canonicalize
        host = parts.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        # Strip trailing slashes uniformly — `https://bbc.com` and
        # `https://bbc.com/` are the same resource per HTTP semantics
        # (servers treat both as the homepage), so dedup must collapse
        # them. urlunsplit handles path="" without leaving a stray slash.
        path = parts.path.rstrip("/")
        # Filter tracking params while preserving order of survivors.
        kept = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not any(k.lower().startswith(p) for p in _TRACKING_PREFIXES)
        ]
        query = urlencode(kept)
        return urlunsplit((parts.scheme.lower(), host, path, query, ""))
    except Exception:
        # Defense in depth — pathological inputs (very rare from a parsed
        # feed) shouldn't crash the poller tick.
        return raw


# Minimum number of meaningful characters a normalized title needs before it's
# eligible for cross-source near-duplicate suppression. Below this, the title
# is too generic ("Live updates", "Markets", "Watch") to safely collapse two
# distinct stories, so such titles bypass title-dedup entirely.
_TITLE_DEDUP_MIN_CHARS: int = 5

# Trailing "source attribution" suffix many aggregators append, e.g.
# "Apple beats earnings - The Verge" or "Bitcoin tops $90k | CoinDesk".
# Stripped before normalization so the SAME story carried by two outlets
# (each appending its own brand) collapses to one key. Only the LAST such
# segment is removed; an interior dash/pipe in a real headline is kept.
# Separators: ASCII hyphen, pipe, en-dash (U+2013), em-dash (U+2014) — the
# dashes are written as escapes (not literal glyphs) to avoid RUF001
# "ambiguous unicode" noise while still matching real-world titles.
_SOURCE_SUFFIX_RE = re.compile(r"\s+[-|\u2013\u2014]\s+[^-|\u2013\u2014]{1,40}$")
# Anything that isn't a word char or whitespace → dropped during normalization.
_TITLE_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACES_RE = re.compile(r"\s+")
_HTML_TAGS_RE = re.compile(r"<[^>]+>")


def _normalize_title(title: str) -> str:
    """Return a normalized dedup key for a headline (or "" to skip dedup).

    Normalization, in order:
      1. lowercase + Unicode-aware,
      2. strip ONE trailing " - Source"/" | Source" attribution suffix,
      3. remove punctuation (keeps word chars + whitespace),
      4. collapse runs of whitespace to single spaces, strip ends.

    Returns the empty string when the result has fewer than
    `_TITLE_DEDUP_MIN_CHARS` meaningful characters (whitespace removed) — such
    titles are too generic to collapse safely and therefore bypass title-dedup
    (callers treat "" as "never suppress"). Defensive: any failure returns ""
    so a pathological title can never crash the poller tick.
    """
    try:
        raw = (title or "").strip().lower()
        if not raw:
            return ""
        # Drop the trailing source-attribution segment, if present.
        raw = _SOURCE_SUFFIX_RE.sub("", raw).strip()
        # Strip punctuation, then collapse whitespace.
        stripped = _TITLE_PUNCT_RE.sub(" ", raw)
        normalized = _SPACES_RE.sub(" ", stripped).strip()
        # Too few meaningful chars (spaces don't count) → skip dedup.
        if len(normalized.replace(" ", "")) < _TITLE_DEDUP_MIN_CHARS:
            return ""
        return normalized
    except Exception:
        return ""


@dataclass
class _SeenCache:
    """Small LRU set so we don't re-emit obviously-duplicate items each tick.

    The deterministic capture_id already gives us idempotency at the storage
    layer, but cutting them out earlier saves a round-trip through the
    supervisor for every poll.

    Keys are passed through `_canonical_url` by the caller before hitting
    this cache — that strips tracking params and www. so the same article
    surfaced through multiple feeds dedups correctly.
    """

    capacity: int = 4096
    _store: OrderedDict[str, None] = field(default_factory=OrderedDict)

    def __contains__(self, key: str) -> bool:
        if key in self._store:
            self._store.move_to_end(key)
            return True
        return False

    def add(self, key: str) -> None:
        self._store[key] = None
        self._store.move_to_end(key)
        while len(self._store) > self.capacity:
            self._store.popitem(last=False)

    def discard(self, key: str) -> None:
        """Remove `key` if present; no-op otherwise (mirrors set.discard).

        Used to roll back a speculative `add()` when the ingest that the add
        was guarding fails — without it, a single transient ingest failure
        permanently drops that URL from the pipeline (the next sighting hits
        `if canon in self._seen: continue` and is silently skipped) until LRU
        eviction pushes it out ~4096 distinct URLs later.
        """
        self._store.pop(key, None)


@dataclass(frozen=True)
class ParsedItem:
    """One news item extracted from an RSS or Atom feed."""

    title: str
    text: str
    url: str
    domain: str
    published_ts: datetime


@dataclass(frozen=True)
class FeedFetchResult:
    """Fetch outcome for one feed, used for per-source health diagnostics.

    `skipped` flags entries the circuit breaker short-circuited — those
    must not count as a poll attempt against the feed (so polls/failures
    stats stay honest) but should still propagate the existing
    consecutive_errors / cooldown_until state to the UI.
    """

    spec: FeedSpec
    items: tuple[ParsedItem, ...] = ()
    status_code: int | None = None
    error: str | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    elapsed_ms: float | None = None
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return not self.skipped and self.error is None and self.status_code == 200


def _strip_html(html_text: str) -> str:
    """Cheap HTML → plain-text. Good enough for RSS descriptions."""
    if not html_text:
        return ""
    # Drop tags, collapse whitespace, unescape entities.
    text = _HTML_TAGS_RE.sub(" ", html_text)
    text = html.unescape(text)
    text = _SPACES_RE.sub(" ", text).strip()
    return text


def _resolve_domain(url: str, fallback: str) -> str:
    try:
        host = urlparse(url).hostname or ""
        # Strip the leading 'www.' so analyst-facing UI shows brand domains.
        return host[4:] if host.startswith("www.") else (host or fallback)
    except Exception:
        return fallback


def _strip_source_suffix(title: str, source_name: str | None) -> str:
    source = (source_name or "").strip()
    if not title or not source:
        return title
    suffix = f" - {source}"
    if title.endswith(suffix):
        return title[: -len(suffix)].rstrip()
    return title


@lru_cache(maxsize=2048)
def _parse_ts_cached(value: str) -> datetime:
    # Try RFC 822 (RSS) and ISO 8601 (Atom).
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (TypeError, ValueError):
        pass

    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)
        except (TypeError, ValueError):
            continue

    raise ValueError(f"Unable to parse timestamp: {value}")


def _parse_ts(value: str | None) -> datetime:
    """Best-effort timestamp parser. Returns now() on failure."""
    if value:
        try:
            return _parse_ts_cached(value)
        except (TypeError, ValueError):
            pass
    return datetime.now(UTC)


def _is_stale_published_ts(published_ts: datetime, now: datetime, max_age_seconds: float) -> bool:
    if max_age_seconds <= 0:
        return False
    published = published_ts if published_ts.tzinfo is not None else published_ts.replace(tzinfo=UTC)
    current = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return (current.astimezone(UTC) - published.astimezone(UTC)).total_seconds() > max_age_seconds


def parse_feed(body: bytes, fallback_domain: str = "") -> list[ParsedItem]:
    """Parse an RSS 2.0 or Atom feed body into a list of ParsedItem.

    Tolerant: anything we can't read gets skipped, not raised. RSS feeds
    in the wild are not standards-compliant.
    """
    from io import BytesIO

    items: list[ParsedItem] = []

    try:
        context = ET.iterparse(BytesIO(body), events=("start", "end"))
        root = None
        parent_stack: list[ET.Element] = []

        for event, elem in context:
            if event == "start":
                if root is None:
                    root = elem
                parent_stack.append(elem)
            else:
                if parent_stack:
                    parent_stack.pop()

                if elem.tag == "item":
                    title = (elem.findtext("title") or "").strip()
                    link = (elem.findtext("link") or "").strip()
                    desc = elem.findtext("description") or ""
                    content_enc = elem.find("content:encoded", _NS)
                    if content_enc is not None and content_enc.text:
                        desc = content_enc.text
                    source_el = elem.find("source")
                    source_name = (source_el.text if source_el is not None else "") or ""
                    source_url = (source_el.attrib.get("url") if source_el is not None else "") or ""
                    pub = elem.findtext("pubDate") or elem.findtext("dc:date", default=None, namespaces=_NS)
                    text = _strip_html(desc) or title
                    title = _strip_source_suffix(title, source_name)
                    if not title and text:
                        title = text[:120].rstrip()
                    if title and link and text:
                        domain = _resolve_domain(link, fallback_domain)
                        source_domain = _resolve_domain(source_url, "") if source_url else ""
                        if domain == "news.google.com" and source_domain:
                            domain = source_domain
                        items.append(
                            ParsedItem(
                                title=title,
                                text=text,
                                url=link,
                                domain=domain,
                                published_ts=_parse_ts(pub),
                            )
                        )
                    elem.clear()
                    if parent_stack:
                        parent_stack[-1].remove(elem)

                elif elem.tag == f"{{{_NS['atom']}}}entry":
                    title_el = elem.find("atom:title", _NS)
                    links = elem.findall("atom:link", _NS)
                    link_el = next(
                        (lk for lk in links if lk.attrib.get("rel") in (None, "", "alternate")),
                        links[0] if links else None,
                    )
                    link = ""
                    if link_el is not None:
                        link = (link_el.attrib.get("href") or link_el.text or "").strip()
                    summary = elem.findtext("atom:summary", default="", namespaces=_NS)
                    content = elem.findtext("atom:content", default="", namespaces=_NS)
                    pub = elem.findtext("atom:updated", default=None, namespaces=_NS) or elem.findtext(
                        "atom:published", default=None, namespaces=_NS
                    )
                    title = (title_el.text if title_el is not None else "").strip()
                    body_text = _strip_html(content or summary) or title
                    if title and link and body_text:
                        items.append(
                            ParsedItem(
                                title=title,
                                text=body_text,
                                url=link,
                                domain=_resolve_domain(link, fallback_domain),
                                published_ts=_parse_ts(pub),
                            )
                        )
                    elem.clear()
                    if parent_stack:
                        parent_stack[-1].remove(elem)
    except Exception as exc:
        logger.warning("rss_parse_error", error=str(exc))
        return []

    return items


# Register the built-in RSS/Atom parser now that parse_feed exists. Source
# packs register additional parsers ("gdelt", "reddit", …) at import time.
register_parser(
    "rss",
    lambda content, fallback_domain: list(parse_feed(content, fallback_domain=fallback_domain)),
)


def _has_max_size_param(func) -> bool:
    try:
        sig = inspect.signature(func)
        return "max_response_size_bytes" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
    except Exception:
        return False


async def fetch_feed_result(
    client: httpx.AsyncClient,
    spec: FeedSpec,
    max_response_size_bytes: int = 10 * 1024 * 1024,
) -> FeedFetchResult:
    """Fetch + parse one feed and return a structured health result.

    The body parser is selected by `spec.parser` (default "rss"), so JSON
    firehoses (GDELT, Reddit) share this exact fetch/health/dedup path.
    """
    started = time.perf_counter()
    try:
        # JSON sources want a JSON Accept header; RSS wants XML. Pick based on
        # the parser key so a non-RSS endpoint isn't sent an XML-only Accept.
        accept = (
            "application/json, */*;q=0.5"
            if spec.parser != "rss"
            else "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5"
        )
        async with client.stream(
            "GET",
            spec.url,
            headers={"User-Agent": _USER_AGENT, "Accept": accept},
            # Pass the pinned client timeout explicitly: httpx applies any
            # request-level timeout in preference to the client default, so a
            # bare float here (was 12.0) would silently expand every phase to
            # 12s and let one stalled feed outlast the 10s poll interval. Reuse
            # _HTTPX_TIMEOUT to keep connect=3/read=5 in force per fetch.
            timeout=_HTTPX_TIMEOUT,
            follow_redirects=True,
        ) as resp:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            if resp.status_code != 200:
                logger.info("rss_non200", feed=spec.name, status=resp.status_code)
                return FeedFetchResult(
                    spec=spec,
                    status_code=resp.status_code,
                    error=f"http_{resp.status_code}",
                    elapsed_ms=elapsed_ms,
                )

            # Check content-length header if present to fast-fail
            content_length = resp.headers.get("content-length")
            if content_length is not None:
                try:
                    if int(content_length) > max_response_size_bytes:
                        raise ValueError(f"Response size exceeds limit of {max_response_size_bytes} bytes")
                except ValueError as e:
                    if "exceeds limit" in str(e):
                        raise

            chunks = []
            bytes_read = 0
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                bytes_read += len(chunk)
                if bytes_read > max_response_size_bytes:
                    raise ValueError(f"Response size exceeds limit of {max_response_size_bytes} bytes")
                chunks.append(chunk)
            body = b"".join(chunks)

        return FeedFetchResult(
            spec=spec,
            items=tuple(get_parser(spec.parser)(body, spec.fallback_domain)),
            status_code=resp.status_code,
            elapsed_ms=elapsed_ms,
        )
    except ValueError as exc:
        logger.info("rss_size_exceeded", feed=spec.name, error=str(exc))
        return FeedFetchResult(
            spec=spec,
            error="ValueError",
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
    except httpx.HTTPError as exc:
        logger.info("rss_fetch_error", feed=spec.name, error=str(exc))
        return FeedFetchResult(
            spec=spec,
            error=exc.__class__.__name__,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
    except Exception as exc:  # belt and suspenders — never let one feed kill the poller
        logger.warning("rss_unexpected", feed=spec.name, error=str(exc))
        return FeedFetchResult(
            spec=spec,
            error=exc.__class__.__name__,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )


async def fetch_feed(
    client: httpx.AsyncClient,
    spec: FeedSpec,
    max_response_size_bytes: int = 10 * 1024 * 1024,
) -> list[ParsedItem]:
    """Fetch + parse one feed. Returns [] on any error so callers can keep going."""
    result = await fetch_feed_result(client, spec, max_response_size_bytes=max_response_size_bytes)
    return list(result.items)


class NewsPoller:
    """Long-lived async task: pull → dedup → ingest → repeat."""

    def __init__(
        self,
        *,
        supervisor: Supervisor,
        settings: Settings,
        feeds: Iterable[FeedSpec] | None = None,
        interval_seconds: float = 10.0,
        startup_grace_seconds: float = 3.0,
        max_item_age_seconds: float = 14 * 24 * 3600,
    ) -> None:
        self._sup = supervisor
        self._settings = settings
        self._feeds: tuple[FeedSpec, ...] = tuple(feeds) if feeds is not None else DEFAULT_FEEDS
        self._interval = max(10.0, float(interval_seconds))  # floor to keep us friendly
        self._grace = max(0.0, float(startup_grace_seconds))
        self._max_item_age_seconds = max(0.0, float(max_item_age_seconds))
        self._seen = _SeenCache()
        # Cross-source near-duplicate TITLE suppression. When many of the 187
        # sources carry the SAME story, exact-URL dedup (`self._seen`) lets
        # each through (different canonical URLs), so the feed floods with
        # near-identical headlines. This window collapses them: the FIRST
        # occurrence of a normalized title ingests; later ones within the
        # window are skipped. 0 disables (getattr fallback keeps old callers
        # that construct NewsPoller without the setting working unchanged).
        self._dedup_title_window = max(
            0.0,
            float(
                getattr(
                    getattr(settings, "news", None),
                    "dedup_title_window_seconds",
                    0.0,
                )
            ),
        )
        # Bounded LRU mapping normalized-title → first-seen time. Capped so a
        # long-running poller can't grow it without bound; oldest evicted.
        self._seen_titles: OrderedDict[str, datetime] = OrderedDict()
        self._seen_titles_cap: int = 5000
        # Adaptive per-source polling. When enabled, a feed that keeps fetching
        # OK but returning zero NEW items backs off to a longer cadence (see
        # `_adaptive_cadence`), saving bandwidth and keeping the median lag
        # window honest. getattr fallback keeps stub-settings callers (and the
        # default) working unchanged; default True matches NewsConfig.
        self._adaptive_polling_enabled = bool(
            getattr(
                getattr(settings, "news", None),
                "adaptive_polling_enabled",
                True,
            )
        )
        self._max_response_size_bytes = int(
            getattr(
                getattr(settings, "news", None),
                "max_response_size_bytes",
                10 * 1024 * 1024,
            )
        )
        # Monotonic count of completed poll cycles — the modulo base the
        # adaptive cadence uses to decide whether a backed-off feed is "due".
        self._cycle_index: int = 0
        # Per-feed: the cycle index at which each feed becomes due to fetch
        # again. Absent feeds are always due (fetched on the next cycle). Only
        # consulted when adaptive polling is enabled.
        self._feed_next_due_cycle: dict[str, int] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()  # serializes concurrent poll_now calls
        self._client: httpx.AsyncClient | None = None
        # Persistent diagnostics for /ui/news-status.
        self.last_run_at: datetime | None = None
        self.last_ingested: int = 0
        self.total_ingested: int = 0
        self.last_error: str | None = None
        self.is_polling: bool = False
        self.next_run_at: datetime | None = None
        # When did we last ingest at least one NEW item? Distinct from
        # last_run_at (which ticks every poll, including zero-result polls).
        # Lets the UI show "last new arrival: 12m ago" so the analyst sees
        # the system is healthy even when the publisher side is quiet.
        self.last_new_at: datetime | None = None
        # Consecutive ticks with no new items. Helps the UI distinguish
        # "actively flowing" from "alive but quiet".
        self.empty_ticks: int = 0
        # Per-poll publisher-lag stats — `now - item.published_ts` at ingest
        # time. The UI surfaces these so the analyst can see that the
        # visible "X min ago" gap is mostly publisher-side RSS lag, not
        # our pipeline (which adds ~4ms in stub mode, ~100ms with real ML).
        self.last_avg_publisher_lag_seconds: float | None = None
        self.last_median_publisher_lag_seconds: float | None = None
        self.feed_health: dict[str, dict[str, object]] = {}
        self.last_stale_skipped: int = 0
        # Per-tick count of items dropped by cross-source near-duplicate TITLE
        # suppression (mirrors last_stale_skipped). Surfaced in tick logging so
        # an operator can see how much the 187-source firehose is collapsing.
        self.last_dupe_titles_skipped: int = 0

    # ── public read-only accessors ─────────────────────────────────────────
    # External callers (api.py news-status endpoint) need these for their
    # response payload. Exposing them as @property keeps internal mutation
    # gated through __init__ — assigning to e.g. `poller.feeds = (...)`
    # raises AttributeError, so config-time invariants (interval floor,
    # frozen feed tuple) cannot be quietly overwritten at runtime.

    @property
    def feeds(self) -> tuple[FeedSpec, ...]:
        """The (frozen) tuple of feed specs this poller fetches."""
        return self._feeds

    @property
    def interval_seconds(self) -> float:
        """Seconds between scheduled polls (clamped at construction time)."""
        return self._interval

    @property
    def max_item_age_seconds(self) -> float:
        """Items older than this (by `published_ts`) are skipped at ingest."""
        return self._max_item_age_seconds

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        # asyncio.get_running_loop() is the Python 3.10+ idiom — it requires
        # a running loop (which FastAPI's `lifespan` provides) and raises if
        # called from sync context, surfacing misuse instead of silently
        # creating a stray loop. `asyncio.get_event_loop()` was deprecated
        # in 3.12 and slated for removal in 3.16.
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._run(), name="catchem-news-poller")
        logger.info("news_poller_started", feeds=len(self._feeds), interval=self._interval)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("news_poller_stopped")

    async def poll_now(self) -> int:
        """Trigger an immediate poll, bypassing the scheduled sleep.

        Used by `POST /ui/news-poll-now` so the UI's "Poll now" button can
        force activity on demand. Returns the number of items ingested.
        Safe to call concurrently — the internal lock serializes runs.

        Runs its OWN immediate tick under `self._lock` and returns that tick's
        ingested count. It deliberately does NOT wake the background sleep:
        doing both (a manual tick AND a poke-triggered background tick) made a
        single button press fetch every feed twice. The background schedule
        simply carries on from where it was, so one press = one fetch pass.
        """
        if self._client is None:
            # Poller hasn't entered its main loop yet; create an ephemeral
            # client so the manual trigger still works during startup grace.
            # Explicit timeout: a slow feed must not stall the manual button.
            # The lock is held here too — two grace-window presses (or a press
            # racing the first background tick the instant grace ends) would
            # otherwise run unlocked ticks that corrupt the shared _seen /
            # _seen_titles OrderedDicts, feed_health, and total_ingested.
            async with self._lock:
                async with httpx.AsyncClient(timeout=_HTTPX_TIMEOUT) as client:
                    return await self._run_one_tick(client)
        async with self._lock:
            return await self._run_one_tick(self._client)

    async def probe_feed_async(self, url: str) -> dict[str, object]:
        """Fetch one configured feed by URL, bypassing the circuit-breaker cooldown.

        Powers the UI per-feed probe button. The fetch goes through the
        same FeedFetchResult path as `_poll_once`, so a successful probe
        closes the breaker via `_record_feed_result` and a failed probe
        climbs the backoff ladder one more rung. New items are ingested
        through the shared supervisor (same as the regular poll). Returns
        the freshly-updated feed_health snapshot for the requested feed.

        Concurrency: the entire body — cooldown clear, fetch, health
        record, dedup, ingest — runs under ``self._lock`` so a manual
        probe and the background ``_run_one_tick`` cannot fight on
        ``self._seen`` (an OrderedDict whose iteration order is corrupted
        by concurrent ``move_to_end`` / ``popitem`` / ``__setitem__``),
        ``self.total_ingested`` (read-modify-write integer), or
        ``self.last_error`` (race-free string). Without the lock a
        Python-side data race could:
          (a) silently drop a probe-discovered URL from ``_seen`` when
              the background tick LRU-evicts it mid-iteration,
          (b) double-count a ``total_ingested`` increment because both
              tasks read the same int before either writes back, or
          (c) leave ``last_error`` pointing at the OTHER task's failure.

        Raises:
            KeyError: if no configured feed matches `url`. Callers (API
                layer) translate this into a 404.
        """
        # Map URL → FeedSpec. The configured tuple is small (<100), linear
        # scan is fine and keeps the public surface URL-keyed.
        spec: FeedSpec | None = next((f for f in self._feeds if f.url == url), None)
        if spec is None:
            raise KeyError(url)

        from .demo import _deterministic_capture_id

        async with self._lock:
            # Clear any existing cooldown so a future regular tick doesn't
            # skip the feed before the breaker actually closes via the fetch
            # result. We only mutate cooldown_until/backed_off; everything
            # else carries through to `_record_feed_result`.
            prev = self.feed_health.get(spec.name, {})
            if prev.get("cooldown_until") is not None or prev.get("backed_off"):
                snapshot = dict(prev)
                snapshot["cooldown_until"] = None
                snapshot["backed_off"] = False
                self.feed_health[spec.name] = snapshot
                logger.info("rss_circuit_probed_manual", feed=spec.name)

            # Use the shared client when the poller's running loop owns one;
            # otherwise spin up an ephemeral one (mirrors poll_now's pattern).
            if self._client is not None:
                if _has_max_size_param(fetch_feed_result):
                    result = await fetch_feed_result(
                        self._client, spec, max_response_size_bytes=self._max_response_size_bytes
                    )
                else:
                    result = await fetch_feed_result(self._client, spec)
            else:
                async with httpx.AsyncClient(timeout=_HTTPX_TIMEOUT) as client:
                    if _has_max_size_param(fetch_feed_result):
                        result = await fetch_feed_result(
                            client, spec, max_response_size_bytes=self._max_response_size_bytes
                        )
                    else:
                        result = await fetch_feed_result(client, spec)

            self._record_feed_result(result)

            # Ingest any genuinely-new items the probe surfaced (same dedup +
            # storage-guard pipeline as `_poll_once`, scaled to one feed).
            now = datetime.now(UTC)
            new_items: list[ParsedItem] = []
            stale_skipped = 0
            dupe_titles_skipped = 0
            for item in result.items:
                if _is_stale_published_ts(item.published_ts, now, self._max_item_age_seconds):
                    stale_skipped += 1
                    continue
                canon = _canonical_url(item.url)
                if canon in self._seen:
                    continue
                self._seen.add(canon)
                cap_id = _deterministic_capture_id(item.text, item.url)
                if self._sup.storage.get_record(cap_id) is not None:
                    continue
                # Same cross-source near-duplicate TITLE gate the regular tick
                # uses, so a manual probe can't smuggle in a headline a recent
                # poll already ingested (and vice versa — shared _seen_titles).
                if self._is_duplicate_title(item.title, now):
                    dupe_titles_skipped += 1
                    continue
                new_items.append(item)
            self.last_dupe_titles_skipped = dupe_titles_skipped

            ingested_ok = 0
            for item in new_items:
                try:
                    await asyncio.to_thread(self._ingest_one, item)
                except Exception as exc:
                    logger.info("news_ingest_failed", feed=spec.name, url=item.url, error=str(exc))
                    # Ingest failed → undo BOTH dedup records so a later sighting
                    # isn't suppressed behind a story that never landed. The
                    # `_seen` (canonical-URL) rollback is load-bearing: the
                    # next sighting of the SAME URL short-circuits on
                    # `if canon in self._seen` before the title check, so the
                    # title rollback alone wouldn't help same-URL re-sightings.
                    self._seen.discard(_canonical_url(item.url))
                    self._rollback_title(item.title)
                else:
                    ingested_ok += 1
            # Manual probes feed into the same total_ingested counter the
            # background tick maintains, so the Sources page totals stay honest.
            # Count only items that ACTUALLY ingested — a swallowed failure must
            # not inflate the total. Increment is inside the lock so a concurrent
            # tick can't lose this update to a read-modify-write race.
            if ingested_ok:
                self.total_ingested += ingested_ok

            return dict(self.feed_health.get(spec.name, {}))

    async def _run_one_tick(self, client: httpx.AsyncClient) -> int:
        """One observable poll cycle: flip is_polling, count, record stats."""
        self.is_polling = True
        try:
            n = await self._poll_once(client)
            self.last_ingested = n
            self.total_ingested += n
            now = datetime.now(UTC)
            self.last_run_at = now
            if n > 0:
                self.last_new_at = now
                self.empty_ticks = 0
            else:
                self.empty_ticks += 1
            self.last_error = None
            await asyncio.to_thread(self._sup.storage.checkpoint)
            return n
        except Exception as exc:
            self.last_error = repr(exc)
            logger.warning("news_poller_tick_failed", error=str(exc))
            return 0
        finally:
            self.is_polling = False

    async def _run(self) -> None:
        # Brief grace so the first tick doesn't race the rest of startup.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=self._grace)
            return  # asked to stop during grace
        except TimeoutError:
            pass

        async with httpx.AsyncClient(timeout=_HTTPX_TIMEOUT) as client:
            self._client = client
            try:
                while not self._stop.is_set():
                    async with self._lock:
                        await self._run_one_tick(client)
                    if self._stop.is_set():
                        break
                    # Compute the next-scheduled time so the UI can show
                    # a "next poll in Xs" countdown.
                    from datetime import timedelta

                    self.next_run_at = datetime.now(UTC) + timedelta(seconds=self._interval)
                    # Sleep for the interval, but wake early on stop(). A
                    # manual `poll_now()` no longer interrupts this sleep — it
                    # runs its own tick under `self._lock` and the background
                    # schedule continues, so one "Poll now" press costs a
                    # single fetch pass rather than two.
                    stop_wait = asyncio.create_task(self._stop.wait())
                    _done, pending = await asyncio.wait(
                        {stop_wait},
                        timeout=self._interval,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
            finally:
                self._client = None

    async def _poll_once(self, client: httpx.AsyncClient) -> int:
        """One pass over all feeds. Returns number of NEW records ingested.

        Two-phase pipeline:
          (1) Fetch all feeds in parallel via asyncio.gather (network-bound).
          (2) For each NEW item, run process_capture() in a worker thread
              via asyncio.to_thread, with a small concurrency cap. The
              shared supervisor's service is read-mostly during process()
              and storage.insert_record holds the SQLite write lock
              briefly — 4 workers is enough to parallelise without
              contention.

        Why parallelise:
          With ML stubs each process_capture is ~4 ms, so even serial
          processing of 300 items costs ~1.2 s — not the bottleneck for
          stub mode. But with real ML (use_ml_stubs=false), per-item cost
          jumps to ~100 ms; serial processing of 300 items would be
          ~30 s and would dominate the user-visible latency. Parallel
          ingest keeps the wall-clock bounded by the slowest item, not
          their sum, so the system performs the same in stub and real-ML
          modes from the user's seat.

        Latency tracking:
          For each ingested item, we record `created_at - item.published_ts`
          — the gap between publisher's claimed publication time and our
          ingest time. Averaged into `last_avg_publisher_lag_seconds` for
          the UI, so the analyst can see that most of the visible "10m
          ago" gap comes from publisher RSS lag, not our pipeline.
        """
        from .demo import _deterministic_capture_id

        # Phase 1: parallel fetch — but skip any feed currently in
        # circuit-breaker cooldown. Cooldown check is O(1) per feed: an
        # ISO-string compare against the cached `feed_health` entry. If
        # the cooldown expired we drop it now so the fetch proceeds and
        # `_record_feed_result` can reset state on success.
        now_for_cooldown = datetime.now(UTC)
        # Advance the cycle counter once per pass. The adaptive cadence gate
        # compares each feed's `next_due_cycle` against this index. Errors and
        # cooldowns are independent of this — only emptiness moves the ladder.
        this_cycle = self._cycle_index
        self._cycle_index += 1
        feeds_to_fetch: list[FeedSpec] = []
        backed_off_results: list[FeedFetchResult] = []
        for spec in self._feeds:
            cooldown_iso = self.feed_health.get(spec.name, {}).get("cooldown_until")
            if cooldown_iso:
                try:
                    cooldown_dt = datetime.fromisoformat(str(cooldown_iso))
                except ValueError:
                    cooldown_dt = None
                if cooldown_dt is not None and now_for_cooldown < cooldown_dt:
                    backed_off_results.append(
                        FeedFetchResult(
                            spec=spec,
                            skipped=True,
                            fetched_at=now_for_cooldown,
                        )
                    )
                    continue
                # Cooldown expired — null it out before the fetch so a
                # second failure climbs the next ladder step rather than
                # re-reading the now-stale value.
                if cooldown_dt is not None:
                    snapshot = dict(self.feed_health.get(spec.name, {}))
                    snapshot["cooldown_until"] = None
                    snapshot["backed_off"] = False
                    self.feed_health[spec.name] = snapshot
                    logger.info("rss_circuit_probing", feed=spec.name)
            # Adaptive emptiness gate — SEPARATE from the cooldown breaker
            # above. A persistently-empty feed (but otherwise healthy) is only
            # fetched once it reaches its `next_due_cycle`; on non-due cycles we
            # skip it entirely (no synthetic result — it neither succeeded nor
            # failed, so polls/errors/health stay frozen, exactly like it was
            # never scheduled). Disabled → every feed is always due.
            if self._adaptive_polling_enabled and this_cycle < self._feed_next_due_cycle.get(spec.name, 0):
                continue
            feeds_to_fetch.append(spec)

        if _has_max_size_param(fetch_feed_result):
            fetched_results = await asyncio.gather(
                *[
                    fetch_feed_result(client, s, max_response_size_bytes=self._max_response_size_bytes)
                    for s in feeds_to_fetch
                ]
            )
        else:
            fetched_results = await asyncio.gather(*[fetch_feed_result(client, s) for s in feeds_to_fetch])
        # Combine real fetches + synthetic skipped results so downstream
        # health/ingest code sees one entry per configured feed.
        results: list[FeedFetchResult] = list(fetched_results) + backed_off_results
        for result in results:
            self._record_feed_result(result)

        # Filter to genuinely-new items (URL not seen + capture_id not in
        # storage). This happens on the event loop — both checks are cheap.
        new_items: list[tuple[FeedSpec, ParsedItem]] = []
        stale_skipped = 0
        dupe_titles_skipped = 0
        # Per-feed NEW-item tally — drives the adaptive emptiness ladder below.
        # Only the post-dedup "genuinely new" count matters: a feed re-serving
        # the same 100 items every cycle is *empty* for our purposes.
        new_per_feed: dict[str, int] = {}
        now = datetime.now(UTC)
        for result in results:
            for item in result.items:
                if _is_stale_published_ts(item.published_ts, now, self._max_item_age_seconds):
                    stale_skipped += 1
                    continue
                canon = _canonical_url(item.url)
                if canon in self._seen:
                    continue
                self._seen.add(canon)
                cap_id = _deterministic_capture_id(item.text, item.url)
                if self._sup.storage.get_record(cap_id) is not None:
                    continue
                # Cross-source near-duplicate TITLE suppression — runs AFTER
                # the exact-URL + storage guards so the first occurrence of a
                # story still ingests; later outlets carrying the same headline
                # within the window are dropped here.
                if self._is_duplicate_title(item.title, now):
                    dupe_titles_skipped += 1
                    continue
                new_items.append((result.spec, item))
                new_per_feed[result.spec.name] = new_per_feed.get(result.spec.name, 0) + 1
        self.last_stale_skipped = stale_skipped
        self.last_dupe_titles_skipped = dupe_titles_skipped

        # Update the adaptive emptiness ladder for every feed we actually
        # FETCHED this cycle and that returned OK (HTTP 200, no error). A feed
        # that yielded >=1 new item resets to every-cycle; a zero-new-item
        # success advances consecutive_empty and stretches the cadence. Errored
        # / cooldown-skipped / not-due feeds are intentionally untouched — the
        # circuit breaker owns failures and emptiness only accrues on success.
        ok_by_name = {r.spec.name: r for r in results if r.ok}
        for spec in feeds_to_fetch:
            result = ok_by_name.get(spec.name)
            if result is None:
                continue  # errored this cycle → breaker's domain, not ours
            self._record_adaptive_yield(spec.name, new_per_feed.get(spec.name, 0), this_cycle)

        if not new_items:
            return 0

        # Phase 2: chunk and process in batches to exploit ML model parallelism
        replay_cfg = getattr(self._settings, "replay", None)
        batch_size = getattr(replay_cfg, "batch_size", 32) if replay_cfg is not None else 32
        publisher_lags_s: list[float] = []

        captures_to_process = []
        item_by_id = {}
        for spec, item in new_items:
            cap = build_capture(
                title=item.title,
                text=item.text,
                domain=item.domain,
                url=item.url,
                published_ts=item.published_ts,
                source_type="rss",
            )
            try:
                archive_root = self._settings.paths.catchem_output_dir / "live-news"
                archive_root.mkdir(parents=True, exist_ok=True)
                write_jsonl(cap, archive_root)
            except OSError as exc:
                logger.info("news_archive_failed", url=item.url, error=str(exc))
            captures_to_process.append(cap)
            item_by_id[cap.capture_id] = (spec, item)

        try:
            is_ingest_one_patched = (self._ingest_one.__func__ is not NewsPoller._ingest_one)
        except AttributeError:
            is_ingest_one_patched = True

        use_batch = not is_ingest_one_patched and hasattr(self._sup, "process_captures_batch")

        if use_batch:
            batches = [captures_to_process[i : i + batch_size] for i in range(0, len(captures_to_process), batch_size)]
            gathered = []
            for batch in batches:
                try:
                    batch_results = await asyncio.to_thread(self._sup.process_captures_batch, batch)
                    gathered.extend(batch_results)
                except Exception as batch_exc:
                    logger.error("news_poller_batch_processing_failed", error=str(batch_exc))
                    gathered.extend([False] * len(batch))
        else:
            sem = asyncio.Semaphore(4)

            async def _ingest_one_async(spec: FeedSpec, item: ParsedItem) -> bool:
                async with sem:
                    try:
                        await asyncio.to_thread(self._ingest_one, item)
                    except Exception as exc:
                        logger.info(
                            "news_ingest_failed feed=%s url=%s error=%s",
                            spec.name,
                            item.url,
                            exc,
                        )
                        return False
                return True

            gathered = await asyncio.gather(
                *(_ingest_one_async(item_by_id[cap.capture_id][0], item_by_id[cap.capture_id][1]) for cap in captures_to_process)
            )

        ingested = 0
        for cap, ok in zip(captures_to_process, gathered, strict=True):
            spec, item = item_by_id[cap.capture_id]
            if ok:
                ingested += 1
                if item.published_ts is not None:
                    publisher_lags_s.append((datetime.now(UTC) - item.published_ts).total_seconds())
            else:
                self._seen.discard(_canonical_url(item.url))
                self._rollback_title(item.title)

        # Compute publisher-lag stats but exclude obviously-old items.
        # Google News and a few wires return historical results matching
        # the search query, not only real-time hits — including those
        # would balloon the median to "48h" and mislead the analyst
        # about real-time freshness. We only consider items whose
        # `published_ts` is within the last 4 hours (the realistic
        # window for "now-ish" news).
        FRESH_WINDOW_S = 4 * 3600
        fresh_lags = [lag for lag in publisher_lags_s if 0 <= lag <= FRESH_WINDOW_S]
        if fresh_lags:
            self.last_avg_publisher_lag_seconds = sum(fresh_lags) / len(fresh_lags)
            srt = sorted(fresh_lags)
            self.last_median_publisher_lag_seconds = srt[len(srt) // 2]
        elif publisher_lags_s:
            # No fresh items this tick — clear the stat rather than show stale data.
            self.last_avg_publisher_lag_seconds = None
            self.last_median_publisher_lag_seconds = None
        if ingested:
            logger.info(
                "news_poll_ingested count=%d fresh_count=%d dupe_titles=%d stale=%d avg_pub_lag=%.0fs median_pub_lag=%.0fs",
                ingested,
                len(fresh_lags),
                self.last_dupe_titles_skipped,
                self.last_stale_skipped,
                self.last_avg_publisher_lag_seconds or 0,
                self.last_median_publisher_lag_seconds or 0,
            )
        return ingested

    def _ingest_one(self, item: ParsedItem) -> None:
        """Process one item through the *shared* supervisor — fast path.

        The previous implementation spawned a brand-new Supervisor per
        item (re-init storage, re-init service, re-load symbol mapper —
        roughly 1-2s of CPU each). On a first poll of 195 items that
        meant ~5 minutes of serial work blocking the asyncio loop, which
        in turn meant the next 60s tick was always way overdue and the
        feed felt frozen.

        `process_capture` reuses the already-warm service + storage
        handle from `__init__`. A persisted JSONL copy is still written
        to `live-news/` so a future replay can re-process the same
        stream offline, but it's no longer on the hot path.
        """
        cap = build_capture(
            title=item.title,
            text=item.text,
            domain=item.domain,
            url=item.url,
            published_ts=item.published_ts,
            source_type="rss",
        )
        # Persist a JSONL copy so the stream is replayable later. Best-effort;
        # failure here must not block the ingest.
        try:
            archive_root = self._settings.paths.catchem_output_dir / "live-news"
            archive_root.mkdir(parents=True, exist_ok=True)
            write_jsonl(cap, archive_root)
        except OSError as exc:
            logger.info("news_archive_failed", url=item.url, error=str(exc))
        # Hot path: warm supervisor → service → storage. The capture_id is
        # deterministic so insert_record is upsert-safe (PRIMARY KEY).
        self._sup.process_capture(cap)

    def _is_duplicate_title(self, title: str, now: datetime) -> bool:
        """Cross-source near-duplicate gate. Returns True → caller skips item.

        Disabled (always False) when `self._dedup_title_window <= 0`. A title
        that normalizes to "" (too generic / too short) also returns False —
        we never suppress those. Otherwise: if the normalized title was seen
        within the window, this is a later duplicate → True (skip, keep the
        earliest). On a first/expired sighting it records title→now (LRU,
        oldest evicted past the cap) and returns False so the item ingests.

        Side-effecting by design: it both DECIDES and RECORDS, so the two
        ingest paths (`_poll_once`, `probe_feed_async`) share one source of
        truth and can't drift. Both call sites run under `self._lock` already,
        so the OrderedDict mutation here is race-free.
        """
        if self._dedup_title_window <= 0:
            return False
        key = _normalize_title(title)
        if not key:
            return False  # generic/short title → bypass title-dedup
        prev = self._seen_titles.get(key)
        if prev is not None:
            elapsed = (now - prev).total_seconds()
            if 0 <= elapsed <= self._dedup_title_window:
                # Within window → later duplicate. Refresh recency so the LRU
                # keeps a hot story resident, but DON'T move first-seen time
                # forward (we keep suppressing relative to the earliest sight).
                self._seen_titles.move_to_end(key)
                return True
        # First sighting (or the prior one aged out of the window): record now
        # as the new first-seen time and let this item through.
        self._seen_titles[key] = now
        self._seen_titles.move_to_end(key)
        while len(self._seen_titles) > self._seen_titles_cap:
            self._seen_titles.popitem(last=False)
        return False

    def _rollback_title(self, title: str) -> None:
        """Undo the `_is_duplicate_title` first-sighting record after the
        ingest that title was let through for FAILED.

        `_is_duplicate_title` is a side-effecting gate: the first time it sees
        a headline it records ``title→now`` and returns False so the item
        ingests. But ingest happens LATER (the phase-2 thread pool in
        `_poll_once`, the ingest loop in `probe_feed_async`) and can raise —
        leaving the title marked "seen" even though no copy ever landed. Every
        later outlet carrying the same headline would then be silently
        suppressed for the whole dedup window, and the story would never reach
        the feed until the window expired.

        Rolling the title back out of `_seen_titles` on ingest failure means
        the NEXT sighting (another outlet this cycle, or the next tick) is
        treated as a fresh first-seen and gets a real chance to ingest.
        Idempotent and safe if the key was already LRU-evicted past the cap or
        the title normalized to "" — we pop-with-default. Runs on the event
        loop under `self._lock`, same as the record, so no race.
        """
        key = _normalize_title(title)
        if key:
            self._seen_titles.pop(key, None)

    def _record_adaptive_yield(self, feed_name: str, new_count: int, this_cycle: int) -> None:
        """Fold one successful fetch's NEW-item count into the adaptive ladder.

        Called once per feed per cycle, AFTER `_record_feed_result`, and ONLY
        for feeds that fetched OK this cycle (see `_poll_once`). Mutates the
        feed's `feed_health` entry in place with the emptiness telemetry:

          * `consecutive_empty` — zero-new-item successes in a row (reset to 0
            on any cycle that yields >=1 new item),
          * `adaptive_cadence`  — current poll-cycle multiplier (`_adaptive_cadence`),
          * `total_new_items`   — cumulative new items since boot.

        Also schedules the next due cycle (`this_cycle + cadence`) so the
        `_poll_once` gate can cheaply skip the feed until then. No-op on the
        cadence schedule when adaptive polling is disabled, but the telemetry
        keys are still maintained so the UI can show them either way.
        """
        prev = self.feed_health.get(feed_name)
        if prev is None:
            # Defensive: _record_feed_result runs first and always seeds the
            # entry, so this should never fire. If it does, skip silently
            # rather than crash the tick.
            return
        if new_count > 0:
            consecutive_empty = 0
        else:
            consecutive_empty = int(prev.get("consecutive_empty", 0)) + 1
        cadence = _adaptive_cadence(consecutive_empty)
        total_new_items = int(prev.get("total_new_items", 0)) + max(0, new_count)
        prev["consecutive_empty"] = consecutive_empty
        prev["adaptive_cadence"] = cadence
        prev["total_new_items"] = total_new_items
        # Schedule the next fetch. With adaptive polling off, cadence is still
        # surfaced but the gate ignores next_due, so every-cycle behavior holds.
        self._feed_next_due_cycle[feed_name] = this_cycle + cadence

    def _record_feed_result(self, result: FeedFetchResult) -> None:
        prev = self.feed_health.get(result.spec.name, {})
        if result.skipped:
            # Cooldown short-circuit: don't bump total_fetches/total_errors
            # (the network attempt never happened) but DO update the
            # last_fetch_at sentinel so the UI can show "we last considered
            # this feed N seconds ago". `backed_off` carries through to
            # /api/news/sources for the dedicated UI badge.
            snapshot: dict[str, object] = dict(prev)
            snapshot.update(
                {
                    "name": result.spec.name,
                    "url": result.spec.url,
                    "fallback_domain": result.spec.fallback_domain,
                    "ok": False,
                    "backed_off": True,
                    "item_count": 0,
                    # items_total and consecutive_errors are intentionally
                    # preserved from `prev` so the stat persists across the
                    # cooldown window.
                    "items_total": int(prev.get("items_total", 0)),
                    "total_fetches": int(prev.get("total_fetches", 0)),
                    "total_errors": int(prev.get("total_errors", 0)),
                    "consecutive_errors": int(prev.get("consecutive_errors", 0)),
                    "cooldown_until": prev.get("cooldown_until"),
                    "status_code": prev.get("status_code"),
                    "error": prev.get("error"),
                    "last_success_at": prev.get("last_success_at"),
                    "last_failure_at": prev.get("last_failure_at"),
                    "elapsed_ms": None,
                    # Preserve adaptive-polling telemetry across a circuit-breaker
                    # cooldown so the emptiness stats don't reset when failures
                    # (a different axis) pause the feed.
                    "consecutive_empty": int(prev.get("consecutive_empty", 0)),
                    "adaptive_cadence": int(prev.get("adaptive_cadence", 1)),
                    "total_new_items": int(prev.get("total_new_items", 0)),
                }
            )
            self.feed_health[result.spec.name] = snapshot
            return
        ok = result.ok
        total_fetches = int(prev.get("total_fetches", 0)) + 1
        total_errors = int(prev.get("total_errors", 0)) + (0 if ok else 1)
        consecutive_errors = 0 if ok else int(prev.get("consecutive_errors", 0)) + 1
        # Circuit breaker: schedule a cooldown once consecutive failures
        # cross the threshold. Successful fetch clears the cooldown so the
        # feed comes straight back online instead of waiting out the
        # remaining window.
        cooldown_until: str | None
        if ok:
            cooldown_until = None
            if prev.get("cooldown_until") is not None:
                logger.info(
                    "rss_circuit_closed",
                    feed=result.spec.name,
                    prior_consecutive_errors=int(prev.get("consecutive_errors", 0)),
                )
        else:
            cooldown_dt = _compute_cooldown_until(consecutive_errors, result.fetched_at)
            cooldown_until = cooldown_dt.isoformat() if cooldown_dt else None
            if cooldown_dt is not None and prev.get("cooldown_until") != cooldown_until:
                logger.info(
                    "rss_circuit_opened",
                    feed=result.spec.name,
                    consecutive_errors=consecutive_errors,
                    cooldown_seconds=int((cooldown_dt - result.fetched_at).total_seconds()),
                    cooldown_until=cooldown_until,
                )
        # Cumulative items_total — sum of item_count across every fetch this
        # process has observed. Distinct from `item_count` (the last-tick
        # value) so the Sources page can show a per-feed "items fetched
        # since boot" number without re-deriving it. Reset on restart, same
        # as every other in-memory poller stat.
        items_total = int(prev.get("items_total", 0)) + len(result.items)
        snapshot = {
            "name": result.spec.name,
            "url": result.spec.url,
            "fallback_domain": result.spec.fallback_domain,
            "ok": ok,
            "backed_off": False,
            "status_code": result.status_code,
            "error": result.error,
            "item_count": len(result.items),
            "items_total": items_total,
            "last_fetch_at": result.fetched_at.isoformat(),
            "elapsed_ms": result.elapsed_ms,
            "total_fetches": total_fetches,
            "total_errors": total_errors,
            "consecutive_errors": consecutive_errors,
            "cooldown_until": cooldown_until,
            "last_success_at": result.fetched_at.isoformat() if ok else prev.get("last_success_at"),
            "last_failure_at": result.fetched_at.isoformat() if not ok else prev.get("last_failure_at"),
            # Adaptive-polling telemetry — preserved across this rebuild so the
            # ladder accumulates. `_record_adaptive_yield` (called right after,
            # only on OK fetches) recomputes consecutive_empty/adaptive_cadence
            # and bumps total_new_items from these carried-over values. Defaults
            # describe a fresh, every-cycle feed.
            "consecutive_empty": int(prev.get("consecutive_empty", 0)),
            "adaptive_cadence": int(prev.get("adaptive_cadence", 1)),
            "total_new_items": int(prev.get("total_new_items", 0)),
        }
        self.feed_health[result.spec.name] = snapshot

    def feed_health_snapshot(self) -> list[dict[str, object]]:
        return [dict(v) for _, v in sorted(self.feed_health.items())]


__all__ = [
    "ADAPTIVE_CADENCE_LADDER",
    "ADAPTIVE_CADENCE_MAX",
    "BACKOFF_LADDER_SECONDS",
    "CIRCUIT_BREAKER_THRESHOLD",
    "DEFAULT_FEEDS",
    "FeedFetchResult",
    "FeedSpec",
    "NewsPoller",
    "ParsedItem",
    "_adaptive_cadence",
    "_compute_cooldown_until",
    "_is_stale_published_ts",
    "_normalize_title",
    "fetch_feed",
    "fetch_feed_result",
    "parse_feed",
]
