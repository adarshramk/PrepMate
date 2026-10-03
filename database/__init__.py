"""
database/__init__.py
Thin SQLite helper. Uses sqlite3 from the standard library — no ORM needed for M1.

For in-memory databases (used during testing) we keep a single connection alive
on the app object so that all requests share the same database instance.
For file-based databases each call gets a fresh connection (standard pattern).

WAL-mode and concurrency notes
------------------------------
``PRAGMA journal_mode=WAL`` is a *database-level* persistent setting: once set
on a file it survives across connections and restarts.  However, switching to
WAL mode on a brand-new (empty) file requires an exclusive lock, and SQLite
does not honour the Python-level ``connect(timeout=)`` or even
``PRAGMA busy_timeout`` for this particular operation on CPython's built-in
sqlite3 module.  As a result, when multiple Gunicorn workers call
``PRAGMA journal_mode=WAL`` simultaneously at first startup, all but one raise
``sqlite3.OperationalError: database is locked``.

Fix:
1.  ``get_db()`` never issues ``PRAGMA journal_mode=WAL`` — doing so on every
    connection is both unnecessary (WAL is persistent) and causes the lock crash.
2.  ``_set_wal_with_retry()`` in ``init_db()`` sets WAL mode once, using an
    autocommit connection and a small retry loop with exponential backoff.
    Once any worker succeeds, the WAL setting is already persisted and
    subsequent retries by other workers are no-ops (SQLite returns 'wal'
    idempotently).
3.  ``PRAGMA busy_timeout`` is set on every connection (both init and normal)
    so that ordinary write contention during request handling causes a brief
    wait rather than an immediate crash.
4.  ``PRAGMA foreign_keys`` is connection-scoped (not persisted) and is set on
    every connection returned by ``get_db()``.
"""

from __future__ import annotations
import logging
import sqlite3
import os
import time

logger = logging.getLogger(__name__)

# Busy timeout for normal request connections (milliseconds).
# SQLite retries for this duration before raising OperationalError.
_BUSY_TIMEOUT_MS = 5_000        # 5 seconds

# Busy timeout used during init_db() connections (milliseconds).
_INIT_BUSY_TIMEOUT_MS = 30_000  # 30 seconds

# Exposed as module-level constants so tests can assert sensible values.
_BUSY_TIMEOUT_S = _BUSY_TIMEOUT_MS / 1000
_INIT_TIMEOUT_S = _INIT_BUSY_TIMEOUT_MS / 1000

# Maximum number of retries for the WAL PRAGMA when a lock is encountered.
_WAL_MAX_RETRIES = 20


def get_db(app):
    """
    Return a sqlite3 connection for the current app.

    - :memory:  → returns the same persistent connection stored on the app,
                  so the schema created during init_db is visible to all callers.
    - file path → returns a new connection each time (standard pattern).

    WAL mode is NOT issued here.  It is set once in ``init_db()`` via
    ``_set_wal_with_retry()``.  Issuing it here would require an exclusive lock
    on every connection open and causes ``database is locked`` when multiple
    Gunicorn workers start concurrently.
    """
    db_path = app.config["DATABASE_PATH"]

    if db_path == ":memory:":
        # Reuse the single in-memory connection attached to the app.
        if not hasattr(app, "_db_conn"):
            conn = sqlite3.connect(":memory:", check_same_thread=False)
            conn.row_factory = sqlite3.Row
            app._db_conn = conn
        return app._db_conn

    # File-based DB: ensure the directory exists then open a fresh connection.
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    # PRAGMA busy_timeout works at the SQLite layer and covers cases that the
    # Python-level timeout= does not (e.g. inside executescript).
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    # foreign_keys is connection-scoped (not persisted in the file).
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _set_wal_with_retry(db_path: str) -> None:
    """
    Set WAL journal mode on a file-based database, with retry on lock.

    Uses an *autocommit* connection (``isolation_level=None``) so no
    implicit BEGIN transaction is held during the PRAGMA.  Retries up to
    ``_WAL_MAX_RETRIES`` times with exponential backoff.

    This is idempotent: if WAL is already set, SQLite returns 'wal' without
    acquiring a lock, so subsequent calls by other workers are instant no-ops.
    """
    for attempt in range(_WAL_MAX_RETRIES):
        try:
            # autocommit so the PRAGMA is not wrapped in a transaction
            conn = sqlite3.connect(db_path, timeout=5, isolation_level=None)
            conn.execute(f"PRAGMA busy_timeout={_INIT_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.close()
            return  # success
        except sqlite3.OperationalError:
            if attempt < _WAL_MAX_RETRIES - 1:
                # Exponential back-off: 50 ms, 100 ms, 150 ms, …
                time.sleep(0.05 * (attempt + 1))
            else:
                raise  # exhausted retries — re-raise the last error


def _open_init_conn(db_path: str) -> sqlite3.Connection:
    """
    Open a connection used only during database initialisation (schema + migrations).

    Sets a generous busy timeout at both the Python and SQLite levels so that
    concurrent Gunicorn workers queue rather than crash.
    """
    conn = sqlite3.connect(db_path, timeout=_INIT_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={_INIT_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(app):
    """
    Create tables from schema.sql if they don't exist, then apply any
    backward-compatible column migrations for existing databases.

    Safe to call multiple times — all schema statements use IF NOT EXISTS
    and migrations only add columns when they are absent.

    Concurrent safety: multiple Gunicorn workers may call init_db() at the
    same time on first deployment.  ``_set_wal_with_retry()`` serialises the
    WAL-mode switch via retry, and the generous busy timeouts on subsequent
    connections allow schema operations to queue rather than crash.
    """
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    db_path = app.config["DATABASE_PATH"]

    if db_path == ":memory:":
        # In-memory DBs are per-process; WAL mode is irrelevant.
        conn = get_db(app)
        with open(schema_path, "r") as f:
            conn.executescript(f.read())
    else:
        # 1. Set WAL mode first (retry-safe, autocommit connection).
        _set_wal_with_retry(db_path)

        # 2. Run the schema with a normal connection that carries a long timeout.
        conn = _open_init_conn(db_path)
        try:
            with open(schema_path, "r") as f:
                conn.executescript(f.read())
        finally:
            conn.close()

    _migrate(app)


# ── Backward-compatible migrations ─────────────────────────────────────────

def _migrate(app):
    """
    Apply additive schema changes to an existing database.

    Each migration is guarded by an existence check so it is safe to run
    on fresh databases (where the column is already present) and on old
    databases (where it must be added).

    Never drops or recreates tables — preserves all existing data.
    """
    conn = get_db(app)
    _add_column_if_missing(
        conn,
        table="roadmaps",
        column="failure_reason",
        definition="TEXT",  # nullable — NULL means AI succeeded or not attempted
    )


def _add_column_if_missing(conn, table: str, column: str, definition: str) -> None:
    """
    Add ``column`` to ``table`` only if it is not already present.
    Uses PRAGMA table_info() which is safe on all SQLite versions.
    """
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    existing_columns = {row[1] for row in rows}  # column name is index 1

    if column not in existing_columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.commit()
        logger.info(
            "Migration applied: added column '%s' to table '%s'.",
            column, table,
        )
    # If the column already exists, do nothing — no log noise.
