from pathlib import Path

import pytest

from catchem.storage import Storage


def test_storage_corruption_recovery(tmp_path: Path):
    db_path = tmp_path / "data" / "catchem.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Write garbage to the database file to simulate corruption
    db_path.write_bytes(b"This is not a SQLite database file! Garbage bytes go here.")
    (db_path.parent / "catchem.sqlite3-wal").write_bytes(b"wal garbage")
    (db_path.parent / "catchem.sqlite3-shm").write_bytes(b"shm garbage")

    # Instantiate Storage. It should automatically detect the corruption,
    # quarantine the corrupt file, and initialize a new database.
    storage = Storage(
        db_path=db_path,
        parquet_dir=tmp_path / "parquet",
        dlq_dir=tmp_path / "dlq",
    )

    try:
        # Check that the database was successfully initialized and works
        with storage._connection() as conn:
            # We should be able to execute queries now
            res = conn.execute("SELECT version FROM schema_version").fetchone()
            assert res["version"] == 1

        # Verify that the corrupt file was quarantined
        corrupt_files = list(db_path.parent.glob("catchem.sqlite3.corrupt.*"))
        assert len(corrupt_files) == 1
        assert corrupt_files[0].read_bytes() == b"This is not a SQLite database file! Garbage bytes go here."
    finally:
        storage.close()


def test_storage_corruption_rename_error(tmp_path: Path):
    import sqlite3
    from unittest.mock import patch

    db_path = tmp_path / "catchem.sqlite3"
    db_path.write_bytes(b"garbage")

    with patch("pathlib.Path.rename", side_effect=OSError("Permission denied")), pytest.raises(sqlite3.DatabaseError):
        Storage(
            db_path=db_path,
            parquet_dir=tmp_path / "parquet",
            dlq_dir=tmp_path / "dlq",
        )


def test_storage_corruption_unlink_error(tmp_path: Path):
    from unittest.mock import patch

    db_path = tmp_path / "catchem.sqlite3"
    db_path.write_bytes(b"garbage")

    # Create sidecar files so unlink is attempted
    wal_path = tmp_path / "catchem.sqlite3-wal"
    wal_path.write_bytes(b"wal data")

    with patch("pathlib.Path.unlink", side_effect=OSError("Cannot delete")):
        storage = Storage(
            db_path=db_path,
            parquet_dir=tmp_path / "parquet",
            dlq_dir=tmp_path / "dlq",
        )
        try:
            with storage._connection() as conn:
                res = conn.execute("SELECT version FROM schema_version").fetchone()
                assert res["version"] == 1
        finally:
            storage.close()


def test_storage_corruption_dir(tmp_path: Path):
    import sqlite3

    import pytest

    db_path = tmp_path / "corrupt_dir"
    db_path.mkdir()

    with pytest.raises(sqlite3.DatabaseError):
        Storage(
            db_path=db_path,
            parquet_dir=tmp_path / "parquet",
            dlq_dir=tmp_path / "dlq",
        )

