"""X / Twitter social source pack — real-time social, auth-free via Nitter.

Market-moving headlines frequently break on X *before* any wire picks them
up: a @DeItaone (Walter Bloomberg) one-liner, a @FirstSquawk flash, an
@unusual_whales flow alert, an @federalreserve / @SECGov regulator post.
This pack pulls a curated set of high-signal finance/markets accounts through
a Nitter instance — Nitter mirrors any public account at ``/<account>/rss``
as a standard RSS 2.0 feed, with NO authentication and NO API key — and
routes each tweet through the exact same fetch → dedup → ingest pipeline the
RSS feeds use, so tweets land in the Live Feed alongside publisher articles.

Wiring (all at import time, no network):

  * ``register_parser("twitter", _parse_twitter)`` teaches the poller how to
    turn a Nitter RSS body into ParsedItems. It REUSES the built-in
    ``parse_feed`` (Nitter emits ordinary RSS 2.0: item/title, item/link,
    item/pubDate, item/description) and then post-processes each item:
      - rewrites the ``nitter.<host>`` permalink back to the canonical
        ``x.com`` URL (so the clickable Live-Feed link + the dedup key are
        the real tweet URL, not whichever flaky instance happened to serve
        it — two instances serving the same tweet therefore dedup),
      - forces ``domain = "x.com"`` so attribution is the platform, not the
        mirror.
  * ``@register_feed_provider`` contributes one FeedSpec per account, each
    pointing at ``https://nitter.net/<account>/rss`` with ``parser="twitter"``
    so the generic GET path knows to hand the body to ``_parse_twitter``.

Reliability note: public Nitter instances are notoriously flaky (rate-limited
or down for stretches). That is fine by design — the poller's circuit breaker
puts a dead feed on a cooldown ladder and keeps polling everything else; this
pack simply contributes whenever the instance is reachable.

Defensive throughout: a body that isn't valid XML, or one ``parse_feed``
can't read, yields ``[]`` — never raises — so one malformed payload can't
take the poll tick down (mirrors every other parser's contract).
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from ..logging import get_logger
from ..news_poller import (
    FeedSpec,
    ParsedItem,
    parse_feed,
    register_feed_provider,
    register_parser,
)

logger = get_logger("catchem.news_sources.x_twitter")

# Canonical platform host every item attributes to + every link is rewritten
# onto. Nitter mirrors live under many host names (nitter.net, nitter.poast.org,
# …); collapsing them all to x.com makes the dedup key instance-independent.
_X_HOST = "x.com"
_X_DOMAIN = "x.com"

# Default Nitter instance. Instances come and go; nitter.net is the canonical
# one. If it's down the circuit breaker backs the whole pack off — the rest of
# the poller is unaffected. Operators can stand up their own and the only edit
# is this constant.
_NITTER_BASE = "https://nitter.net"

# High-signal finance / markets / macro / regulator accounts. These are the
# venues where market-moving information surfaces first on X. Kept in one place
# so the feed provider (and any future per-account tuning) reads a single list.
_ACCOUNTS: tuple[str, ...] = (
    "DeItaone",        # Walter Bloomberg — fastest market headline relayer
    "FirstSquawk",     # real-time market squawk
    "LiveSquawk",      # real-time market/macro squawk
    "financialjuice",  # market headlines aggregator
    "unusual_whales",  # options flow / unusual activity
    "zerohedge",       # markets/macro commentary
    "markets",         # Bloomberg Markets
    "CNBC",            # CNBC
    "ReutersBiz",      # Reuters Business
    "federalreserve",  # Federal Reserve (regulator / central bank)
    "SECGov",          # U.S. SEC (regulator)
    "WatcherGuru",     # crypto / markets breaking
)


def _rewrite_to_x(url: str) -> str:
    """Rewrite a Nitter permalink to its canonical ``x.com`` equivalent.

    Nitter serves tweet permalinks on its own host, e.g.
    ``https://nitter.net/DeItaone/status/123#m`` or
    ``https://nitter.poast.org/DeItaone/status/123``. The path component
    (``/<account>/status/<id>``) is identical to x.com's, so we keep the
    path + query and only swap the host to ``x.com`` (https). Nitter often
    appends a ``#m`` fragment for the media anchor — drop it so two links to
    the same tweet (with/without the fragment, or from different instances)
    collapse to one dedup key.

    Anything we can't confidently parse as a URL is returned unchanged — the
    downstream dedup still benefits from literal-key matching in that case.
    """
    raw = (url or "").strip()
    if not raw:
        return raw
    try:
        parts = urlsplit(raw)
        if not parts.scheme or not parts.netloc:
            return raw  # not a URL we can rewrite with confidence
        # Swap host → x.com, force https, keep path + query, drop fragment.
        return urlunsplit(("https", _X_HOST, parts.path, parts.query, ""))
    except Exception:
        return raw


def _parse_twitter(body: bytes, fallback_domain: str) -> list[ParsedItem]:
    """Parse a Nitter RSS 2.0 body into ParsedItems attributed to x.com.

    Reuses the built-in ``parse_feed`` (Nitter is plain RSS 2.0) and then, for
    each item, rewrites the nitter.* permalink to the canonical x.com URL and
    pins ``domain="x.com"``. ``parse_feed`` already yields tz-aware UTC
    ``published_ts`` (RFC-822 ``pubDate`` via ``_parse_ts``), so timestamps
    satisfy the ParsedItem contract unchanged.

    Tolerant by contract: ``parse_feed`` swallows XML/parse errors and returns
    ``[]``; we wrap the whole body in a try/except as belt-and-suspenders so a
    pathological input can never raise out of the poll tick.
    """
    try:
        raw_items = parse_feed(body, fallback_domain=fallback_domain or _X_DOMAIN)
    except Exception as exc:  # defensive — parse_feed already guards, but never raise
        logger.warning("twitter_parse_error", error=str(exc))
        return []

    items: list[ParsedItem] = []
    for it in raw_items:
        url = _rewrite_to_x(it.url)
        # A rewrite that didn't produce an http(s) x.com link is unusable for a
        # clickable, dedupable tweet reference — skip it rather than emit a
        # half-broken row.
        if not url.startswith("https://"):
            continue
        items.append(
            ParsedItem(
                title=it.title,
                text=it.text,
                url=url,
                domain=_X_DOMAIN,
                published_ts=it.published_ts,
            )
        )
    return items


# Register the parser under the key the FeedSpecs reference.
register_parser("twitter", _parse_twitter)


@register_feed_provider
def _x_twitter_feeds() -> list[FeedSpec]:
    """One FeedSpec per configured X account's Nitter RSS feed.

    Naming: ``x-<account-lower>`` so the Sources page groups them obviously.
    All share ``fallback_domain="x.com"`` and ``parser="twitter"``.
    """
    return [
        FeedSpec(
            name=f"x-{account.lower()}",
            url=f"{_NITTER_BASE}/{account}/rss",
            fallback_domain=_X_DOMAIN,
            parser="twitter",
        )
        for account in _ACCOUNTS
    ]
