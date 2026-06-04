from __future__ import annotations

import pytest

from catchem.finance_filter import FastPrefilter
from catchem.taxonomy import default_taxonomy_path, load_taxonomy


@pytest.fixture
def prefilter() -> FastPrefilter:
    return FastPrefilter(taxonomy=load_taxonomy(default_taxonomy_path()))


def test_finance_capture_keeps(prefilter, synth_capture) -> None:
    res = prefilter.evaluate(synth_capture())
    assert res.keep is True
    assert res.rule_score >= 0.3
    assert (
        "fed" in {k.lower() for k in res.matched_keywords}
        or "rate" in res.matched_keywords
        or "rates" in res.matched_keywords
    )


def test_obvious_sports_is_rejected(prefilter, synth_non_finance_capture) -> None:
    res = prefilter.evaluate(synth_non_finance_capture)
    # ESPN with sports keywords and no finance hits → conservative reject
    assert res.keep is False
    assert res.rule_score < 0.10


def test_cashtag_signal_is_captured(prefilter, synth_capture) -> None:
    cap = synth_capture(title="$AAPL beats on earnings", text="$AAPL beat earnings expectations.")
    res = prefilter.evaluate(cap)
    assert "cashtag" in res.matched_keywords


def test_unknown_domain_uses_default_prior(prefilter, synth_capture) -> None:
    cap = synth_capture(domain="nowhere.example.com")
    res = prefilter.evaluate(cap)
    # default prior is 0.45 — present in component
    assert 0.30 <= res.domain_prior <= 0.5


# ---------------------------------------------------------------------------
# _word_match: the matching primitive (whole-word vs substring)
# ---------------------------------------------------------------------------


def test_word_match_empty_keyword_returns_false() -> None:
    """A blank/whitespace keyword can never match (guards line 66)."""
    assert FastPrefilter._word_match("the fed raised rates", "") is False
    assert FastPrefilter._word_match("the fed raised rates", "   ") is False


def test_word_match_whole_word_for_alnum_keyword() -> None:
    """Alphanumeric keywords match on word boundaries, not substrings."""
    # "fed" must NOT match inside "federated".
    assert FastPrefilter._word_match("a federated identity system", "fed") is False
    # but matches the standalone token.
    assert FastPrefilter._word_match("the fed met today", "fed") is True


def test_word_match_substring_for_symbolic_keyword() -> None:
    """Non-alnum keywords (e.g. '$') fall back to substring matching."""
    assert FastPrefilter._word_match("shares hit $100 today", "$") is True
    assert FastPrefilter._word_match("no cashtag here", "$") is False


def test_word_match_multiword_keyword() -> None:
    """Multi-word alnum phrases still match (whitespace is alnum-adjacent)."""
    assert FastPrefilter._word_match("the central bank acted", "central bank") is True
    assert FastPrefilter._word_match("a central role", "central bank") is False


def test_word_match_fallback_compilation() -> None:
    """An alphanumeric keyword not in precompiled cache falls back to on-the-fly compilation."""
    assert FastPrefilter._word_match("apple tree", "apple") is True
    assert FastPrefilter._word_match("pineapple tree", "apple") is False


# ---------------------------------------------------------------------------
# Score composition / thresholds
# ---------------------------------------------------------------------------


def test_finance_capture_has_strong_domain_prior(prefilter, synth_capture) -> None:
    """reuters.com is a high-prior domain (0.95 in the default taxonomy)."""
    res = prefilter.evaluate(synth_capture())
    assert res.domain_prior == pytest.approx(0.95)
    assert res.source_type_prior == pytest.approx(0.55)  # rss


def test_rule_score_in_unit_interval(prefilter, synth_capture) -> None:
    res = prefilter.evaluate(synth_capture())
    assert 0.0 <= res.rule_score <= 1.0


def test_short_text_uses_domain_prior_floor(prefilter, synth_capture) -> None:
    """Below min_text_chars, rule_score = max(domain_prior, 0.20)."""
    # Low-prior domain + tiny text → score floors at 0.20.
    cap = synth_capture(domain="espn.com", title="hi", text="short")
    res = prefilter.evaluate(cap)
    assert res.rule_score == pytest.approx(0.20)


def test_short_text_high_prior_domain_keeps_domain_prior(prefilter, synth_capture) -> None:
    """Short text from a high-prior domain keeps the (higher) domain prior."""
    cap = synth_capture(domain="reuters.com", title="hi", text="x")
    res = prefilter.evaluate(cap)
    assert res.rule_score == pytest.approx(0.95)


def test_keyword_signal_saturates_at_four_hits(synth_capture) -> None:
    """keyword_signal = min(1.0, hits/4): >=4 distinct finance terms saturate."""
    pf = FastPrefilter(taxonomy=load_taxonomy(default_taxonomy_path()))
    cap = synth_capture(
        domain="nowhere.example.com",
        title="Markets digest",
        text=(
            "Earnings season continues as revenue beats and dividend hikes "
            "lift equity prices. Treasury yields ease while the dollar holds. "
            "Inflation data and the latest CPI print remain in focus."
        ),
    )
    res = pf.evaluate(cap)
    assert len(res.matched_keywords) >= 4
    # default prior 0.45, source rss 0.55, keyword_signal saturated to 1.0:
    # 0.45*0.45 + 0.20*0.55 + 0.35*1.0 = 0.6625
    assert res.rule_score == pytest.approx(0.45 * 0.45 + 0.20 * 0.55 + 0.35 * 1.0)


# ---------------------------------------------------------------------------
# Cashtag detection (case-sensitive on the ORIGINAL text)
# ---------------------------------------------------------------------------


def test_cashtag_requires_uppercase_ticker(prefilter, synth_capture) -> None:
    """The $TICKER regex is uppercase-only on raw text; '$aapl' is not a cashtag
    via that rule, though the bare '$' keyword still matches the lowercased text."""
    cap = synth_capture(title="lowercase mention", text="watching $aapl closely today and tomorrow")
    res = prefilter.evaluate(cap)
    assert "cashtag" not in res.matched_keywords
    # bare "$" still matched as a finance keyword on the lowercased excerpt.
    assert "$" in res.matched_keywords


def test_cashtag_detected_on_uppercase(prefilter, synth_capture) -> None:
    cap = synth_capture(title="ticker", text="big day for $TSLA and $NVDA earnings reports")
    res = prefilter.evaluate(cap)
    assert "cashtag" in res.matched_keywords


# ---------------------------------------------------------------------------
# Two-rule negative veto (>=2 blocked AND no finance hits AND domain_prior<0.30)
# ---------------------------------------------------------------------------


def test_veto_drops_when_two_blocked_no_finance_low_prior(prefilter, synth_capture) -> None:
    """All three veto conditions met → keep=False, rule_score pinned to 0.05."""
    cap = synth_capture(
        domain="espn.com",  # prior 0.05 < 0.30
        title="Match recap",
        text=(
            "The scoreboard showed a thrilling finish. A late goal and a stunning "
            "touchdown highlighted the contest as fans celebrated all night long."
        ),
    )
    res = prefilter.evaluate(cap)
    assert len(res.blocked_keywords) >= 2
    assert res.matched_keywords == ()
    assert res.keep is False
    assert res.rule_score == pytest.approx(0.05)


def test_no_veto_when_finance_keyword_present(prefilter, synth_capture) -> None:
    """A single finance hit cancels the veto even with sports keywords present."""
    cap = synth_capture(
        domain="espn.com",
        title="Stadium financing",
        text=(
            "The scoreboard deal aside, the club's revenue and earnings drew "
            "investor attention. A late goal and a touchdown capped the night."
        ),
    )
    res = prefilter.evaluate(cap)
    assert len(res.blocked_keywords) >= 2
    assert res.matched_keywords != ()  # 'revenue'/'earnings' matched
    assert res.keep is True  # veto suppressed


def test_no_veto_when_only_one_blocked_keyword(prefilter, synth_capture) -> None:
    """Fewer than 2 blocked keywords → no veto, item kept."""
    cap = synth_capture(
        domain="espn.com",
        title="Single sports word",
        text=(
            "The team's recipe for success this season has been consistency and "
            "depth across the roster according to the analysts covering the league."
        ),
    )
    res = prefilter.evaluate(cap)
    assert len(res.blocked_keywords) == 1  # only 'recipe'
    assert res.keep is True


def test_no_veto_when_domain_prior_high(synth_capture) -> None:
    """A trusted domain (prior >= 0.30) is never vetoed, even with sports words."""
    pf = FastPrefilter(taxonomy=load_taxonomy(default_taxonomy_path()))
    cap = synth_capture(
        domain="reuters.com",  # prior 0.95
        title="Sports business",
        text=(
            "The scoreboard rivalry aside, a contested transfer window and a "
            "disputed goal dominated coverage with no clear financial angle here."
        ),
    )
    res = pf.evaluate(cap)
    assert len(res.blocked_keywords) >= 2
    assert res.matched_keywords == ()
    assert res.keep is True  # high domain prior cancels veto


# ---------------------------------------------------------------------------
# Robustness: missing / empty fields
# ---------------------------------------------------------------------------


def _bare_capture(title: str, text: str, *, domain: str = "nowhere.example.com"):
    """An AwarenessCaptureView with exactly the given (possibly empty) fields.

    The ``synth_capture`` fixture substitutes a finance-heavy default body when
    ``text`` is falsy, so genuinely-empty-field cases are built directly.
    """
    from datetime import UTC, datetime

    from catchem.schemas import AwarenessCaptureView

    return AwarenessCaptureView(
        capture_id="c",
        doc_id="d",
        title=title,
        text=text,
        domain=domain,
        url=f"https://{domain}/a",
        source_type="rss",
        discovery_channel=f"rss:{domain}",
        language="en",
        fetch_ts=datetime.now(UTC),
        observed_ts=datetime.now(UTC),
        content_hash="h",
        robots_decision="not_applicable",
    )


def test_empty_title_and_text_does_not_crash(prefilter) -> None:
    """Genuinely empty title+text is tolerated; the short-text branch runs."""
    cap = _bare_capture("", "")
    res = prefilter.evaluate(cap)
    assert res.keep is True  # nothing to veto on
    assert res.matched_keywords == ()
    assert res.blocked_keywords == ()
    # default domain prior 0.45 > 0.20 floor → short-text branch returns 0.45
    assert res.rule_score == pytest.approx(0.45)


def test_none_title_does_not_crash(prefilter) -> None:
    """A None title (schema allows it) is coerced to '' by _excerpt."""
    cap = _bare_capture(None, "", domain="nowhere.example.com")  # type: ignore[arg-type]
    res = prefilter.evaluate(cap)
    assert res.keep is True
    assert res.matched_keywords == ()


def test_excerpt_is_lowercased_and_joined() -> None:
    """_excerpt lowercases and joins title + truncated body."""
    from datetime import UTC, datetime

    from catchem.schemas import AwarenessCaptureView

    cap = AwarenessCaptureView(
        capture_id="c",
        doc_id="d",
        title="FED RAISES RATES",
        text="Inflation Persists.",
        domain="reuters.com",
        url="https://reuters.com/a",
        source_type="rss",
        discovery_channel="rss:reuters.com",
        language="en",
        fetch_ts=datetime.now(UTC),
        observed_ts=datetime.now(UTC),
        content_hash="h",
        robots_decision="not_applicable",
    )
    excerpt = FastPrefilter._excerpt(cap)
    assert excerpt == "fed raises rates\ninflation persists."


def test_excerpt_truncates_body_to_max_chars() -> None:
    """_excerpt caps the body at max_chars (default 800)."""
    from datetime import UTC, datetime

    from catchem.schemas import AwarenessCaptureView

    long_body = "a" * 2000
    cap = AwarenessCaptureView(
        capture_id="c",
        doc_id="d",
        title="t",
        text=long_body,
        domain="reuters.com",
        url="https://reuters.com/a",
        source_type="rss",
        discovery_channel="rss:reuters.com",
        language="en",
        fetch_ts=datetime.now(UTC),
        observed_ts=datetime.now(UTC),
        content_hash="h",
        robots_decision="not_applicable",
    )
    excerpt = FastPrefilter._excerpt(cap, max_chars=50)
    # "t" + "\n" + 50 'a's
    assert excerpt == "t\n" + "a" * 50


def test_custom_min_text_chars_threshold(synth_capture) -> None:
    """min_text_chars is configurable and routes scoring accordingly."""
    pf = FastPrefilter(taxonomy=load_taxonomy(default_taxonomy_path()), min_text_chars=5)
    # Body long enough to clear the tiny 5-char threshold → keyword branch runs.
    cap = synth_capture(domain="nowhere.example.com", title="Fed", text="rate hike news today")
    res = pf.evaluate(cap)
    # keyword branch (not the short-text floor) → blend, not max(prior, 0.20)
    assert res.rule_score != pytest.approx(0.20)
    assert res.matched_keywords != ()
