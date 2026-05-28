"""Tests for the SQLite truth-store backup/import endpoints.

The endpoints are exercised through the full TestClient lifespan so the
supervisor + storage path actually opens the DB file on disk first —
otherwise `db_info` would lie about a phantom DB and we'd miss the bug
where `sqlite_path()` resolves to a non-existent location.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem.api import create_app
from catchem.rate_limit import reset_all_buckets
from catchem.settings import load_settings, reload_settings


SQLITE_MAGIC = b"SQLite format 3\x00"


@pytest.fixture(autouse=True)
def _reset_rate_limit() -> None:
    """Reset module-level rate-limit buckets between tests.

    /api/db/import uses cost=5 on a capacity-6 bucket — three import
    tests run in sequence will exhaust the bucket and the third hits
    a 429 instead of the expected response. Resetting per-test keeps
    these tests independent and order-agnostic.
    """
    reset_all_buckets()
    yield
    reset_all_buckets()


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """Open the API with a fresh tmp output dir so the SQLite file is born here."""
    reload_settings()
    s = load_settings()
    app = create_app(s)
    c = TestClient(app)
    c.__enter__()
    yield c
    c.__exit__(None, None, None)


def _make_fake_sqlite_bytes() -> bytes:
    """Construct a minimal byte blob with the SQLite magic header.

    Real backend validation only checks the first 16 bytes, so we don't
    need a syntactically valid DB — any bytes that start with the magic
    string pass the gate. Production restores would be a real file, but
    the test only proves the validator path, not the SQL semantics.
    """
    return SQLITE_MAGIC + b"\x00" * 100


def test_db_info_returns_metadata(client: TestClient) -> None:
    r = client.get("/api/db/info")
    assert r.status_code == 200
    data = r.json()
    # The supervisor lifespan opens the DB file via Storage, so by the
    # time the first request lands the file exists on disk.
    assert data["exists"] is True
    assert isinstance(data["size_bytes"], int)
    assert data["size_bytes"] >= 0
    assert isinstance(data["modified_at"], str)
    assert "path" in data
    # Path should be tilde-redacted when under $HOME, but in tests we
    # use a /tmp dir so it just passes through.
    assert data["path"]


def test_db_info_path_is_redacted_when_under_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """db_info path must use ~/ rather than leaking the absolute /Users/<name>/... form."""
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    # Park the catchem output dir directly under the fake $HOME so the
    # display-path redactor has something to fold.
    out = fake_home / "Documents" / "Catchem" / "data"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(out))
    reload_settings()
    s = load_settings()
    app = create_app(s)
    with TestClient(app) as c:
        r = c.get("/api/db/info")
        assert r.status_code == 200
        data = r.json()
        # File may not yet exist on first call — wait until storage has
        # actually opened it. The supervisor lifespan triggers the open.
        assert data["path"].startswith("~/"), data


def test_db_export_returns_attachment(client: TestClient) -> None:
    r = client.get("/api/db/export")
    assert r.status_code == 200
    # FastAPI/Starlette FileResponse with filename= sets:
    #   Content-Type: application/octet-stream
    #   Content-Disposition: attachment; filename="catchem_...sqlite3"
    assert r.headers.get("content-type") == "application/octet-stream"
    disposition = r.headers.get("content-disposition", "")
    assert "attachment" in disposition.lower()
    assert "catchem_" in disposition and ".sqlite3" in disposition
    # The body must lead with the SQLite magic — confirms the file we
    # streamed is the real DB, not an HTML fallback.
    assert r.content[:16] == SQLITE_MAGIC


def test_db_export_returns_404_when_db_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the truth-store file has been deleted, export 404s rather than streaming nothing."""
    # Build an app whose output dir contains no DB file.
    out_dir = tmp_path / "empty_data"
    out_dir.mkdir()
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(out_dir))
    reload_settings()
    # Reset module-level _SETTINGS pinned by an earlier TestClient lifespan,
    # otherwise the endpoint resolves against the prior test's tmp dir.
    import catchem.api as _api_mod
    _api_mod._SETTINGS = None
    s = load_settings()
    app = create_app(s)
    # Don't enter the lifespan — that would create the DB. Hit the
    # endpoint directly through a non-lifespan TestClient.
    c = TestClient(app)
    # Wipe any DB the storage layer may have laid down during settings parse.
    db_path = s.sqlite_path()
    if db_path.exists():
        db_path.unlink()
    r = c.get("/api/db/export")
    assert r.status_code == 404
    assert "database not found" in r.text.lower()


def test_db_import_accepts_valid_sqlite_and_creates_backup(
    client: TestClient, tmp_path: Path
) -> None:
    # First snapshot the pre-import path so we know where the backup
    # should land.
    info_before = client.get("/api/db/info").json()
    assert info_before["exists"] is True
    settings = load_settings()
    db_path = settings.sqlite_path()
    db_dir = db_path.parent
    pre_existing_backups = set(db_dir.glob("catchem_backup_*.sqlite3"))

    payload = _make_fake_sqlite_bytes()
    files = {"file": ("snapshot.sqlite3", io.BytesIO(payload), "application/octet-stream")}
    r = client.post("/api/db/import", files=files)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["imported_size_bytes"] == len(payload)
    # backup_path must be set (we asserted the DB existed before import)
    assert data["backup_path"] is not None
    # Confirm a NEW backup file landed on disk (above whatever was there).
    new_backups = set(db_dir.glob("catchem_backup_*.sqlite3")) - pre_existing_backups
    assert len(new_backups) == 1, f"expected 1 new backup, got {new_backups}"
    backup_file = new_backups.pop()
    assert backup_file.exists()
    assert backup_file.stat().st_size > 0
    # The truth-store should now BE the imported payload (first 16 bytes).
    assert db_path.read_bytes()[:16] == SQLITE_MAGIC


def test_db_import_rejects_invalid_bytes(client: TestClient) -> None:
    files = {"file": ("not-a-db.bin", io.BytesIO(b"this is plain text, not a database"), "application/octet-stream")}
    r = client.post("/api/db/import", files=files)
    assert r.status_code == 400
    assert "not a valid sqlite file" in r.text.lower()


def test_db_import_rejects_empty_upload(client: TestClient) -> None:
    files = {"file": ("empty.sqlite3", io.BytesIO(b""), "application/octet-stream")}
    r = client.post("/api/db/import", files=files)
    assert r.status_code == 422
    assert "empty" in r.text.lower()


def test_db_import_when_no_prior_db_skips_backup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Import into a fresh output dir with no existing DB → backup_path is None."""
    out_dir = tmp_path / "fresh_data"
    monkeypatch.setenv("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", str(out_dir))
    reload_settings()
    # Reset the lifespan-pinned _SETTINGS so the endpoint resolves against
    # this test's fresh output dir instead of a previous test's leftover.
    import catchem.api as _api_mod
    _api_mod._SETTINGS = None
    s = load_settings()
    # Don't open lifespan — we want a clean disk where no DB has been born.
    db_path = s.sqlite_path()
    if db_path.exists():
        db_path.unlink()
    app = create_app(s)
    c = TestClient(app)
    payload = _make_fake_sqlite_bytes()
    files = {"file": ("seed.sqlite3", io.BytesIO(payload), "application/octet-stream")}
    r = c.post("/api/db/import", files=files)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["backup_path"] is None
    # And the DB exists now after import.
    assert db_path.exists()
    assert db_path.read_bytes()[:16] == SQLITE_MAGIC
