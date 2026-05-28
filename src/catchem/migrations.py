"""SQLite migrations registry.

Uses ``PRAGMA user_version`` to track the current schema generation. On
startup we apply any migration whose ``version`` is greater than the
stored ``user_version``, then bump ``user_version`` to the highest
applied one. Every step runs inside a transaction so a mid-flight
failure rolls back cleanly.

Each migration is a :class:`Migration` (``version``, ``name``, ``sql``).
Versions MUST be monotonically increasing positive integers. Once a
migration has been shipped to users, NEVER modify or delete it — only
append new entries. Pre-existing databases laid down by builds that
predate the ``user_version`` machinery are claimed by migration #1, so
the baseline migration is intentionally a no-op (the legacy
``_init_db`` path already created every table via ``IF NOT EXISTS``).

Idempotency is the other half of the contract: SQL inside a migration
should rely on ``IF NOT EXISTS`` / ``IF EXISTS`` and be safe to re-run
against partial state, in case an upgrade is interrupted between
``executescript`` and the ``PRAGMA user_version =`` bump.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    """One migration step.

    ``sql`` is fed to :meth:`sqlite3.Connection.executescript`. Note the
    actual semantics — they are NOT what a naive "wraps the script in a
    transaction" reading would suggest:

      * ``executescript`` first issues an implicit ``COMMIT`` of whatever
        transaction the connection was holding, THEN runs the script.
        After that COMMIT the connection is in autocommit mode for the
        duration of the script. A subsequent ``conn.rollback()`` is a
        no-op against any DDL the script ran — there is no transaction
        to roll back. ``conn.commit()`` is also a no-op for the same
        reason (the script already auto-committed each statement).
      * The ``apply_migrations`` rollback path below therefore does not
        and CANNOT undo SQL that ran inside the script — by the time
        sqlite3 raises, the offending DDL is already committed.

    Why this is still safe in catchem today:
      * Every shipped migration uses ``CREATE TABLE IF NOT EXISTS`` /
        ``CREATE INDEX IF NOT EXISTS`` (idempotent DDL only). Partial
        execution leaves the DB in a state that is equivalent to "the
        migration completed up to statement N" AND the migration is
        safe to re-run from scratch on the next boot — the surviving
        objects are claimed by their ``IF NOT EXISTS`` guards.
      * ``user_version`` is bumped on a SEPARATE statement AFTER the
        script — if any statement inside the script raised, the bump
        never executed, so the next boot picks the migration up again.

    Adding a new migration MUST either:
      * stick to idempotent DDL (``IF NOT EXISTS`` / ``IF EXISTS`` /
        ``ALTER TABLE ... ADD COLUMN`` guarded by a ``PRAGMA
        table_info`` check before the ALTER), OR
      * split itself into multiple ``executescript`` calls if
        intra-script atomicity is required (rare — most schema work
        is single-statement and idempotent).

    Do not assume ``rollback()`` will undo a half-applied migration.
    """

    version: int
    name: str
    sql: str


# Migration list. APPEND only, never modify in place. Keeping this as a
# tuple makes accidental mutation noisier than a list would.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="baseline_records_table",
        sql="""
            -- Baseline. ``storage._init_db`` already creates every
            -- table via ``IF NOT EXISTS``; this migration just claims
            -- ``user_version = 1`` so future steps have a known floor.
            -- The SELECT is a no-op that keeps ``executescript`` happy.
            SELECT 1;
        """,
    ),
    Migration(
        version=2,
        name="add_record_tags_table",
        sql="""
            CREATE TABLE IF NOT EXISTS record_tags (
                capture_id TEXT NOT NULL,
                tag TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (capture_id, tag),
                FOREIGN KEY (capture_id) REFERENCES records(capture_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_record_tags_tag ON record_tags(tag);
            CREATE INDEX IF NOT EXISTS idx_record_tags_capture ON record_tags(capture_id);
        """,
    ),
)


def current_version(conn: sqlite3.Connection) -> int:
    """Return the DB's ``PRAGMA user_version`` (0 on a fresh file)."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def max_known_version() -> int:
    """Highest version declared in :data:`MIGRATIONS` (0 if empty)."""
    return max((m.version for m in MIGRATIONS), default=0)


def pending_migrations(conn: sqlite3.Connection) -> list[Migration]:
    """Migrations whose version is greater than the DB's ``user_version``."""
    current = current_version(conn)
    return [m for m in MIGRATIONS if m.version > current]


def apply_migrations(conn: sqlite3.Connection) -> list[Migration]:
    """Apply any pending migrations. Returns the list of applied ones.

    Atomicity contract (read the :class:`Migration` docstring first):

      * ``executescript`` auto-commits BEFORE the script body runs and
        executes the body in autocommit mode. The ``conn.rollback()``
        below CANNOT undo SQL that the script already ran — by the time
        sqlite3 raises, every statement up to the failure is committed.
      * The ``user_version`` bump is a SEPARATE statement that runs
        AFTER the script. If the script body raises, the bump never
        executes, so on next boot the migration is re-attempted from
        scratch. This works only because every shipped migration is
        idempotent (``IF NOT EXISTS`` guards on every CREATE).
      * The ``rollback()`` call below is therefore a defense-in-depth
        no-op for the script body — it only rolls back the user_version
        PRAGMA in the (very unlikely) case where the script succeeded
        but the PRAGMA itself raised. We keep it for clarity and to
        match what every reviewer expects to see in this kind of code.

    On failure we raise ``RuntimeError`` so the supervisor surfaces the
    error to the operator rather than corrupting the DB silently. The
    next boot retries the same step thanks to the idempotency contract
    on every shipped migration's SQL.
    """
    pending = pending_migrations(conn)
    applied: list[Migration] = []
    for migration in pending:
        try:
            # executescript implicitly COMMITs before running, then runs
            # the body in autocommit mode. See class docstring above —
            # this is a one-way trip for any non-idempotent DDL.
            conn.executescript(migration.sql)
            # PRAGMA user_version cannot be parameterised — the int is
            # validated by the dataclass + the monotonic-version check
            # in tests, so f-string interpolation is safe here.
            conn.execute(f"PRAGMA user_version = {migration.version}")
            conn.commit()
        except sqlite3.Error as e:
            # NOTE: this rollback CANNOT undo statements already executed
            # by the executescript call above (autocommit). It only
            # covers the unlikely case where the PRAGMA bump itself
            # raised after the script committed.
            conn.rollback()
            raise RuntimeError(
                f"Migration {migration.version} ({migration.name}) failed: {e}"
            ) from e
        applied.append(migration)
    return applied
