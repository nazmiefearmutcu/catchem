"""Migration framework tests.

Exercises the registry against an in-memory SQLite connection — no need
to spin up Storage. The endpoint integration is covered via the
TestClient lifespan, which proves the wiring inside ``Storage._init_db``
actually fires on app boot.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catchem import migrations as migrations_module
from catchem.api import create_app
from catchem.migrations import (
    MIGRATIONS,
    Migration,
    apply_migrations,
    current_version,
    max_known_version,
    pending_migrations,
)
from catchem.settings import load_settings, reload_settings


def _fresh_conn() -> sqlite3.Connection:
    """Open an isolation-level=None in-memory connection."""
    return sqlite3.connect(":memory:", isolation_level=None)


@pytest.fixture
def conn() -> Generator[sqlite3.Connection, None, None]:
    """Provide a fresh in-memory SQLite connection and close it after the test."""
    c = _fresh_conn()
    yield c
    c.close()


def test_fresh_db_runs_every_migration(conn: sqlite3.Connection) -> None:
    """An untouched DB starts at user_version=0 and ends at max_known."""
    assert current_version(conn) == 0
    applied = apply_migrations(conn)
    assert len(applied) == len(MIGRATIONS)
    assert [m.version for m in applied] == [m.version for m in MIGRATIONS]
    assert current_version(conn) == max_known_version()


def test_reapply_is_noop_when_already_current(conn: sqlite3.Connection) -> None:
    """Apply twice — second call returns an empty list and leaves version pinned."""
    apply_migrations(conn)
    expected = current_version(conn)
    applied_again = apply_migrations(conn)
    assert applied_again == []
    assert current_version(conn) == expected


def test_failing_migration_rolls_back_user_version(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A migration whose SQL errors leaves user_version untouched.

    Injects a bogus migration AFTER the real ones (version = max+1) so the
    real schema bootstrap still runs. The bad SQL references a table we
    never create — sqlite raises, ``apply_migrations`` rolls back, and
    the persisted ``user_version`` reflects only the migrations that
    succeeded before the failure.
    """
    apply_migrations(conn)  # land at max_known_version
    baseline_version = current_version(conn)

    bad = Migration(
        version=baseline_version + 99,
        name="intentionally_broken",
        sql="INSERT INTO definitely_not_a_real_table (x) VALUES (1);",
    )
    monkeypatch.setattr(migrations_module, "MIGRATIONS", (*MIGRATIONS, bad))

    with pytest.raises(RuntimeError, match="intentionally_broken"):
        apply_migrations(conn)

    # user_version must NOT have been bumped past the last successful migration.
    assert current_version(conn) == baseline_version


def test_monotonic_versions_in_registry() -> None:
    """Registered versions are strictly increasing positive integers."""
    versions = [m.version for m in MIGRATIONS]
    assert versions, "registry must declare at least one baseline migration"
    for v in versions:
        assert isinstance(v, int) and v > 0, f"non-positive version: {v}"
    assert versions == sorted(set(versions)), "versions must be strictly increasing without duplicates"


def test_unique_migration_names() -> None:
    """Each migration name is unique — they show up in logs as the human handle."""
    names = [m.name for m in MIGRATIONS]
    assert len(names) == len(set(names)), f"duplicate migration name in {names}"


def test_pending_migrations_filters_by_current_version(conn: sqlite3.Connection) -> None:
    """A DB pinned mid-history reports exactly the unapplied tail."""
    # Pretend a partial migration: claim version 1 is already applied,
    # so pending should drop entries with version <= 1.
    conn.execute("PRAGMA user_version = 1")
    pending = pending_migrations(conn)
    expected = [m for m in MIGRATIONS if m.version > 1]
    assert [m.version for m in pending] == [m.version for m in expected]


def test_applied_migrations_create_record_tags_table(conn: sqlite3.Connection) -> None:
    """End-to-end: after migrating a fresh DB, migration #2's table exists."""
    apply_migrations(conn)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='record_tags'"
    ).fetchone()
    assert row is not None, "record_tags table should be created by migration 2"
    # Its indexes are present too (idempotent IF NOT EXISTS DDL).
    idx = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='record_tags'"
        ).fetchall()
    }
    assert {"idx_record_tags_tag", "idx_record_tags_capture"} <= idx


def test_applied_migrations_create_portfolio_table(conn: sqlite3.Connection) -> None:
    """Migration #3 lays down the READ-ONLY portfolio holdings table."""
    apply_migrations(conn)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='portfolio'"
    ).fetchone()
    assert row is not None, "portfolio table should be created by migration 3"
    cols = {r[1] for r in conn.execute("PRAGMA table_info(portfolio)").fetchall()}
    assert {
        "id",
        "symbol",
        "label",
        "shares",
        "weight",
        "cost_basis",
        "notes",
        "added_at",
    } <= cols
    idx = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='portfolio'"
        ).fetchall()
    }
    assert "idx_portfolio_symbol" in idx


def test_apply_migrations_idempotent_against_partial_state(conn: sqlite3.Connection) -> None:
    """Re-running a migration whose objects already exist is a clean no-op.

    Pre-creates migration #2's table by hand, then resets user_version to 1
    so apply_migrations re-runs #2. The IF NOT EXISTS guards must absorb the
    pre-existing objects without error, and the version must land at max.
    """
    apply_migrations(conn)
    assert current_version(conn) == max_known_version()
    # Simulate an interrupted upgrade: objects exist but version is behind.
    conn.execute("PRAGMA user_version = 1")
    reapplied = apply_migrations(conn)
    assert [m.version for m in reapplied] == [
        m.version for m in MIGRATIONS if m.version > 1
    ]
    assert current_version(conn) == max_known_version()


def test_pending_on_fresh_db_is_full_registry(conn: sqlite3.Connection) -> None:
    """A pristine connection reports every migration as pending."""
    assert [m.version for m in pending_migrations(conn)] == [m.version for m in MIGRATIONS]


def test_max_known_version_matches_registry_tail() -> None:
    """max_known_version equals the largest declared version."""
    assert max_known_version() == max(m.version for m in MIGRATIONS)


def test_baseline_migration_is_noop_select() -> None:
    """Migration #1 is intentionally a no-op SELECT (legacy DBs claim it).

    The SQL body must contain the no-op SELECT and emit no DDL. We strip
    ``-- ...`` comment lines first because the baseline's explanatory
    comment legitimately mentions the legacy path that *creates* tables.
    """
    baseline = next(m for m in MIGRATIONS if m.version == 1)
    assert "SELECT 1" in baseline.sql
    code = "\n".join(
        line.split("--", 1)[0] for line in baseline.sql.splitlines()
    ).upper()
    assert "CREATE" not in code
    assert "ALTER" not in code


def test_storage_init_lands_at_max_known_version(tmp_path: Path) -> None:
    """The Storage._init_db path runs apply_migrations end-to-end."""
    from catchem.storage import Storage

    db_path = tmp_path / "data" / "test.sqlite3"
    parquet_dir = tmp_path / "parquet"
    dlq_dir = tmp_path / "dlq"
    storage = Storage(db_path=db_path, parquet_dir=parquet_dir, dlq_dir=dlq_dir)
    try:
        with storage._connection() as conn:
            assert current_version(conn) == max_known_version()
    finally:
        storage.close()


def test_schema_version_endpoint(tmp_path: Path) -> None:
    """The /api/db/schema_version surface exposes the live DB's state."""
    reload_settings()
    s = load_settings()
    app = create_app(s)
    with TestClient(app) as c:
        r = c.get("/api/db/schema_version")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["user_version"] == max_known_version()
        assert data["max_known"] == max_known_version()
        assert data["migrations_pending"] == []
        assert "generated_at" in data
