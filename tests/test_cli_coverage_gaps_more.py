"""Additional tests for coverage gaps in catchem/cli.py."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from catchem.cli import _format_size, _override_mode, app
from catchem.schemas import FinancialImpactRecord, ProcessingMode, SentimentLabel
from catchem.settings import load_settings
from catchem.storage import Storage, load_storage_from_settings

runner = CliRunner()


class MockConnectionWrapper:
    def __init__(
        self,
        real_conn,
        error_on_records=False,
        error_on_tags=False,
        error_on_count=False,
        error_on_last_record=False,
    ):
        self._real_conn = real_conn
        self.error_on_records = error_on_records
        self.error_on_tags = error_on_tags
        self.error_on_count = error_on_count
        self.error_on_last_record = error_on_last_record

    def execute(self, sql, *args):
        if self.error_on_records and "FROM records" in sql:
            raise sqlite3.OperationalError("mock operational error")
        if self.error_on_last_record and "created_at FROM records" in sql:
            raise sqlite3.OperationalError("mock last record error")
        if self.error_on_tags and "FROM record_tags" in sql:
            raise sqlite3.OperationalError("mock tags error")
        if self.error_on_count and "COUNT(*)" in sql:
            raise sqlite3.Error("cannot count")
        if self.error_on_count and "type='index'" in sql:
            cur = self._real_conn.cursor()
            cur.execute("SELECT 1 WHERE 1=0")
            return cur
        return self._real_conn.execute(sql, *args)

    def executescript(self, sql):
        return self._real_conn.executescript(sql)

    def fetchone(self):
        return self._real_conn.fetchone()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def __getattr__(self, name):
        return getattr(self._real_conn, name)


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
    settings = load_settings()
    storage = load_storage_from_settings(settings)
    fixtures = [
        _make_record("c-aapl", "Apple beats earnings expectations", "wsj.com"),
        _make_record(
            "c-msft",
            "Microsoft cloud growth accelerates",
            "reuters.com",
            symbols=("MSFT",),
        ),
    ]
    for rec in fixtures:
        storage.insert_record(rec)
    storage.close()
    return settings, storage


def test_format_size_large() -> None:
    # Coverage for line 258 fallback GB return
    res = _format_size(5 * 1024 * 1024 * 1024)
    assert res == "5.0 GB"


def test_override_mode_none() -> None:
    # Coverage for line 28->exit branch (mode is None)
    _override_mode(None)


def test_run_live_tail(monkeypatch) -> None:
    # Coverage for line 45
    from catchem.supervisor import Supervisor

    called = False

    def mock_run_tail(self):
        nonlocal called
        called = True

    monkeypatch.setattr(Supervisor, "run_tail", mock_run_tail)

    result = runner.invoke(app, ["run", "--mode", "live_tail"])
    assert result.exit_code == 0
    assert called


def test_benchmark_extended(tmp_path) -> None:
    # Coverage for line 105 and 369 in cli_bench / cli_benchmark
    ext_file = tmp_path / "extended.jsonl"
    ext_file.write_text(
        json.dumps(
            {
                "capture_id": "g-ext",
                "title": "Ext title",
                "text": "Ext text",
                "expected_finance_relevant": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # test benchmark command (line 105)
    result = runner.invoke(app, ["benchmark", "--golden", "--extended", str(ext_file)])
    assert result.exit_code == 0

    # test bench command (line 369)
    result2 = runner.invoke(app, ["bench", "--extended", str(ext_file)])
    assert result2.exit_code == 0


def test_bench_failure(monkeypatch) -> None:
    # Coverage for lines 374-376
    import catchem.golden

    def mock_run_benchmark(*a, **kw):
        raise ValueError("simulated benchmark failure")

    monkeypatch.setattr(catchem.golden, "run_benchmark", mock_run_benchmark)

    result = runner.invoke(app, ["bench"])
    assert result.exit_code == 1
    assert "error: benchmark failed" in result.output


def test_bench_none_fields(monkeypatch) -> None:
    # Coverage for lines 387->389, 390->393, and 396
    import catchem.golden

    class MockReport:
        def to_dict(self):
            return {
                "dataset_name": "mock_set",
                "n": 0,
                "relevance": {"precision": 0.5, "recall": 0.5, "f1": 0.5},
                "asset_class_f1": {},
                "reason_code_f1": {},
                "symbol_recall": None,
                "sentiment_accuracy": None,
                "per_item": [],
            }

    monkeypatch.setattr(catchem.golden, "run_benchmark", lambda *a, **kw: MockReport())
    result = runner.invoke(app, ["bench"])
    assert result.exit_code == 0
    assert "Relevance P/R/F1:" in result.output
    assert "Symbol recall" not in result.output
    assert "Sentiment acc" not in result.output


def test_db_info_operational_error(monkeypatch, seeded_storage) -> None:
    # Coverage for lines 284-285
    orig_connect = sqlite3.connect

    def mock_connect(*a, **kw):
        conn = orig_connect(*a, **kw)
        return MockConnectionWrapper(conn, error_on_records=True)

    monkeypatch.setattr(sqlite3, "connect", mock_connect)

    result = runner.invoke(app, ["db-info"])
    assert result.exit_code == 0
    assert "0 total, 0 finance-relevant" in result.output


def test_db_backup_sqlite_error(monkeypatch, seeded_storage, tmp_path) -> None:
    # Coverage for lines 338-340
    def mock_connect(*a, **kw):
        raise sqlite3.Error("simulated connection failure")

    monkeypatch.setattr(sqlite3, "connect", mock_connect)

    target = tmp_path / "failed_backup.sqlite3"
    result = runner.invoke(app, ["db-backup", "--output", str(target)])
    assert result.exit_code == 1
    assert "error: backup failed" in result.output


def test_search_limits_and_matching(monkeypatch, seeded_storage) -> None:
    # Coverage for lines 437, 465-471, 499-500, 501->exit
    from catchem.quant import QuantEngine

    class MockCluster:
        def __init__(self, cluster_id, size, dominant_symbols):
            self.cluster_id = cluster_id
            self.size = size
            self.dominant_symbols = dominant_symbols

    def mock_clusters(self, limit=2000):
        return [
            MockCluster("cluster-1", 5, ["AAPL", "MSFT"]),
            MockCluster("cluster-2", 3, []),  # Empty symbols
            MockCluster("cluster-3", 2, ["GOOG"]),
        ]

    monkeypatch.setattr(QuantEngine, "clusters", mock_clusters)

    # 1. Search limit check
    result = runner.invoke(app, ["search", "Apple", "--limit", "1"])
    assert result.exit_code == 0

    # 2. Match by cluster symbols and limit
    result = runner.invoke(app, ["search", "AAPL", "--limit", "1"])
    assert result.exit_code == 0
    assert "cluster-1" in result.output

    # 3. No symbols fallback
    result = runner.invoke(app, ["search", "cluster-2"])
    assert result.exit_code == 0
    assert "(no symbols)" in result.output

    # 4. No matches check
    result = runner.invoke(app, ["search", "nonexistent_query"])
    assert result.exit_code == 0
    assert "(no matches)" in result.output


def test_search_clusters_exception(monkeypatch, seeded_storage) -> None:
    # Coverage for lines 459-460
    from catchem.quant import QuantEngine

    def mock_clusters_error(self, limit=2000):
        raise ValueError("clusters error")

    monkeypatch.setattr(QuantEngine, "clusters", mock_clusters_error)

    result = runner.invoke(app, ["search", "Apple"])
    assert result.exit_code == 0


def test_export_non_list_non_string(monkeypatch, seeded_storage, tmp_path) -> None:
    # Coverage for lines 587->585, 591->589
    bad_record = {
        "capture_id": "c-bad",
        "title": 123,  # Not a string
        "domain": "bad.com",
        "asset_classes": None,  # Not a list
        "impact_reason_codes": None,
        "candidate_symbols": None,
        "finance_relevance_score": 0.9,
    }
    monkeypatch.setattr(Storage, "recent_records", lambda *a, **kw: [bad_record])

    target = tmp_path / "bad.csv"
    result = runner.invoke(app, ["export", "csv", "--output", str(target)])
    assert result.exit_code == 0
    assert target.exists()


def test_tag_remove_value_error(monkeypatch, seeded_storage) -> None:
    # Coverage for lines 676-678
    def mock_remove_tag(*a, **kw):
        raise ValueError("invalid tag formatting")

    monkeypatch.setattr(Storage, "remove_record_tag", mock_remove_tag)

    result = runner.invoke(app, ["tag-remove", "c-aapl", "bad tag"])
    assert result.exit_code == 1
    assert "error: invalid tag" in result.output


def test_portfolio_add_value_error(monkeypatch, seeded_storage) -> None:
    # Coverage for lines 850-852
    def mock_add_holding(*a, **kw):
        raise ValueError("invalid portfolio parameters")

    monkeypatch.setattr(Storage, "add_holding", mock_add_holding)

    result = runner.invoke(app, ["portfolio", "add", "INVALID"])
    assert result.exit_code == 1
    assert "error:" in result.output


def test_portfolio_show_empty(monkeypatch, seeded_storage) -> None:
    # Coverage for lines 916-917
    monkeypatch.setattr(Storage, "list_holdings", lambda *a, **kw: [])

    result = runner.invoke(app, ["portfolio", "show"])
    assert result.exit_code == 0
    assert "no holdings" in result.output


def test_portfolio_show_no_quote_and_long_headline(monkeypatch, seeded_storage) -> None:
    # Coverage for lines 927 and 934
    monkeypatch.setattr(Storage, "list_holdings", lambda *a, **kw: [{"id": 1, "symbol": "AAPL"}])

    import catchem.portfolio

    def mock_enrich_holdings(*a, **kw):
        return [
            {
                "id": 1,
                "symbol": "AAPL",
                "quote": None,  # Price string = "no quote"
                "coverage": {"covered": False},
                "recent_news_count": 5,
                "recent_top": [
                    {
                        "title": "A very long headline that definitely exceeds sixty characters in total length to trigger the truncation branch"
                    }
                ],
            }
        ]

    monkeypatch.setattr(catchem.portfolio, "enrich_holdings", mock_enrich_holdings)

    result = runner.invoke(app, ["portfolio", "show"])
    assert result.exit_code == 0
    assert "no quote" in result.output
    assert "..." in result.output


def test_watch_errors(monkeypatch, seeded_storage) -> None:
    # Coverage for lines 989-990
    def mock_sleep(*a, **kw):
        raise KeyboardInterrupt()

    monkeypatch.setattr(time, "sleep", mock_sleep)

    orig_connection = Storage._connection

    @contextmanager
    def mock_connection(self):
        with orig_connection(self) as conn:
            yield MockConnectionWrapper(conn, error_on_tags=True)

    monkeypatch.setattr(Storage, "_connection", mock_connection)

    result = runner.invoke(app, ["watch", "--interval", "1.0"])
    assert result.exit_code == 0
    assert "0 tags" in result.output


def test_db_stats_errors_and_no_indexes(monkeypatch, seeded_storage) -> None:
    # Coverage for lines 1046-1047, 1078->exit
    orig_connection = Storage._connection

    @contextmanager
    def mock_connection(self):
        with orig_connection(self) as conn:
            yield MockConnectionWrapper(conn, error_on_count=True)

    monkeypatch.setattr(Storage, "_connection", mock_connection)

    result = runner.invoke(app, ["db-stats"])
    assert result.exit_code == 0
    assert "ERR" in result.output


def test_signals_diagnostics_unreachable_json(monkeypatch) -> None:
    # Coverage for line 1110
    def _raise(*_a, **_kw):
        raise httpx.ConnectError("no sidecar")

    monkeypatch.setattr(httpx, "get", _raise)

    result = runner.invoke(app, ["signals", "--diagnostics", "--json"])
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert "sidecar unreachable" in payload["error"]


def test_awareness_live_empty_parsers(monkeypatch) -> None:
    # Coverage for line 1197
    class _StubResp:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {
                "sources_total": 0,
                "sources_by_parser": {},
                "poll_interval_seconds": 10,
                "median_publisher_lag_seconds": None,
                "window_estimate_seconds": None,
                "total_ingested": 0,
            }

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _StubResp())

    result = runner.invoke(app, ["awareness", "--live"])
    assert result.exit_code == 0
    assert "none — poller not configured" in result.output


def test_awareness_static_empty(monkeypatch) -> None:
    # Coverage for line 1261
    import catchem.news_poller

    monkeypatch.setattr(catchem.news_poller, "assemble_feeds", lambda: [])

    result = runner.invoke(app, ["awareness"])
    assert result.exit_code == 0
    assert "none configured" in result.output


def test_coverage_gaps_custom_returns(monkeypatch, seeded_storage) -> None:
    # Coverage for lines 1462, 1470-1473, and 1476
    import catchem.awareness_gaps

    def mock_find_gaps_1(*a, **kw):
        return {
            "gaps": [],
            "covered": [
                {"term": "AAPL", "last_seen_age_seconds": 7200.0, "mention_count": 1},
                {"term": "MSFT", "last_seen_age_seconds": 172800.0, "mention_count": 2},
            ],
        }

    monkeypatch.setattr(catchem.awareness_gaps, "find_coverage_gaps", mock_find_gaps_1)

    result = runner.invoke(app, ["coverage-gaps"])
    assert result.exit_code == 0
    assert "none — every watched term has fresh coverage" in result.output
    assert "2.0h ago" in result.output
    assert "2.0d ago" in result.output

    def mock_find_gaps_2(*a, **kw):
        return {"gaps": ["AAPL"], "covered": []}

    monkeypatch.setattr(catchem.awareness_gaps, "find_coverage_gaps", mock_find_gaps_2)

    result = runner.invoke(app, ["coverage-gaps"])
    assert result.exit_code == 0
    assert "none — no watched term seen in window" in result.output


def test_status_errors_and_sidecar_partial_success(monkeypatch, seeded_storage) -> None:
    # Coverage for lines 1501-1502, 1509-1510, and 1526-1534
    orig_connection = Storage._connection

    @contextmanager
    def mock_connection(self):
        with orig_connection(self) as conn:
            yield MockConnectionWrapper(conn, error_on_tags=True, error_on_last_record=True)

    monkeypatch.setattr(Storage, "_connection", mock_connection)

    def mock_get(url, *a, **kw):
        class MockResponse:
            def __init__(self, status_code, data=None):
                self.status_code = status_code
                self._data = data

            def json(self):
                return self._data

        if "/healthz" in url:
            return MockResponse(200)
        else:
            raise httpx.HTTPError("stats failure")

    monkeypatch.setattr(httpx, "get", mock_get)

    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output.strip().split("\n")[-1])
    assert payload["tags"] == 0
    assert payload["last_record_at"] is None
    assert payload["sidecar_ok"] is True
    assert payload["sidecar_pid"] is None


def test_status_sidecar_success(monkeypatch) -> None:
    # Coverage for lines 1530-1532
    def mock_get(url, *a, **kw):
        class MockResponse:
            def __init__(self, status_code, data=None):
                self.status_code = status_code
                self._data = data

            def json(self):
                return self._data

        if "/healthz" in url:
            return MockResponse(200)
        else:
            return MockResponse(200, {"pid": 12345})

    monkeypatch.setattr(httpx, "get", mock_get)

    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output.strip().split("\n")[-1])
    assert payload["sidecar_ok"] is True
    assert payload["sidecar_pid"] == 12345


def test_status_sidecar_non_200(monkeypatch) -> None:
    # Coverage for lines 1527->1538
    def mock_get(url, *a, **kw):
        class MockResponse:
            def __init__(self, status_code):
                self.status_code = status_code

        return MockResponse(500)

    monkeypatch.setattr(httpx, "get", mock_get)

    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output.strip().split("\n")[-1])
    assert payload["sidecar_ok"] is False


def test_status_sidecar_stats_non_200(monkeypatch) -> None:
    # Coverage for line 1530->1538 branch
    def mock_get(url, *a, **kw):
        class MockResponse:
            def __init__(self, status_code, data=None):
                self.status_code = status_code
                self._data = data

            def json(self):
                return self._data

        if "/healthz" in url:
            return MockResponse(200)
        else:
            return MockResponse(500)

    monkeypatch.setattr(httpx, "get", mock_get)

    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output.strip().split("\n")[-1])
    assert payload["sidecar_ok"] is True
    assert payload["sidecar_pid"] is None
