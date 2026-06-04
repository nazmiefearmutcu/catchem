"""Coverage for the analyst utility CLI subcommands added under task #118.

Each command is exercised through ``typer.testing.CliRunner`` so the typer-
declared options (Argument vs Option, short flags, validators) are honoured.
Tests build on the ``isolated_env`` autouse fixture in conftest.py — the
SQLite path therefore lives under ``tmp_path/data/db/`` per test.

`ping-deepseek` is intentionally not covered here (network-dependent); a
test would have to either mock httpx (covered by the API-side tests) or
contact the live endpoint.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from catchem.cli import app
from catchem.schemas import (
    FinancialImpactRecord,
    ProcessingMode,
    SentimentLabel,
)
from catchem.settings import load_settings
from catchem.storage import load_storage_from_settings

runner = CliRunner()


def _make_record(
    capture_id: str,
    title: str,
    domain: str,
    *,
    asset_classes: tuple[str, ...] = ("equities",),
    reason_codes: tuple[str, ...] = ("earnings",),
    symbols: tuple[str, ...] = ("AAPL",),
    score: float = 0.85,
) -> FinancialImpactRecord:
    return FinancialImpactRecord(
        capture_id=capture_id,
        doc_id=f"doc-{capture_id}",
        title=title,
        text_excerpt=f"Body of {title}.",
        domain=domain,
        url=f"https://{domain}/{capture_id}",
        language="en",
        is_finance_relevant=True,
        finance_relevance_score=score,
        asset_classes=list(asset_classes),
        impact_reason_codes=list(reason_codes),
        candidate_symbols=list(symbols),
        candidate_entities=[],
        impact_horizons=["short_term"],
        sentiment_label=SentimentLabel.POSITIVE,
        sentiment_score=0.7,
        evidence_sentences=[f"Sentence about {title}."],
        reason_text=f"matched on {','.join(reason_codes)}",
        component_scores={"finance_relevance_score": score},
        diagnostic_multimodal_enabled=False,
        diagnostic_multimodal_result=None,
        processing_mode=ProcessingMode.PRODUCTION_SAFE,
        model_versions={"stub": "v0"},
    )


@pytest.fixture
def seeded_storage(tmp_path: Path):
    """Populate the configured SQLite path with a handful of records.

    The autouse ``isolated_env`` fixture already points
    `CATCHEM_PATHS__CATCHEM_OUTPUT_DIR` at ``tmp_path/data``, so the DB lands
    inside the test's tmpdir. We insert via Storage directly (no Supervisor
    spin-up) to keep the seed deterministic.
    """
    settings = load_settings()
    storage = load_storage_from_settings(settings)
    fixtures = [
        _make_record("c-aapl", "Apple beats earnings expectations", "wsj.com"),
        _make_record(
            "c-msft", "Microsoft cloud growth accelerates", "reuters.com",
            symbols=("MSFT",),
        ),
        _make_record(
            "c-btc", "Bitcoin rallies past 80k on ETF inflows", "coindesk.com",
            asset_classes=("crypto",), reason_codes=("flow",), symbols=("BTC",),
        ),
        _make_record(
            "c-fed", "Fed holds rates steady amid stable inflation", "federalreserve.gov",
            asset_classes=("rates", "macro"), reason_codes=("central_bank",), symbols=(),
            score=0.92,
        ),
    ]
    for rec in fixtures:
        storage.insert_record(rec)
    storage.close()
    return settings, storage


def test_db_info_runs_when_db_exists(seeded_storage) -> None:
    result = runner.invoke(app, ["db-info"])
    assert result.exit_code == 0, result.output
    assert "Schema version:" in result.output
    assert "Records:" in result.output
    assert "4" in result.output  # 4 seeded rows total


def test_db_info_json_emits_machine_shape(seeded_storage) -> None:
    result = runner.invoke(app, ["db-info", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["records_total"] == 4
    assert payload["records_relevant"] == 4
    assert payload["schema_version"] >= 1
    assert Path(payload["path"]).name.endswith(".sqlite3")


def test_db_info_errors_when_db_missing(tmp_path: Path) -> None:
    # autouse fixture points the output dir at tmp_path/data but no Storage
    # has been instantiated yet, so the .sqlite3 file does not exist.
    result = runner.invoke(app, ["db-info"])
    assert result.exit_code == 1
    assert "db not found" in (result.output + (result.stderr or ""))


def test_db_backup_copies_to_target_path(seeded_storage, tmp_path: Path) -> None:
    target = tmp_path / "backups" / "snapshot.sqlite3"
    result = runner.invoke(app, ["db-backup", "--output", str(target)])
    assert result.exit_code == 0, result.output
    assert target.exists(), "backup file was not created"
    assert target.stat().st_size > 0
    assert str(target) in result.output


def test_bench_prints_relevance_metrics() -> None:
    # bench runs the synthetic golden set in-process; uses the stub
    # service so it is fast and offline.
    result = runner.invoke(app, ["bench"])
    assert result.exit_code == 0, result.output
    assert "Relevance P/R/F1:" in result.output
    assert "Dataset:" in result.output


def test_bench_json_emits_full_report() -> None:
    result = runner.invoke(app, ["bench", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "relevance" in payload
    assert {"precision", "recall", "f1"} <= payload["relevance"].keys()
    assert payload.get("n", 0) > 0


def test_search_finds_records_by_title_substring(seeded_storage) -> None:
    result = runner.invoke(app, ["search", "Apple", "--limit", "10"])
    assert result.exit_code == 0, result.output
    assert "Records" in result.output
    assert "Apple beats earnings" in result.output


def test_search_finds_symbols_by_ticker(seeded_storage) -> None:
    result = runner.invoke(app, ["search", "BTC"])
    assert result.exit_code == 0, result.output
    # Symbol section must surface BTC at least once.
    assert "BTC" in result.output


def test_search_json_returns_structured_payload(seeded_storage) -> None:
    result = runner.invoke(app, ["search", "Microsoft", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["query"] == "Microsoft"
    titles = [r.get("title") for r in payload["records"]]
    assert any("Microsoft" in (t or "") for t in titles)


def test_export_csv_writes_header_and_rows(seeded_storage, tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    result = runner.invoke(app, ["export", "csv", "--output", str(target)])
    assert result.exit_code == 0, result.output
    assert target.exists()

    rows = list(csv.DictReader(target.read_text(encoding="utf-8").splitlines()))
    assert rows, "no rows written"
    # header validation: all critical columns present
    sample = rows[0]
    for col in ("capture_id", "title", "domain", "finance_relevance_score",
                "asset_classes", "candidate_symbols"):
        assert col in sample, f"missing column {col} in CSV header"
    assert any(r["capture_id"] == "c-aapl" for r in rows)


def test_export_json_includes_filter_metadata(seeded_storage, tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    result = runner.invoke(
        app,
        ["export", "json", "--symbol", "AAPL", "--output", str(target)],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["filters"]["symbol"] == "AAPL"
    assert payload["count"] >= 1
    assert all("AAPL" in (it.get("candidate_symbols") or []) for it in payload["items"])


def test_export_rejects_unknown_format(seeded_storage) -> None:
    result = runner.invoke(app, ["export", "xml"])
    assert result.exit_code == 1


def test_quant_snapshot_returns_json_payload(seeded_storage) -> None:
    result = runner.invoke(app, ["quant-snapshot", "--limit", "100"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "snapshot" in payload
    assert "generated_at" in payload
    assert payload["limit"] == 100


# ── v57: catchem status ────────────────────────────────────────────────────

def test_status_text_output(seeded_storage) -> None:
    """Default text output is a single human-readable line with key counts."""
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    # Last line is the status line (logs may precede)
    line = result.output.strip().split("\n")[-1]
    assert line.startswith("[catchem v")
    assert "records" in line
    assert "tags" in line
    assert "sidecar:" in line


def test_status_json_output(seeded_storage) -> None:
    """--json emits the parseable envelope with all fields present."""
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    # JSON is the last non-empty line (logs may precede)
    body = result.output.strip().split("\n")[-1]
    payload = json.loads(body)
    for key in ("records", "tags", "schema_version", "last_record_at",
                "sidecar_ok", "sidecar_pid", "generated_at"):
        assert key in payload, f"missing {key} in JSON payload"
    assert isinstance(payload["records"], int)
    assert isinstance(payload["tags"], int)
    assert isinstance(payload["schema_version"], int)
    assert isinstance(payload["sidecar_ok"], bool)


def test_status_reports_sidecar_stopped_when_unreachable(seeded_storage, monkeypatch) -> None:
    """In test env there should be no sidecar — must report 'stopped', not crash.

    v66 audit fix: the previous version of `cli_status` read `settings.api_host`
    (silently always None on top-level Settings), so it never probed a real port.
    The fix uses `settings.api.host/port`, which in dev CI happens to match the
    operator's running sidecar. Mock httpx.get so the test stays deterministic
    regardless of what listens on 8087 during the run.
    """
    import httpx
    def _raise(*_a, **_kw):
        raise httpx.ConnectError("no sidecar in test env")
    monkeypatch.setattr(httpx, "get", _raise)

    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    body = result.output.strip().split("\n")[-1]
    payload = json.loads(body)
    assert payload["sidecar_ok"] is False
    assert payload["sidecar_pid"] is None


# ── v58: catchem top-recent ─────────────────────────────────────────────────

def test_top_recent_filters_by_min_score(seeded_storage) -> None:
    """--min-score gates out low-scoring records."""
    result = runner.invoke(app, ["top-recent", "--min-score", "1.0"])
    assert result.exit_code == 0, result.output
    # No record has score >= 1.0 → empty-state line
    assert "no records" in result.output.lower()


def test_top_recent_json_output_shape(seeded_storage) -> None:
    """--json emits a parseable envelope with limit/min_score/count/items."""
    result = runner.invoke(app, ["top-recent", "--json", "--limit", "3", "--min-score", "0.5"])
    assert result.exit_code == 0, result.output
    body = result.output.strip().split("\n")[-1]
    payload = json.loads(body)
    assert payload["limit"] == 3
    assert payload["min_score"] == 0.5
    assert "count" in payload
    assert isinstance(payload["items"], list)
    for item in payload["items"]:
        assert "capture_id" in item
        assert "score" in item


def test_top_recent_sorts_score_desc(seeded_storage) -> None:
    """Output is sorted highest-score-first."""
    result = runner.invoke(app, ["top-recent", "--json", "--min-score", "0.0", "--limit", "20"])
    assert result.exit_code == 0, result.output
    body = result.output.strip().split("\n")[-1]
    payload = json.loads(body)
    scores = [float(item["score"] or 0) for item in payload["items"]]
    # Monotonically non-increasing
    for i in range(len(scores) - 1):
        assert scores[i] >= scores[i + 1], f"sort broken at index {i}: {scores}"


# ── v62: catchem signals ────────────────────────────────────────────────────

def test_signals_lists_all_18_quant_signals() -> None:
    """Static catalog — no fixture needed. Must list 18 entries."""
    result = runner.invoke(app, ["signals"])
    assert result.exit_code == 0, result.output
    assert "18 entries" in result.output
    # spot-check a few known signals
    for name in ("event_clustering", "anomaly", "persistence", "news_velocity", "backtest"):
        assert name in result.output


def test_signals_json_envelope() -> None:
    """--json emits a parseable catalog with schema_version + count + signals[]."""
    result = runner.invoke(app, ["signals", "--json"])
    assert result.exit_code == 0, result.output
    body = result.output.strip().split("\n")[-1]
    payload = json.loads(body)
    assert payload["schema_version"] == 1
    assert payload["count"] == 18
    assert len(payload["signals"]) == 18
    for entry in payload["signals"]:
        assert {"name", "endpoint", "summary"} <= set(entry.keys())
        assert entry["endpoint"].startswith("/api/")


# ── v73: catchem signals --diagnostics ────────────────────────────────────


def test_signals_diagnostics_healthy_text(monkeypatch) -> None:
    """Healthy steady state: nominal banner + capacity hint, exit 0."""
    import httpx

    class _StubResp:
        status_code = 200

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict:
            return {
                "schema_version": 1,
                "generated_at": "2026-05-28T15:36:03Z",
                "total_failures": 0,
                "per_signal": {},
                "recent": [],
                "buffer_capacity": 50,
            }

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _StubResp())

    result = runner.invoke(app, ["signals", "--diagnostics"])
    assert result.exit_code == 0, result.output
    assert "all signals nominal" in result.output
    assert "capacity: 50" in result.output


def test_signals_diagnostics_degraded_text(monkeypatch) -> None:
    """Degraded state: per-signal counts + most-recent list with class + ts."""
    import httpx

    class _StubResp:
        status_code = 200

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict:
            return {
                "schema_version": 1,
                "generated_at": "2026-05-28T15:36:03Z",
                "total_failures": 3,
                "per_signal": {"spillover": 2, "anomaly": 1},
                "recent": [
                    {
                        "signal": "anomaly",
                        "error_class": "KeyError",
                        "error": "missing 'symbols' field",
                        "elapsed_ms": 1.4,
                        "ts": "2026-05-28T15:35:50Z",
                    },
                    {
                        "signal": "spillover",
                        "error_class": "ZeroDivisionError",
                        "error": "zero-bucket window",
                        "elapsed_ms": 0.8,
                        "ts": "2026-05-28T15:35:45Z",
                    },
                ],
                "buffer_capacity": 50,
            }

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _StubResp())

    result = runner.invoke(app, ["signals", "--diagnostics"])
    assert result.exit_code == 0, result.output
    assert "3 signal failure(s)" in result.output
    # Per-signal counts present, sorted desc by count
    sp_idx = result.output.find("spillover")
    an_idx = result.output.find("anomaly")
    assert sp_idx >= 0 and an_idx >= 0
    assert sp_idx < an_idx, "spillover (count=2) must list before anomaly (count=1)"
    # Recent entries surface error_class + ts
    assert "KeyError" in result.output
    assert "ZeroDivisionError" in result.output
    assert "2026-05-28T15:35:50Z" in result.output


def test_signals_diagnostics_json_passthrough(monkeypatch) -> None:
    """--json + --diagnostics returns the raw HTTP payload (script-friendly)."""
    import httpx

    expected = {
        "schema_version": 1,
        "generated_at": "2026-05-28T15:36:03Z",
        "total_failures": 1,
        "per_signal": {"spillover": 1},
        "recent": [
            {
                "signal": "spillover",
                "error_class": "ValueError",
                "error": "bad input",
                "elapsed_ms": 2.0,
                "ts": "2026-05-28T15:35:40Z",
                "traceback_head": "Traceback...",
            }
        ],
        "buffer_capacity": 50,
    }

    class _StubResp:
        status_code = 200

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict:
            return expected

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _StubResp())

    result = runner.invoke(app, ["signals", "--diagnostics", "--json"])
    assert result.exit_code == 0, result.output
    body = result.output.strip().split("\n")[-1]
    assert json.loads(body) == expected


def test_signals_diagnostics_handles_unreachable_sidecar(monkeypatch) -> None:
    """No sidecar listening → exit code 2 + actionable error message."""
    import httpx

    def _boom(*_a, **_kw):
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(httpx, "get", _boom)

    result = runner.invoke(app, ["signals", "--diagnostics"])
    assert result.exit_code == 2, result.output
    # stderr is captured into result.output by Typer's runner
    assert "sidecar unreachable" in result.output
    assert "catchem serve" in result.output


# ── v63: catchem db-stats ──────────────────────────────────────────────────

def test_db_stats_lists_known_tables(seeded_storage) -> None:
    """db-stats lists every catchem table including record_tags (v38)."""
    result = runner.invoke(app, ["db-stats"])
    assert result.exit_code == 0, result.output
    for table in ("records", "reviews", "record_tags", "model_versions"):
        assert table in result.output


def test_db_stats_json_envelope(seeded_storage) -> None:
    """--json envelope has tables[], indexes[], page_count, size."""
    result = runner.invoke(app, ["db-stats", "--json"])
    assert result.exit_code == 0
    body = result.output.strip().split("\n")[-1]
    payload = json.loads(body)
    for key in ("schema_version", "tables", "indexes", "total_tables",
                "page_count", "page_size_bytes", "estimated_size_bytes"):
        assert key in payload
    assert payload["total_tables"] >= 5  # records, reviews, dlq, record_tags, model_versions...
    assert all("name" in t and "rows" in t for t in payload["tables"])


# ── v66: catchem watch (interactive — only --help is unit-testable) ────────

def test_watch_help_lists_continuous_refresh() -> None:
    """The command must be registered AND describe itself as continuous."""
    result = runner.invoke(app, ["watch", "--help"])
    assert result.exit_code == 0
    assert "Continuous-refresh" in result.output
    assert "--interval" in result.output
    assert "0.5" in result.output  # floor


def test_watch_rejects_sub_second_interval() -> None:
    """Refresh interval must be ≥ 0.5s — protect SQLite from runaway loops."""
    result = runner.invoke(app, ["watch", "--interval", "0.1"])
    # typer/click rejects out-of-range before command body runs
    assert result.exit_code != 0
    assert "0.5" in result.output or "range" in result.output.lower() or "invalid" in result.output.lower()
