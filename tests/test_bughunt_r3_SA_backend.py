"""Round-3 bug-hunt regression tests — file group SA-backend.

Each test FAILS before its corresponding fix and PASSES after. One test per
confirmed finding; see the docstring on each for the finding it pins.
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any

import pytest

from catchem import bootstrap as bootstrap_mod
from catchem.awareness_reader import iter_captures
from catchem.backtest import run_backtest
from catchem.golden import load_extended
from catchem.quant.intensity import compute_by_scope, compute_overall
from catchem.redaction import _classify_guard_error


# ── Finding 1: golden.load_extended crashes on explicit JSON null ────────────
def test_load_extended_tolerates_explicit_null_list_fields(tmp_path: Path) -> None:
    """`"expected_asset_classes": null` passes validate_golden_row by design, so
    `tuple(None)` must NOT raise — the row should load with empty tuples."""
    path = tmp_path / "extended.jsonl"
    row = {
        "capture_id": "g1",
        "title": "Fed hikes",
        "text": "The Federal Reserve raised rates.",
        "expected_finance_relevant": True,
        "expected_asset_classes": None,
        "expected_reason_codes": None,
        "expected_symbols": None,
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    # Before the fix this raised TypeError: 'NoneType' object is not iterable
    # under strict=True, aborting the whole benchmark run.
    items = load_extended(path, strict=True)
    assert len(items) == 1
    it = items[0]
    assert it.expected_asset_classes == ()
    assert it.expected_reason_codes == ()
    assert it.expected_symbols == ()


# ── Finding 2: run_backtest propagates NaN into summary metrics ──────────────
class _FakeStorage:
    def __init__(self, pairs: list[tuple[dict[str, Any], dict[str, Any]]]) -> None:
        self._pairs = pairs

    def reviews_with_pair(self, a: str, b: str, limit: int = 500):  # noqa: ANN001
        return self._pairs[:limit]


class _FakeSupervisor:
    def __init__(self, storage: _FakeStorage) -> None:
        self.storage = storage


def test_run_backtest_skips_non_finite_scores() -> None:
    """A NaN finance_relevance_score must not poison mean/max error into NaN
    (which FastAPI would serialize as a bare `NaN` token, breaking the UI)."""
    pairs = [
        # Healthy pair — kept.
        (
            {"capture_id": "ok", "payload": {"finance_relevance_score": 0.4}},
            {"capture_id": "ok", "payload": {"finance_relevance_score": 0.6}},
        ),
        # Poisoned pair — NaN predicted score, must be dropped.
        (
            {"capture_id": "bad", "payload": {"finance_relevance_score": float("nan")}},
            {"capture_id": "bad", "payload": {"finance_relevance_score": 0.5}},
        ),
    ]
    run = run_backtest(_FakeSupervisor(_FakeStorage(pairs)))
    assert run.items_evaluated == 1  # only the healthy pair survives
    for key in ("mean_abs_error", "mean_signed_error", "max_abs_error"):
        val = run.summary[key]
        assert math.isfinite(val), f"{key} is non-finite: {val!r}"
    # And the whole envelope must be strict-JSON serializable (no NaN token).
    json.dumps(run.summary, allow_nan=False)


# ── Finding 3: iter_captures loses data on in-place truncation ───────────────
def test_iter_captures_resets_offset_on_truncated_file(tmp_path: Path) -> None:
    """A file rewritten in place to be SHORTER than the saved offset must be
    reprocessed from line 1, not skipped into oblivion."""
    path = tmp_path / "captures.jsonl"

    def _valid_row(cid: str) -> dict[str, Any]:
        return {
            "capture_id": cid,
            "doc_id": f"doc-{cid}",
            "title": f"Title {cid}",
            "text": "The Federal Reserve raised interest rates today.",
            "domain": "reuters.com",
            "url": f"https://reuters.com/{cid}",
            "source_type": "rss",
            "discovery_channel": "rss:reuters.com",
            "language": "en",
            "fetch_ts": "2026-05-29T00:00:00+00:00",
            "observed_ts": "2026-05-29T00:00:00+00:00",
            "content_hash": f"hash-{cid}",
            "robots_decision": "not_applicable",
        }

    # First file with 5 valid lines, consumed to offset 5.
    path.write_text(
        "\n".join(json.dumps(_valid_row(f"a{i}")) for i in range(5)) + "\n",
        encoding="utf-8",
    )
    consumed = list(iter_captures(path, start_offset=0))
    assert len(consumed) == 5

    # File truncated/rewritten in place with only 3 NEW captures.
    path.write_text(
        "\n".join(json.dumps(_valid_row(f"b{i}")) for i in range(3)) + "\n",
        encoding="utf-8",
    )
    # Resume with the stale offset 5 — before the fix this yielded [] (data loss).
    resumed = [cap.capture_id for _, cap in iter_captures(path, start_offset=5)]
    assert resumed == ["b0", "b1", "b2"]


# ── Finding 4: bootstrap silently skips guard verifier in wheel layout ───────
def test_bootstrap_guard_skip_is_fail_stop_in_production_safe(
    tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the verifier can't be located (e.g. packaged wheel) the guard status
    is 'skip'; in production_safe mode bootstrap must fail-stop rather than
    silently proceeding past the quarantine gate."""
    # Force the 'verifier_missing' skip regardless of repo layout.
    monkeypatch.setattr(bootstrap_mod, "_verifier_script_path", lambda: None)
    summary = bootstrap_mod.bootstrap(skip_warm=True)
    assert summary["newsimpact_guard"]["status"] == "skip"
    # Default mode is production_safe (conftest doesn't override it), so the
    # warm/kaggle steps must NOT have run — bootstrap returned early.
    assert "models_warmed" not in summary
    assert "kaggle_attempted" not in summary


def test_verifier_script_path_resolves_in_source_layout() -> None:
    """In the source checkout the verifier must be found (parents[2]/scripts)."""
    found = bootstrap_mod._verifier_script_path()
    assert found is not None and found.name == "verify_newsimpact_guard.py"


# ── Finding 5: bootstrap jsonl count disagrees with reader discovery ─────────
def test_count_finalized_jsonl_matches_reader_discovery(tmp_path: Path) -> None:
    """When Awareness writes JSONL directly under data_dir (no jsonl/ subdir),
    the bootstrap count must reflect what replay actually consumes (>0)."""
    data_dir = tmp_path / "aw"
    data_dir.mkdir(parents=True)
    (data_dir / "a.jsonl").write_text("{}\n", encoding="utf-8")
    count = bootstrap_mod._count_finalized_jsonl(data_dir)
    assert count == 1


# ── Finding 6: guard-error classifier hijacked by path contents ──────────────
def test_classify_guard_error_ignores_path_substrings() -> None:
    """An OSError whose message embeds the default newsimpact path
    '/tmp/merged_news-missing' must NOT be misclassified as missing when the
    actual fault is a read/parse error."""
    msg = "unreadable governance index: [Errno 13] Permission denied: '/tmp/merged_news-missing/models/governance_index/governance_index.json'"
    assert _classify_guard_error(msg) == "malformed_governance_index"
    # A genuinely-missing index still classifies as missing.
    assert (
        _classify_guard_error("missing governance index at /tmp/merged_news-missing/x.json")
        == "missing_governance_index"
    )


# ── Finding 7: ws_push storage-check exception permanently drops URL ──────────
class _FlakyStorage:
    """get_record raises once, then succeeds (returns None = not seen)."""

    def __init__(self) -> None:
        self.calls = 0

    def get_record(self, cap_id: str):  # noqa: ANN001
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient sqlite lock on dedup probe")
        return None


class _WsFakeSupervisor:
    def __init__(self) -> None:
        self.storage = _FlakyStorage()
        self.ingested: list[str] = []


def test_ws_storage_check_failure_does_not_permanently_drop_url(
    tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient storage error on the dedup probe must roll back the
    speculative _seen.add so the next sighting of the same URL is re-ingestable."""
    from catchem import ws_push
    from catchem.ws_push import ParsedItem, WebSocketNewsChannel, WsSourceSpec, _SourceState

    sup = _WsFakeSupervisor()
    chan = WebSocketNewsChannel(supervisor=sup, settings=tmp_settings, sources=[])

    item = ParsedItem(
        title="Apple earnings beat",
        text="Apple reported earnings above estimates.",
        url="https://example.com/apple-earnings",
        domain="example.com",
        published_ts=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )

    # Register a stub parser that always returns our item.
    monkeypatch.setitem(ws_push._FRAME_PARSERS, "stub_r3", lambda raw, fd="": item)
    spec = WsSourceSpec(name="t", url="ws://t", parser="stub_r3")
    st = _SourceState(name="t", url="ws://t")

    captured_ingests: list[ParsedItem] = []
    monkeypatch.setattr(chan, "_ingest_one", lambda it: captured_ingests.append(it))

    # First frame: storage probe raises → must discard canon (no ingest).
    asyncio.run(chan._handle_frame(spec, st, "frame1"))
    assert captured_ingests == []

    # Second frame, SAME url: before the fix this short-circuited on _seen and
    # was silently dropped. After the fix it is re-probed (calls==2 → None) and
    # ingested.
    asyncio.run(chan._handle_frame(spec, st, "frame1"))
    assert len(captured_ingests) == 1
    assert captured_ingests[0].url == item.url


# ── Finding 8: intensity top_records leaks raw NaN/Inf ───────────────────────
def _rec(cid: str, rel: Any, sent: Any, asset: str = "equities") -> dict[str, Any]:
    return {
        "capture_id": cid,
        "title": cid,
        "finance_relevance_score": rel,
        "sentiment_score": sent,
        "sentiment_label": "neutral",
        "asset_classes": [asset],
    }


def test_intensity_top_records_scrub_non_finite() -> None:
    """A stored NaN/Inf score or sentiment_score must be scrubbed to None in the
    drill-down rows so the JSON response stays strict-JSON serializable."""
    records = [
        _rec("nan", float("nan"), float("inf")),
        _rec("ok", 0.8, -0.5),
    ]
    overall = compute_overall(records)
    by_scope = compute_by_scope(records, scope_key="asset_classes")

    rows = list(overall.top_records)
    for b in by_scope:
        rows.extend(b.top_records)
    assert rows, "expected drill-down rows"

    for row in rows:
        # Strict-JSON serialization would raise on a raw NaN/Inf.
        json.dumps({"score": row["score"], "sentiment_score": row["sentiment_score"]}, allow_nan=False)
        for k in ("score", "sentiment_score"):
            v = row[k]
            assert v is None or math.isfinite(v), f"{k}={v!r} leaked non-finite"

    # The healthy record must keep its real values (None-preserving guard).
    ok_rows = [r for r in rows if r["capture_id"] == "ok"]
    assert ok_rows and ok_rows[0]["score"] == 0.8 and ok_rows[0]["sentiment_score"] == -0.5
