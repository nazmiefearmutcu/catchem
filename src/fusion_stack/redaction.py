"""Server-side redaction for production-safe mode.

Defense in depth. The pipeline already emits `diagnostic_multimodal_*` as
`False`/`None` in production_safe — but if a future bug or a tampered storage
row carries truthy diagnostic data, the API surface MUST still scrub it before
it reaches a client.

The rules are simple and intentionally NOT toggleable from the client:

  * In production_safe mode:
      diagnostic_multimodal_enabled  → False
      diagnostic_multimodal_result   → None

  * In any non-production-safe mode the values pass through unchanged so the
    research-diagnostic UI can render the read-only stamp.

This module also exposes a tiny `safe_guard_view` that strips local
filesystem paths and other internals from the guard snapshot before it
crosses the wire.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping


# Fields we ALWAYS want present on a record payload but force to safe values in
# production_safe. Listed explicitly so reviewers can audit the closed set.
PRODUCTION_SAFE_DIAGNOSTIC_FIELDS = (
    "diagnostic_multimodal_enabled",
    "diagnostic_multimodal_result",
)


def redact_record_for_mode(record: Mapping[str, Any] | None, *, production_safe: bool) -> dict[str, Any] | None:
    """Return a copy of `record` with diagnostic fields scrubbed in prod-safe mode.

    Pure function. Never mutates the input. Returns `None` if input is None.
    """
    if record is None:
        return None
    out = dict(record)
    if production_safe:
        out["diagnostic_multimodal_enabled"] = False
        out["diagnostic_multimodal_result"] = None
    return out


def redact_records_for_mode(records: Iterable[Mapping[str, Any]] | None, *, production_safe: bool) -> list[dict[str, Any]]:
    if not records:
        return []
    return [redact_record_for_mode(r, production_safe=production_safe) or {} for r in records]


# Keys we are willing to expose from a GuardSnapshot via the API surface.
# Notably ABSENT: governance_index_path (filesystem path), any error strings
# that may contain absolute paths.
SAFE_GUARD_KEYS = (
    "ok",
    "release_gate_passed",
    "quarantine_state",
    "fusion_verdict_class",
    "safe_to_publish",
    "safe_to_promote",
    "governance_index_sha256",
)


def safe_guard_view(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Project a guard snapshot to only the keys safe for client consumption.

    The `error` key, if present, is collapsed to a short token so we never
    leak a filesystem path.
    """
    out: dict[str, Any] = {k: snapshot.get(k) for k in SAFE_GUARD_KEYS if k in snapshot}
    if not snapshot.get("ok", True):
        out["ok"] = False
        # Don't expose raw error strings (they often include local paths).
        err = snapshot.get("error", "")
        out["error_code"] = _classify_guard_error(err)
    return out


def _classify_guard_error(msg: str | None) -> str:
    if not msg:
        return "unknown"
    msg = str(msg).lower()
    if "missing" in msg:
        return "missing_governance_index"
    if "unreadable" in msg or "parse" in msg:
        return "malformed_governance_index"
    if "release_gate_passed_unexpectedly_true" in msg:
        return "release_gate_flipped"
    if "no candidates" in msg:
        return "empty_governance_index"
    return "unspecified_guard_error"


__all__ = [
    "PRODUCTION_SAFE_DIAGNOSTIC_FIELDS",
    "SAFE_GUARD_KEYS",
    "redact_record_for_mode",
    "redact_records_for_mode",
    "safe_guard_view",
]
