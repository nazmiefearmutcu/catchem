"""Finance / markets PODCAST source pack — catch the day's catalysts fast.

`DEFAULT_FEEDS` plus the other packs cover written wires, regulators, and
search aggregation, but they miss a fast, high-signal channel: the daily
markets podcast. Shows like Bloomberg Surveillance, WSJ's Minute Briefing,
Marketplace, and CNBC's Squawk Pod publish a tight audio recap within
minutes of the US close that names the day's catalysts (Fed, CPI prints,
mega-cap earnings, oil) before the long-form written coverage lands. A
podcast RSS *item* carries a Title + show-notes Description + episode Link
triple, so the built-in RSS/Atom parser ingests them with zero new code —
the show-notes text is exactly the kind of catalyst summary the impact
classifier wants.

Design (mirrors the other packs):
  * Pure RSS — every feed uses the built-in ``parser="rss"`` (podcast feeds
    ARE RSS 2.0 with an iTunes namespace the parser ignores harmlessly), so
    there is NO new parser here. The pack only calls
    `register_feed_provider`.
  * Publisher-documented endpoints — each URL is the show's own well-known
    podcast RSS path on its host platform (Megaphone / Simplecast / Art19 /
    NPR / publisher feed). Where a network exposes the feed on a CDN host we
    use that host and set a brand-recognizable fallback_domain.
  * Realistic `fallback_domain` per source — the show/network host, so an
    episode arriving without a usable link still attributes correctly.
  * Additive only — this module never edits the shared DEFAULT_FEEDS tuple.
    `assemble_feeds()` merges it in and de-dupes by name (DEFAULT_FEEDS
    wins ties), which keeps parallel source-pack authorship collision-free.

Names are prefixed ``pod-`` (podcast) to stay clear of any DEFAULT_FEEDS
name and any other pack.
"""

from __future__ import annotations

from ..news_poller import FeedSpec, register_feed_provider

# Curated finance/markets PODCAST RSS feeds. Each tuple is
# (name, url, fallback_domain); the parser is the built-in "rss" default.
# Every URL is a publisher-documented, no-auth podcast RSS endpoint.
_PODCAST_FEEDS: tuple[tuple[str, str, str], ...] = (
    # ── Bloomberg (Megaphone-hosted network feeds)
    ("pod-bloomberg-surveillance", "https://feeds.megaphone.fm/BLM4574482312", "bloomberg.com"),
    ("pod-odd-lots", "https://feeds.megaphone.fm/BLM5926477868", "bloomberg.com"),
    # ── NPR / American Public Media flagship economics shows
    ("pod-planet-money", "https://feeds.npr.org/510289/podcast.xml", "npr.org"),
    ("pod-marketplace", "https://www.marketplace.org/feed/podcast/marketplace", "marketplace.org"),
    # ── Dow Jones / WSJ daily briefings (Megaphone-hosted)
    ("pod-wsj-minute-briefing", "https://feeds.megaphone.fm/WSJ7704604665", "wsj.com"),
    ("pod-wsj-whats-news", "https://video-api.wsj.com/podcast/rss/wsj/whats-news", "wsj.com"),
    # ── Independent markets / trading shows
    ("pod-animal-spirits", "https://feeds.megaphone.fm/animalspirits", "thecompoundnews.com"),
    ("pod-the-compound", "https://feeds.megaphone.fm/thecompoundshow", "thecompoundnews.com"),
    ("pod-chat-with-traders", "https://feeds.megaphone.fm/chatwithtraders", "chatwithtraders.com"),
    ("pod-macro-voices", "https://feeds.feedburner.com/MacroVoices", "macrovoices.com"),
    # ── CNBC
    ("pod-cnbc-squawk-pod", "https://feeds.megaphone.fm/cnbc-squawkpod", "cnbc.com"),
)


@register_feed_provider
def _podcast_feeds() -> list[FeedSpec]:
    """Contribute the finance/markets PODCAST RSS feeds.

    Every spec uses the default RSS parser; only the URL/domain differ.
    """
    return [
        FeedSpec(name=name, url=url, fallback_domain=fallback_domain)
        for name, url, fallback_domain in _PODCAST_FEEDS
    ]
