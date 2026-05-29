"""Google News GLOBAL (non-English) locale pack.

DEFAULT_FEEDS and the existing ``gnews_sectors`` / ``gnews_tickers`` packs all
pin US-English locale params (``hl=en-US&gl=US&ceid=US:en``), so a market-mover
that breaks first in Turkish, German, Japanese, or Chinese reaches the feed only
once an English wire re-reports it — minutes-to-hours late. This pack closes
that gap by issuing the SAME Google News search RSS endpoint in a dozen major
non-English locales, each with a language-native market/economy query. The
built-in "rss" parser ingests every one unchanged — each feed is just another
``FeedSpec`` with the default parser.

Locale params matter: ``hl`` (UI/result language), ``gl`` (geo edition), and
``ceid`` (``<COUNTRY>:<lang>`` edition id) together steer Google News to the
local edition, so the results are genuinely the local market's coverage rather
than English articles translated. Queries are kept tight with OR groups around
each language's words for "stock market / economy / equities" so feeds stay
on-theme rather than drifting into general local news.

Contract (kept deliberately small so packs can be authored in parallel):

    from ..news_poller import FeedSpec, register_feed_provider

    @register_feed_provider
    def _provider() -> list[FeedSpec]: ...

``assemble_feeds()`` merges this provider's output into DEFAULT_FEEDS and
de-dups by name, so dropping this file in is all that's required to enable it.
Names are ``gnews-<lang>`` (gnews-tr, gnews-de, …) and chosen NOT to collide
with the topic-level ``gnews-<slug>`` set or the ``gnews-tkr-*`` ticker set.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from ..news_poller import FeedSpec, register_feed_provider

# Google News search RSS template. `q` is URL-encoded per-query; the locale
# params (hl/gl/ceid) are filled per-feed so each one pins a distinct local
# edition rather than the US-English default the other packs use.
_GNEWS_BASE = "https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"
_FALLBACK_DOMAIN = "news.google.com"

# (lang-slug, hl, gl, ceid, query) tuples. The slug becomes ``gnews-<lang>``.
# Queries use each language's native words for stock market / economy /
# equities so the local edition surfaces market-movers in that language. A few
# locales (es, es-419) intentionally share a slug-family but use distinct geo
# editions, so they get disambiguated slugs (es, es-mx) to stay unique.
_LOCALE_QUERIES: tuple[tuple[str, str, str, str, str], ...] = (
    ("tr", "tr", "TR", "TR:tr", "borsa OR ekonomi OR faiz"),
    ("de", "de", "DE", "DE:de", "Börse OR Wirtschaft OR Aktien"),
    ("fr", "fr", "FR", "FR:fr", "bourse OR économie OR actions"),
    ("ja", "ja", "JP", "JP:ja", "株式 OR 経済 OR 日経"),
    ("zh", "zh-Hans", "CN", "CN:zh-Hans", "股市 OR 经济"),
    ("es", "es", "ES", "ES:es", "bolsa OR economía"),
    ("es-mx", "es-419", "MX", "MX:es-419", "bolsa OR economía"),
    ("pt", "pt-BR", "BR", "BR:pt-BR", "bolsa OR economia"),
    ("it", "it", "IT", "IT:it", "borsa OR economia OR azioni"),
    ("ko", "ko", "KR", "KR:ko", "증시 OR 경제 OR 코스피"),
    ("ar", "ar", "SA", "SA:ar", "البورصة OR الاقتصاد"),
    ("hi", "hi", "IN", "IN:hi", "शेयर बाजार OR अर्थव्यवस्था"),
    ("ru", "ru", "RU", "RU:ru", "биржа OR экономика OR акции"),
)


@register_feed_provider
def gnews_global_feeds() -> list[FeedSpec]:
    """Return the non-English locale Google News search feeds.

    Each query is URL-encoded with ``quote_plus`` so spaces become ``+`` and
    non-ASCII / reserved characters are percent-escaped — the resulting URL is a
    valid Google News RSS search endpoint that the default "rss" parser ingests
    unchanged. The hl/gl/ceid params are passed through verbatim (they are
    already URL-safe tokens) to pin each feed to its local edition.
    """
    return [
        FeedSpec(
            name=f"gnews-{slug}",
            url=_GNEWS_BASE.format(q=quote_plus(query), hl=hl, gl=gl, ceid=ceid),
            fallback_domain=_FALLBACK_DOMAIN,
        )
        for slug, hl, gl, ceid, query in _LOCALE_QUERIES
    ]
