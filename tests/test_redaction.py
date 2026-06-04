"""Dedicated unit tests for catchem.redaction.

This module is the defense-in-depth scrub layer for production-safe mode.
Two responsibilities, both safety-critical:

  * `redact_record_for_mode` / `redact_records_for_mode` — force diagnostic
    fields to safe values so research-mode artifacts (or tampered DB rows)
    never leak through prod-safe API routes.
  * `safe_guard_view` / `_classify_guard_error` — project a guard snapshot to a
    whitelisted key set and collapse error strings (which often embed absolute
    filesystem paths / PII) into a closed set of opaque error codes.

Existing files exercise these via the API surface; this file pins the pure
contract directly (clean pass-through untouched, secrets/paths stripped,
no-mutation, edge cases: empty, None, already-redacted, partial matches).
"""

from __future__ import annotations

import pytest

from catchem.redaction import (
    PRODUCTION_SAFE_DIAGNOSTIC_FIELDS,
    SAFE_GUARD_KEYS,
    _classify_guard_error,
    redact_record_for_mode,
    redact_records_for_mode,
    safe_guard_view,
)

# ── exported constants ──────────────────────────────────────────────────────


def test_production_safe_diagnostic_fields_closed_set() -> None:
    # Reviewers audit this exact set; lock it.
    assert PRODUCTION_SAFE_DIAGNOSTIC_FIELDS == (
        "diagnostic_multimodal_enabled",
        "diagnostic_multimodal_result",
    )


def test_safe_guard_keys_never_include_filesystem_paths() -> None:
    # governance_index_PATH must never be whitelisted (only the sha256 hash is).
    assert "governance_index_path" not in SAFE_GUARD_KEYS
    assert "error" not in SAFE_GUARD_KEYS
    assert "governance_index_sha256" in SAFE_GUARD_KEYS


# ── redact_record_for_mode: prod-safe scrubbing ─────────────────────────────


def test_prod_safe_forces_diagnostic_enabled_false_and_result_none() -> None:
    rec = {
        "capture_id": "x",
        "diagnostic_multimodal_enabled": True,
        "diagnostic_multimodal_result": {"label": "secret-leak"},
    }
    out = redact_record_for_mode(rec, production_safe=True)
    assert out is not None
    assert out["diagnostic_multimodal_enabled"] is False
    assert out["diagnostic_multimodal_result"] is None
    # Non-diagnostic data is preserved verbatim.
    assert out["capture_id"] == "x"


def test_prod_safe_injects_safe_fields_even_if_absent() -> None:
    # A record that never carried the diagnostic keys still gets them pinned
    # so the wire shape is uniform.
    out = redact_record_for_mode({"capture_id": "y"}, production_safe=True)
    assert out is not None
    assert out["diagnostic_multimodal_enabled"] is False
    assert out["diagnostic_multimodal_result"] is None


def test_already_redacted_record_stays_redacted_and_idempotent() -> None:
    rec = {
        "capture_id": "z",
        "diagnostic_multimodal_enabled": False,
        "diagnostic_multimodal_result": None,
    }
    once = redact_record_for_mode(rec, production_safe=True)
    assert once is not None
    twice = redact_record_for_mode(once, production_safe=True)
    assert twice == once
    assert twice is not None
    assert twice["diagnostic_multimodal_enabled"] is False


# ── redact_record_for_mode: non-prod-safe pass-through ──────────────────────


def test_research_mode_passes_diagnostic_through_unchanged() -> None:
    rec = {
        "capture_id": "x",
        "diagnostic_multimodal_enabled": True,
        "diagnostic_multimodal_result": {"label": "ok-in-research"},
    }
    out = redact_record_for_mode(rec, production_safe=False)
    assert out is not None
    assert out["diagnostic_multimodal_enabled"] is True
    assert out["diagnostic_multimodal_result"] == {"label": "ok-in-research"}


def test_none_input_returns_none_in_both_modes() -> None:
    assert redact_record_for_mode(None, production_safe=True) is None
    assert redact_record_for_mode(None, production_safe=False) is None


# ── purity: never mutate the caller's dict ──────────────────────────────────


def test_does_not_mutate_input_in_prod_safe() -> None:
    rec = {
        "capture_id": "x",
        "diagnostic_multimodal_enabled": True,
        "diagnostic_multimodal_result": {"label": "leak"},
    }
    out = redact_record_for_mode(rec, production_safe=True)
    # Caller's object is untouched.
    assert rec["diagnostic_multimodal_enabled"] is True
    assert rec["diagnostic_multimodal_result"] == {"label": "leak"}
    # The returned copy is a different object.
    assert out is not rec


def test_does_not_mutate_input_in_research_mode() -> None:
    rec = {"capture_id": "x", "diagnostic_multimodal_enabled": True}
    out = redact_record_for_mode(rec, production_safe=False)
    assert out is not rec
    assert out == {"capture_id": "x", "diagnostic_multimodal_enabled": True}


# ── redact_records_for_mode (list variant) ──────────────────────────────────


def test_records_list_scrubs_every_element_in_prod_safe() -> None:
    recs = [
        {"capture_id": "a", "diagnostic_multimodal_enabled": True,
         "diagnostic_multimodal_result": {"x": 1}},
        {"capture_id": "b", "diagnostic_multimodal_enabled": True,
         "diagnostic_multimodal_result": {"y": 2}},
    ]
    out = redact_records_for_mode(recs, production_safe=True)
    assert len(out) == 2
    for item in out:
        assert item["diagnostic_multimodal_enabled"] is False
        assert item["diagnostic_multimodal_result"] is None
    # Capture ids preserved and ordered.
    assert [r["capture_id"] for r in out] == ["a", "b"]


def test_records_list_passthrough_in_research_mode() -> None:
    recs = [{"capture_id": "a", "diagnostic_multimodal_enabled": True}]
    out = redact_records_for_mode(recs, production_safe=False)
    assert out[0]["diagnostic_multimodal_enabled"] is True


@pytest.mark.parametrize("empty", [None, [], ()])
def test_records_list_handles_empty_and_none(empty: object) -> None:
    assert redact_records_for_mode(empty, production_safe=True) == []  # type: ignore[arg-type]
    assert redact_records_for_mode(empty, production_safe=False) == []  # type: ignore[arg-type]


def test_records_list_does_not_mutate_input() -> None:
    recs = [{"capture_id": "a", "diagnostic_multimodal_enabled": True}]
    redact_records_for_mode(recs, production_safe=True)
    assert recs[0]["diagnostic_multimodal_enabled"] is True


# ── safe_guard_view: whitelist projection ───────────────────────────────────


def test_safe_guard_view_keeps_only_whitelisted_keys() -> None:
    snap = {
        "ok": True,
        "release_gate_passed": False,
        "quarantine_state": "QUARANTINED",
        "fusion_verdict_class": "FUSION_REGRESSIVE",
        "safe_to_publish": False,
        "safe_to_promote": False,
        "governance_index_sha256": "deadbeef",
        # Sensitive — must be dropped:
        "governance_index_path": "/Users/nazmi/secret/governance_index.json",
        "internal_secret": "shhh",
        "api_key": "sk-live-1234567890",
    }
    out = safe_guard_view(snap)
    for k in SAFE_GUARD_KEYS:
        assert k in out, f"whitelisted key dropped: {k}"
    assert "governance_index_path" not in out
    assert "internal_secret" not in out
    assert "api_key" not in out
    # Full-object string scan: no path / secret leakage anywhere.
    blob = str(out)
    assert "/Users/" not in blob
    assert "sk-live" not in blob
    assert "shhh" not in blob


def test_safe_guard_view_omits_keys_not_present_in_snapshot() -> None:
    # Only `ok` provided → only `ok` projected (no None-padding of the rest).
    out = safe_guard_view({"ok": True})
    assert out == {"ok": True}


def test_safe_guard_view_preserves_none_values_for_present_keys() -> None:
    snap = {"ok": True, "release_gate_passed": None, "quarantine_state": None}
    out = safe_guard_view(snap)
    assert out["release_gate_passed"] is None
    assert out["quarantine_state"] is None


# ── safe_guard_view: error handling (ok flag) ───────────────────────────────


def test_ok_true_means_no_error_code_added() -> None:
    # Even if a leaky error string is present, ok=True suppresses error_code.
    snap = {"ok": True, "error": "/Users/foo/governance_index.json missing"}
    out = safe_guard_view(snap)
    assert out["ok"] is True
    assert "error_code" not in out
    assert "error" not in out
    assert "/Users" not in str(out)


def test_missing_ok_defaults_truthy_so_no_error_code() -> None:
    # `snapshot.get("ok", True)` — absence of ok is treated as healthy.
    out = safe_guard_view({"quarantine_state": "OK"})
    assert "error_code" not in out


def test_ok_false_collapses_error_to_code_and_drops_raw_string() -> None:
    snap = {"ok": False, "error": "/Users/secret/path/governance_index.json missing"}
    out = safe_guard_view(snap)
    assert out["ok"] is False
    assert out["error_code"] == "missing_governance_index"
    assert "error" not in out
    assert "/Users" not in str(out)


def test_ok_false_with_no_error_string_yields_unknown_code() -> None:
    out = safe_guard_view({"ok": False})
    assert out["ok"] is False
    assert out["error_code"] == "unknown"


# ── _classify_guard_error: closed mapping, all branches ──────────────────────


@pytest.mark.parametrize("msg", ["", None])
def test_classify_unknown_for_empty(msg: str | None) -> None:
    assert _classify_guard_error(msg) == "unknown"


def test_classify_missing_governance_index() -> None:
    assert _classify_guard_error("governance file is MISSING") == "missing_governance_index"


def test_classify_malformed_via_unreadable() -> None:
    assert _classify_guard_error("index file unreadable") == "malformed_governance_index"


def test_classify_malformed_via_parse() -> None:
    assert _classify_guard_error("JSON parse failure at byte 12") == "malformed_governance_index"


def test_classify_release_gate_flipped() -> None:
    assert (
        _classify_guard_error("release_gate_passed_unexpectedly_true")
        == "release_gate_flipped"
    )


def test_classify_empty_governance_index() -> None:
    assert _classify_guard_error("found no candidates in index") == "empty_governance_index"


def test_classify_unspecified_for_unrecognised() -> None:
    assert (
        _classify_guard_error("disk on fire and nothing matches")
        == "unspecified_guard_error"
    )


def test_classify_is_case_insensitive() -> None:
    assert _classify_guard_error("MISSING") == "missing_governance_index"
    assert _classify_guard_error("Unreadable") == "malformed_governance_index"


def test_classify_coerces_non_string_input() -> None:
    # Defensive: a non-str truthy value is str()-ified, not crashed on.
    assert _classify_guard_error(12345) == "unspecified_guard_error"  # type: ignore[arg-type]


def test_classify_first_matching_branch_wins() -> None:
    # "missing" is checked before "parse"/"unreadable"; a string containing
    # both resolves to the earlier branch deterministically.
    assert _classify_guard_error("missing and unreadable") == "missing_governance_index"
