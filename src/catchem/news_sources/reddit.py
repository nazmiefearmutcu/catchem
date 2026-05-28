"""Reddit social-sentiment source pack.

Retail/social chatter often LEADS mainstream financial coverage — a thesis
gets argued on r/wallstreetbets or r/stocks hours before a wire picks it up.
This pack pulls each subreddit's public ``new.json`` listing and routes every
post through the exact same fetch → dedup → ingest pipeline the RSS feeds use,
so social posts land in the Live Feed alongside publisher articles.

Wiring (all at import time, no network):

  * ``register_parser("reddit", _parse_reddit)`` teaches the poller how to
    turn a Reddit listing's JSON body into ParsedItems.
  * ``@register_feed_provider`` contributes one FeedSpec per subreddit, each
    pointing at ``/r/<sub>/new.json?limit=50`` with ``parser="reddit"`` so
    the generic GET path knows to hand the body to ``_parse_reddit``.

Mapping (per the Reddit listing schema):
    {"data": {"children": [{"data": {...}}, ...]}}
  title        ← child.data["title"]
  text         ← child.data["selftext"] or title   (link posts have no body)
  url          ← "https://www.reddit.com" + child.data["permalink"]
                 (the *discussion* permalink — stable + always reddit.com,
                  unlike `url` which points off-site for link posts and would
                  collide with the same article surfaced through an RSS feed)
  domain       ← "reddit.com"
  published_ts ← datetime.fromtimestamp(created_utc, tz=UTC)  (epoch float)

Defensive throughout: a body that isn't valid JSON, a missing ``data`` key,
or a child that lacks ``title``/``permalink`` is skipped — never raised — so
one malformed payload can't take the poll tick down.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from ..logging import get_logger
from ..news_poller import (
    FeedSpec,
    ParsedItem,
    register_feed_provider,
    register_parser,
)

logger = get_logger("catchem.news_sources.reddit")

# Subreddits whose `new` listing we poll. Finance/markets/macro/crypto — the
# venues where retail forms (and front-runs) a view. Kept in one place so the
# feed provider and any future per-sub tuning read from a single source.
_SUBREDDITS: tuple[str, ...] = (
    "wallstreetbets",
    "stocks",
    "investing",
    "StockMarket",
    "cryptocurrency",
    "economy",
    "finance",
)

# Listing endpoint template. `new.json` is the public, auth-free JSON view of
# a subreddit's newest posts; limit=50 is the per-request cap we want each
# tick. The poller sends a JSON Accept header for any non-"rss" parser.
_NEW_JSON_URL = "https://www.reddit.com/r/{sub}/new.json?limit=50"

# Permalinks are site-relative ("/r/stocks/comments/abc/title/"); prefix the
# canonical host so the dedup key + the clickable Live-Feed link are absolute.
_REDDIT_BASE = "https://www.reddit.com"


def _parse_reddit(body: bytes, fallback_domain: str) -> list[ParsedItem]:
    """Parse a Reddit ``new.json`` listing body into ParsedItems.

    Tolerant by contract: a non-JSON body, an unexpected top-level shape, or
    an individual child missing ``title``/``permalink`` is skipped rather than
    raised, mirroring the RSS parser's "never let one bad payload kill the
    tick" guarantee. ``fallback_domain`` is accepted for parser-signature
    parity; Reddit items always attribute to "reddit.com" (the discussion
    lives there) so it isn't otherwise consulted.
    """
    try:
        payload = json.loads(body)
    except (ValueError, TypeError) as exc:
        logger.warning("reddit_parse_error", error=str(exc))
        return []

    if not isinstance(payload, dict):
        return []
    children = (payload.get("data") or {}).get("children")
    if not isinstance(children, list):
        return []

    items: list[ParsedItem] = []
    for child in children:
        if not isinstance(child, dict):
            continue
        data = child.get("data")
        if not isinstance(data, dict):
            continue

        title = (data.get("title") or "").strip()
        permalink = (data.get("permalink") or "").strip()
        # Skip posts that lack the two fields we can't synthesize: a title to
        # show and a stable discussion permalink to link/dedup on.
        if not title or not permalink:
            continue

        # Link posts carry an empty selftext; fall back to the title so the
        # `text` channel (used for capture body + ML) is never empty.
        selftext = (data.get("selftext") or "").strip()
        text = selftext or title

        url = _REDDIT_BASE + permalink

        # created_utc is an epoch float (UTC seconds). Build a tz-aware UTC
        # datetime so it matches the ParsedItem contract; tolerate a missing
        # or unparseable value by falling back to "now".
        created = data.get("created_utc")
        try:
            published_ts = datetime.fromtimestamp(float(created), tz=UTC)
        except (TypeError, ValueError, OSError, OverflowError):
            published_ts = datetime.now(UTC)

        items.append(
            ParsedItem(
                title=title,
                text=text,
                url=url,
                domain="reddit.com",
                published_ts=published_ts,
            )
        )

    return items


# Register the parser under the key the FeedSpecs reference.
register_parser("reddit", _parse_reddit)


@register_feed_provider
def _reddit_feeds() -> list[FeedSpec]:
    """One FeedSpec per configured subreddit's `new` listing.

    Naming: ``reddit-<sub>`` so the Sources page groups them obviously.
    All share ``fallback_domain="reddit.com"`` and ``parser="reddit"``.
    """
    return [
        FeedSpec(
            name=f"reddit-{sub}",
            url=_NEW_JSON_URL.format(sub=sub),
            fallback_domain="reddit.com",
            parser="reddit",
        )
        for sub in _SUBREDDITS
    ]
