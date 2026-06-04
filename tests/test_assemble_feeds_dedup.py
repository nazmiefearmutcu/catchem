"""`assemble_feeds` de-dup + collision handling (v80 audit fix).

`assemble_feeds()` merges DEFAULT_FEEDS with every registered source-pack
provider. Two collision classes must both be dropped (first-wins) so the poller
never (a) shares feed_health/cooldown state across two specs under one name, or
(b) fetches the SAME endpoint twice per poll behind two different names:

  * name collision  — a later spec reusing an existing name,
  * URL collision   — a later spec with a new name but a URL already seen.

We isolate the registry by monkeypatching `_FEED_PROVIDERS` to just our test
providers, so the assertions don't depend on which real packs are installed.
"""

from __future__ import annotations

import catchem.news_poller as np
from catchem.news_poller import FeedSpec, assemble_feeds


def test_assemble_feeds_drops_duplicate_name_and_url(monkeypatch) -> None:
    def _p1():
        return [
            FeedSpec("zz-test-alpha", "https://zz-test.example/alpha", "zz-test.example"),
            FeedSpec("zz-test-beta", "https://zz-test.example/beta", "zz-test.example"),
        ]

    def _p2():
        return [
            # duplicate NAME of alpha (different URL) → dropped, first wins
            FeedSpec("zz-test-alpha", "https://zz-test.example/alpha-OTHER", "zz-test.example"),
            # duplicate URL of beta (different name) → dropped (no double-fetch)
            FeedSpec("zz-test-gamma", "https://zz-test.example/beta", "zz-test.example"),
            # genuinely unique → kept
            FeedSpec("zz-test-delta", "https://zz-test.example/delta", "zz-test.example"),
        ]

    # Replace the auto-discovered providers with only our two (the side-effect
    # `import catchem.news_sources` inside assemble_feeds is already cached, so
    # discovery won't repopulate the list we just swapped in).
    monkeypatch.setattr(np, "_FEED_PROVIDERS", [_p1, _p2])

    feeds = assemble_feeds()
    names = [f.name for f in feeds]
    urls = [f.url for f in feeds]

    # First-wins survivors from our providers.
    assert "zz-test-alpha" in names
    assert "zz-test-beta" in names
    assert "zz-test-delta" in names
    # The duplicate-URL spec is dropped even though its NAME was unique.
    assert "zz-test-gamma" not in names, "duplicate-URL spec must be dropped"
    # alpha keeps its FIRST URL (p1), not p2's clobber.
    alpha = next(f for f in feeds if f.name == "zz-test-alpha")
    assert alpha.url == "https://zz-test.example/alpha"

    # Whole-output invariant: no duplicate names and no duplicate URLs anywhere
    # (also proves DEFAULT_FEEDS carries no internal dupes).
    assert len(names) == len(set(names)), "no duplicate feed names in assembled output"
    assert len(urls) == len(set(urls)), "no duplicate feed URLs in assembled output"


def test_assemble_feeds_survives_a_throwing_provider(monkeypatch) -> None:
    # One bad provider must never break the rest (fail-soft contract).
    def _good():
        return [FeedSpec("zz-test-good", "https://zz-test.example/good", "zz-test.example")]

    def _boom():
        raise RuntimeError("pack blew up at assemble time")

    monkeypatch.setattr(np, "_FEED_PROVIDERS", [_boom, _good])

    feeds = assemble_feeds()
    assert any(f.name == "zz-test-good" for f in feeds), "a throwing provider must not drop the others"
