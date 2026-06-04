"""Tests for ``catchem.quant.novelty``.

Covers the contract listed in the spec:
  * empty corpus => score 1.0, no neighbor
  * exact-content twin => near-duplicate bucket
  * unrelated record => high novelty
  * self-exclusion when corpus contains the target's own id
  * ``score_corpus`` preserves input order
  * None title + empty fields don't crash
"""

from __future__ import annotations

import pytest

from catchem.quant.novelty import (
    NoveltyResult,
    compute_novelty,
    score_corpus,
)


def _rec(
    capture_id: str,
    *,
    title: str | None = "Untitled",
    text: str = "",
    symbols: list[str] | None = None,
    reasons: list[str] | None = None,
    classes: list[str] | None = None,
) -> dict:
    return {
        "capture_id": capture_id,
        "title": title,
        "text_excerpt": text,
        "domain": "example.com",
        "published_ts": "2026-05-27T00:00:00Z",
        "candidate_symbols": symbols or [],
        "asset_classes": classes or [],
        "impact_reason_codes": reasons or [],
    }


# --- compute_novelty -------------------------------------------------------


def test_empty_corpus_means_max_novelty() -> None:
    target = _rec("a", title="Apple raises guidance", text="iPhone demand strong")

    result = compute_novelty(target, corpus=[])

    assert isinstance(result, NoveltyResult)
    assert result.capture_id == "a"
    assert result.novelty_score == 1.0
    assert result.max_similarity_to_corpus == 0.0
    assert result.nearest_capture_id is None
    assert result.nearest_title is None
    assert result.matched_symbols == ()
    assert result.explanation == "first of kind in corpus"


def test_near_duplicate_is_detected() -> None:
    original = _rec(
        "orig",
        title="Apple raises full-year guidance on iPhone demand",
        text="Cupertino reported stronger than expected iPhone unit sales.",
        symbols=["AAPL"],
        reasons=["GUIDANCE_RAISE"],
        classes=["EQUITY"],
    )
    twin = _rec(
        "twin",
        title="Apple raises full-year guidance on iPhone demand",
        text="Cupertino reported stronger than expected iPhone unit sales.",
        symbols=["AAPL"],
        reasons=["GUIDANCE_RAISE"],
        classes=["EQUITY"],
    )

    result = compute_novelty(twin, corpus=[original])

    assert result.max_similarity_to_corpus >= 0.85
    assert result.novelty_score <= 0.15
    assert result.nearest_capture_id == "orig"
    assert "AAPL".lower() in result.matched_symbols  # symbols are normalized
    assert "near-duplicate" in result.explanation


def test_unrelated_record_is_highly_novel() -> None:
    target = _rec(
        "target",
        title="Brazilian central bank surprises with 75bp cut",
        text="Selic taken to 9.25% citing softer inflation prints across services.",
        symbols=["BRL"],
        reasons=["MONETARY_POLICY"],
        classes=["FX"],
    )
    corpus = [
        _rec(
            "n1",
            title="Apple raises full-year guidance",
            text="Cupertino reported stronger iPhone sales.",
            symbols=["AAPL"],
            reasons=["GUIDANCE_RAISE"],
            classes=["EQUITY"],
        ),
        _rec(
            "n2",
            title="Bitcoin breaks above $90k",
            text="Spot ETF flows accelerate.",
            symbols=["BTC"],
            reasons=["FLOW_SHOCK"],
            classes=["CRYPTO"],
        ),
    ]

    result = compute_novelty(target, corpus=corpus)

    assert result.novelty_score >= 0.8
    assert result.max_similarity_to_corpus <= 0.20
    # explanation should land in either "first of kind" or "low overlap"
    assert result.explanation in {
        "first of kind in corpus",
        "low overlap with prior coverage",
    }


def test_self_excluded_when_corpus_contains_target_id() -> None:
    target = _rec(
        "same-id",
        title="Tesla pushes Cybertruck delivery date",
        text="Production ramp slips again amid supply constraints.",
        symbols=["TSLA"],
        reasons=["GUIDANCE_CUT"],
        classes=["EQUITY"],
    )
    other = _rec(
        "different-id",
        title="Brazilian central bank surprises with 75bp cut",
        text="Selic taken to 9.25%.",
        symbols=["BRL"],
        reasons=["MONETARY_POLICY"],
        classes=["FX"],
    )

    # Corpus contains the *same* capture_id as the target — should be ignored.
    result = compute_novelty(target, corpus=[target, other])

    # If self-exclusion didn't work, max_similarity would be 1.0.
    assert result.nearest_capture_id == "different-id"
    assert result.max_similarity_to_corpus < 0.5


# --- score_corpus ----------------------------------------------------------


def test_score_corpus_preserves_order_and_size() -> None:
    corpus = [
        _rec("first", title="alpha bravo charlie", text="x" * 10, symbols=["A"]),
        _rec("second", title="delta echo foxtrot", text="y" * 10, symbols=["B"]),
        _rec("third", title="golf hotel india", text="z" * 10, symbols=["C"]),
    ]

    results = score_corpus(corpus)

    assert [r.capture_id for r in results] == ["first", "second", "third"]
    assert len(results) == 3
    # Three unrelated rows => each compared to two unrelated neighbors;
    # similarity should be near zero.
    for r in results:
        assert r.max_similarity_to_corpus <= 0.2
        assert r.novelty_score >= 0.8


def test_score_corpus_detects_internal_duplicate() -> None:
    twin_a = _rec(
        "twin-a",
        title="Tesla pushes Cybertruck delivery date",
        text="Production ramp slips again amid supply constraints.",
        symbols=["TSLA"],
        reasons=["GUIDANCE_CUT"],
        classes=["EQUITY"],
    )
    twin_b = _rec(
        "twin-b",
        title="Tesla pushes Cybertruck delivery date",
        text="Production ramp slips again amid supply constraints.",
        symbols=["TSLA"],
        reasons=["GUIDANCE_CUT"],
        classes=["EQUITY"],
    )
    odd = _rec(
        "odd",
        title="Brazilian central bank surprises with 75bp cut",
        text="Selic taken to 9.25%.",
        symbols=["BRL"],
        reasons=["MONETARY_POLICY"],
        classes=["FX"],
    )

    results = score_corpus([twin_a, twin_b, odd])
    by_id = {r.capture_id: r for r in results}

    assert by_id["twin-a"].max_similarity_to_corpus >= 0.85
    assert by_id["twin-b"].max_similarity_to_corpus >= 0.85
    assert "near-duplicate" in by_id["twin-a"].explanation
    assert by_id["twin-a"].nearest_capture_id == "twin-b"
    assert by_id["twin-b"].nearest_capture_id == "twin-a"
    # The unrelated record stays novel.
    assert by_id["odd"].novelty_score >= 0.8


# --- edge cases ------------------------------------------------------------


def test_none_title_does_not_crash() -> None:
    target = _rec("nt", title=None, text="some excerpt about markets")
    corpus = [_rec("other", title=None, text="completely different excerpt")]

    result = compute_novelty(target, corpus=corpus)

    assert isinstance(result.explanation, str)
    assert result.explanation  # non-empty
    assert 0.0 <= result.novelty_score <= 1.0


def test_completely_empty_record_against_empty_corpus() -> None:
    target = _rec("empty", title=None, text="")

    result = compute_novelty(target, corpus=[])

    assert result.novelty_score == 1.0
    assert result.explanation == "first of kind in corpus"


def test_completely_empty_record_against_nonempty_corpus() -> None:
    target = _rec("empty", title=None, text="")
    corpus = [_rec("real", title="A", text="some content", symbols=["AAPL"])]

    result = compute_novelty(target, corpus=corpus)

    # No features => all Jaccards are zero => max similarity is zero, but
    # corpus is non-empty so nearest neighbor is still reported.
    assert result.max_similarity_to_corpus == 0.0
    assert result.novelty_score == 1.0
    assert result.nearest_capture_id == "real"
    # Falls into the "first of kind" bucket because similarity < 0.10.
    assert result.explanation == "first of kind in corpus"


def test_mid_band_similarity_picks_theme_bucket() -> None:
    # Engineer overlap that lands in the 0.50-0.85 band: same symbols /
    # reasons / classes (covers 0.45 of the weighted blend) plus partial
    # token overlap.
    a = _rec(
        "a",
        title="Apple raises full-year guidance on iPhone demand",
        text="Cupertino reports strong holiday quarter.",
        symbols=["AAPL"],
        reasons=["GUIDANCE_RAISE"],
        classes=["EQUITY"],
    )
    b = _rec(
        "b",
        title="Apple slashes full-year guidance amid weak iPad sales",
        text="Cupertino warns of softer holiday quarter.",
        symbols=["AAPL"],
        reasons=["GUIDANCE_RAISE"],
        classes=["EQUITY"],
    )

    result = compute_novelty(a, corpus=[b])

    assert 0.50 <= result.max_similarity_to_corpus < 0.85
    assert result.explanation.startswith("shares symbols + theme")


@pytest.mark.parametrize(
    "field",
    ["candidate_symbols", "asset_classes", "impact_reason_codes"],
)
def test_missing_list_fields_treated_as_empty(field: str) -> None:
    target = _rec("t", title="x", text="y")
    target.pop(field)  # actually missing key, not just empty list

    corpus = [_rec("o", title="x", text="y")]

    result = compute_novelty(target, corpus=corpus)

    assert 0.0 <= result.novelty_score <= 1.0
    assert isinstance(result.explanation, str)


# --- internal helper coverage ----------------------------------------------


def test_to_string_set_rejects_non_collections() -> None:
    """Scalars / strings / falsy values coerce to an empty frozenset."""

    from catchem.quant.novelty import _to_string_set  # type: ignore[attr-defined]

    assert _to_string_set(None) == frozenset()
    assert _to_string_set("") == frozenset()
    assert _to_string_set(0) == frozenset()
    # A bare string is list-ish to Python but not a list/tuple/set here.
    assert _to_string_set("AAPL") == frozenset()
    assert _to_string_set(123) == frozenset()


def test_to_string_set_skips_none_lowers_and_strips() -> None:
    """None entries are dropped; survivors are stripped + lowercased."""

    from catchem.quant.novelty import _to_string_set  # type: ignore[attr-defined]

    assert _to_string_set(["AAPL", None, "  msft ", ""]) == frozenset({"aapl", "msft"})


def test_jaccard_empty_sets_is_zero() -> None:
    """Two empty token sets have Jaccard 0.0 (no divide-by-zero)."""

    from catchem.quant.novelty import _jaccard  # type: ignore[attr-defined]

    assert _jaccard(frozenset(), frozenset()) == 0.0


def test_jaccard_identical_and_disjoint() -> None:
    from catchem.quant.novelty import _jaccard  # type: ignore[attr-defined]

    assert _jaccard(frozenset({"a", "b"}), frozenset({"a", "b"})) == 1.0
    assert _jaccard(frozenset({"a"}), frozenset({"b"})) == 0.0
    assert _jaccard(frozenset({"a", "b"}), frozenset({"b", "c"})) == pytest.approx(1 / 3)


def test_explain_low_overlap_band() -> None:
    """A 0.10-0.50 similarity lands in the 'low overlap' bucket."""

    from catchem.quant.novelty import _explain  # type: ignore[attr-defined]

    assert _explain(0.30, "Some Title", corpus_empty=False) == "low overlap with prior coverage"


def test_explain_buckets_full_ladder() -> None:
    """Each similarity band maps to its documented phrase."""

    from catchem.quant.novelty import _explain  # type: ignore[attr-defined]

    assert _explain(0.0, None, corpus_empty=True) == "first of kind in corpus"
    assert _explain(0.05, "T", corpus_empty=False) == "first of kind in corpus"
    assert _explain(0.90, "T", corpus_empty=False) == "near-duplicate of T"
    assert _explain(0.60, "T", corpus_empty=False) == "shares symbols + theme with T"
    # Untitled nearest record falls back to a stable label.
    assert _explain(0.90, None, corpus_empty=False) == "near-duplicate of untitled record"


def test_low_overlap_band_end_to_end() -> None:
    """A real pair with small-but-nonzero overlap reports 'low overlap'.

    Same asset class (0.10 weight) plus one shared token nudges similarity
    into [0.10, 0.50), exercising the final ``_explain`` branch via the
    public API rather than a direct helper call.
    """

    a = _rec(
        "a",
        title="Apple quarterly earnings beat expectations widely",
        text="strong revenue growth across services segment reported today",
        symbols=["AAPL"],
        reasons=["GUIDANCE_RAISE"],
        classes=["EQUITY"],
    )
    b = _rec(
        "b",
        title="Sovereign bond auction draws tepid demand from investors",
        text="yields rose as buyers stepped back from the latest issuance",
        symbols=["UST"],
        reasons=["SUPPLY_SHOCK"],
        classes=["EQUITY"],  # only the asset class overlaps
    )

    result = compute_novelty(a, corpus=[b])

    assert 0.10 <= result.max_similarity_to_corpus < 0.50
    assert result.explanation == "low overlap with prior coverage"


def test_jaccard_union_zero_coverage() -> None:
    from catchem.quant.novelty import _jaccard

    class FakeSet:
        def __init__(self, is_empty: bool) -> None:
            self.is_empty = is_empty

        def __bool__(self) -> bool:
            return not self.is_empty

        def __and__(self, other: FakeSet) -> FakeSet:
            return FakeSet(True)

        def __or__(self, other: FakeSet) -> FakeSet:
            return FakeSet(True)

        def __len__(self) -> int:
            return 0

    assert _jaccard(FakeSet(False), FakeSet(False)) == 0.0


def test_score_corpus_single_element() -> None:
    corpus = [_rec("only-one", title="Unique Title", text="Unique content")]
    results = score_corpus(corpus)
    assert len(results) == 1
    assert results[0].capture_id == "only-one"
    assert results[0].novelty_score == 1.0
    assert results[0].max_similarity_to_corpus == 0.0
    assert results[0].nearest_capture_id is None
    assert results[0].explanation == "first of kind in corpus"


def test_score_corpus_empty() -> None:
    assert score_corpus([]) == []
