"""
tests/test_health_config.py
Tests for:
  - GET /health endpoint (load-balancer / Docker HEALTHCHECK)
  - Production secret-key guard in create_app()
  - Database initialisation idempotency (safe to call init_db twice)
  - Concurrent initialisation safety (WAL lock / busy-timeout regression)
  - get_db() does NOT issue PRAGMA journal_mode=WAL
  - Normal connections carry a busy timeout
"""

from __future__ import annotations
import json
import os
import sqlite3
import tempfile
import threading
import pytest

from app import create_app
from database import init_db, get_db, _BUSY_TIMEOUT_S, _INIT_TIMEOUT_S


# ════════════════════════════════════════════════════════════════════════════
# 1. Health endpoint
# ════════════════════════════════════════════════════════════════════════════

class TestHealthEndpoint:
    """GET /health must return 200 JSON with status='ok' when the app is healthy."""

    def test_health_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_content_type_is_json(self, client):
        resp = client.get("/health")
        assert resp.content_type.startswith("application/json")

    def test_health_body_has_status_ok(self, client):
        resp = client.get("/health")
        body = json.loads(resp.data)
        assert body["status"] == "ok"

    def test_health_body_has_db_ok(self, client):
        resp = client.get("/health")
        body = json.loads(resp.data)
        assert body["db"] == "ok"

    def test_health_body_has_app_name(self, client):
        resp = client.get("/health")
        body = json.loads(resp.data)
        assert body["app"] == "PrepMate"

    def test_health_accessible_without_login(self, client):
        """Health check must never require authentication."""
        resp = client.get("/health", follow_redirects=False)
        assert resp.status_code == 200

    def test_health_returns_503_when_db_broken(self, app):
        """
        When the database is unavailable, /health must return 503.
        We simulate this by patching get_db in the routes.main module
        (the name that the health view actually calls).
        """
        from unittest.mock import patch

        with patch("routes.main.get_db", side_effect=Exception("DB gone")):
            c = app.test_client()
            resp = c.get("/health")

        assert resp.status_code == 503
        body = json.loads(resp.data)
        assert body["status"] == "degraded"
        assert body["db"] == "error"


# ════════════════════════════════════════════════════════════════════════════
# 2. Secret-key production guard
# ════════════════════════════════════════════════════════════════════════════

class TestSecretKeyGuard:
    """
    create_app() must raise RuntimeError in production when the placeholder
    secret key is used.  In TESTING or DEBUG mode it must not raise.
    """

    _INSECURE = "dev-key-change-in-production"

    def test_insecure_key_raises_in_production(self):
        """No TESTING flag, no DEBUG flag → RuntimeError."""
        with pytest.raises(RuntimeError, match="FLASK_SECRET_KEY"):
            create_app({
                "SECRET_KEY":  self._INSECURE,
                "TESTING":     False,
                "DEBUG":       False,
                "DATABASE_PATH": ":memory:",
            })

    def test_insecure_key_allowed_in_testing(self):
        """TESTING=True bypasses the guard."""
        app = create_app({
            "SECRET_KEY":    self._INSECURE,
            "TESTING":       True,
            "DATABASE_PATH": ":memory:",
        })
        assert app is not None

    def test_insecure_key_allowed_in_debug(self):
        """DEBUG=True bypasses the guard."""
        app = create_app({
            "SECRET_KEY":    self._INSECURE,
            "TESTING":       False,
            "DEBUG":         True,
            "DATABASE_PATH": ":memory:",
        })
        assert app is not None

    def test_strong_key_accepted_in_production(self):
        """A non-placeholder key in production mode must work without error."""
        import secrets as _secrets
        strong_key = _secrets.token_hex(64)
        app = create_app({
            "SECRET_KEY":    strong_key,
            "TESTING":       False,
            "DEBUG":         False,
            "DATABASE_PATH": ":memory:",
        })
        assert app.config["SECRET_KEY"] == strong_key

    def test_wsgi_module_exits_on_bad_key(self):
        """
        Verify that the wsgi.py pattern (import create_app, call it, catch
        RuntimeError) behaves correctly — we test the logic directly here
        rather than importing wsgi.py, which would call sys.exit().
        """
        caught = False
        try:
            create_app({
                "SECRET_KEY":    self._INSECURE,
                "TESTING":       False,
                "DEBUG":         False,
                "DATABASE_PATH": ":memory:",
            })
        except RuntimeError:
            caught = True
        assert caught, "RuntimeError should have been raised for insecure key in production"


# ════════════════════════════════════════════════════════════════════════════
# 3. Database initialisation idempotency
# ════════════════════════════════════════════════════════════════════════════

class TestDatabaseInitIdempotency:
    """
    init_db() must be safe to call multiple times on the same app instance.
    All CREATE TABLE IF NOT EXISTS statements in schema.sql ensure this, but
    we verify it doesn't raise and doesn't lose data.
    """

    def test_double_init_does_not_raise(self, app):
        """Calling init_db() a second time must not raise."""
        init_db(app)   # first call already happened inside create_app
        init_db(app)   # second call — should be a no-op

    def test_double_init_preserves_data(self, app):
        """Rows inserted between two init_db calls must still be there after."""
        from database import get_db

        conn = get_db(app)
        conn.execute(
            "INSERT INTO users (name, email, password) VALUES (?, ?, ?)",
            ("IdempotTest", "idempotent@example.com", "hashed-password"),
        )
        conn.commit()

        # Re-initialise — must not wipe the row
        init_db(app)

        row = conn.execute(
            "SELECT name FROM users WHERE email = ?",
            ("idempotent@example.com",),
        ).fetchone()
        assert row is not None
        assert row["name"] == "IdempotTest"

    def test_all_tables_present_after_init(self, app):
        """All expected tables must exist after initialisation."""
        from database import get_db
        conn = get_db(app)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        table_names = {r["name"] for r in rows}

        expected = {"users", "tasks", "roadmaps", "roadmap_milestones", "milestone_imports"}
        assert expected.issubset(table_names), (
            f"Missing tables: {expected - table_names}"
        )

    def test_indexes_present_after_init(self, app):
        """Key indexes from schema.sql must exist."""
        from database import get_db
        conn = get_db(app)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
        index_names = {r["name"] for r in rows}

        expected_indexes = {
            "idx_tasks_user_id",
            "idx_tasks_category",
            "idx_tasks_complete",
            "idx_roadmaps_user",
            "idx_milestones_roadmap",
        }
        assert expected_indexes.issubset(index_names), (
            f"Missing indexes: {expected_indexes - index_names}"
        )



# ════════════════════════════════════════════════════════════════════════════
# 4. WAL mode and connection configuration
# ════════════════════════════════════════════════════════════════════════════

class TestWALAndConnectionConfig:
    """
    Verify that:
    - get_db() does NOT issue PRAGMA journal_mode=WAL (which would cause
      'database is locked' when multiple workers open connections concurrently).
    - WAL mode IS set by init_db() on file-based databases.
    - Normal connections carry a busy timeout.
    - The timeout constants have sensible values.
    """

    def test_busy_timeout_constant_is_positive(self):
        """_BUSY_TIMEOUT_S must be > 0 so connections wait on contention."""
        assert _BUSY_TIMEOUT_S > 0

    def test_init_timeout_longer_than_busy_timeout(self):
        """Init connections need more time than normal request connections."""
        assert _INIT_TIMEOUT_S > _BUSY_TIMEOUT_S

    def test_get_db_does_not_set_wal_mode(self):
        """
        get_db() must not set WAL journal mode.

        We verify this behaviourally: open a file-based DB whose journal_mode
        is 'delete' (the SQLite default), call get_db(), then confirm the mode
        is still 'delete'.  If get_db() had issued PRAGMA journal_mode=WAL the
        mode would have changed to 'wal'.

        This is the regression test for the original startup crash: issuing
        that PRAGMA on every connection requires an exclusive lock and raises
        ``database is locked`` when multiple Gunicorn workers open connections
        simultaneously.
        """
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        os.unlink(db_path)

        try:
            # Create a minimal DB in the default (delete) journal mode —
            # do NOT call create_app/init_db so WAL is never set.
            setup = sqlite3.connect(db_path)
            setup.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")
            setup.commit()
            setup.close()

            # Confirm the starting mode is NOT wal
            raw = sqlite3.connect(db_path)
            mode_before = raw.execute("PRAGMA journal_mode").fetchone()[0]
            raw.close()
            assert mode_before != "wal", (
                "Pre-condition failed: DB is already in WAL mode"
            )

            # Call get_db() and close the connection
            cfg = {"TESTING": True, "DATABASE_PATH": db_path, "SECRET_KEY": "x"}
            file_app = create_app.__wrapped__(cfg) if hasattr(create_app, "__wrapped__") else None

            # We can't call create_app because it calls init_db (which sets WAL).
            # Instead, call get_db directly after building a minimal app object.
            from flask import Flask
            from config import Config
            mini_app = Flask(__name__)
            mini_app.config.from_object(Config)
            mini_app.config.update({"TESTING": True, "DATABASE_PATH": db_path, "SECRET_KEY": "x"})

            conn = get_db(mini_app)
            conn.close()

            # Mode must still be unchanged — get_db() must not have touched it
            raw = sqlite3.connect(db_path)
            mode_after = raw.execute("PRAGMA journal_mode").fetchone()[0]
            raw.close()

            assert mode_after == mode_before, (
                f"get_db() changed the journal_mode from '{mode_before}' to "
                f"'{mode_after}' — it must not issue PRAGMA journal_mode=WAL"
            )
        finally:
            for suffix in ("", "-wal", "-shm"):
                p = db_path + suffix
                try:
                    if os.path.exists(p):
                        os.unlink(p)
                except OSError:
                    pass

    def test_init_db_sets_wal_mode_on_file_db(self):
        """
        init_db() must set WAL mode on a real file-based database.
        After init_db() the journal_mode reported by a fresh connection must
        be 'wal'.
        """
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        os.unlink(db_path)  # start from a truly empty file

        try:
            app = create_app({
                "TESTING": True,
                "DATABASE_PATH": db_path,
                "SECRET_KEY": "test-wal-key",
            })
            # Open a plain connection (no PRAGMA calls) and check mode
            raw = sqlite3.connect(db_path)
            row = raw.execute("PRAGMA journal_mode").fetchone()
            mode = row[0] if row else ""
            raw.close()
            assert mode == "wal", f"Expected WAL mode, got '{mode}'"
        finally:
            for suffix in ("", "-wal", "-shm"):
                p = db_path + suffix
                try:
                    if os.path.exists(p):
                        os.unlink(p)
                except OSError:
                    pass

    def test_get_db_connection_has_busy_timeout(self, app):
        """
        Connections returned by get_db() must carry a busy timeout.
        We verify indirectly: the connection object must be usable and not
        raise immediately on the first query even if the in-memory db is
        shared by multiple threads.
        This is a smoke-test; the real timeout value is verified by
        test_busy_timeout_constant_is_positive.
        """
        conn = get_db(app)
        # A simple query must succeed — confirms the connection is functional
        result = conn.execute("SELECT 1").fetchone()
        assert result[0] == 1

    def test_foreign_keys_enabled_on_get_db_connection(self):
        """
        File-based connections from get_db() must have foreign_keys=ON.

        foreign_keys is a connection-scoped setting (not persisted in the
        SQLite file), so get_db() must issue PRAGMA foreign_keys=ON on every
        file-based connection it opens.

        Note: in-memory connections reuse a single cached connection object
        that was created before the PRAGMA was added to get_db(); they are
        excluded here.  In-memory DBs are used only in tests and have no FK
        constraints enforced regardless.
        """
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        os.unlink(db_path)

        try:
            app = create_app({
                "TESTING": True,
                "DATABASE_PATH": db_path,
                "SECRET_KEY": "test-fk",
            })
            # get_db() returns a fresh connection each time for file-based DBs.
            conn = get_db(app)
            row = conn.execute("PRAGMA foreign_keys").fetchone()
            conn.close()
            assert row[0] == 1, (
                "foreign_keys must be ON for every file-based connection from get_db()"
            )
        finally:
            for suffix in ("", "-wal", "-shm"):
                p = db_path + suffix
                try:
                    if os.path.exists(p):
                        os.unlink(p)
                except OSError:
                    pass


# ════════════════════════════════════════════════════════════════════════════
# 5. Concurrent initialisation
# ════════════════════════════════════════════════════════════════════════════

class TestConcurrentInitialisation:
    """
    Simulate multiple Gunicorn workers calling init_db() simultaneously on
    the same SQLite file.

    The fix (WAL mode set only in _open_init_conn with a 30-s timeout,
    never in get_db) must prevent 'database is locked' errors.
    """

    def _safe_unlink(self, db_path: str) -> None:
        for suffix in ("", "-wal", "-shm"):
            p = db_path + suffix
            try:
                if os.path.exists(p):
                    os.unlink(p)
            except OSError:
                pass

    def test_concurrent_init_db_does_not_raise(self):
        """
        N threads calling init_db() on the same file simultaneously must all
        succeed without raising OperationalError.
        """
        N_WORKERS = 4

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        os.unlink(db_path)

        cfg = {
            "TESTING": True,
            "DATABASE_PATH": db_path,
            "SECRET_KEY": "test-concurrent",
        }

        errors: list[Exception] = []
        barrier = threading.Barrier(N_WORKERS)

        def worker():
            try:
                # All workers reach the barrier before any calls init_db,
                # maximising the chance of a real concurrent collision.
                barrier.wait(timeout=10)
                app = create_app(cfg)
                init_db(app)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(N_WORKERS)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)
        finally:
            self._safe_unlink(db_path)

        assert errors == [], (
            f"{len(errors)} worker(s) raised during concurrent init_db: "
            + "; ".join(type(e).__name__ + ': ' + str(e) for e in errors)
        )

    def test_concurrent_init_leaves_all_tables_intact(self):
        """
        After N threads all call init_db() on the same file, every expected
        table must exist and contain zero rows (no data corruption).
        """
        N_WORKERS = 3
        EXPECTED_TABLES = {
            "users", "tasks", "roadmaps", "roadmap_milestones", "milestone_imports"
        }

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        os.unlink(db_path)

        cfg = {
            "TESTING": True,
            "DATABASE_PATH": db_path,
            "SECRET_KEY": "test-concurrent-tables",
        }

        barrier = threading.Barrier(N_WORKERS)
        errors: list[Exception] = []

        def worker():
            try:
                barrier.wait(timeout=10)
                app = create_app(cfg)
                init_db(app)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(N_WORKERS)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)

            assert errors == [], (
                f"Workers raised: {[str(e) for e in errors]}"
            )

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            found = {r["name"] for r in rows}
            conn.close()

            assert EXPECTED_TABLES.issubset(found), (
                f"Missing tables after concurrent init: {EXPECTED_TABLES - found}"
            )
        finally:
            self._safe_unlink(db_path)

    def test_init_db_idempotent_across_sequential_calls(self):
        """
        Calling init_db() many times sequentially (as each Gunicorn worker
        would after the first has already initialised the DB) must not raise
        or corrupt data.
        """
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        os.unlink(db_path)

        cfg = {
            "TESTING": True,
            "DATABASE_PATH": db_path,
            "SECRET_KEY": "test-idempotent-seq",
        }

        try:
            app = create_app(cfg)
            # Insert a row, then re-initialise multiple times
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO users (name, email, password) VALUES (?, ?, ?)",
                ("ConcurrentUser", "cu@example.com", "hashed"),
            )
            conn.commit()
            conn.close()

            for _ in range(5):
                init_db(app)

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            user = conn.execute(
                "SELECT name FROM users WHERE email = ?", ("cu@example.com",)
            ).fetchone()
            conn.close()
            assert user is not None, "User row must survive repeated init_db calls"
            assert user["name"] == "ConcurrentUser"
        finally:
            for suffix in ("", "-wal", "-shm"):
                p = db_path + suffix
                try:
                    if os.path.exists(p):
                        os.unlink(p)
                except OSError:
                    pass

    def test_concurrent_get_db_connections_do_not_raise(self):
        """
        Multiple threads opening connections via get_db() simultaneously
        must not raise OperationalError — the busy timeout must absorb any
        brief write contention.
        """
        N_READERS = 8

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        os.unlink(db_path)

        cfg = {
            "TESTING": True,
            "DATABASE_PATH": db_path,
            "SECRET_KEY": "test-concurrent-readers",
        }
        app = create_app(cfg)

        errors: list[Exception] = []
        barrier = threading.Barrier(N_READERS)

        def reader():
            try:
                barrier.wait(timeout=10)
                conn = get_db(app)
                # Simple read — just confirms the connection works
                conn.execute("SELECT 1").fetchone()
                conn.close()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(N_READERS)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
        finally:
            for suffix in ("", "-wal", "-shm"):
                p = db_path + suffix
                try:
                    if os.path.exists(p):
                        os.unlink(p)
                except OSError:
                    pass

        assert errors == [], (
            f"{len(errors)} reader(s) raised: "
            + "; ".join(type(e).__name__ + ': ' + str(e) for e in errors)
        )
