"""Regression tests for the H-config bug-hunt group.

Each test fails on the pre-fix code and passes after the fix:

  1. gnews_global 'pt' locale ceid language token must agree with hl.
  2. `catchem replay --path FILE` must replay ONLY that file (single-file
     contract), not every sibling *.jsonl in the parent directory.
  3. `replay.awareness_jsonl_glob` must actually scope the directory scan
     (was dead config) — exposed via ReplayConfig.replay_pattern().
  4. Coverage-gap detector must match free-text watch terms on WORD
     BOUNDARIES, not bare substring (short tickers no longer false-cover).
  5. run_demo must NOT evict the global Settings lru_cache.
  6. enrich_holdings must populate `sentiment_label` (was a dead UI field).
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from catchem.awareness_gaps import find_coverage_gaps
from catchem.news_sources.gnews_global import gnews_global_feeds
from catchem.portfolio import enrich_holdings
from catchem.settings import ReplayConfig, load_settings


# ── Finding 1: gnews_global pt ceid/hl agreement ───────────────────────────
def test_gnews_global_pt_ceid_language_matches_hl() -> None:
    """The Brazil feed's ceid language token must equal its hl (pt-BR)."""
    feeds = {f.name: f for f in gnews_global_feeds()}
    spec = feeds["gnews-pt"]
    qs = parse_qs(urlsplit(spec.url).query)
    hl = qs["hl"][0]
    ceid = qs["ceid"][0]
    assert ":" in ceid, spec.url
    gl_token, _, lang_token = ceid.partition(":")
    # The whole invariant the other locales obey: ceid == "<gl>:<hl>".
    assert hl == "pt-BR", spec.url
    assert gl_token == qs["gl"][0], spec.url
    assert lang_token == hl, f"ceid lang {lang_token!r} must match hl {hl!r}: {spec.url}"


def test_gnews_global_all_locales_obey_ceid_invariant() -> None:
    """Every locale (not just pt) must follow ceid == '<gl>:<hl>'."""
    for spec in gnews_global_feeds():
        qs = parse_qs(urlsplit(spec.url).query)
        hl, gl, ceid = qs["hl"][0], qs["gl"][0], qs["ceid"][0]
        assert ceid == f"{gl}:{hl}", f"{spec.name}: ceid {ceid!r} != {gl}:{hl}"


# ── Finding 4: coverage-gap free-text word-boundary matching ───────────────
def _rec(title: str, *, ts: datetime, **extra: object) -> dict:
    return {"title": title, "published_ts": ts.isoformat(), **extra}


def test_coverage_gaps_short_ticker_not_substring_covered() -> None:
    """A 1-letter ticker must NOT be marked covered by an unrelated word.

    Pre-fix: `lc in text_blob` makes ticker 'T' covered by the 't' in any
    headline. Post-fix: word-boundary match → 'T' stays a genuine gap.
    """
    now = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
    records = [_rec("Boston transit changes announced today", ts=now)]
    result = find_coverage_gaps(records, ["T", "ON", "GE"], now=now)
    # None of these short tickers are actually mentioned as words.
    assert set(result["gaps"]) == {"T", "ON", "GE"}, result
    assert result["covered"] == []


def test_coverage_gaps_word_boundary_still_covers_real_mention() -> None:
    """A genuine standalone-word mention is still counted as covered."""
    now = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
    records = [_rec("Shares of T jump after the merger", ts=now)]
    result = find_coverage_gaps(records, ["T"], now=now)
    assert result["gaps"] == []
    assert [c["term"] for c in result["covered"]] == ["T"]


def test_coverage_gaps_exact_symbol_field_still_covers() -> None:
    """The exact symbol-set branch is untouched: a symbol-list hit covers."""
    now = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
    records = [_rec("totally unrelated", ts=now, candidate_symbols=["GM"])]
    result = find_coverage_gaps(records, ["GM"], now=now)
    assert result["gaps"] == []
    assert [c["term"] for c in result["covered"]] == ["GM"]


# ── Finding 6: enrich_holdings populates sentiment_label ───────────────────
def test_enrich_holdings_populates_sentiment_label() -> None:
    """sentiment_label is derived from the top-scoring matching record."""
    now = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
    records = [
        {
            "title": "NVDA soars on blowout earnings",
            "published_ts": now.isoformat(),
            "candidate_symbols": ["NVDA"],
            "finance_relevance_score": 0.9,
            "sentiment_label": "positive",
        },
        {
            "title": "NVDA slips slightly midday",
            "published_ts": now.isoformat(),
            "candidate_symbols": ["NVDA"],
            "finance_relevance_score": 0.4,
            "sentiment_label": "negative",
        },
    ]
    enriched = enrich_holdings(
        [{"symbol": "NVDA"}], records=records, quote_fn=lambda s: None, now=now
    )
    assert "sentiment_label" in enriched[0]
    # Top-scoring (0.9) record drives the label.
    assert enriched[0]["sentiment_label"] == "positive"


def test_enrich_holdings_sentiment_none_when_no_match() -> None:
    """No matching record → sentiment_label is None (not absent)."""
    now = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
    enriched = enrich_holdings(
        [{"symbol": "NVDA"}], records=[], quote_fn=lambda s: None, now=now
    )
    assert enriched[0]["sentiment_label"] is None


def test_enrich_holdings_short_ticker_word_boundary() -> None:
    """enrich_holdings free-text match is on word boundaries too (parity with
    find_coverage_gaps), so a short ticker is not falsely counted."""
    now = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
    records = [
        {"title": "Boston changes its transit map", "published_ts": now.isoformat()}
    ]
    enriched = enrich_holdings(
        [{"symbol": "ON"}], records=records, quote_fn=lambda s: None, now=now
    )
    assert enriched[0]["recent_news_count"] == 0
    assert enriched[0]["coverage"]["covered"] is False


# ── Finding 3: replay.awareness_jsonl_glob actually scopes the scan ────────
def test_replay_pattern_trims_jsonl_prefix() -> None:
    """The default project-relative glob is trimmed to be root-relative.

    discover_awareness_jsonl_root already lands on `…/jsonl`, so the
    configured `data/jsonl/captures/**/*.jsonl` must become
    `captures/**/*.jsonl` (the part past the first `jsonl/`).
    """
    cfg = ReplayConfig()
    assert cfg.replay_pattern() == "captures/**/*.jsonl"


def test_replay_pattern_custom_value_is_used() -> None:
    """A custom glob without a jsonl/ segment is used verbatim (no longer a
    silent no-op)."""
    cfg = ReplayConfig(awareness_jsonl_glob="2026/**/*.jsonl")
    assert cfg.replay_pattern() == "2026/**/*.jsonl"


def test_replay_pattern_blank_falls_back() -> None:
    cfg = ReplayConfig(awareness_jsonl_glob="   ")
    assert cfg.replay_pattern() == "**/*.jsonl"


# ── Finding 2: replay --path FILE replays only that file ───────────────────
def test_run_replay_single_file_isolates_target(write_jsonl, synth_capture) -> None:
    """Supervisor.run_replay(file=X) ingests ONLY X, not its siblings."""
    from catchem.supervisor import Supervisor

    target = write_jsonl([synth_capture(capture_id="cap-target").model_dump(mode="json")], name="target.jsonl")
    # Sibling in the SAME directory that the pre-fix code would have swept in.
    write_jsonl(
        [synth_capture(capture_id="cap-sibling", doc_id="doc-sibling").model_dump(mode="json")],
        name="sibling.jsonl",
    )

    s = load_settings()
    sup = Supervisor(s)
    try:
        counts = sup.run_replay(file=target)
        assert sup.storage.get_record("cap-target") is not None
        # The sibling must NOT have been ingested.
        assert sup.storage.get_record("cap-sibling") is None
        assert counts["processed"] == 1
    finally:
        sup.close()


# ── Finding 5: run_demo does not evict the global Settings cache ───────────
def test_run_demo_does_not_clear_settings_cache(tmp_settings) -> None:
    """run_demo() must reuse the cached Settings, not call cache_clear()."""
    from catchem import demo as demo_mod

    before = load_settings()
    # Same object identity proves the lru_cache is warm.
    assert load_settings() is before

    called = {"clear": 0}
    real_clear = load_settings.cache_clear

    def _spy_clear() -> None:
        called["clear"] += 1
        real_clear()

    # Monkeypatch the cache_clear used by reload_settings to detect eviction.
    orig = load_settings.cache_clear
    try:
        load_settings.cache_clear = _spy_clear  # type: ignore[method-assign]
        demo_mod.run_demo(
            title="Fed holds rates steady",
            text="The Federal Reserve left interest rates unchanged on Wednesday.",
            domain="reuters.com",
        )
    finally:
        load_settings.cache_clear = orig  # type: ignore[method-assign]

    assert called["clear"] == 0, "run_demo must not evict the global Settings cache"
    # Cache still warm + same object as before the demo.
    assert load_settings() is before
