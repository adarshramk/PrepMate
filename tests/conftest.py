"""
tests/conftest.py
Shared pytest fixtures for the PrepMate test suite.

All fixtures use an in-memory SQLite database so no real files are created
or touched during the test run.  AWS credentials are left blank so boto3
never attempts a real Bedrock call; tests that exercise Bedrock paths mock
boto3 directly (see tests/test_llm.py).
"""

from __future__ import annotations
import pytest
from app import create_app

# ── Base test config ───────────────────────────────────────────────────────

BASE_CONFIG = {
    "TESTING":               True,
    "DATABASE_PATH":         ":memory:",
    "SECRET_KEY":            "test-secret-key",
    "WTF_CSRF_ENABLED":      False,
    # Blank credentials → Bedrock call will fail → rule-based fallback used
    "AWS_ACCESS_KEY_ID":     "",
    "AWS_SECRET_ACCESS_KEY": "",
    "BEDROCK_REGION":        "us-east-1",
    "BEDROCK_MODEL_ID":      "amazon.nova-micro-v1:0",
    "BEDROCK_TIMEOUT":       "5",
}


# ── Core fixtures ─────────────────────────────────────────────────────────

@pytest.fixture()
def app():
    """Flask application with a fresh in-memory database for each test."""
    return create_app(BASE_CONFIG)


@pytest.fixture()
def client(app):
    """Unauthenticated Flask test client."""
    return app.test_client()


# ── Auth helpers ───────────────────────────────────────────────────────────

def register_and_login(
    client,
    name: str = "Alice",
    email: str = "alice@example.com",
    password: str = "password123",
):
    """Register + auto-login a user. Returns the client."""
    client.post(
        "/register",
        data={
            "name":             name,
            "email":            email,
            "password":         password,
            "confirm_password": password,
        },
        follow_redirects=True,
    )
    return client


@pytest.fixture()
def auth_client(client):
    """Authenticated test client (Alice)."""
    return register_and_login(client)


@pytest.fixture()
def auth_client2(app):
    """A second, independent authenticated test client (Bob)."""
    c = app.test_client()
    register_and_login(c, name="Bob", email="bob@example.com", password="bobpass12")
    return c
