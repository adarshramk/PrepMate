"""
tests/test_health_config.py
Tests for:
  - GET /health endpoint (load-balancer / Docker HEALTHCHECK)
  - Production secret-key guard in create_app()
  - Database initialisation idempotency (safe to call init_db twice)
"""

from __future__ import annotations
import json
import pytest

from app import create_app
from database import init_db


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
