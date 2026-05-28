"""YouTube finance/markets video source pack — minutes-fresh segments.

Major financial broadcasters and analyst channels publish to YouTube within
minutes: live earnings-call streams, CNBC/Bloomberg TV segments, Yahoo Finance
market wraps, Reuters clips. Each YouTube channel exposes a standard Atom feed
at::

    https://www.youtube.com/feeds/videos.xml?channel_id=<UCID>

(documented by YouTube — the same per-channel RSS the "Subscribe via RSS"
flow and countless readers consume). Because it is plain Atom, every entry
rides the built-in ``parser="rss"`` and the exact same fetch → dedup → ingest
pipeline as ``DEFAULT_FEEDS`` — no new parser is required.

``fallback_domain`` is set to ``youtube.com`` for every feed so an entry that
somehow arrives without a usable link still attributes correctly in the Live
Feed. Names are ``yt-<slug>`` and are disjoint from ``DEFAULT_FEEDS``.

Channel IDs below are the well-documented public ``UC…`` identifiers for each
broadcaster's primary channel (visible in each channel's page source /
``?channel_id=`` share URL and widely catalogued). If any single ID drifts,
the URL *shape* is still correct and the feed simply returns no entries — one
empty feed never affects the rest of the poller.

Coverage (12 feeds):
  * Broadcasters     — CNBC, CNBC Television, Bloomberg Television,
                       Yahoo Finance, Reuters, Fox Business.
  * Markets / data   — Bloomberg Originals, Financial Times, The Economist.
  * Analyst / retail — Yahoo Finance is broadcaster; retail-investor coverage
                       via Benzinga, Wall Street Journal, and Investopedia-style
                       explainers (Bloomberg Quicktake).
"""

from __future__ import annotations

from ..news_poller import FeedSpec, register_feed_provider

# YouTube's documented per-channel Atom feed endpoint. Format the channel_id in.
_ATOM_TEMPLATE = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"

# (slug, channel_id) pairs. The slug becomes the feed name "yt-<slug>"; the
# channel_id is the public UC… identifier for that broadcaster's primary
# channel. All produce the same standard Atom (parser="rss") feed shape.
_CHANNELS: tuple[tuple[str, str], ...] = (
    # ── Major financial broadcasters ───────────────────────────────────────
    ("cnbc", "UCvJJ_dzjViJCoLf5uKUTwoA"),              # CNBC
    ("cnbc-television", "UCrp_UI8XtuYfpiqluWLD7Lw"),   # CNBC Television
    ("bloomberg-television", "UCIALMKvOb4usv5oMjxQyM-Q"),  # Bloomberg Television
    ("bloomberg-originals", "UCUMZ7gohGI9HcU9VNsr2FJQ"),   # Bloomberg Originals (Quicktake)
    ("yahoo-finance", "UCEAZeUIeJs0IjQiqTCdVSIg"),    # Yahoo Finance
    ("reuters", "UChqUTb7kYRX8-EiaN3XFrSQ"),          # Reuters
    ("fox-business", "UCCXoCcu9Rp7NPbTzIvogpZg"),     # Fox Business
    # ── Markets / financial press ──────────────────────────────────────────
    ("wall-street-journal", "UCK7tptUDHh-RYDsdxO1-5QQ"),  # The Wall Street Journal
    ("financial-times", "UCoUxsWakJucWg46KW5RsvPw"),   # Financial Times
    ("the-economist", "UC0p5jTq6Xx_DosDFxVXnWaQ"),    # The Economist
    # ── Analyst / retail-investor coverage ─────────────────────────────────
    ("benzinga", "UCdVKWApDmDM6n6yLcfLZ8oA"),         # Benzinga
    ("cnbc-international", "UCpkUFq5XPSObVQ1JmKMjMng"),  # CNBC International TV
)


@register_feed_provider
def _youtube_feeds() -> list[FeedSpec]:
    """Contribute finance YouTube channel Atom feeds (parser='rss')."""
    return [
        FeedSpec(
            name=f"yt-{slug}",
            url=_ATOM_TEMPLATE.format(cid=cid),
            fallback_domain="youtube.com",
        )
        for slug, cid in _CHANNELS
    ]
