"""Tests for the Drive archiver.

We don't make real network or cloud calls — the archiver is pure file I/O
against a temp directory + a freshly-built Supervisor on a temp SQLite.
These tests pin the public surface (row_to_csv_dict, csv_path_for_today)
and the drain semantics (write CSV, then delete from local).
"""

from __future__ import annotations

import asyncio
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


# ──────────────────────────────────────────────────────────────────────────────
# CSV formula-injection escaping (CWE-1236)
# ──────────────────────────────────────────────────────────────────────────────


def test_row_to_csv_dict_escapes_formula_injection() -> None:
    # Feed-supplied values that start with a spreadsheet formula trigger must be
    # neutralized with a leading apostrophe so Excel/Sheets treat them as text
    # rather than executing them on open.
    row = {
        "title": '=HYPERLINK("http://evil","x")',
        "domain": "+cmd|'/C calc'!A0",
        "url": "@SUM(1+1)",
        "candidate_symbols_json": json.dumps(["=1+2", "AAPL"]),
        "reason_text": "-2+3 leading minus",
    }
    out = row_to_csv_dict(row)
    assert out["title"].startswith("'="), "formula title must be apostrophe-escaped"
    assert out["domain"].startswith("'+")
    assert out["url"].startswith("'@")
    assert out["symbols"].startswith("'="), "joined symbols starting with = must escape"
    assert out["reasoning"].startswith("'-")


def test_row_to_csv_dict_leaves_ordinary_values_untouched() -> None:
    # No leading trigger → no apostrophe. Pins that the escape never mangles a
    # normal news title (the overwhelmingly common case).
    row = {
        "title": "Fed raises rates by 25 bps",
        "domain": "reuters.com",
        "url": "https://reuters.com/x",
        "candidate_symbols_json": json.dumps(["AAPL", "MSFT"]),
    }
    out = row_to_csv_dict(row)
    assert out["title"] == "Fed raises rates by 25 bps"
    assert out["domain"] == "reuters.com"
    assert out["url"] == "https://reuters.com/x"
    assert out["symbols"] == "AAPL, MSFT"



# ──────────────────────────────────────────────────────────────────────────────
# Extra Coverage / Branch Target Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_detect_drive_dir_various_cloud_providers(monkeypatch) -> None:
    # Set up basic fake home path
    fake_home = Path("/fake/home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    
    # We will mock Path.is_dir and Path.glob to simulate different setups
    is_dir_registry = set()
    glob_registry = {}
    
    original_is_dir = Path.is_dir
    def fake_is_dir(self):
        if str(self) in is_dir_registry:
            return True
        return original_is_dir(self)
        
    def fake_glob(self, pattern):
        return glob_registry.get((str(self), pattern), [])
        
    monkeypatch.setattr(Path, "is_dir", fake_is_dir)
    monkeypatch.setattr(Path, "glob", fake_glob)
    
    # 1. CloudStorage dir exists and has Google Drive with My Drive
    is_dir_registry.clear()
    glob_registry.clear()
    is_dir_registry.add("/fake/home/Library/CloudStorage")
    is_dir_registry.add("/fake/home/Library/CloudStorage/GoogleDrive-user")
    is_dir_registry.add("/fake/home/Library/CloudStorage/GoogleDrive-user/My Drive")
    glob_registry[("/fake/home/Library/CloudStorage", "GoogleDrive-*")] = [
        Path("/fake/home/Library/CloudStorage/GoogleDrive-user")
    ]
    assert detect_drive_dir() == Path("/fake/home/Library/CloudStorage/GoogleDrive-user/My Drive/Catchem")
    
    # 2. Google Drive without My Drive
    is_dir_registry.remove("/fake/home/Library/CloudStorage/GoogleDrive-user/My Drive")
    assert detect_drive_dir() == Path("/fake/home/Library/CloudStorage/GoogleDrive-user/Catchem")
    
    # 3. OneDrive
    is_dir_registry.clear()
    glob_registry.clear()
    is_dir_registry.add("/fake/home/Library/CloudStorage")
    is_dir_registry.add("/fake/home/Library/CloudStorage/OneDrive-personal")
    glob_registry[("/fake/home/Library/CloudStorage", "OneDrive-*")] = [
        Path("/fake/home/Library/CloudStorage/OneDrive-personal")
    ]
    assert detect_drive_dir() == Path("/fake/home/Library/CloudStorage/OneDrive-personal/Catchem")
    
    # 4. Dropbox
    is_dir_registry.clear()
    glob_registry.clear()
    is_dir_registry.add("/fake/home/Library/CloudStorage")
    is_dir_registry.add("/fake/home/Library/CloudStorage/Dropbox-user")
    glob_registry[("/fake/home/Library/CloudStorage", "Dropbox*")] = [
        Path("/fake/home/Library/CloudStorage/Dropbox-user")
    ]
    assert detect_drive_dir() == Path("/fake/home/Library/CloudStorage/Dropbox-user/Catchem")


def test_fallback_drive_dir(monkeypatch, tmp_path) -> None:
    from catchem.archive import fallback_drive_dir
    fake_home = tmp_path / "fakehome"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    assert fallback_drive_dir() == fake_home / "Documents" / "Catchem"


def test_archived_capture_ids_edge_cases(tmp_path) -> None:
    from catchem.archive import _archived_capture_ids
    
    # 1. OSError on non-existent file
    assert _archived_capture_ids(tmp_path / "does_not_exist") == set()
    
    # 2. Empty capture_id or missing field
    csv_file = tmp_path / "test.csv"
    with csv_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["title", "capture_id"])
        writer.writerow(["Sample A", ""])
        writer.writerow(["Sample B", "  "])
        writer.writerow(["Sample C", "id-1"])
    
    assert _archived_capture_ids(csv_file) == {"id-1"}


def test_row_to_csv_dict_json_errors() -> None:
    # evidence_json not valid json list
    row = {
        "evidence_json": "invalid-json",
        "asset_classes_json": "not-a-list-json",
    }
    out = row_to_csv_dict(row)
    assert out["reasoning"] == ""
    assert out["asset_classes"] == ""
    
    row2 = {
        "asset_classes_json": "123", # Not a list
    }
    out2 = row_to_csv_dict(row2)
    assert out2["asset_classes"] == ""


def test_archiver_start_twice_and_stop_not_running(archiver_with_records) -> None:
    archiver, _, _ = archiver_with_records
    
    # Call stop on unstarted archiver (no-op / line 318)
    async def run_stop():
        await archiver.stop()
    asyncio.run(run_stop())
    
    # Start it once
    async def run_start_and_double():
        archiver.start()
        # Start again when already running (returns immediately / line 299)
        archiver.start()
        await archiver.stop()
        
    asyncio.run(run_start_and_double())


def test_archiver_mkdir_oserror_handling(archiver_with_records, monkeypatch) -> None:
    archiver, _, _ = archiver_with_records
    
    # Mock mkdir to raise OSError
    def fake_mkdir(*args, **kwargs):
        raise OSError("Permission Denied")
        
    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    
    # Calling start should log a warning but not raise
    async def run_start_and_stop():
        archiver.start()
        await archiver.stop()
    asyncio.run(run_start_and_stop())


def test_archiver_run_loop_exceptions(archiver_with_records, monkeypatch) -> None:
    archiver, _, _ = archiver_with_records
    
    # Mock _archive_once to raise Exception
    def fake_archive_once():
        raise RuntimeError("DB Crash")
        
    monkeypatch.setattr(archiver, "_archive_once", fake_archive_once)
    
    # Use short interval
    archiver._interval = 0.01
    
    # Mock wait_for to raise TimeoutError immediately for the 5s grace period
    original_wait_for = asyncio.wait_for
    async def fake_wait_for(aw, timeout, **kwargs):
        if timeout == 5.0:
            raise TimeoutError()
        return await original_wait_for(aw, timeout, **kwargs)
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    
    # Run loop briefly and cancel
    async def run_briefly():
        archiver.start()
        await asyncio.sleep(0.05)
        await archiver.stop()
        
    asyncio.run(run_briefly())
    assert "DB Crash" in archiver.last_error


def test_archiver_already_running_result(archiver_with_records) -> None:
    archiver, _, _ = archiver_with_records
    
    # Mock lock object entirely since RLock attributes are read-only
    class FakeLock:
        def acquire(self, blocking=True):
            return False
        def release(self):
            pass
            
    archiver._run_lock = FakeLock()
    res = archiver._archive_once()
    assert res.archived == 0
    assert res.error == "already running"



def test_archiver_mkdir_fallback_failure(archiver_with_records, monkeypatch) -> None:
    archiver, _, _ = archiver_with_records
    
    # Force primary and fallback mkdir to fail
    def fake_mkdir(*args, **kwargs):
        raise OSError("Disk Read-Only")
        
    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    monkeypatch.setattr(archiver, "_drive_dir", Path("/invalid/path"))
    
    import catchem.archive as archive_mod
    monkeypatch.setattr(archive_mod, "fallback_drive_dir", lambda: Path("/invalid/fallback"))
    
    res = archiver._archive_once()
    assert res.archived == 0
    assert "mkdir failed" in res.error or "fallback mkdir failed" in res.error


def test_archiver_mtime_vector_skip(archiver_with_records, tmp_path) -> None:
    archiver, sup, _ = archiver_with_records
    
    # Set up vector index mockup with root path
    class FakeVectorIndex:
        def __init__(self, root):
            self.root = root
        def delete(self, cid):
            pass
            
    vec_root = tmp_path / "vectors"
    vec_root.mkdir()
    sup.vector_index = FakeVectorIndex(vec_root)
    
    # Create a vector file for a capture_id
    with sup.storage.cursor() as cur:
        cur.execute("SELECT capture_id FROM records LIMIT 1")
        cid = cur.fetchone()[0]
        
    npy_file = vec_root / f"{cid}.npy"
    npy_file.parent.mkdir(parents=True, exist_ok=True)
    npy_file.write_text("dummy")
    
    # Set mtime to current time (which is >= sweep_started)
    import os
    import time
    os.utime(npy_file, (time.time() + 10, time.time() + 10))
    
    # Perform archive sweep
    res = archiver._archive_once()
    assert res.archived == 150
    assert res.error is None
    # The file should still exist since its mtime was >= sweep_started
    assert npy_file.exists()


def test_detect_drive_dir_more_branches(monkeypatch) -> None:
    fake_home = Path("/fake/home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    
    is_dir_registry = set()
    glob_registry = {}
    
    def fake_is_dir(self):
        if str(self) in is_dir_registry:
            return True
        return False
        
    def fake_glob(self, pattern):
        return glob_registry.get((str(self), pattern), [])
        
    monkeypatch.setattr(Path, "is_dir", fake_is_dir)
    monkeypatch.setattr(Path, "glob", fake_glob)
    
    # Set up cloud storage dir
    is_dir_registry.add("/fake/home/Library/CloudStorage")
    
    # GoogleDrive-* matches, but d is not a dir (and mydrive is not a dir)
    glob_registry[("/fake/home/Library/CloudStorage", "GoogleDrive-*")] = [
        Path("/fake/home/Library/CloudStorage/GoogleDrive-1"),
        Path("/fake/home/Library/CloudStorage/GoogleDrive-2")
    ]
    # OneDrive-* matches, but d is not a dir
    glob_registry[("/fake/home/Library/CloudStorage", "OneDrive-*")] = [
        Path("/fake/home/Library/CloudStorage/OneDrive-1")
    ]
    # Dropbox* matches, but d is not a dir
    glob_registry[("/fake/home/Library/CloudStorage", "Dropbox*")] = [
        Path("/fake/home/Library/CloudStorage/Dropbox-1")
    ]
    
    # Run detect_drive_dir. It should glob everything, find no valid directories,
    # and finally return the fallback: /fake/home/Documents/Catchem.
    assert detect_drive_dir() == Path("/fake/home/Documents/Catchem")


def test_row_to_csv_dict_evidence_not_list() -> None:
    row = {
        "evidence_json": "123",
    }
    out = row_to_csv_dict(row)
    assert out["reasoning"] == ""
    
    row2 = {
        "evidence_json": '{"a": 1}',
    }
    out2 = row_to_csv_dict(row2)
    assert out2["reasoning"] == ""


def test_archiver_property_getters(archiver_with_records) -> None:
    archiver, _, _ = archiver_with_records
    assert isinstance(archiver.drive_dir, Path)
    assert archiver.local_cap == 100
    assert archiver.interval_seconds == 15.0


def test_archiver_archive_now(archiver_with_records) -> None:
    archiver, _, _ = archiver_with_records
    async def run_archive_now():
        res = await archiver.archive_now()
        assert res.error is None
    asyncio.run(run_archive_now())


def test_archiver_run_loop_comprehensive(archiver_with_records, monkeypatch) -> None:
    archiver, _, _ = archiver_with_records
    original_wait_for = asyncio.wait_for
    
    # 1. Test normal grace period return
    async def test_grace_period_exit():
        archiver.start()
        archiver._stop.set()
        await archiver._task
    asyncio.run(test_grace_period_exit())
    
    # Reset
    archiver._task = None
    archiver._stop.clear()
    
    # 2. Test loop check evaluates to False
    async def mock_wait_for_false_loop(aw, timeout, **kwargs):
        if timeout == 5.0:
            archiver._stop.set()
            raise TimeoutError()
        return await original_wait_for(aw, timeout, **kwargs)
        
    monkeypatch.setattr(asyncio, "wait_for", mock_wait_for_false_loop)
    
    async def test_loop_false_exit():
        archiver.start()
        await archiver._task
    asyncio.run(test_loop_false_exit())
    
    # Reset
    archiver._task = None
    archiver._stop.clear()
    
    # 3. Test successful execution of _archive_once from the background run loop
    interval_calls = 0
    async def mock_wait_for_success(aw, timeout, **kwargs):
        nonlocal interval_calls
        if timeout == 5.0:
            raise TimeoutError()
        elif timeout == archiver._interval:
            interval_calls += 1
            if interval_calls == 1:
                raise TimeoutError()
            else:
                return None
        return await original_wait_for(aw, timeout, **kwargs)
        
    monkeypatch.setattr(asyncio, "wait_for", mock_wait_for_success)
    
    archive_once_called = 0
    original_archive_once = archiver._archive_once
    def mock_archive_once():
        nonlocal archive_once_called
        archive_once_called += 1
        return original_archive_once()
    monkeypatch.setattr(archiver, "_archive_once", mock_archive_once)
    
    async def test_loop_success_execution():
        archiver.start()
        await archiver._task
        assert archive_once_called == 2
    asyncio.run(test_loop_success_execution())
    
    # Reset
    archiver._task = None
    archiver._stop.clear()
    
    # 4. Test CancelledError during loop execution
    def mock_archive_once_cancel():
        raise asyncio.CancelledError()
    monkeypatch.setattr(archiver, "_archive_once", mock_archive_once_cancel)
    
    async def mock_wait_for_cancel(aw, timeout, **kwargs):
        if timeout == 5.0:
            raise TimeoutError()
        return await original_wait_for(aw, timeout, **kwargs)
        
    monkeypatch.setattr(asyncio, "wait_for", mock_wait_for_cancel)
    
    async def test_loop_cancel_execution():
        archiver.start()
        with pytest.raises(asyncio.CancelledError):
            await archiver._task
    asyncio.run(test_loop_cancel_execution())


def test_archiver_mkdir_already_fallback_fails(archiver_with_records, monkeypatch) -> None:
    archiver, _, _ = archiver_with_records
    
    fallback_path = Path("/invalid/fallback/path")
    import catchem.archive as archive_mod
    monkeypatch.setattr(archive_mod, "fallback_drive_dir", lambda: fallback_path)
    archiver._drive_dir = fallback_path
    
    def fake_mkdir(*args, **kwargs):
        raise OSError("Permission Denied")
    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    
    res = archiver._archive_once()
    assert res.archived == 0
    assert "mkdir failed" in res.error


def test_archive_once_skips_already_written_capture_ids(archiver_with_records) -> None:
    archiver, sup, drive_dir = archiver_with_records
    
    drive_dir.mkdir(parents=True, exist_ok=True)
    csv_path = csv_path_for_today(drive_dir)
    
    with sup.storage.cursor() as cur:
        cur.execute("SELECT capture_id FROM records LIMIT 1")
        cid = cur.fetchone()[0]
        
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        writer.writerow({col: "dummy" if col != "capture_id" else cid for col in CSV_COLUMNS})
        
    res = archiver._archive_once()
    assert res.error is None
    
    with csv_path.open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 150


def test_archive_once_write_oserror_handling(archiver_with_records) -> None:
    archiver, _, drive_dir = archiver_with_records
    
    drive_dir.mkdir(parents=True, exist_ok=True)
    csv_path = csv_path_for_today(drive_dir)
    csv_path.mkdir()
    
    res = archiver._archive_once()
    assert res.archived == 0
    assert "write failed" in res.error


def test_archive_once_no_vector_index(archiver_with_records) -> None:
    archiver, sup, _ = archiver_with_records
    sup.vector_index = None
    res = archiver._archive_once()
    assert res.error is None
    assert res.archived == 150


def test_archive_once_vector_skip_still_present(archiver_with_records, monkeypatch) -> None:
    archiver, sup, _ = archiver_with_records
    original_connection = sup.storage._connection
    
    class WrappedConnection:
        def __init__(self, real_conn):
            self._real = real_conn
    
        def execute(self, sql, *args, **kwargs):
            if sql.strip().startswith("SELECT 1 FROM records WHERE capture_id = ?"):
                class MockCursor:
                    def fetchone(self):
                        return (1,)
                return MockCursor()
            return self._real.execute(sql, *args, **kwargs)
    
        def __getattr__(self, name):
            return getattr(self._real, name)
    
        def __enter__(self):
            self._real.__enter__()
            return self
    
        def __exit__(self, exc_type, exc_val, exc_tb):
            return self._real.__exit__(exc_type, exc_val, exc_tb)
            
    from contextlib import contextmanager
    @contextmanager
    def mock_connection_ctx():
        with original_connection() as conn:
            yield WrappedConnection(conn)
            
    monkeypatch.setattr(sup.storage, "_connection", mock_connection_ctx)
    
    class FakeVectorIndex:
        def __init__(self):
            self.root = None
            self.deleted_ids = []
        def delete(self, cid):
            self.deleted_ids.append(cid)
            
    fake_vec = FakeVectorIndex()
    sup.vector_index = fake_vec
    
    res = archiver._archive_once()
    assert res.error is None
    assert res.archived == 150
    assert len(fake_vec.deleted_ids) == 0


def test_archive_once_vector_no_root(archiver_with_records) -> None:
    archiver, sup, _ = archiver_with_records
    
    class FakeVectorIndex:
        def __init__(self):
            self.root = None
            self.deleted_ids = []
        def delete(self, cid):
            self.deleted_ids.append(cid)
            
    fake_vec = FakeVectorIndex()
    sup.vector_index = fake_vec
    
    res = archiver._archive_once()
    assert res.error is None
    assert res.archived == 150
    assert len(fake_vec.deleted_ids) == 150




