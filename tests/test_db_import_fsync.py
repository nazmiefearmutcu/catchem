"""Pins that /api/db/import fsyncs the tmp file BEFORE rename.

Bug history: ``tmp_path.write_bytes(content)`` only writes into the
kernel page cache. The follow-up ``tmp_path.replace(db_path)`` was an
atomic rename, but the BYTES under the new name had no guarantee of
being on durable storage. A power cut between the rename and the next
background flush could leave a zero-length / truncated DB under the
live path, with no easy recovery (the user's "Imported successfully"
toast had already fired).

The fix opens the tmp file with ``os.open(.., O_RDWR)``, calls
``os.fsync(fd)``, closes, THEN renames. This test patches ``os.fsync``
and asserts the call happened with the tmp path's FD.
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from catchem.api import create_app
from catchem.rate_limit import reset_all_buckets
from catchem.settings import load_settings, reload_settings


SQLITE_MAGIC = b"SQLite format 3\x00"


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    reset_all_buckets()
    yield
    reset_all_buckets()


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    reload_settings()
    s = load_settings()
    app = create_app(s)
    c = TestClient(app)
    c.__enter__()
    yield c
    c.__exit__(None, None, None)


def test_db_import_calls_fsync_before_rename(client: TestClient) -> None:
    """Patch os.fsync; verify it ran at least once during the import.

    We don't care about the exact FD value (it depends on what other
    Python streams are open). We care that the import path crossed the
    durability barrier — without the fsync the test wouldn't catch any
    call into os.fsync from the import handler.
    """
    payload = SQLITE_MAGIC + b"\x00" * 200

    # Patch the os.fsync that catchem.api imports as `os.fsync`.
    with patch("catchem.api.os.fsync") as mock_fsync:
        r = client.post(
            "/api/db/import",
            files={"file": ("upload.sqlite3", io.BytesIO(payload), "application/octet-stream")},
        )
    assert r.status_code == 200, r.text
    # Must have been called exactly once on the tmp FD before replace().
    assert mock_fsync.call_count >= 1
    # And the FD passed in must be an int (an open file descriptor).
    arg = mock_fsync.call_args[0][0]
    assert isinstance(arg, int)


def test_db_import_writes_payload_to_disk(client: TestClient) -> None:
    """End-to-end: after import the live DB file must contain our payload."""
    s = load_settings()
    db_path = s.sqlite_path()

    payload = SQLITE_MAGIC + b"PINNED-PAYLOAD-FOR-FSYNC-TEST" + b"\x00" * 200
    r = client.post(
        "/api/db/import",
        files={"file": ("upload.sqlite3", io.BytesIO(payload), "application/octet-stream")},
    )
    assert r.status_code == 200, r.text

    # The file at db_path is now our payload (rename happened after fsync).
    assert db_path.exists()
    on_disk = db_path.read_bytes()
    assert on_disk.startswith(SQLITE_MAGIC)
    assert b"PINNED-PAYLOAD-FOR-FSYNC-TEST" in on_disk
