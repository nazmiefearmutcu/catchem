"""Google News SECTOR / THEME query pack.

Google News' search RSS endpoint is the cheapest path to broad, fresh
coverage: a query like ``semiconductor OR chip stocks`` surfaces every major
publisher's matching article within minutes of publication, in clean RSS that
the built-in "rss" parser already understands. No new body parser is needed —
each feed is just another ``FeedSpec`` with the default parser.

DEFAULT_FEEDS already ships nine Google News queries (finance markets, stocks
earnings, bitcoin, fed/FOMC, inflation/CPI, recession, M&A/IPO, oil/OPEC,
sanctions/geopolitics). This pack ADDS ~20 distinct sector/theme queries that
those don't cover (semiconductors, banks, AI capex, EVs, housing, treasury
yields, the dollar, gold, China, layoffs, retail, biotech, airlines, defense,
…). Explicit-ticker queries are intentionally left to a separate pack.

Contract (kept deliberately small so packs can be authored in parallel):

    from ..news_poller import FeedSpec, register_feed_provider

    @register_feed_provider
    def _provider() -> list[FeedSpec]: ...

``assemble_feeds()`` merges this provider's output into DEFAULT_FEEDS and
de-dups by name, so dropping this file in is all that's required to enable it.
Names are ``gnews-<slug>`` and chosen NOT to collide with the built-in
``gnews-*`` set.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from ..news_poller import FeedSpec, register_feed_provider

# Google News search RSS template. `q` is URL-encoded per-query; the locale
# params (hl/gl/ceid) pin US-English results so the feed stays predictable.
_GNEWS_BASE = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
_FALLBACK_DOMAIN = "news.google.com"

# (slug, query) pairs. Queries are kept tight with OR groups so each feed
# stays on-theme rather than drifting into generic market noise (which the
# built-in gnews-finance / gnews-stocks feeds already cover). Slugs become
# the ``gnews-<slug>`` feed name and must not duplicate the built-in set.
_SECTOR_QUERIES: tuple[tuple[str, str], ...] = (
    ("semiconductors", "semiconductor OR chip stocks OR Nvidia OR TSMC"),
    ("banks", "bank stocks OR banking crisis OR regional banks OR deposits"),
    ("ai-capex", "AI datacenter OR AI capex OR cloud spending OR GPU demand"),
    ("ev", "electric vehicles OR EV sales OR Tesla OR battery makers"),
    ("housing", "housing market OR mortgage rates OR home sales OR homebuilders"),
    ("treasuries", "treasury yields OR bond market OR 10-year yield OR government bonds"),
    ("dollar-fx", "US dollar OR currency markets OR forex OR yen OR euro"),
    ("gold-commodities", "gold prices OR silver OR copper OR commodities"),
    ("china-economy", "China economy OR Chinese stocks OR yuan OR Beijing stimulus"),
    ("layoffs", "layoffs OR job cuts OR workforce reduction OR hiring freeze"),
    ("retail-consumer", "retail sales OR consumer spending OR consumer confidence"),
    ("biotech-fda", "biotech OR FDA approval OR drug trial OR pharma stocks"),
    ("airlines-travel", "airlines OR air travel OR airline stocks OR jet fuel"),
    ("defense", "defense stocks OR military spending OR defense contractors"),
    ("autos", "auto industry OR car sales OR automakers OR vehicle production"),
    ("semis-equipment", "chip equipment OR ASML OR lithography OR foundry capacity"),
    ("real-estate-cre", "commercial real estate OR office vacancies OR REIT"),
    ("utilities-power", "utilities stocks OR electricity prices OR power grid OR nuclear"),
    ("shipping-freight", "shipping rates OR freight OR supply chain OR ports"),
    ("insurance", "insurance industry OR insurers OR reinsurance OR catastrophe losses"),
)


@register_feed_provider
def gnews_sector_feeds() -> list[FeedSpec]:
    """Return the sector/theme Google News search feeds.

    Each query is URL-encoded with ``quote_plus`` so spaces become ``+`` and
    reserved characters are escaped — the resulting URL is a valid Google News
    RSS search endpoint that the default "rss" parser ingests unchanged.
    """
    return [
        FeedSpec(
            name=f"gnews-{slug}",
            url=_GNEWS_BASE.format(q=quote_plus(query)),
            fallback_domain=_FALLBACK_DOMAIN,
        )
        for slug, query in _SECTOR_QUERIES
    ]
