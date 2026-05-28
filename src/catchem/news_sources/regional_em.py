"""REGIONAL / EMERGING-MARKETS source pack — Turkey-first, plus LatAm/MENA/SEA/Africa.

`DEFAULT_FEEDS` (news_poller) and the `global_wires` pack between them cover
US/UK/EU plus a slice of developed Asia (Nikkei, SCMP, Straits Times) and
India (Times of India, Economic Times, Moneycontrol, Livemint). What's still
thin is the *emerging-markets* belt where a lot of the operator's day actually
moves: **Turkey first** (the operator is Turkish — TRY/BIST/CBRT headlines
break in Turkish-language financial media hours before any US desk re-reports
them), then Latin America, the Gulf/MENA, the rest of South-East Asia, and
Africa.

This pack contributes a curated set of widely-known, stable, no-auth RSS feeds
from publishers across those regions so the Live Feed catches an Istanbul,
São Paulo, Dubai, Jakarta, or Lagos move in its home market instead of waiting
for it to be laundered through a New York wire.

Design (identical contract to `global_wires`):
  * Pure RSS — every feed uses the built-in ``parser="rss"`` (RSS/Atom XML),
    so there is NO new parser here. The pack only calls
    `register_feed_provider`.
  * Publisher-documented endpoints — each URL is the publisher's own
    well-known RSS path. Where a site exposes several feeds we prefer the
    business / economy / markets section.
  * Realistic `fallback_domain` per source — the brand host, so items that
    arrive without a usable link still attribute correctly in the UI.
  * Additive only — this module never edits the shared DEFAULT_FEEDS tuple.
    `assemble_feeds()` merges it in and de-dupes by name (DEFAULT_FEEDS wins
    ties), which keeps parallel source-pack authorship collision-free.
  * India coverage (Moneycontrol/ET/ToI/Livemint) is intentionally NOT
    duplicated here — those already ship in `global_wires`. We add Business
    Standard, which is not in any existing pack.

Names are prefixed ``rem-`` (regional / emerging markets) to stay clear of
DEFAULT_FEEDS, the ``gw-`` global-wires pack, and every other pack.
"""

from __future__ import annotations

from ..news_poller import FeedSpec, register_feed_provider

# Curated REGIONAL / EMERGING-MARKETS business & markets RSS feeds. Each tuple
# is (name, url, fallback_domain); the parser is the built-in "rss" default.
# Every URL is a publisher-documented, no-auth RSS/Atom endpoint.
_REGIONAL_EM_FEEDS: tuple[tuple[str, str, str], ...] = (
    # ── TURKEY (priority — operator's home market; Turkish-language sources)
    # BloombergHT — leading TR business/markets broadcaster, "/sondakika" =
    # breaking-news firehose.
    ("rem-bloomberght", "https://www.bloomberght.com/rss/sondakika", "bloomberght.com"),
    # Dünya gazetesi — long-running TR financial daily; site-wide RSS.
    ("rem-dunya-ekonomi", "https://www.dunya.com/rss", "dunya.com"),
    # Anadolu Ajansı — state news agency, economy desk RSS (Turkish).
    ("rem-anadolu-ekonomi", "https://www.aa.com.tr/tr/rss/default?cat=ekonomi", "aa.com.tr"),
    # Habertürk — major outlet, economy section RSS.
    ("rem-haberturk-ekonomi", "https://www.haberturk.com/rss/ekonomi.xml", "haberturk.com"),
    # NTV — economy section RSS.
    ("rem-ntv-ekonomi", "https://www.ntv.com.tr/ekonomi.rss", "ntv.com.tr"),
    # Bigpara (Hürriyet) — markets/borsa portal; Hürriyet ekonomi RSS as the
    # documented feed surface for the same publisher group.
    ("rem-hurriyet-ekonomi", "https://www.hurriyet.com.tr/rss/ekonomi", "hurriyet.com.tr"),
    # Para Analiz — TR markets/analysis site, site-wide RSS.
    ("rem-paraanaliz", "https://www.paraanaliz.com/feed/", "paraanaliz.com"),
    # ── LATIN AMERICA
    # InfoMoney (Brazil) — markets/business, WordPress feed.
    ("rem-infomoney-br", "https://www.infomoney.com.br/feed/", "infomoney.com.br"),
    # Valor Econômico / Valor Investe (Brazil) — Globo markets RSS.
    ("rem-valor-br", "https://valorinveste.globo.com/rss/valorinveste/", "valorinveste.globo.com"),
    # El Financiero (Mexico) — economy section RSS.
    ("rem-elfinanciero-mx", "https://www.elfinanciero.com.mx/rss/economia.xml", "elfinanciero.com.mx"),
    # ── MENA / GULF
    # Al Arabiya — business section RSS (English).
    ("rem-alarabiya-business", "https://english.alarabiya.net/feed/rss2/business.xml", "alarabiya.net"),
    # Gulf News (UAE) — business section RSS.
    ("rem-gulfnews-business", "https://gulfnews.com/rss?generatorType=business", "gulfnews.com"),
    # The National (UAE) — business section RSS.
    ("rem-thenational-business", "https://www.thenationalnews.com/business/rss/", "thenationalnews.com"),
    # ── SOUTH-EAST ASIA / ASIA EM (India = Business Standard only; ET/MC/ToI
    # already in global_wires)
    # The Jakarta Post (Indonesia) — business channel RSS.
    ("rem-jakartapost-business", "https://www.thejakartapost.com/business/rss", "thejakartapost.com"),
    # Bangkok Post (Thailand) — business section RSS.
    ("rem-bangkokpost-business", "https://www.bangkokpost.com/rss/data/business.xml", "bangkokpost.com"),
    # Business Standard (India) — markets RSS (not in global_wires).
    ("rem-businessstandard-markets", "https://www.business-standard.com/rss/markets-106.rss", "business-standard.com"),
    # ── AFRICA
    # BusinessDay (Nigeria) — business daily, WordPress feed.
    ("rem-businessday-ng", "https://businessday.ng/feed/", "businessday.ng"),
    # Moneyweb (South Africa) — markets/business RSS.
    ("rem-moneyweb-za", "https://www.moneyweb.co.za/feed/", "moneyweb.co.za"),
)


@register_feed_provider
def _regional_em_feeds() -> list[FeedSpec]:
    """Contribute the REGIONAL / EMERGING-MARKETS business & markets RSS feeds.

    Every spec uses the default RSS parser; only the URL/domain differ.
    """
    return [
        FeedSpec(name=name, url=url, fallback_domain=fallback_domain)
        for name, url, fallback_domain in _REGIONAL_EM_FEEDS
    ]
