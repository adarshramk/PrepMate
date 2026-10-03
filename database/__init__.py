"""
database/__init__.py
Thin SQLite helper. Uses sqlite3 from the standard library — no ORM needed for M1.

For in-memory databases (used during testing) we keep a single connection alive
on the app object so that all requests share the same database instance.
For file-based databases each call gets a fresh connection (WAL mode is safe).
"""

import logging
import sqlite3
import os

logger = logging.getLogger(__name__)


def get_db(app):
    """
    Return a sqlite3 connection for the current app.

    - :memory:  → returns the same persistent connection stored on the app,
                  so the schema created during init_db is visible to all callers.
    - file path → returns a new connection each time (standard pattern).
    """
    db_path = app.config["DATABASE_PATH"]

    if db_path == ":memory:":
        # Reuse the single in-memory connection attached to the app
        if not hasattr(app, "_db_conn"):
            conn = sqlite3.connect(":memory:", check_same_thread=False)
            conn.row_factory = sqlite3.Row
            app._db_conn = conn
        return app._db_conn

    # File-based DB: ensure directory exists, open fresh connection
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(app):
    """
    Create tables from schema.sql if they don't exist, then apply any
    backward-compatible column migrations for existing databases.

    Safe to call multiple times — all schema statements use IF NOT EXISTS
    and migrations only add columns when they are absent.
    """
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    db_path = app.config["DATABASE_PATH"]

    if db_path == ":memory:":
        # Ensure the persistent in-memory connection exists, then run schema
        conn = get_db(app)
        with open(schema_path, "r") as f:
            conn.executescript(f.read())
    else:
        with get_db(app) as conn:
            with open(schema_path, "r") as f:
                conn.executescript(f.read())

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
    Add `column` to `table` only if it is not already present.
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
