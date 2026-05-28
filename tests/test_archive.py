"""Tests for the Drive archiver.

We don't make real network or cloud calls — the archiver is pure file I/O
against a temp directory + a freshly-built Supervisor on a temp SQLite.
These tests pin the public surface (row_to_csv_dict, csv_path_for_today)
and the drain semantics (write CSV, then delete from local).
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from catchem.archive import (
    CSV_COLUMNS,
    DriveArchiver,
    csv_path_for_today,
    detect_drive_dir,
    row_to_csv_dict,
)

# ──────────────────────────────────────────────────────────────────────────────
# row_to_csv_dict — pure projection function
# ──────────────────────────────────────────────────────────────────────────────


def test_row_to_csv_dict_handles_a_typical_record() -> None:
    row = {
        "capture_id": "demo-abc",
        "title": "Fed raises rates by 25 bps",
        "domain": "reuters.com",
        "url": "https://reuters.com/x",
        "finance_relevance_score": 0.873,
        "is_finance_relevant": 1,
        "asset_classes_json": json.dumps(["rates", "fx"]),
        "impact_reason_codes_json": json.dumps(["central_bank", "inflation"]),
        "candidate_symbols_json": json.dumps(["MSFT", "BP"]),
        "sentiment_label": "neutral",
        "sentiment_score": 0.1,
        "evidence_json": json.dumps([
            "The Federal Reserve voted to raise rates today.",
            "Markets reacted with a 0.8% decline on the S&P 500.",
        ]),
        "reason_text": "Direct mention of central bank policy with quantified impact.",
        "language": "en",
        "processing_mode": "production_safe",
        "diagnostic_enabled": 0,
        "published_ts": "2026-05-17T10:00:00+00:00",
        "created_at": "2026-05-17T10:05:23+00:00",
    }
    out = row_to_csv_dict(row)

    # Each declared column has a value (string).
    for col in CSV_COLUMNS:
        assert col in out, f"missing column {col}"
        assert isinstance(out[col], str), f"column {col} is not a string"

    # Spot-check the projections
    assert out["score"] == "0.873"
    assert out["is_finance_relevant"] == "1"
    assert out["asset_classes"] == "rates, fx"
    assert out["reason_codes"] == "central_bank, inflation"
    assert out["symbols"] == "MSFT, BP"
    assert out["ingested_at"] == "2026-05-17T10:05:23+00:00"
    assert out["published_at"] == "2026-05-17T10:00:00+00:00"
    # reasoning = reason_text + first 3 evidence sentences joined by " // "
    assert "central bank policy" in out["reasoning"]
    assert "Federal Reserve voted" in out["reasoning"]


def test_row_to_csv_dict_tolerates_missing_or_malformed_fields() -> None:
    """An incomplete row shouldn't raise — defaults should keep CSV clean."""
    row = {
        "capture_id": "demo-z",
        "title": None,
        "asset_classes_json": "not-json",
        "impact_reason_codes_json": None,
        "candidate_symbols_json": "[]",
        "finance_relevance_score": None,
        "evidence_json": "[]",
        "reason_text": None,
        "created_at": "",
        "published_ts": None,
    }
    out = row_to_csv_dict(row)
    assert out["score"] == ""
    assert out["asset_classes"] == ""
    assert out["reason_codes"] == ""
    assert out["symbols"] == ""
    assert out["reasoning"] == ""
    # Every declared column must still exist.
    assert set(out.keys()) == set(CSV_COLUMNS)


def test_row_to_csv_dict_strips_newlines_from_title_and_reasoning() -> None:
    """CSV with un-escaped newlines confuses Excel; reasoning + title must be flat."""
    row = {
        "title": "Line one\nLine two\rwith a return",
        "reason_text": "Why?\nBecause.",
        "evidence_json": json.dumps(["x\ny\nz"]),
    }
    out = row_to_csv_dict(row)
    assert "\n" not in out["title"]
    assert "\r" not in out["title"]
    assert "\n" not in out["reasoning"]
    assert "\r" not in out["reasoning"]


# ──────────────────────────────────────────────────────────────────────────────
# csv_path_for_today
# ──────────────────────────────────────────────────────────────────────────────


def test_csv_path_for_today_uses_utc_date_in_filename() -> None:
    p = csv_path_for_today(Path("/tmp/Catchem"))
    assert p.parent == Path("/tmp/Catchem")
    # Today's UTC date — pin the format, accept any actual date.
    fname = p.name
    assert fname.startswith("news_archive_")
    assert fname.endswith(".csv")
    # Date string is between the prefix + suffix
    date = fname[len("news_archive_"):-len(".csv")]
    # Must parse as YYYY-MM-DD
    datetime.strptime(date, "%Y-%m-%d")


# ──────────────────────────────────────────────────────────────────────────────
# detect_drive_dir — always returns SOMETHING
# ──────────────────────────────────────────────────────────────────────────────


def test_detect_drive_dir_returns_a_path_even_with_no_cloud_storage(monkeypatch, tmp_path) -> None:
    """The fallback path (~/Documents/Catchem) should kick in if no cloud mount exists."""
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    result = detect_drive_dir()
    assert "Catchem" in result.name or "Catchem" in str(result)
    # Should fall back to ~/Documents/Catchem under the fake home.
    assert str(fake_home) in str(result)


# ──────────────────────────────────────────────────────────────────────────────
# DriveArchiver._archive_once — end-to-end against a real (temp) supervisor
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def archiver_with_records(tmp_path, monkeypatch):
    """Build a real Supervisor on a temp SQLite, ingest 250 demo records,
    and return (archiver, supervisor, drive_dir)."""
    # Point storage at the temp dir.
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CATCHEM_MODELS__USE_ML_STUBS", "true")
    monkeypatch.setenv("CATCHEM_NEWS__POLLER_ENABLED", "false")

    from catchem.demo import build_capture
    from catchem.settings import load_settings, reload_settings
    from catchem.supervisor import Supervisor

    reload_settings()
    settings = load_settings()
    sup = Supervisor(settings)

    # Ingest 250 records (200 + 50 buffer) so we have plenty to drain past
    # the local_cap (set to 100 in this test).
    base = datetime.now(UTC) - timedelta(hours=1)
    for i in range(250):
        cap = build_capture(
            title=f"Sample {i}: Federal Reserve raises rates by 25 bps",
            text="The Fed raised interest rates 25 basis points today, citing sticky inflation.",
            domain="reuters.com",
            url=f"https://example.com/article-{i}",
            published_ts=base + timedelta(seconds=i),
        )
        sup.process_capture(cap)
    sup.storage.flush()

    drive_dir = tmp_path / "Drive" / "Catchem"
    archiver = DriveArchiver(
        supervisor=sup,
        settings=settings,
        drive_dir=drive_dir,
        local_cap_rows=100,
        interval_seconds=15.0,
    )
    try:
        yield archiver, sup, drive_dir
    finally:
        sup.close()


def test_archive_once_drains_excess_rows_into_csv(archiver_with_records) -> None:
    """After one sweep: local row count should drop to local_cap, CSV should have the rest."""
    archiver, sup, _drive_dir = archiver_with_records

    # Sanity: 250 rows pre-archive
    with sup.storage.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM records")
        assert cur.fetchone()[0] == 250

    result = archiver._archive_once()
    assert result.error is None
    assert result.archived == 150  # 250 - local_cap (100)
    assert result.csv_path is not None
    assert result.csv_path.exists()

    # Local cap honored
    with sup.storage.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM records")
        assert cur.fetchone()[0] == 100

    # CSV has header + 150 data rows
    with result.csv_path.open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 150
    assert set(rows[0].keys()) == set(CSV_COLUMNS)


def test_archive_once_is_a_no_op_when_local_below_cap(archiver_with_records) -> None:
    """If local count is already at/below cap, no rows should be archived."""
    archiver, _sup, _drive_dir = archiver_with_records

    # Drain everything down to the cap first.
    archiver._archive_once()
    # Run again — should be a clean no-op.
    second = archiver._archive_once()
    assert second.archived == 0
    assert second.error is None


def test_archive_once_appends_across_runs(archiver_with_records, monkeypatch) -> None:
    """A second sweep should APPEND to the same daily CSV, not overwrite it."""
    archiver, sup, _drive_dir = archiver_with_records

    from catchem.demo import build_capture

    # First sweep: 150 archived.
    first = archiver._archive_once()
    assert first.archived == 150
    first_csv = first.csv_path

    # Push more rows past the cap.
    base = datetime.now(UTC)
    for i in range(60):
        cap = build_capture(
            title=f"Round-2 {i}",
            text="Markets steady ahead of Fed minutes.",
            domain="ft.com",
            url=f"https://example.com/r2-{i}",
            published_ts=base + timedelta(seconds=i),
        )
        sup.process_capture(cap)
    sup.storage.flush()

    second = archiver._archive_once()
    assert second.archived == 60  # round-2 inserts past the cap of 100
    assert second.csv_path == first_csv  # same daily file

    # CSV now has both batches.
    with first_csv.open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 210


def test_archive_once_does_not_delete_locally_if_csv_write_fails(
    archiver_with_records, monkeypatch, tmp_path
) -> None:
    """If the CSV write fails (primary AND fallback both blocked), the rows
    must remain in SQLite for the next tick."""
    archiver, sup, drive_dir = archiver_with_records

    # Block the primary drive_dir: replace it with a regular file so
    # mkdir(parents=True, exist_ok=True) raises.
    drive_dir.parent.mkdir(parents=True, exist_ok=True)
    if drive_dir.is_dir():
        for f in drive_dir.iterdir():
            f.unlink()
        drive_dir.rmdir()
    drive_dir.write_text("blocking file")

    # Also block the auto-fallback (~/Documents/Catchem by default) by
    # monkeypatching fallback_drive_dir to point at another blocking file.
    blocked_fallback = tmp_path / "blocked_fallback"
    blocked_fallback.write_text("blocking file")
    import catchem.archive as archive_mod
    monkeypatch.setattr(archive_mod, "fallback_drive_dir", lambda: blocked_fallback)

    pre_count = None
    with sup.storage.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM records")
        pre_count = cur.fetchone()[0]

    result = archiver._archive_once()
    assert result.archived == 0
    assert result.error is not None

    # Local count is unchanged — failure mode preserves data.
    with sup.storage.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM records")
        assert cur.fetchone()[0] == pre_count


def test_archive_once_auto_falls_back_to_documents_when_primary_unwritable(
    archiver_with_records, monkeypatch, tmp_path
) -> None:
    """If the configured drive_dir is unwritable, we should switch to the
    documented fallback (~/Documents/Catchem in production) and still
    archive successfully — instead of silently dropping data on the floor."""
    archiver, _sup, drive_dir = archiver_with_records

    # Block primary
    drive_dir.parent.mkdir(parents=True, exist_ok=True)
    if drive_dir.is_dir():
        for f in drive_dir.iterdir():
            f.unlink()
        drive_dir.rmdir()
    drive_dir.write_text("blocking file")

    # Redirect fallback to a writable temp path so we don't actually
    # touch the real ~/Documents/Catchem.
    fallback_path = tmp_path / "fallback" / "Catchem"
    import catchem.archive as archive_mod
    monkeypatch.setattr(archive_mod, "fallback_drive_dir", lambda: fallback_path)

    result = archiver._archive_once()
    assert result.error is None
    assert result.archived == 150
    assert result.csv_path is not None
    assert str(fallback_path) in str(result.csv_path)
    assert fallback_path.is_dir()


def test_archive_csv_columns_are_stable_and_complete() -> None:
    """The CSV column order is part of the contract — once a file has rows,
    we can't reshuffle these without breaking downstream readers."""
    assert CSV_COLUMNS[0] == "ingested_at"
    assert "score" in CSV_COLUMNS
    assert "reasoning" in CSV_COLUMNS
    assert "capture_id" in CSV_COLUMNS
    # No duplicates.
    assert len(CSV_COLUMNS) == len(set(CSV_COLUMNS))
