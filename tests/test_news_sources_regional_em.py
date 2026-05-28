"""Spec-shape tests for the REGIONAL / EMERGING-MARKETS source pack.

These validate the FeedSpec list the pack registers WITHOUT any network: we
never fetch a feed, we only assert the static spec shape and that the pack is
wired into `assemble_feeds()` with no name collisions against `DEFAULT_FEEDS`.
Keeping it offline means the suite stays deterministic and fast regardless of
publisher uptime.

Beyond the generic shape checks (shared with the global_wires pack) this file
pins the pack's *reason for existing*: a strong Turkish-source contingent,
since the operator is Turkish and local TR financial media is high-value.
"""

from __future__ import annotations

from urllib.parse import urlparse

from catchem.news_poller import DEFAULT_FEEDS, FeedSpec, assemble_feeds
from catchem.news_sources.regional_em import _regional_em_feeds

# Turkish source host fragments / slugs we expect to see represented. A spec
# counts as "Turkish" if its name or fallback_domain contains one of these.
_TURKISH_MARKERS: tuple[str, ...] = (
    "bloomberght",
    "dunya",
    "aa.com.tr",
    "anadolu",
    "haberturk",
    "ntv.com.tr",
    "hurriyet",
    "bigpara",
    "paraanaliz",
    ".com.tr",
)


def _specs() -> list[FeedSpec]:
    return list(_regional_em_feeds())


def _is_turkish(spec: FeedSpec) -> bool:
    hay = f"{spec.name.lower()} {spec.fallback_domain.lower()}"
    return any(marker in hay for marker in _TURKISH_MARKERS)


def test_provider_returns_at_least_twelve_feeds() -> None:
    assert len(_specs()) >= 12


def test_all_specs_are_feedspec_instances() -> None:
    for spec in _specs():
        assert isinstance(spec, FeedSpec)


def test_names_are_unique() -> None:
    names = [s.name for s in _specs()]
    assert len(names) == len(set(names)), f"duplicate names: {names}"


def test_all_names_use_rem_prefix() -> None:
    for spec in _specs():
        assert spec.name.startswith("rem-"), f"{spec.name} missing rem- prefix"


def test_names_are_nonempty_strings() -> None:
    for spec in _specs():
        assert isinstance(spec.name, str) and spec.name.strip()


def test_all_urls_are_https() -> None:
    for spec in _specs():
        parsed = urlparse(spec.url)
        assert parsed.scheme == "https", f"{spec.name} is not https: {spec.url}"
        assert parsed.netloc, f"{spec.name} has no host: {spec.url}"


def test_all_specs_use_default_rss_parser() -> None:
    # The pack adds NO new parser — every feed must ride the built-in "rss".
    for spec in _specs():
        assert spec.parser == "rss", f"{spec.name} uses non-rss parser {spec.parser!r}"


def test_fallback_domains_are_sensible() -> None:
    for spec in _specs():
        fb = spec.fallback_domain
        assert isinstance(fb, str) and fb.strip(), f"{spec.name} has empty fallback_domain"
        # A bare host: contains a dot, no scheme, no path, no whitespace.
        assert "." in fb, f"{spec.name} fallback_domain looks non-domain: {fb!r}"
        assert "://" not in fb, f"{spec.name} fallback_domain has a scheme: {fb!r}"
        assert "/" not in fb, f"{spec.name} fallback_domain has a path: {fb!r}"
        assert " " not in fb, f"{spec.name} fallback_domain has whitespace: {fb!r}"


def test_fallback_domain_is_substring_of_url_host() -> None:
    """Each fallback_domain should plausibly match its feed's host so an item
    arriving without a link attributes to the right publisher."""
    for spec in _specs():
        host = (urlparse(spec.url).hostname or "").lower()
        fb = spec.fallback_domain.lower()
        # fallback is the brand host or a sub/parent of the feed host.
        assert fb in host or host.endswith(fb) or host in fb, (
            f"{spec.name}: fallback_domain {fb!r} unrelated to host {host!r}"
        )


def test_strong_turkish_coverage() -> None:
    """Operator is Turkish — at least five feeds must be Turkish sources."""
    turkish = [s for s in _specs() if _is_turkish(s)]
    assert len(turkish) >= 5, (
        f"expected >=5 Turkish feeds, got {len(turkish)}: "
        f"{[s.name for s in turkish]}"
    )


def test_specific_turkish_publishers_present() -> None:
    """Pin the named Turkish publishers the pack promises to carry."""
    fallback_domains = {s.fallback_domain for s in _specs()}
    for expected in ("bloomberght.com", "dunya.com", "aa.com.tr"):
        assert expected in fallback_domains, f"expected {expected} in pack"


def test_regions_beyond_turkey_are_covered() -> None:
    """LatAm + MENA + SEA + Africa must each be represented."""
    fallback_domains = {s.fallback_domain for s in _specs()}
    # one representative host per region
    for region_host in (
        "infomoney.com.br",        # LatAm
        "gulfnews.com",            # MENA
        "thejakartapost.com",      # SEA
        "moneyweb.co.za",          # Africa
    ):
        assert region_host in fallback_domains, f"missing region anchor {region_host}"


def test_no_name_collisions_with_default_feeds() -> None:
    default_names = {s.name for s in DEFAULT_FEEDS}
    pack_names = {s.name for s in _specs()}
    assert default_names.isdisjoint(pack_names), (
        f"collision with DEFAULT_FEEDS: {default_names & pack_names}"
    )


def test_pack_feeds_present_in_assemble_feeds() -> None:
    assembled_names = {s.name for s in assemble_feeds()}
    for spec in _specs():
        assert spec.name in assembled_names, f"{spec.name} missing from assemble_feeds()"


def test_assemble_feeds_has_no_duplicate_names_overall() -> None:
    names = [s.name for s in assemble_feeds()]
    assert len(names) == len(set(names)), "assemble_feeds() produced duplicate names"
