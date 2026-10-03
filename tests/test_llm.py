"""
tests/test_llm.py
Unit tests for the AWS Bedrock credential-chain behaviour in services/llm.py.

These tests never make real AWS API calls.  All boto3 interactions are
replaced with unittest.mock objects so the suite is free to run in any
environment, including CI without AWS credentials.

Scenarios covered
-----------------
1. Blank explicit credentials → boto3.Session is called WITHOUT
   aws_access_key_id / aws_secret_access_key, so boto3 can discover
   credentials through its standard chain.

2. Both explicit credentials present → boto3.Session IS called WITH
   aws_access_key_id and aws_secret_access_key so the configured keys
   are forwarded to Bedrock.

3. Bedrock raises any exception → generate_roadmap() catches it, falls
   back to the rule-based roadmap, and returns source='fallback'.
"""

from __future__ import annotations
import json
import pytest
from unittest.mock import patch, MagicMock, call

from app import create_app
from services.llm import generate_roadmap, _call_bedrock
from models.tasks import CATEGORIES


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_converse_response(target_role: str = "SDE", weeks: int = 2) -> dict:
    """
    Build a minimal mock response that looks like what
    boto3 bedrock-runtime client.converse() would return.
    The embedded JSON satisfies _parse_and_validate().
    """
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
    return {
        "output": {
            "message": {
                "content": [{"text": roadmap_json}]
            }
        }
    }


def _base_config(**overrides) -> dict:
    cfg = {
        "TESTING":               True,
        "DATABASE_PATH":         ":memory:",
        "SECRET_KEY":            "test-secret-key",
        "WTF_CSRF_ENABLED":      False,
        "BEDROCK_REGION":        "us-east-1",
        "BEDROCK_MODEL_ID":      "amazon.nova-micro-v1:0",
        "BEDROCK_TIMEOUT":       "30",
    }
    cfg.update(overrides)
    return cfg


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture()
def app_no_creds():
    """Flask app with blank explicit AWS credentials."""
    return create_app(_base_config(
        AWS_ACCESS_KEY_ID="",
        AWS_SECRET_ACCESS_KEY="",
    ))


@pytest.fixture()
def app_with_creds():
    """Flask app with explicit AWS credentials configured."""
    return create_app(_base_config(
        AWS_ACCESS_KEY_ID="AKIAIOSFODNN7EXAMPLE",
        AWS_SECRET_ACCESS_KEY="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    ))


# ══════════════════════════════════════════════════════════════════════════
# Scenario 1 — Blank credentials use the standard boto3 credential chain
# ══════════════════════════════════════════════════════════════════════════

class TestBlankCredentialsUseStandardChain:
    """
    When AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are both blank,
    _call_bedrock() must NOT pass aws_access_key_id or aws_secret_access_key
    to boto3.Session so that boto3 can fall back to its own credential
    discovery chain (env vars, ~/.aws/credentials, IAM role, etc.).
    """

    def test_boto3_session_called_without_explicit_keys(self, app_no_creds):
        with patch("boto3.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session
            mock_client = MagicMock()
            mock_session.client.return_value = mock_client
            mock_client.converse.return_value = _make_converse_response()

            with app_no_creds.app_context():
                generate_roadmap(app_no_creds, "SDE", "Python", 2.0, 2)

        # boto3.Session must have been called
        assert mock_session_cls.called, "boto3.Session was never called"

        # The call must NOT include explicit credential keyword arguments
        _, kwargs = mock_session_cls.call_args
        assert "aws_access_key_id" not in kwargs, (
            "aws_access_key_id was passed to boto3.Session despite blank config"
        )
        assert "aws_secret_access_key" not in kwargs, (
            "aws_secret_access_key was passed to boto3.Session despite blank config"
        )

    def test_boto3_session_receives_configured_region(self, app_no_creds):
        with patch("boto3.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session
            mock_session.client.return_value = MagicMock(
                converse=MagicMock(return_value=_make_converse_response())
            )

            with app_no_creds.app_context():
                generate_roadmap(app_no_creds, "SDE", "Python", 2.0, 2)

        _, kwargs = mock_session_cls.call_args
        assert kwargs.get("region_name") == "us-east-1"

    def test_bedrock_client_is_still_created_with_blank_creds(self, app_no_creds):
        """
        The bedrock-runtime client must be created even when no explicit
        credentials are in config — boto3 will use its chain internally.
        """
        with patch("boto3.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session
            mock_client = MagicMock()
            mock_session.client.return_value = mock_client
            mock_client.converse.return_value = _make_converse_response()

            with app_no_creds.app_context():
                generate_roadmap(app_no_creds, "SDE", "Python", 2.0, 2)

        mock_session.client.assert_called_once()
        service_name = mock_session.client.call_args[0][0]
        assert service_name == "bedrock-runtime"

    def test_successful_bedrock_response_returns_ai_source(self, app_no_creds):
        with patch("boto3.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session
            mock_session.client.return_value = MagicMock(
                converse=MagicMock(return_value=_make_converse_response())
            )

            with app_no_creds.app_context():
                result = generate_roadmap(app_no_creds, "SDE", "Python", 2.0, 2)

        assert result["source"] == "ai"


# ══════════════════════════════════════════════════════════════════════════
# Scenario 2 — Explicit credentials are forwarded to boto3.Session
# ══════════════════════════════════════════════════════════════════════════

class TestExplicitCredentialsForwarded:
    """
    When both AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are non-empty,
    they must be passed to boto3.Session so Bedrock uses those keys.
    """

    def test_boto3_session_called_with_explicit_keys(self, app_with_creds):
        with patch("boto3.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session
            mock_client = MagicMock()
            mock_session.client.return_value = mock_client
            mock_client.converse.return_value = _make_converse_response()

            with app_with_creds.app_context():
                generate_roadmap(app_with_creds, "Backend Engineer", "Python", 3.0, 4)

        _, kwargs = mock_session_cls.call_args
        assert kwargs.get("aws_access_key_id") == "AKIAIOSFODNN7EXAMPLE"
        assert kwargs.get("aws_secret_access_key") == "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

    def test_explicit_credentials_preserve_region(self, app_with_creds):
        with patch("boto3.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session
            mock_session.client.return_value = MagicMock(
                converse=MagicMock(return_value=_make_converse_response())
            )

            with app_with_creds.app_context():
                generate_roadmap(app_with_creds, "SDE", "Java", 2.0, 4)

        _, kwargs = mock_session_cls.call_args
        assert kwargs.get("region_name") == "us-east-1"

    def test_successful_response_with_explicit_creds_returns_ai_source(self, app_with_creds):
        with patch("boto3.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session
            mock_session.client.return_value = MagicMock(
                converse=MagicMock(return_value=_make_converse_response())
            )

            with app_with_creds.app_context():
                result = generate_roadmap(app_with_creds, "SDE", "Java", 2.0, 4)

        assert result["source"] == "ai"

    def test_only_one_credential_present_does_not_forward_either(self):
        """
        If only one of key_id / key_sec is set, neither must be forwarded
        (to avoid passing partial credentials that would confuse boto3).
        """
        app_one_cred = create_app(_base_config(
            AWS_ACCESS_KEY_ID="AKIAIOSFODNN7EXAMPLE",
            AWS_SECRET_ACCESS_KEY="",   # blank → should not forward either
        ))

        with patch("boto3.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session
            mock_session.client.return_value = MagicMock(
                converse=MagicMock(return_value=_make_converse_response())
            )

            with app_one_cred.app_context():
                generate_roadmap(app_one_cred, "SDE", "Python", 2.0, 2)

        _, kwargs = mock_session_cls.call_args
        assert "aws_access_key_id" not in kwargs
        assert "aws_secret_access_key" not in kwargs


# ══════════════════════════════════════════════════════════════════════════
# Scenario 3 — Any Bedrock failure triggers the rule-based fallback
# ══════════════════════════════════════════════════════════════════════════

class TestBedrockFailureFallback:
    """
    When _call_bedrock() raises, generate_roadmap() must:
      - catch the exception without re-raising
      - return a roadmap with source='fallback'
      - never expose credential values or raw exception details to the caller
    """

    def _mock_session_raising(self, exc: Exception):
        """Return a patched boto3.Session whose converse() raises `exc`."""
        mock_session_cls = MagicMock()
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_client = MagicMock()
        mock_session.client.return_value = mock_client
        mock_client.converse.side_effect = exc
        return mock_session_cls

    def test_fallback_on_no_credentials_error(self, app_no_creds):
        import botocore.exceptions
        exc = botocore.exceptions.NoCredentialsError()
        with patch("boto3.Session", self._mock_session_raising(exc)):
            with app_no_creds.app_context():
                result = generate_roadmap(app_no_creds, "SDE", "Python", 2.0, 4)
        assert result["source"] == "fallback"

    def test_fallback_on_client_error(self, app_with_creds):
        import botocore.exceptions
        exc = botocore.exceptions.ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "Access denied"}},
            "Converse",
        )
        with patch("boto3.Session", self._mock_session_raising(exc)):
            with app_with_creds.app_context():
                result = generate_roadmap(app_with_creds, "Backend Engineer", "Go", 2.0, 4)
        assert result["source"] == "fallback"

    def test_fallback_on_endpoint_resolution_error(self, app_no_creds):
        import botocore.exceptions
        exc = botocore.exceptions.EndpointResolutionError(msg="Unable to resolve endpoint")
        with patch("boto3.Session", self._mock_session_raising(exc)):
            with app_no_creds.app_context():
                result = generate_roadmap(app_no_creds, "SDE", "Python", 2.0, 4)
        assert result["source"] == "fallback"

    def test_fallback_on_generic_exception(self, app_no_creds):
        with patch("boto3.Session", self._mock_session_raising(RuntimeError("network down"))):
            with app_no_creds.app_context():
                result = generate_roadmap(app_no_creds, "SDE", "Python", 2.0, 4)
        assert result["source"] == "fallback"

    def test_fallback_roadmap_has_required_keys(self, app_no_creds):
        with patch("boto3.Session", self._mock_session_raising(Exception("boom"))):
            with app_no_creds.app_context():
                result = generate_roadmap(app_no_creds, "SDE", "Python", 2.0, 4)
        assert "title" in result
        assert "milestones" in result
        assert isinstance(result["milestones"], list)
        assert len(result["milestones"]) > 0

    def test_fallback_milestones_have_valid_categories(self, app_no_creds):
        with patch("boto3.Session", self._mock_session_raising(Exception("oops"))):
            with app_no_creds.app_context():
                result = generate_roadmap(app_no_creds, "Cloud Engineer", "Linux", 2.0, 6)
        for m in result["milestones"]:
            assert m["category"] in CATEGORIES

    def test_exception_not_re_raised(self, app_no_creds):
        """generate_roadmap() must never propagate a Bedrock exception."""
        with patch("boto3.Session", self._mock_session_raising(Exception("fatal"))):
            with app_no_creds.app_context():
                try:
                    generate_roadmap(app_no_creds, "SDE", "Python", 2.0, 4)
                except Exception:  # noqa: BLE001
                    pytest.fail("generate_roadmap() must not re-raise Bedrock exceptions")
