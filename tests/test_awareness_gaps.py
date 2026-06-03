"""Unit pins for the awareness BLIND-SPOT detector (``find_coverage_gaps``).

The function is pure + deterministic — ``now`` is injected so every case here
is reproducible. We pin the load-bearing contract:

  * covered vs gap classification (mention present vs absent),
  * the rolling-window boundary (in-window counts, out-of-window does not),
  * case-insensitivity of term matching against title + text,
  * symbols-field matching (exact token, case-insensitive),
  * freshness (`last_seen_age_seconds`) + `mention_count`,
  * tolerance of empty inputs + malformed records,
  * the stable output envelope shape.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from catchem.awareness_gaps import find_coverage_gaps

# Fixed reference instant for all deterministic cases.
NOW = datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)


def _rec(title: str = "", *, age_seconds: float = 0.0, **extra: object) -> dict:
    """Build a record dict published ``age_seconds`` before NOW."""
    rec: dict[str, object] = {
        "title": title,
        "published_ts": (NOW - timedelta(seconds=age_seconds)).isoformat(),
    }
    rec.update(extra)
    return rec


# ── Envelope shape ────────────────────────────────────────────────────────────


def test_output_envelope_shape() -> None:
    out = find_coverage_gaps([], ["AAPL"], window_seconds=3600.0, now=NOW)
    assert set(out) == {"generated_at", "window_seconds", "covered", "gaps"}
    assert out["generated_at"] == NOW.isoformat()
    assert out["window_seconds"] == 3600.0
    assert out["covered"] == []
    assert out["gaps"] == ["AAPL"]


# ── covered vs gap classification ─────────────────────────────────────────────


def test_covered_and_gap_split() -> None:
    records = [_rec("AAPL beats on earnings", age_seconds=60.0)]
    out = find_coverage_gaps(records, ["AAPL", "TSLA"], window_seconds=86400.0, now=NOW)

    assert out["gaps"] == ["TSLA"]  # never mentioned → blind spot
    assert [c["term"] for c in out["covered"]] == ["AAPL"]
    covered = out["covered"][0]
    assert covered["mention_count"] == 1
    assert covered["last_seen_age_seconds"] == 60.0


def test_mention_count_and_freshness_pick_most_recent() -> None:
    records = [
        _rec("AAPL old news", age_seconds=500.0),
        _rec("AAPL fresh scoop", age_seconds=30.0),
        _rec("AAPL mid", age_seconds=200.0),
    ]
    out = find_coverage_gaps(records, ["AAPL"], window_seconds=86400.0, now=NOW)
    covered = out["covered"][0]
    assert covered["mention_count"] == 3
    # freshness = age of the MOST recent mention, not the first seen.
    assert covered["last_seen_age_seconds"] == 30.0


# ── window boundary ───────────────────────────────────────────────────────────


def test_window_boundary_inclusive_and_exclusive() -> None:
    window = 3600.0
    records = [
        _rec("AAPL exactly on edge", age_seconds=3600.0),  # == window → in
        _rec("MSFT just outside", age_seconds=3600.1),      # > window → out
    ]
    out = find_coverage_gaps(records, ["AAPL", "MSFT"], window_seconds=window, now=NOW)
    assert [c["term"] for c in out["covered"]] == ["AAPL"]
    assert out["gaps"] == ["MSFT"]


def test_record_without_timestamp_is_ignored() -> None:
    records = [{"title": "AAPL surges"}]  # no published_ts / created_at
    out = find_coverage_gaps(records, ["AAPL"], window_seconds=86400.0, now=NOW)
    assert out["covered"] == []
    assert out["gaps"] == ["AAPL"]


def test_created_at_used_when_published_ts_absent() -> None:
    rec = {
        "title": "AAPL guidance raised",
        "created_at": (NOW - timedelta(seconds=120.0)).isoformat(),
    }
    out = find_coverage_gaps([rec], ["AAPL"], window_seconds=86400.0, now=NOW)
    assert out["covered"][0]["last_seen_age_seconds"] == 120.0


# ── case-insensitivity ────────────────────────────────────────────────────────


def test_case_insensitive_title_match() -> None:
    records = [_rec("apple (aapl) hits record high", age_seconds=10.0)]
    out = find_coverage_gaps(records, ["AAPL", "Apple"], window_seconds=86400.0, now=NOW)
    assert {c["term"] for c in out["covered"]} == {"AAPL", "Apple"}
    assert out["gaps"] == []


def test_case_insensitive_text_field_match() -> None:
    records = [_rec("Markets mixed", age_seconds=10.0, text="Shares of TSLA fell 3%")]
    out = find_coverage_gaps(records, ["tsla"], window_seconds=86400.0, now=NOW)
    assert [c["term"] for c in out["covered"]] == ["tsla"]


def test_text_excerpt_field_is_searched() -> None:
    records = [_rec("Headline", age_seconds=10.0, text_excerpt="NVDA up on AI demand")]
    out = find_coverage_gaps(records, ["NVDA"], window_seconds=86400.0, now=NOW)
    assert out["covered"][0]["term"] == "NVDA"


# ── symbols-field match ───────────────────────────────────────────────────────


def test_symbols_field_match_case_insensitive() -> None:
    records = [_rec("Unrelated headline", age_seconds=10.0, symbols=["aapl", "msft"])]
    out = find_coverage_gaps(records, ["AAPL", "TSLA"], window_seconds=86400.0, now=NOW)
    assert [c["term"] for c in out["covered"]] == ["AAPL"]
    assert out["gaps"] == ["TSLA"]


def test_candidate_symbols_field_match() -> None:
    records = [_rec("Earnings roundup", age_seconds=10.0, candidate_symbols=["GOOGL"])]
    out = find_coverage_gaps(records, ["GOOGL"], window_seconds=86400.0, now=NOW)
    assert out["covered"][0]["term"] == "GOOGL"


def test_symbol_match_is_exact_token_not_substring() -> None:
    """A short ticker must not be 'covered' by an unrelated symbol containing
    it as a substring (``T`` should not match ``TSLA``)."""
    records = [_rec("News", age_seconds=10.0, symbols=["TSLA"])]
    out = find_coverage_gaps(records, ["T"], window_seconds=86400.0, now=NOW)
    assert out["gaps"] == ["T"]
    assert out["covered"] == []


# ── empty / malformed inputs ──────────────────────────────────────────────────


def test_empty_watch_terms_returns_empty_lists() -> None:
    out = find_coverage_gaps([_rec("AAPL up", age_seconds=10.0)], [], now=NOW)
    assert out["covered"] == []
    assert out["gaps"] == []


def test_empty_records_all_terms_are_gaps() -> None:
    out = find_coverage_gaps([], ["AAPL", "MSFT"], now=NOW)
    assert out["covered"] == []
    assert out["gaps"] == ["AAPL", "MSFT"]


def test_malformed_records_do_not_raise() -> None:
    records = [
        None,                                   # not a dict
        {"title": None, "symbols": None},       # null fields
        {"symbols": [None, 123, "AAPL"]},       # mixed junk list, no ts → ignored
        _rec("AAPL real", age_seconds=5.0),     # the one real hit
        {"title": "MSFT", "published_ts": "not-a-date"},  # junk ts → ignored
    ]
    out = find_coverage_gaps(records, ["AAPL", "MSFT"], window_seconds=86400.0, now=NOW)
    assert [c["term"] for c in out["covered"]] == ["AAPL"]
    assert out["gaps"] == ["MSFT"]


def test_duplicate_watch_terms_collapsed_case_insensitively() -> None:
    out = find_coverage_gaps([], ["AAPL", "aapl", "Aapl"], now=NOW)
    assert out["gaps"] == ["AAPL"]  # first-seen casing kept, dupes dropped


def test_covered_sorted_freshest_first() -> None:
    records = [
        _rec("MSFT moves", age_seconds=300.0),
        _rec("AAPL moves", age_seconds=50.0),
    ]
    out = find_coverage_gaps(records, ["MSFT", "AAPL"], window_seconds=86400.0, now=NOW)
    ages = [c["last_seen_age_seconds"] for c in out["covered"]]
    assert ages == sorted(ages)  # freshest (smallest age) first
    assert out["covered"][0]["term"] == "AAPL"


def test_naive_now_treated_as_utc() -> None:
    naive_now = datetime(2026, 5, 29, 12, 0, 0)  # no tzinfo
    rec = {"title": "AAPL", "published_ts": NOW.isoformat()}
    out = find_coverage_gaps([rec], ["AAPL"], window_seconds=3600.0, now=naive_now)
    assert out["covered"][0]["last_seen_age_seconds"] == 0.0


def test_datetime_timestamp_objects_accepted() -> None:
    rec = {"title": "AAPL", "published_ts": NOW - timedelta(seconds=90.0)}
    out = find_coverage_gaps([rec], ["AAPL"], window_seconds=3600.0, now=NOW)
    assert out["covered"][0]["last_seen_age_seconds"] == 90.0


def test_coerce_dt_edge_cases() -> None:
    # 1. string that strips to empty
    assert find_coverage_gaps([{"title": "AAPL", "published_ts": "   "}], ["AAPL"], now=NOW)["covered"] == []
    # 2. string ending with "z" or "Z"
    rec_z = {"title": "AAPL", "published_ts": "2026-05-29T12:00:00z"}
    rec_Z = {"title": "AAPL", "published_ts": "2026-05-29T12:00:00Z"}
    assert len(find_coverage_gaps([rec_z], ["AAPL"], now=NOW)["covered"]) == 1
    assert len(find_coverage_gaps([rec_Z], ["AAPL"], now=NOW)["covered"]) == 1
    # 3. non-None/str/datetime value (e.g. integer)
    rec_int = {"title": "AAPL", "published_ts": 12345}
    assert find_coverage_gaps([rec_int], ["AAPL"], now=NOW)["covered"] == []


def test_symbols_field_matching_edge_cases() -> None:
    # 1. symbols field is a single string
    rec_str = {"title": "Nothing", "published_ts": NOW.isoformat(), "symbols": "  AAPL  "}
    out = find_coverage_gaps([rec_str], ["AAPL"], now=NOW)
    assert len(out["covered"]) == 1
    assert out["covered"][0]["term"] == "AAPL"

    # 1b. symbols field is a single string but empty/whitespace
    rec_str_empty = {"title": "Nothing", "published_ts": NOW.isoformat(), "symbols": "   "}
    out_empty = find_coverage_gaps([rec_str_empty], ["AAPL"], now=NOW)
    assert out_empty["covered"] == []

    # 2. list/tuple/set with non-string and empty string items
    rec_junk = {"title": "Nothing", "published_ts": NOW.isoformat(), "symbols": [123, None, "", "   ", "AAPL"]}
    out = find_coverage_gaps([rec_junk], ["AAPL"], now=NOW)
    assert len(out["covered"]) == 1

    # 3. tuple and set symbol types
    rec_tuple = {"title": "Nothing", "published_ts": NOW.isoformat(), "symbols": ("AAPL",)}
    assert len(find_coverage_gaps([rec_tuple], ["AAPL"], now=NOW)["covered"]) == 1

    rec_set = {"title": "Nothing", "published_ts": NOW.isoformat(), "symbols": {"AAPL"}}
    assert len(find_coverage_gaps([rec_set], ["AAPL"], now=NOW)["covered"]) == 1


def test_now_parameter_omitted_defaults_to_utcnow() -> None:
    # Omitted now parameter - defaults to UTC now
    out = find_coverage_gaps([], ["AAPL"])
    assert "generated_at" in out
    assert isinstance(out["generated_at"], str)


def test_window_seconds_invalid_coercion() -> None:
    # window_seconds is a value that cannot be coerced to float
    out = find_coverage_gaps([_rec("AAPL", age_seconds=50.0)], ["AAPL"], window_seconds="not-a-float", now=NOW)
    assert len(out["covered"]) == 1


def test_watch_terms_invalid_types_and_empty() -> None:
    # Watch terms list contains invalid types (None, int) and empty strings
    out = find_coverage_gaps([_rec("AAPL", age_seconds=10.0)], ["AAPL", 123, None, "", "   "], now=NOW)
    assert out["gaps"] == []
    assert len(out["covered"]) == 1
    assert out["covered"][0]["term"] == "AAPL"


def test_record_with_no_searchable_content() -> None:
    # Record has timestamp but no text fields and no symbol fields
    rec = {"published_ts": NOW.isoformat()}
    out = find_coverage_gaps([rec], ["AAPL"], now=NOW)
    assert out["covered"] == []
    assert out["gaps"] == ["AAPL"]

