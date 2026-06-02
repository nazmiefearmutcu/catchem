"""Contract tests for the Reddit social-sentiment source pack.

Pure-unit + offline: a hand-built Reddit ``new.json`` byte fixture is fed
directly to ``_parse_reddit`` (no network), and the registered feed provider
is verified to surface in ``assemble_feeds()`` with the right parser key. The
fixture deliberately mixes well-formed posts with malformed ones so the
parser's tolerance contract (skip, never raise) is pinned.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from catchem.news_poller import ParsedItem, assemble_feeds
from catchem.news_sources.reddit import _SUBREDDITS, _parse_reddit

# ── Sample listing fixture ────────────────────────────────────────────────
# Shape mirrors Reddit's real /r/<sub>/new.json: {"data":{"children":[{kind,
# data:{...}}, ...]}}. Five children exercise every branch:
#   [0] self-post with a real selftext body          → text == selftext
#   [1] link-post with empty selftext                → text falls back to title
#   [2] missing title                                → skipped
#   [3] missing permalink                            → skipped
#   [4] child whose `data` is not a dict             → skipped
# Two epochs chosen so the UTC conversion is unambiguous to assert.
_TS_SELF = 1_716_900_000  # 2024-05-28 14:00:00 UTC
_TS_LINK = 1_716_903_600  # 2024-05-28 15:00:00 UTC

_SAMPLE: dict = {
    "kind": "Listing",
    "data": {
        "after": "t3_xyz",
        "children": [
            {
                "kind": "t3",
                "data": {
                    "title": "GME to the moon — DD inside",
                    "selftext": "Here is my deep dive on the short interest.",
                    "permalink": "/r/wallstreetbets/comments/abc123/gme_to_the_moon/",
                    "url": "https://www.reddit.com/r/wallstreetbets/comments/abc123/gme_to_the_moon/",
                    "created_utc": float(_TS_SELF),
                    "subreddit": "wallstreetbets",
                },
            },
            {
                "kind": "t3",
                "data": {
                    "title": "Fed signals rate hold",
                    "selftext": "",  # link post — no body
                    "permalink": "/r/stocks/comments/def456/fed_signals_rate_hold/",
                    "url": "https://example.com/fed-article",
                    "created_utc": float(_TS_LINK),
                    "subreddit": "stocks",
                },
            },
            {
                "kind": "t3",
                "data": {
                    # no "title" → must be skipped
                    "selftext": "Body without a title",
                    "permalink": "/r/investing/comments/ghi789/no_title/",
                    "created_utc": float(_TS_SELF),
                },
            },
            {
                "kind": "t3",
                "data": {
                    "title": "Has a title but no permalink",
                    "selftext": "x",
                    # no "permalink" → must be skipped
                    "created_utc": float(_TS_SELF),
                },
            },
            {
                "kind": "t3",
                "data": "not-a-dict",  # malformed child data -> must be skipped
            },
            "completely-malformed-child-not-even-a-dict",
        ],
    },
}


def _sample_bytes() -> bytes:
    return json.dumps(_SAMPLE).encode("utf-8")


# ── Parser behaviour ───────────────────────────────────────────────────────


def test_parse_reddit_maps_fields_and_skips_malformed() -> None:
    items = _parse_reddit(_sample_bytes(), "reddit.com")

    # Only the two well-formed children survive; the three malformed ones
    # (no title / no permalink / non-dict data) are silently skipped.
    assert len(items) == 2
    assert all(isinstance(i, ParsedItem) for i in items)

    self_post, link_post = items

    # ── self-post: text comes from selftext, url is the permalink discussion
    assert self_post.title == "GME to the moon — DD inside"
    assert self_post.text == "Here is my deep dive on the short interest."
    assert self_post.url == (
        "https://www.reddit.com/r/wallstreetbets/comments/abc123/gme_to_the_moon/"
    )
    assert self_post.domain == "reddit.com"
    # epoch float → tz-aware UTC datetime
    assert self_post.published_ts == datetime.fromtimestamp(_TS_SELF, tz=UTC)
    assert self_post.published_ts.tzinfo is not None
    assert self_post.published_ts.utcoffset() == UTC.utcoffset(None)

    # ── link-post: empty selftext falls back to the title for `text`
    assert link_post.title == "Fed signals rate hold"
    assert link_post.text == "Fed signals rate hold"
    # url is the *discussion* permalink, NOT the off-site `url` field
    assert link_post.url == (
        "https://www.reddit.com/r/stocks/comments/def456/fed_signals_rate_hold/"
    )
    assert link_post.domain == "reddit.com"
    assert link_post.published_ts == datetime.fromtimestamp(_TS_LINK, tz=UTC)


def test_parse_reddit_permalink_is_prefixed_with_host() -> None:
    """Site-relative permalinks must become absolute reddit.com URLs."""
    items = _parse_reddit(_sample_bytes(), "reddit.com")
    assert all(i.url.startswith("https://www.reddit.com/r/") for i in items)


def test_parse_reddit_tolerates_garbage_bytes() -> None:
    """Non-JSON / unexpected shapes return [] rather than raising."""
    assert _parse_reddit(b"this is not json", "reddit.com") == []
    assert _parse_reddit(b"", "reddit.com") == []
    assert _parse_reddit(b"[1, 2, 3]", "reddit.com") == []  # top-level list
    assert _parse_reddit(b'{"data": {}}', "reddit.com") == []  # no children
    assert _parse_reddit(b'{"data": {"children": "nope"}}', "reddit.com") == []


def test_parse_reddit_handles_missing_created_utc() -> None:
    """A post without a usable created_utc still parses (falls back to now)."""
    payload = {
        "data": {
            "children": [
                {
                    "data": {
                        "title": "No timestamp here",
                        "selftext": "body",
                        "permalink": "/r/economy/comments/jkl/no_ts/",
                        # created_utc intentionally absent
                    }
                }
            ]
        }
    }
    before = datetime.now(UTC)
    items = _parse_reddit(json.dumps(payload).encode(), "reddit.com")
    after = datetime.now(UTC)
    assert len(items) == 1
    assert items[0].published_ts.tzinfo is not None
    assert before <= items[0].published_ts <= after


# ── Feed-provider registration ───────────────────────────────────────────────


def test_reddit_feeds_present_in_assemble_feeds() -> None:
    feeds = assemble_feeds()
    by_name = {f.name: f for f in feeds}

    for sub in _SUBREDDITS:
        name = f"reddit-{sub}"
        assert name in by_name, f"missing feed {name} from assemble_feeds()"
        spec = by_name[name]
        assert spec.parser == "reddit"
        assert spec.fallback_domain == "reddit.com"
        assert spec.url == f"https://www.reddit.com/r/{sub}/new.json?limit=50"


def test_required_subreddits_are_configured() -> None:
    """Pin the exact required subreddit set from the task spec."""
    assert set(_SUBREDDITS) == {
        "wallstreetbets",
        "stocks",
        "investing",
        "StockMarket",
        "cryptocurrency",
        "economy",
        "finance",
    }
