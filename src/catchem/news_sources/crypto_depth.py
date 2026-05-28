"""CRYPTO-DEPTH source pack — broaden crypto / DeFi / on-chain awareness.

Catchem's DEFAULT_FEEDS already carries five crypto wires (CoinDesk, Decrypt,
The Block, Cointelegraph, Bitcoin Magazine). That set skews toward mainstream
spot-market headlines and misses the DeFi / on-chain / research layer where a
lot of market-moving narrative actually breaks first.

This pack contributes a curated batch of widely-known, no-auth, stable RSS
endpoints from crypto-native publications NOT already covered, with a deliberate
tilt toward DeFi and on-chain coverage (The Defiant, Bankless, Messari,
Blockworks) alongside high-volume general crypto desks (CryptoSlate,
Bitcoin.com, CoinJournal, BeInCrypto, Protos, CoinGape, AMBCrypto, NewsBTC,
U.Today, CryptoPotato).

Every endpoint is a plain RSS/Atom feed, so the pack reuses the default
``parser="rss"`` — no new body parser is registered. Following the source-pack
contract, this module only ADDS its own FeedSpecs via ``register_feed_provider``
and never edits the shared DEFAULT_FEEDS tuple, keeping parallel authorship
collision-free. ``assemble_feeds()`` de-dups by name, so even if one of these
ever clashed with a default it would simply be dropped rather than duplicated.
"""

from __future__ import annotations

from ..news_poller import FeedSpec, register_feed_provider

# Crypto-native feeds NOT already in DEFAULT_FEEDS. Each tuple is
# (name, url, fallback_domain). Names are namespaced under no shared prefix on
# purpose — they read as the publication slug in the source-health UI. URLs are
# the publications' canonical, long-stable public RSS endpoints (all https).
_CRYPTO_DEPTH_FEEDS: tuple[tuple[str, str, str], ...] = (
    # ── DeFi / on-chain / research tilt
    ("the-defiant", "https://thedefiant.io/api/feed", "thedefiant.io"),
    ("bankless", "https://www.bankless.com/rss/feed", "bankless.com"),
    ("messari", "https://messari.io/rss", "messari.io"),
    ("blockworks", "https://blockworks.co/feed", "blockworks.co"),
    # ── High-volume general crypto desks
    ("cryptoslate", "https://cryptoslate.com/feed/", "cryptoslate.com"),
    ("bitcoin-com-news", "https://news.bitcoin.com/feed/", "news.bitcoin.com"),
    ("coinjournal", "https://coinjournal.net/feed/", "coinjournal.net"),
    ("beincrypto", "https://beincrypto.com/feed/", "beincrypto.com"),
    ("protos", "https://protos.com/feed/", "protos.com"),
    ("coingape", "https://coingape.com/feed/", "coingape.com"),
    ("ambcrypto", "https://ambcrypto.com/feed/", "ambcrypto.com"),
    ("newsbtc", "https://www.newsbtc.com/feed/", "newsbtc.com"),
    ("u-today", "https://u.today/rss", "u.today"),
    ("cryptopotato", "https://cryptopotato.com/feed/", "cryptopotato.com"),
)


@register_feed_provider
def _crypto_depth_feeds() -> list[FeedSpec]:
    """Contribute the CRYPTO-DEPTH RSS wires (default rss parser)."""
    return [
        FeedSpec(name=name, url=url, fallback_domain=fallback_domain)
        for name, url, fallback_domain in _CRYPTO_DEPTH_FEEDS
    ]
