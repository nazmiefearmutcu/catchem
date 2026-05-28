"""Branch-coverage for catchem.redaction._classify_guard_error.

The classifier maps free-form error strings to a closed set of error codes.
test_guard_redaction_in_production already covers the "missing" branch via
safe_guard_view. This file pins the remaining branches explicitly so a
silent typo in the substring matchers can't ship.
"""

from __future__ import annotations

from catchem.redaction import (
    _classify_guard_error,
    redact_record_for_mode,
    redact_records_for_mode,
    safe_guard_view,
)


def test_classify_unknown_when_empty_message() -> None:
    assert _classify_guard_error("") == "unknown"
    assert _classify_guard_error(None) == "unknown"


def test_classify_missing_branch() -> None:
    assert _classify_guard_error("governance file missing") == "missing_governance_index"


def test_classify_malformed_via_unreadable() -> None:
    assert _classify_guard_error("file is unreadable") == "malformed_governance_index"


def test_classify_malformed_via_parse() -> None:
    # The 'parse' keyword should also map to malformed_governance_index
    assert _classify_guard_error("parse error at line 3") == "malformed_governance_index"


def test_classify_release_gate_flipped() -> None:
    assert (
        _classify_guard_error("release_gate_passed_unexpectedly_true")
        == "release_gate_flipped"
    )


def test_classify_empty_governance_index() -> None:
    assert _classify_guard_error("no candidates available") == "empty_governance_index"


def test_classify_unspecified_for_unrecognised_message() -> None:
    assert (
        _classify_guard_error("some random failure not matching keywords")
        == "unspecified_guard_error"
    )


def test_safe_guard_view_strips_error_when_ok_true() -> None:
    # ok=True → no error_code added even if upstream included a leaky error string
    snap = {"ok": True, "error": "/Users/foo/something.json missing"}
    out = safe_guard_view(snap)
    assert out.get("ok") is True
    assert "error_code" not in out
    assert "error" not in out


def test_redact_record_for_mode_none_input_returns_none() -> None:
    # Covers the `if record is None` early-return branch.
    assert redact_record_for_mode(None, production_safe=True) is None
    assert redact_record_for_mode(None, production_safe=False) is None


def test_redact_records_for_mode_in_research_mode_is_passthrough() -> None:
    # Non-prod-safe mode preserves diagnostic fields untouched.
    recs = [{"capture_id": "a", "diagnostic_multimodal_enabled": True}]
    out = redact_records_for_mode(recs, production_safe=False)
    assert out[0]["diagnostic_multimodal_enabled"] is True
