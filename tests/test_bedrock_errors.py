"""
tests/test_bedrock_errors.py
Tests for Bedrock error classification, fallback persistence, and DB migration.

All AWS calls are mocked — no real Bedrock requests are ever made.

Scenarios covered
-----------------
1.  No credentials available               → failure_reason='no_credentials'
2.  Partial credentials (one key missing)  → failure_reason='auth'
3.  Explicit credentials forwarded         → source='ai', failure_reason=None
4.  Standard boto3 credential chain        → Session called without explicit keys
5.  AccessDeniedException                  → failure_reason='auth'
6.  ValidationException / Operation not allowed  → failure_reason='config'
7.  Invalid / unavailable model ID         → failure_reason='config'
8.  ResourceNotFoundException              → failure_reason='config'
9.  ServiceUnavailableException            → failure_reason='transient'
10. ThrottlingException                    → failure_reason='transient'
11. ConnectTimeoutError                    → failure_reason='transient'
12. Malformed / empty JSON output          → failure_reason='parse'
13. Empty milestones list                  → failure_reason='parse'
14. Successful AI response                 → source='ai', failure_reason=None
15. Fallback roadmap validity              → all required keys, valid categories
16. Persistence of failure_reason in DB   → row-level verification
17. DB migration: existing schema without failure_reason column
18. Fresh DB: failure_reason column present from the start
19. Existing data preserved through migration
20. Idempotent migration (column already present)
"""

from __future__ import annotations
import json
import sqlite3
import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock

from app import create_app
from database import init_db, get_db
from services.llm import (
    generate_roadmap,
    _categorise_bedrock_error,
    _log_bedrock_failure,
    _rule_based_fallback,
)
from models.planner import save_roadmap, get_roadmap
from models.tasks import CATEGORIES


# ── Helpers ────────────────────────────────────────────────────────────────

def _base_config(**overrides) -> dict:
    cfg = {
        "TESTING":               True,
        "DATABASE_PATH":         ":memory:",
        "SECRET_KEY":            "test-secret-key",
        "WTF_CSRF_ENABLED":      False,
        "BEDROCK_REGION":        "us-east-1",
        "BEDROCK_MODEL_ID":      "amazon.nova-micro-v1:0",
        "BEDROCK_TIMEOUT":       "30",
        "AWS_ACCESS_KEY_ID":     "",
        "AWS_SECRET_ACCESS_KEY": "",
    }
    cfg.update(overrides)
    return cfg


def _make_converse_response(target_role: str = "SDE", weeks: int = 2) -> dict:
    roadmap_json = json.dumps({
        "title": f"{target_role} Roadmap",
        "milestones": [
            {
                "week_number": i + 1,
                "title": f"Step {i + 1}",
                "priority": "medium",
                "category": CATEGORIES[i % len(CATEGORIES)],
                "description": "A test milestone.",
                "daily_tasks": ["Task A", "Task B", "Task C"],
            }
            for i in range(weeks * 2)
        ],
    })
    return {"output": {"message": {"content": [{"text": roadmap_json}]}}}


def _make_session_mock_raising(exc: Exception):
    """Return a patched boto3.Session whose converse() raises exc."""
    mock_session_cls = MagicMock()
    mock_session = MagicMock()
    mock_session_cls.return_value = mock_session
    mock_client = MagicMock()
    mock_session.client.return_value = mock_client
    mock_client.converse.side_effect = exc
    return mock_session_cls


def _make_session_mock_success(target_role="SDE", weeks=2):
    """Return a patched boto3.Session whose converse() returns a valid response."""
    mock_session_cls = MagicMock()
    mock_session = MagicMock()
    mock_session_cls.return_value = mock_session
    mock_client = MagicMock()
    mock_session.client.return_value = mock_client
    mock_client.converse.return_value = _make_converse_response(target_role, weeks)
    return mock_session_cls


def _client_error(code: str, message: str = "test error") -> "botocore.exceptions.ClientError":
    import botocore.exceptions
    return botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": message}},
        "Converse",
    )


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture()
def app():
    return create_app(_base_config())


@pytest.fixture()
def app_with_creds():
    return create_app(_base_config(
        AWS_ACCESS_KEY_ID="AKIAIOSFODNN7EXAMPLE",
        AWS_SECRET_ACCESS_KEY="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    ))


# ══════════════════════════════════════════════════════════════════════════
# 1. _categorise_bedrock_error — unit tests
# ══════════════════════════════════════════════════════════════════════════

class TestCategoriseBedrockError:
    """Direct unit tests for _categorise_bedrock_error()."""

    def test_no_credentials_error(self):
        import botocore.exceptions
        exc = botocore.exceptions.NoCredentialsError()
        assert _categorise_bedrock_error(exc) == "no_credentials"

    def test_partial_credentials_error(self):
        import botocore.exceptions
        exc = botocore.exceptions.PartialCredentialsError(
            provider="env", cred_var="AWS_SECRET_ACCESS_KEY"
        )
        assert _categorise_bedrock_error(exc) == "auth"

    def test_access_denied_exception(self):
        assert _categorise_bedrock_error(_client_error("AccessDeniedException")) == "auth"

    def test_invalid_client_token(self):
        assert _categorise_bedrock_error(_client_error("InvalidClientTokenId")) == "auth"

    def test_expired_token_exception(self):
        assert _categorise_bedrock_error(_client_error("ExpiredTokenException")) == "auth"

    def test_unrecognized_client(self):
        assert _categorise_bedrock_error(_client_error("UnrecognizedClientException")) == "auth"

    def test_auth_failure(self):
        assert _categorise_bedrock_error(_client_error("AuthFailure")) == "auth"

    def test_validation_exception(self):
        """ValidationException (incl. 'Operation not allowed') → config."""
        assert _categorise_bedrock_error(
            _client_error("ValidationException", "Operation not allowed")
        ) == "config"

    def test_resource_not_found(self):
        assert _categorise_bedrock_error(_client_error("ResourceNotFoundException")) == "config"

    def test_model_not_ready(self):
        assert _categorise_bedrock_error(_client_error("ModelNotReadyException")) == "config"

    def test_model_error_exception(self):
        assert _categorise_bedrock_error(_client_error("ModelErrorException")) == "config"

    def test_service_unavailable(self):
        assert _categorise_bedrock_error(
            _client_error("ServiceUnavailableException")
        ) == "transient"

    def test_throttling_exception(self):
        assert _categorise_bedrock_error(_client_error("ThrottlingException")) == "transient"

    def test_model_timeout_exception(self):
        assert _categorise_bedrock_error(_client_error("ModelTimeoutException")) == "transient"

    def test_internal_server_exception(self):
        assert _categorise_bedrock_error(_client_error("InternalServerException")) == "transient"

    def test_connect_timeout(self):
        import botocore.exceptions
        exc = botocore.exceptions.ConnectTimeoutError(endpoint_url="https://bedrock.us-east-1.amazonaws.com")
        assert _categorise_bedrock_error(exc) == "transient"

    def test_read_timeout(self):
        import botocore.exceptions
        exc = botocore.exceptions.ReadTimeoutError(endpoint_url="https://bedrock.us-east-1.amazonaws.com")
        assert _categorise_bedrock_error(exc) == "transient"

    def test_endpoint_connection_error(self):
        import botocore.exceptions
        exc = botocore.exceptions.EndpointConnectionError(endpoint_url="https://invalid")
        assert _categorise_bedrock_error(exc) == "transient"

    def test_unknown_client_error_code_falls_back_to_config(self):
        """An unrecognised ClientError code is treated as a config issue."""
        assert _categorise_bedrock_error(
            _client_error("SomeUnknownErrorCode")
        ) == "config"

    def test_generic_exception_is_transient(self):
        """An unexpected exception type falls back to 'transient'."""
        assert _categorise_bedrock_error(RuntimeError("unexpected")) == "transient"

    def test_import_error_without_botocore(self):
        """
        If botocore is not importable for some reason, categorisation should
        still return 'transient' rather than raising.
        """
        import builtins
        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "botocore.exceptions":
                raise ImportError("botocore not available")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            result = _categorise_bedrock_error(RuntimeError("boom"))
        assert result == "transient"


# ══════════════════════════════════════════════════════════════════════════
# 2. generate_roadmap failure_reason propagation
# ══════════════════════════════════════════════════════════════════════════

class TestGenerateRoadmapFailureReasons:
    """
    Verify that generate_roadmap() sets failure_reason correctly in all cases.
    """

    def test_no_credentials_sets_reason(self, app):
        import botocore.exceptions
        exc = botocore.exceptions.NoCredentialsError()
        with patch("boto3.Session", _make_session_mock_raising(exc)):
            with app.app_context():
                result = generate_roadmap(app, "SDE", "Python", 2.0, 4)
        assert result["source"] == "fallback"
        assert result["failure_reason"] == "no_credentials"

    def test_partial_credentials_sets_auth_reason(self, app):
        import botocore.exceptions
        exc = botocore.exceptions.PartialCredentialsError(
            provider="env", cred_var="AWS_SECRET_ACCESS_KEY"
        )
        with patch("boto3.Session", _make_session_mock_raising(exc)):
            with app.app_context():
                result = generate_roadmap(app, "SDE", "Python", 2.0, 4)
        assert result["failure_reason"] == "auth"

    def test_access_denied_sets_auth_reason(self, app):
        exc = _client_error("AccessDeniedException")
        with patch("boto3.Session", _make_session_mock_raising(exc)):
            with app.app_context():
                result = generate_roadmap(app, "SDE", "Python", 2.0, 4)
        assert result["failure_reason"] == "auth"

    def test_validation_exception_sets_config_reason(self, app):
        exc = _client_error("ValidationException", "Operation not allowed")
        with patch("boto3.Session", _make_session_mock_raising(exc)):
            with app.app_context():
                result = generate_roadmap(app, "SDE", "Python", 2.0, 4)
        assert result["failure_reason"] == "config"

    def test_invalid_model_id_sets_config_reason(self, app):
        exc = _client_error("ResourceNotFoundException", "Model not found")
        with patch("boto3.Session", _make_session_mock_raising(exc)):
            with app.app_context():
                result = generate_roadmap(app, "SDE", "Python", 2.0, 4)
        assert result["failure_reason"] == "config"

    def test_throttling_sets_transient_reason(self, app):
        exc = _client_error("ThrottlingException")
        with patch("boto3.Session", _make_session_mock_raising(exc)):
            with app.app_context():
                result = generate_roadmap(app, "SDE", "Python", 2.0, 4)
        assert result["failure_reason"] == "transient"

    def test_service_unavailable_sets_transient_reason(self, app):
        exc = _client_error("ServiceUnavailableException")
        with patch("boto3.Session", _make_session_mock_raising(exc)):
            with app.app_context():
                result = generate_roadmap(app, "SDE", "Python", 2.0, 4)
        assert result["failure_reason"] == "transient"

    def test_timeout_sets_transient_reason(self, app):
        import botocore.exceptions
        exc = botocore.exceptions.ConnectTimeoutError(
            endpoint_url="https://bedrock.us-east-1.amazonaws.com"
        )
        with patch("boto3.Session", _make_session_mock_raising(exc)):
            with app.app_context():
                result = generate_roadmap(app, "SDE", "Python", 2.0, 4)
        assert result["failure_reason"] == "transient"

    def test_malformed_json_sets_parse_reason(self, app):
        """Bedrock responds successfully but returns invalid JSON → parse."""
        mock_session_cls = MagicMock()
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_client = MagicMock()
        mock_session.client.return_value = mock_client
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "THIS IS NOT JSON {{{{"}]}}
        }
        with patch("boto3.Session", mock_session_cls):
            with app.app_context():
                result = generate_roadmap(app, "SDE", "Python", 2.0, 4)
        assert result["source"] == "fallback"
        assert result["failure_reason"] == "parse"

    def test_empty_milestones_sets_parse_reason(self, app):
        """Bedrock returns valid JSON but with an empty milestones list → parse."""
        mock_session_cls = MagicMock()
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_client = MagicMock()
        mock_session.client.return_value = mock_client
        empty_response = json.dumps({"title": "Empty", "milestones": []})
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": empty_response}]}}
        }
        with patch("boto3.Session", mock_session_cls):
            with app.app_context():
                result = generate_roadmap(app, "SDE", "Python", 2.0, 4)
        assert result["source"] == "fallback"
        assert result["failure_reason"] == "parse"

    def test_successful_ai_response_has_none_failure_reason(self, app):
        """When Bedrock succeeds, failure_reason must be None."""
        with patch("boto3.Session", _make_session_mock_success()):
            with app.app_context():
                result = generate_roadmap(app, "SDE", "Python", 2.0, 2)
        assert result["source"] == "ai"
        assert result["failure_reason"] is None

    def test_fallback_roadmap_never_raises(self, app):
        """generate_roadmap() must not propagate any exception."""
        with patch("boto3.Session", _make_session_mock_raising(Exception("boom"))):
            with app.app_context():
                try:
                    generate_roadmap(app, "SDE", "Python", 2.0, 4)
                except Exception:
                    pytest.fail("generate_roadmap() must not propagate exceptions")

    def test_fallback_roadmap_is_valid(self, app):
        """Fallback roadmap has all required keys and valid milestones."""
        with patch("boto3.Session", _make_session_mock_raising(Exception("any"))):
            with app.app_context():
                result = generate_roadmap(app, "Backend Engineer", "Python", 3.0, 8)
        assert "title" in result
        assert "source" in result
        assert "failure_reason" in result
        assert "milestones" in result
        assert result["source"] == "fallback"
        assert len(result["milestones"]) > 0
        for m in result["milestones"]:
            assert m["category"] in CATEGORIES
            assert m["priority"] in ("high", "medium", "low")
            assert isinstance(m["daily_tasks"], list)
            assert len(m["daily_tasks"]) > 0

    def test_explicit_credentials_forwarded_on_success(self, app_with_creds):
        """When both keys are set, they must be forwarded to boto3.Session."""
        with patch("boto3.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session
            mock_session.client.return_value = MagicMock(
                converse=MagicMock(return_value=_make_converse_response())
            )
            with app_with_creds.app_context():
                result = generate_roadmap(app_with_creds, "SDE", "Python", 2.0, 2)
        _, kwargs = mock_session_cls.call_args
        assert kwargs.get("aws_access_key_id") == "AKIAIOSFODNN7EXAMPLE"
        assert kwargs.get("aws_secret_access_key") == "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        assert result["source"] == "ai"

    def test_blank_credentials_use_standard_chain(self, app):
        """When credentials are blank, boto3.Session must not receive explicit keys."""
        with patch("boto3.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session
            mock_session.client.return_value = MagicMock(
                converse=MagicMock(return_value=_make_converse_response())
            )
            with app.app_context():
                generate_roadmap(app, "SDE", "Python", 2.0, 2)
        _, kwargs = mock_session_cls.call_args
        assert "aws_access_key_id" not in kwargs
        assert "aws_secret_access_key" not in kwargs


# ══════════════════════════════════════════════════════════════════════════
# 3. Persistence of failure_reason
# ══════════════════════════════════════════════════════════════════════════

class TestFailureReasonPersistence:
    """Verify failure_reason is stored and retrieved correctly from the DB."""

    def test_failure_reason_persisted_as_auth(self, app):
        rid = save_roadmap(
            app, 1, "SDE", "Python", 2.0, 4, "Test", "fallback",
            [{"week_number": 1, "title": "M1", "priority": "medium",
              "category": CATEGORIES[0], "description": "", "daily_tasks": ["T1"], "position": 0}],
            failure_reason="auth",
        )
        row = get_roadmap(app, rid, 1)
        assert row["failure_reason"] == "auth"

    def test_failure_reason_persisted_as_config(self, app):
        rid = save_roadmap(
            app, 1, "SDE", "Python", 2.0, 4, "Test", "fallback",
            [{"week_number": 1, "title": "M1", "priority": "medium",
              "category": CATEGORIES[0], "description": "", "daily_tasks": ["T1"], "position": 0}],
            failure_reason="config",
        )
        row = get_roadmap(app, rid, 1)
        assert row["failure_reason"] == "config"

    def test_failure_reason_persisted_as_no_credentials(self, app):
        rid = save_roadmap(
            app, 1, "SDE", "Python", 2.0, 4, "Test", "fallback",
            [{"week_number": 1, "title": "M1", "priority": "medium",
              "category": CATEGORIES[0], "description": "", "daily_tasks": ["T1"], "position": 0}],
            failure_reason="no_credentials",
        )
        row = get_roadmap(app, rid, 1)
        assert row["failure_reason"] == "no_credentials"

    def test_failure_reason_persisted_as_transient(self, app):
        rid = save_roadmap(
            app, 1, "SDE", "Python", 2.0, 4, "Test", "fallback",
            [{"week_number": 1, "title": "M1", "priority": "medium",
              "category": CATEGORIES[0], "description": "", "daily_tasks": ["T1"], "position": 0}],
            failure_reason="transient",
        )
        row = get_roadmap(app, rid, 1)
        assert row["failure_reason"] == "transient"

    def test_failure_reason_persisted_as_parse(self, app):
        rid = save_roadmap(
            app, 1, "SDE", "Python", 2.0, 4, "Test", "fallback",
            [{"week_number": 1, "title": "M1", "priority": "medium",
              "category": CATEGORIES[0], "description": "", "daily_tasks": ["T1"], "position": 0}],
            failure_reason="parse",
        )
        row = get_roadmap(app, rid, 1)
        assert row["failure_reason"] == "parse"

    def test_ai_roadmap_has_null_failure_reason(self, app):
        """AI-generated roadmap must have NULL (None) failure_reason."""
        rid = save_roadmap(
            app, 1, "SDE", "Python", 2.0, 4, "AI Roadmap", "ai",
            [{"week_number": 1, "title": "M1", "priority": "medium",
              "category": CATEGORIES[0], "description": "", "daily_tasks": ["T1"], "position": 0}],
            failure_reason=None,
        )
        row = get_roadmap(app, rid, 1)
        assert row["failure_reason"] is None

    def test_failure_reason_persisted_via_generate_roadmap(self, app):
        """
        End-to-end: failure_reason from generate_roadmap() must be stored
        correctly when save_roadmap() is called with it.
        """
        import botocore.exceptions
        exc = botocore.exceptions.NoCredentialsError()
        with patch("boto3.Session", _make_session_mock_raising(exc)):
            with app.app_context():
                roadmap_data = generate_roadmap(app, "SDE", "Python", 2.0, 4)

        assert roadmap_data["failure_reason"] == "no_credentials"

        rid = save_roadmap(
            app,
            user_id=1,
            target_role="SDE",
            skills="Python",
            hours_per_day=2.0,
            duration_weeks=4,
            title=roadmap_data["title"],
            source=roadmap_data["source"],
            milestones=roadmap_data["milestones"],
            failure_reason=roadmap_data.get("failure_reason"),
        )
        row = get_roadmap(app, rid, 1)
        assert row["failure_reason"] == "no_credentials"
        assert row["source"] == "fallback"


# ══════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════
# 4. Database migration
# ══════════════════════════════════════════════════════════════════════

class TestDatabaseMigration:
    """
    Verify the backward-compatible migration that adds failure_reason to
    existing roadmaps tables that were created before the column existed.
    """

    _OLD_SCHEMA_SQL = (
        "CREATE TABLE IF NOT EXISTS users ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL, "
        "email TEXT NOT NULL UNIQUE, "
        "password TEXT NOT NULL, "
        "created_at DATETIME DEFAULT CURRENT_TIMESTAMP);"
        "CREATE TABLE IF NOT EXISTS tasks ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "user_id INTEGER NOT NULL, "
        "title TEXT NOT NULL, "
        "category TEXT NOT NULL, "
        "description TEXT, "
        "due_date TEXT, "
        "is_complete INTEGER NOT NULL DEFAULT 0, "
        "created_at DATETIME DEFAULT CURRENT_TIMESTAMP);"
        "CREATE TABLE IF NOT EXISTS roadmaps ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "user_id INTEGER NOT NULL, "
        "target_role TEXT NOT NULL, "
        "skills TEXT NOT NULL, "
        "hours_per_day REAL NOT NULL, "
        "duration_weeks INTEGER NOT NULL, "
        "title TEXT NOT NULL, "
        "source TEXT NOT NULL DEFAULT 'fallback', "
        "is_draft INTEGER NOT NULL DEFAULT 1, "
        "created_at DATETIME DEFAULT CURRENT_TIMESTAMP);"
        "CREATE TABLE IF NOT EXISTS roadmap_milestones ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "roadmap_id INTEGER NOT NULL, "
        "week_number INTEGER NOT NULL, "
        "title TEXT NOT NULL, "
        "priority TEXT NOT NULL DEFAULT 'medium', "
        "category TEXT NOT NULL, "
        "description TEXT, "
        "daily_tasks TEXT NOT NULL DEFAULT '[]', "
        "position INTEGER NOT NULL DEFAULT 0);"
        "CREATE TABLE IF NOT EXISTS milestone_imports ("
        "milestone_id INTEGER NOT NULL, "
        "task_id INTEGER NOT NULL, "
        "PRIMARY KEY (milestone_id, task_id));"
    )

    def _create_old_schema_db(self, db_path):
        """Create a DB with the pre-migration schema (no failure_reason)."""
        conn = sqlite3.connect(db_path)
        conn.executescript(self._OLD_SCHEMA_SQL)
        conn.commit()
        conn.close()

    def _get_columns(self, db_path):
        conn = sqlite3.connect(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(roadmaps)").fetchall()}
        conn.close()
        return cols

    def _safe_unlink(self, db_path):
        """Delete a SQLite DB and its WAL/SHM sidecars. Best-effort on Windows."""
        for suffix in ("", "-wal", "-shm"):
            p = db_path + suffix
            try:
                if os.path.exists(p):
                    os.unlink(p)
            except OSError:
                pass

    def test_migration_adds_column_to_old_schema(self):
        """Running init_db() on a DB without failure_reason must add the column."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            self._create_old_schema_db(db_path)
            assert "failure_reason" not in self._get_columns(db_path)
            _app = create_app(_base_config(DATABASE_PATH=db_path))
            assert "failure_reason" in self._get_columns(db_path)
        finally:
            self._safe_unlink(db_path)

    def test_migration_preserves_existing_rows(self):
        """All data present before migration must still be present after."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            self._create_old_schema_db(db_path)
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO users (name, email, password) VALUES (?, ?, ?)",
                ("MigUser", "mig@example.com", "hash"),
            )
            conn.execute(
                "INSERT INTO roadmaps "
                "(user_id, target_role, skills, hours_per_day, duration_weeks, title, source) "
                "VALUES (1, 'SDE', 'Python', 2.0, 4, 'Old Roadmap', 'fallback')"
            )
            conn.commit()
            conn.close()
            _app = create_app(_base_config(DATABASE_PATH=db_path))
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            user = conn.execute(
                "SELECT * FROM users WHERE email = ?", ("mig@example.com",)
            ).fetchone()
            assert user is not None
            assert user["name"] == "MigUser"
            roadmap = conn.execute(
                "SELECT * FROM roadmaps WHERE title = ?", ("Old Roadmap",)
            ).fetchone()
            assert roadmap is not None
            assert roadmap["failure_reason"] is None  # NULL for pre-migration rows
            conn.close()
        finally:
            self._safe_unlink(db_path)

    def test_migration_idempotent_when_column_already_exists(self):
        """Running init_db() twice must not raise or duplicate columns."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            app = create_app(_base_config(DATABASE_PATH=db_path))
            cols_before = self._get_columns(db_path)
            assert "failure_reason" in cols_before
            init_db(app)
            assert self._get_columns(db_path) == cols_before
        finally:
            self._safe_unlink(db_path)

    def test_fresh_db_has_failure_reason_column(self):
        """A brand-new database must have failure_reason from schema.sql."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        os.unlink(db_path)  # ensure truly fresh
        try:
            _app = create_app(_base_config(DATABASE_PATH=db_path))
            assert "failure_reason" in self._get_columns(db_path)
        finally:
            self._safe_unlink(db_path)

    def test_in_memory_db_has_failure_reason_column(self, app):
        """In-memory databases (used in tests) must also have the column."""
        conn = get_db(app)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(roadmaps)").fetchall()}
        assert "failure_reason" in cols

    def test_migration_does_not_lose_milestones(self):
        """Milestones stored before migration must survive it."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            self._create_old_schema_db(db_path)
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO roadmaps "
                "(user_id, target_role, skills, hours_per_day, duration_weeks, title, source) "
                "VALUES (1, 'SDE', 'Python', 2.0, 4, 'Test', 'fallback')"
            )
            conn.execute(
                "INSERT INTO roadmap_milestones "
                "(roadmap_id, week_number, title, priority, category, daily_tasks, position) "
                "VALUES (1, 1, 'OldMs', 'high', 'DSA & Programming', '[]', 0)"
            )
            conn.commit()
            conn.close()
            _app = create_app(_base_config(DATABASE_PATH=db_path))
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM roadmap_milestones WHERE title = ?", ("OldMs",)
            ).fetchone()
            assert row is not None
            conn.close()
        finally:
            self._safe_unlink(db_path)

class TestLogSafety:
    """
    _log_bedrock_failure() must only log the exception class name, the AWS
    error Code, and the failure_reason — never credentials or full messages.
    """

    def test_client_error_logs_code_not_message(self, caplog):
        import logging
        exc = _client_error("AccessDeniedException", "secret-account-arn-info")
        with caplog.at_level(logging.WARNING, logger="services.llm"):
            _log_bedrock_failure(exc, "auth")
        log_text = caplog.text
        assert "AccessDeniedException" in log_text
        assert "auth" in log_text
        # The raw AWS message must NOT appear in the log
        assert "secret-account-arn-info" not in log_text

    def test_no_credentials_error_logs_class_name(self, caplog):
        import logging
        import botocore.exceptions
        exc = botocore.exceptions.NoCredentialsError()
        with caplog.at_level(logging.WARNING, logger="services.llm"):
            _log_bedrock_failure(exc, "no_credentials")
        assert "NoCredentialsError" in caplog.text
        assert "no_credentials" in caplog.text

    def test_generic_error_logs_class_name(self, caplog):
        import logging
        exc = RuntimeError("some internal detail")
        with caplog.at_level(logging.WARNING, logger="services.llm"):
            _log_bedrock_failure(exc, "transient")
        assert "RuntimeError" in caplog.text
        # Raw exception message should not be logged
        assert "some internal detail" not in caplog.text
