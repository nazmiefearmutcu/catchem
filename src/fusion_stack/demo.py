"""Paste-news → AwarenessCaptureView → JSONL → replay → record.

Goal: let a reviewer paste a single news article and see the full pipeline
output (relevance, asset class, reason code, symbols, evidence) in one
command, without having to spin up Awareness or hand-craft JSONL.

This deliberately re-uses the same path the real Awareness JSONL takes:

  1. Build an `AwarenessCaptureView` with a deterministic capture_id so
     re-running on identical text is idempotent (no DB churn).
  2. Append it to `data/jsonl/captures/Y/M/D/demo-<ts>.jsonl` — the same
     layout the real Awareness writer uses.
  3. Run `Supervisor.run_replay()` against the demo-only data dir so it
     processes the new row without colliding with any real captures.
  4. Read the resulting `FinancialImpactRecord` from storage and pretty-
     print it.

The function returns the record dict so it can also be called from tests
without printing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schemas import AwarenessCaptureView
from .settings import Settings, load_settings, reload_settings
from .supervisor import Supervisor


@dataclass(frozen=True)
class DemoResult:
    capture_id: str
    record: dict[str, Any]
    jsonl_path: Path
    processed: int
    skipped: int


def _deterministic_capture_id(text: str, url: str | None) -> str:
    """Same text + url → same capture_id. Lets the demo be idempotent."""
    h = hashlib.blake2b(((url or "") + "\n" + text).encode("utf-8"), digest_size=16).hexdigest()
    return f"demo-{h}"


def build_capture(
    *,
    title: str,
    text: str,
    domain: str = "demo.local",
    url: str | None = None,
    published_ts: datetime | None = None,
    language: str = "en",
    source_type: str = "rss",
) -> AwarenessCaptureView:
    """Construct an AwarenessCaptureView the same shape the real pipeline ingests."""
    cap_id = _deterministic_capture_id(text, url)
    now = datetime.now(timezone.utc)
    return AwarenessCaptureView(
        capture_id=cap_id,
        doc_id=f"doc-{cap_id}",
        title=title,
        text=text,
        domain=domain,
        url=url or f"https://{domain}/demo",
        canonical_url=url or f"https://{domain}/demo",
        source_type=source_type,
        discovery_channel=f"demo:{domain}",
        language=language,
        fetch_ts=now,
        observed_ts=now,
        published_ts=published_ts or now,
        content_hash=cap_id,
        robots_decision="not_applicable",
    )


def write_jsonl(cap: AwarenessCaptureView, root: Path) -> Path:
    """Write the capture under the Awareness-style JSONL layout."""
    now = datetime.now(timezone.utc)
    day_dir = root / "jsonl" / "captures" / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.day:02d}"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"demo-{int(now.timestamp() * 1000)}.jsonl"
    payload = cap.model_dump(mode="json")
    path.write_text(json.dumps(payload, default=str) + "\n", encoding="utf-8")
    return path


def run_demo(
    *,
    title: str,
    text: str,
    domain: str = "demo.local",
    url: str | None = None,
    published_ts: datetime | None = None,
    settings: Settings | None = None,
) -> DemoResult:
    """End-to-end demo. Returns the processed record (or a synthetic miss).

    The replay reads the demo's awareness_data_dir, which defaults to the
    fusion_stack data dir under a `demo-input/` subfolder so it never touches
    the real Awareness JSONL. To overlay onto the real path, pass a Settings
    whose paths.awareness_data_dir points there.
    """
    if settings is None:
        reload_settings()
        settings = load_settings()

    # Default to a demo-input subdir under the fusion output so we never
    # touch /Users/.../awareness/data on demo runs.
    demo_root = settings.paths.fusion_output_dir / "demo-input"
    demo_root.mkdir(parents=True, exist_ok=True)

    cap = build_capture(
        title=title, text=text, domain=domain, url=url,
        published_ts=published_ts,
    )
    jsonl_path = write_jsonl(cap, demo_root)

    # Point a fresh Supervisor at the demo-input dir for this run only.
    import os
    prev = os.environ.get("FUSION_PATHS__AWARENESS_DATA_DIR")
    os.environ["FUSION_PATHS__AWARENESS_DATA_DIR"] = str(demo_root)
    try:
        reload_settings()
        sup = Supervisor(load_settings())
        try:
            counts = sup.run_replay(max_records=50)
            rec = sup.storage.get_record(cap.capture_id) or {}
        finally:
            sup.close()
    finally:
        if prev is None:
            os.environ.pop("FUSION_PATHS__AWARENESS_DATA_DIR", None)
        else:
            os.environ["FUSION_PATHS__AWARENESS_DATA_DIR"] = prev
        reload_settings()

    return DemoResult(
        capture_id=cap.capture_id,
        record=rec,
        jsonl_path=jsonl_path,
        processed=counts.get("processed", 0),
        skipped=counts.get("skipped", 0),
    )


def render_demo_report(result: DemoResult) -> str:
    """Compact human-readable summary suitable for the CLI."""
    r = result.record
    if not r:
        return (
            f"demo: capture {result.capture_id} processed={result.processed} skipped={result.skipped}\n"
            f"      (no record materialized — check DLQ; jsonl at {result.jsonl_path})\n"
        )
    lines = [
        f"demo capture: {result.capture_id}",
        f"  jsonl       {result.jsonl_path}",
        f"  processed   {result.processed}",
        f"  title       {r.get('title')}",
        f"  domain      {r.get('domain')}",
        f"  relevant    {r.get('is_finance_relevant')}",
        f"  score       {r.get('finance_relevance_score'):.3f}"
            if isinstance(r.get('finance_relevance_score'), (int, float)) else "  score       —",
        f"  asset_cls   {', '.join(r.get('asset_classes', [])) or '—'}",
        f"  reasons     {', '.join(r.get('impact_reason_codes', [])) or '—'}",
        f"  symbols     {', '.join(r.get('candidate_symbols', [])) or '—'}",
        f"  sentiment   {r.get('sentiment_label')} ({r.get('sentiment_score')})",
        f"  evidence    {(r.get('evidence_sentences') or ['—'])[0][:120]}",
        f"  diag        enabled={r.get('diagnostic_multimodal_enabled')}",
        f"  mode        {r.get('processing_mode')}",
        "",
        "  open in UI:",
        f"    http://127.0.0.1:8087/feed/{result.capture_id}",
        f"    curl -s http://127.0.0.1:8087/record/{result.capture_id} | python -m json.tool",
    ]
    return "\n".join(lines) + "\n"


__all__ = ["DemoResult", "build_capture", "write_jsonl", "run_demo", "render_demo_report"]
