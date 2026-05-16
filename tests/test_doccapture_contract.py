"""DocCapture compatibility tests.

These tests load a real Awareness JSONL row (if present) and assert fusion_stack
can validate it. They also check the AwarenessCaptureView accepts the real-world
field set without dropping data.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fusion_stack.awareness_reader import parse_capture_line
from fusion_stack.schemas import AwarenessCaptureView


REQUIRED_KEYS = (
    "doc_id", "capture_id", "source_type", "source_name", "discovery_channel",
    "ingest_version", "fetch_ts", "observed_ts", "text", "content_hash",
)


@pytest.mark.regression
def test_view_accepts_minimal_capture() -> None:
    cap = AwarenessCaptureView(capture_id="x", doc_id="y", text="hello world")
    assert cap.capture_id == "x"
    assert cap.text == "hello world"


@pytest.mark.regression
def test_view_validates_real_jsonl(real_jsonl_root: Path | None) -> None:
    if real_jsonl_root is None:
        pytest.skip("no Awareness JSONL captures available on this machine")
    files = list(real_jsonl_root.glob("**/*.jsonl"))
    assert files, "expected at least one finalized JSONL"
    seen = 0
    parsed = 0
    for path in files[:5]:
        with path.open() as fh:
            for line in fh:
                seen += 1
                cap = parse_capture_line(line)
                if cap is None:
                    continue
                # Confirm key fields stayed put
                raw = json.loads(line)
                for k in REQUIRED_KEYS:
                    assert k in raw, f"awareness key missing: {k}"
                assert cap.capture_id == raw["capture_id"]
                assert cap.doc_id == raw["doc_id"]
                parsed += 1
                if parsed >= 10:
                    break
        if parsed >= 10:
            break
    assert seen > 0
    assert parsed > 0, "fusion_stack failed to parse any real Awareness rows"


@pytest.mark.regression
def test_view_is_not_strict_about_extras() -> None:
    """If Awareness adds new optional fields, fusion_stack should keep working."""
    raw = {
        "capture_id": "c",
        "doc_id": "d",
        "text": "hello",
        "future_field_added_later": True,  # synthetic forward-compat
    }
    cap = AwarenessCaptureView.model_validate(raw)
    assert cap.text == "hello"
