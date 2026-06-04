from pathlib import Path

from catchem.storage import Storage


def test_storage_corruption_recovery(tmp_path: Path):
    db_path = tmp_path / "data" / "catchem.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Write garbage to the database file to simulate corruption
    db_path.write_bytes(b"This is not a SQLite database file! Garbage bytes go here.")

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
