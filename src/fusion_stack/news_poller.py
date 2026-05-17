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
  * Off by default in tests (`FUSION_NEWS__POLLER_ENABLED=false`).
  * UA strings on every fetch — many feeds 403 the default httpx UA.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable
from urllib.parse import urlparse

import httpx

from .demo import build_capture, write_jsonl
from .logging import get_logger
from .settings import Settings
from .supervisor import Supervisor

logger = get_logger("fusion.news_poller")

# Pre-flight checks: keep UA distinctive so admins can spot us in their
# server logs. macOS Catchem identifies itself as Catchem-News-Poller/0.1.
_USER_AGENT = "Catchem-News-Poller/0.1 (+local fusion_stack sidecar)"

# Atom/RSS namespace bag — defensive over many feed variants.
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "media": "http://search.yahoo.com/mrss/",
}


@dataclass(frozen=True)
class FeedSpec:
    """One configured feed source."""

    name: str
    url: str
    # Default domain to attribute when an item lacks one (rare but happens).
    fallback_domain: str = ""


# Curated default set — public, no-auth, stable over years. Each one is a
# clean RSS/Atom endpoint with a sensible Title + Description + Link triple.
# Operators can override via FUSION_NEWS__FEEDS but the defaults Just Work.
#
# Coverage strategy: broad enough that *every* poll has a non-trivial chance
# of surfacing a new URL (mainstream + financial + tech + crypto + regulator).
# Each candidate was live-tested 2026-05-17; sources known to 404 or 403 the
# common UA are intentionally absent. If you add new sources, run
#   python -c "import asyncio, httpx; from fusion_stack.news_poller import \
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
    FeedSpec("marketwatch-top", "https://feeds.content.dowjones.io/public/rss/mw_topstories", "marketwatch.com"),
    FeedSpec("marketwatch-marketpulse", "https://feeds.content.dowjones.io/public/rss/mw_marketpulse", "marketwatch.com"),
    FeedSpec("marketwatch-bulletins", "https://feeds.content.dowjones.io/public/rss/mw_bulletins", "marketwatch.com"),
    FeedSpec("marketwatch-realtime", "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines", "marketwatch.com"),
    FeedSpec("yahoo-finance", "https://finance.yahoo.com/news/rssindex", "finance.yahoo.com"),
    FeedSpec("seekingalpha", "https://seekingalpha.com/feed.xml", "seekingalpha.com"),
    FeedSpec("benzinga-news", "https://www.benzinga.com/news/feed", "benzinga.com"),
    FeedSpec("investing-news", "https://www.investing.com/rss/news.rss", "investing.com"),
    FeedSpec("zerohedge", "https://feeds.feedburner.com/zerohedge/feed", "zerohedge.com"),
    # ── Press wires (high volume — many per hour)
    FeedSpec("prnewswire-all", "https://www.prnewswire.com/rss/all-news-releases-list.rss", "prnewswire.com"),
    FeedSpec("prnewswire-financial", "https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss", "prnewswire.com"),
    # ── Regulators / central banks
    FeedSpec("fed-press-all", "https://www.federalreserve.gov/feeds/press_all.xml", "federalreserve.gov"),
    FeedSpec("sec-edgar-current", "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=&output=atom", "sec.gov"),
    FeedSpec("ecb-press", "https://www.ecb.europa.eu/rss/press.html", "ecb.europa.eu"),
    # ── Crypto
    FeedSpec("coindesk-business", "https://www.coindesk.com/arc/outboundfeeds/rss?outputType=xml", "coindesk.com"),
    FeedSpec("decrypt-crypto", "https://decrypt.co/feed", "decrypt.co"),
    FeedSpec("theblock", "https://www.theblock.co/rss.xml", "theblockcrypto.com"),
    FeedSpec("cointelegraph", "https://cointelegraph.com/rss", "cointelegraph.com"),
    # ── Tech-adjacent (HN regularly covers fintech, regulation, market moves)
    FeedSpec("hackernews", "https://news.ycombinator.com/rss", "news.ycombinator.com"),
)


@dataclass
class _SeenCache:
    """Small LRU set so we don't re-emit obviously-duplicate items each tick.

    The deterministic capture_id already gives us idempotency at the storage
    layer, but cutting them out earlier saves a round-trip through the
    supervisor for every poll.
    """

    capacity: int = 4096
    _store: "OrderedDict[str, None]" = field(default_factory=OrderedDict)

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


@dataclass(frozen=True)
class ParsedItem:
    """One news item extracted from an RSS or Atom feed."""

    title: str
    text: str
    url: str
    domain: str
    published_ts: datetime


def _strip_html(html_text: str) -> str:
    """Cheap HTML → plain-text. Good enough for RSS descriptions."""
    if not html_text:
        return ""
    # Drop tags, collapse whitespace, unescape entities.
    text = re.sub(r"<[^>]+>", " ", html_text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _resolve_domain(url: str, fallback: str) -> str:
    try:
        host = urlparse(url).hostname or ""
        # Strip the leading 'www.' so analyst-facing UI shows brand domains.
        return host[4:] if host.startswith("www.") else (host or fallback)
    except Exception:
        return fallback


def _parse_ts(value: str | None) -> datetime:
    """Best-effort timestamp parser. Returns now() on failure."""
    if value:
        # Try RFC 822 (RSS) and ISO 8601 (Atom).
        try:
            dt = parsedate_to_datetime(value)
            if dt is not None:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
        except (TypeError, ValueError):
            pass
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f%z"):
            try:
                dt = datetime.strptime(value, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except ValueError:
                continue
    return datetime.now(timezone.utc)


def parse_feed(body: bytes, fallback_domain: str = "") -> list[ParsedItem]:
    """Parse an RSS 2.0 or Atom feed body into a list of ParsedItem.

    Tolerant: anything we can't read gets skipped, not raised. RSS feeds
    in the wild are not standards-compliant.
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        logger.warning("rss_parse_error error=%s", exc)
        return []

    items: list[ParsedItem] = []

    # RSS 2.0: <rss><channel><item>...</item></channel></rss>
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = item.findtext("description") or ""
        content_enc = item.find("content:encoded", _NS)
        if content_enc is not None and content_enc.text:
            desc = content_enc.text
        pub = item.findtext("pubDate") or item.findtext("dc:date", default=None, namespaces=_NS)
        text = _strip_html(desc) or title
        if not title or not link or not text:
            continue
        items.append(ParsedItem(
            title=title,
            text=text,
            url=link,
            domain=_resolve_domain(link, fallback_domain),
            published_ts=_parse_ts(pub),
        ))

    # Atom: <feed><entry>...</entry></feed>
    for entry in root.iter(f"{{{_NS['atom']}}}entry"):
        title_el = entry.find("atom:title", _NS)
        link_el = entry.find("atom:link", _NS)
        link = ""
        if link_el is not None:
            link = (link_el.attrib.get("href") or link_el.text or "").strip()
        summary = entry.findtext("atom:summary", default="", namespaces=_NS)
        content = entry.findtext("atom:content", default="", namespaces=_NS)
        pub = entry.findtext("atom:updated", default=None, namespaces=_NS) or \
              entry.findtext("atom:published", default=None, namespaces=_NS)
        title = (title_el.text if title_el is not None else "").strip()
        body_text = _strip_html(content or summary) or title
        if not title or not link or not body_text:
            continue
        items.append(ParsedItem(
            title=title,
            text=body_text,
            url=link,
            domain=_resolve_domain(link, fallback_domain),
            published_ts=_parse_ts(pub),
        ))

    return items


async def fetch_feed(client: httpx.AsyncClient, spec: FeedSpec) -> list[ParsedItem]:
    """Fetch + parse one feed. Returns [] on any error so callers can keep going."""
    try:
        resp = await client.get(
            spec.url,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5"},
            timeout=12.0,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            logger.info("rss_non200 feed=%s status=%d", spec.name, resp.status_code)
            return []
        return parse_feed(resp.content, fallback_domain=spec.fallback_domain)
    except httpx.HTTPError as exc:
        logger.info("rss_fetch_error feed=%s error=%s", spec.name, exc)
        return []
    except Exception as exc:  # belt and suspenders — never let one feed kill the poller
        logger.warning("rss_unexpected feed=%s error=%s", spec.name, exc, exc_info=False)
        return []


class NewsPoller:
    """Long-lived async task: pull → dedup → ingest → repeat."""

    def __init__(
        self,
        *,
        supervisor: Supervisor,
        settings: Settings,
        feeds: Iterable[FeedSpec] | None = None,
        interval_seconds: float = 20.0,
        startup_grace_seconds: float = 3.0,
    ) -> None:
        self._sup = supervisor
        self._settings = settings
        self._feeds: tuple[FeedSpec, ...] = tuple(feeds) if feeds is not None else DEFAULT_FEEDS
        self._interval = max(10.0, float(interval_seconds))  # floor to keep us friendly
        self._grace = max(0.0, float(startup_grace_seconds))
        self._seen = _SeenCache()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._poke = asyncio.Event()      # signals "skip the sleep, poll now"
        self._lock = asyncio.Lock()       # serializes concurrent poll_now calls
        self._client: httpx.AsyncClient | None = None
        # Persistent diagnostics for /ui/news-status.
        self.last_run_at: datetime | None = None
        self.last_ingested: int = 0
        self.total_ingested: int = 0
        self.last_error: str | None = None
        self.is_polling: bool = False
        self.next_run_at: datetime | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._run(), name="catchem-news-poller")
        logger.info("news_poller_started feeds=%d interval=%.0fs", len(self._feeds), self._interval)

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
        """
        if self._client is None:
            # Poller hasn't entered its main loop yet; create an ephemeral
            # client so the manual trigger still works during startup grace.
            async with httpx.AsyncClient() as client:
                return await self._run_one_tick(client)
        self._poke.set()
        async with self._lock:
            return await self._run_one_tick(self._client)

    async def _run_one_tick(self, client: httpx.AsyncClient) -> int:
        """One observable poll cycle: flip is_polling, count, record stats."""
        self.is_polling = True
        try:
            n = await self._poll_once(client)
            self.last_ingested = n
            self.total_ingested += n
            self.last_run_at = datetime.now(timezone.utc)
            self.last_error = None
            return n
        except Exception as exc:
            self.last_error = repr(exc)
            logger.warning("news_poller_tick_failed error=%s", exc, exc_info=False)
            return 0
        finally:
            self.is_polling = False

    async def _run(self) -> None:
        # Brief grace so the first tick doesn't race the rest of startup.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=self._grace)
            return  # asked to stop during grace
        except asyncio.TimeoutError:
            pass

        async with httpx.AsyncClient() as client:
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
                    self.next_run_at = datetime.now(timezone.utc) + timedelta(seconds=self._interval)
                    # Sleep for the interval, but wake early on stop OR poke.
                    waiters = [
                        asyncio.create_task(self._stop.wait()),
                        asyncio.create_task(self._poke.wait()),
                    ]
                    done, pending = await asyncio.wait(
                        waiters,
                        timeout=self._interval,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
                    # Reset the poke event so the next manual call wakes us.
                    self._poke.clear()
            finally:
                self._client = None

    async def _poll_once(self, client: httpx.AsyncClient) -> int:
        """One pass over all feeds. Returns number of NEW records ingested."""
        # Fetch all feeds in parallel — they're independent.
        results = await asyncio.gather(*[fetch_feed(client, s) for s in self._feeds])
        ingested = 0
        for spec, items in zip(self._feeds, results):
            for item in items:
                if item.url in self._seen:
                    continue
                self._seen.add(item.url)
                # Compute the same capture_id demo.py would compute; if it's
                # already in storage we skip the heavier replay path.
                from .demo import _deterministic_capture_id
                cap_id = _deterministic_capture_id(item.text, item.url)
                if self._sup.storage.get_record(cap_id) is not None:
                    continue
                try:
                    self._ingest_one(item)
                    ingested += 1
                except Exception as exc:
                    logger.info("news_ingest_failed feed=%s url=%s error=%s",
                                spec.name, item.url, exc)
        if ingested:
            logger.info("news_poll_ingested count=%d", ingested)
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
            archive_root = self._settings.paths.fusion_output_dir / "live-news"
            archive_root.mkdir(parents=True, exist_ok=True)
            write_jsonl(cap, archive_root)
        except OSError as exc:
            logger.info("news_archive_failed url=%s error=%s", item.url, exc)
        # Hot path: warm supervisor → service → storage. The capture_id is
        # deterministic so insert_record is upsert-safe (PRIMARY KEY).
        self._sup.process_capture(cap)


__all__ = [
    "FeedSpec",
    "DEFAULT_FEEDS",
    "ParsedItem",
    "parse_feed",
    "fetch_feed",
    "NewsPoller",
]
