"""
tests/test_basic.py
Automated tests for PrepMate Milestone 1.
Uses Flask test client + in-memory SQLite so no real DB is touched.
"""

import pytest
from app import create_app
from config import TestingConfig
from database import init_db


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture()
def app():
    """Create an application configured for testing (in-memory DB)."""
    application = create_app(
        {
            "TESTING": True,
            "DATABASE_PATH": ":memory:",
            "SECRET_KEY": "test-secret-key",
        }
    )
    yield application


@pytest.fixture()
def client(app):
    """Flask test client."""
    return app.test_client()


@pytest.fixture()
def registered_client(client):
    """A test client that already has a registered and logged-in user."""
    client.post(
        "/register",
        data={
            "name": "Test User",
            "email": "test@example.com",
            "password": "password123",
            "confirm_password": "password123",
        },
        follow_redirects=True,
    )
    return client


# ── Landing page ──────────────────────────────────────────────────────────

class TestLandingPage:
    def test_index_returns_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_index_contains_brand(self, client):
        resp = client.get("/")
        assert b"PrepMate" in resp.data


# ── Registration ──────────────────────────────────────────────────────────

class TestRegister:
    def test_register_page_loads(self, client):
        resp = client.get("/register")
        assert resp.status_code == 200

    def test_successful_registration_redirects_to_dashboard(self, client):
        resp = client.post(
            "/register",
            data={
                "name": "Alice",
                "email": "alice@example.com",
                "password": "securepass1",
                "confirm_password": "securepass1",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Dashboard" in resp.data or b"Alice" in resp.data

    def test_duplicate_email_shows_error(self, client):
        data = {
            "name": "Bob",
            "email": "bob@example.com",
            "password": "password123",
            "confirm_password": "password123",
        }
        # First registration succeeds and logs the user in
        client.post("/register", data=data)
        # Log out so the second attempt is not redirected to dashboard
        client.get("/logout")
        resp = client.post("/register", data=data, follow_redirects=True)
        assert resp.status_code == 200
        assert b"already exists" in resp.data

    def test_short_password_shows_error(self, client):
        resp = client.post(
            "/register",
            data={
                "name": "Charlie",
                "email": "charlie@example.com",
                "password": "short",
                "confirm_password": "short",
            },
            follow_redirects=True,
        )
        assert b"8 characters" in resp.data

    def test_mismatched_passwords_shows_error(self, client):
        resp = client.post(
            "/register",
            data={
                "name": "Dave",
                "email": "dave@example.com",
                "password": "password123",
                "confirm_password": "different123",
            },
            follow_redirects=True,
        )
        assert b"do not match" in resp.data

    def test_invalid_email_shows_error(self, client):
        resp = client.post(
            "/register",
            data={
                "name": "Eve",
                "email": "not-an-email",
                "password": "password123",
                "confirm_password": "password123",
            },
            follow_redirects=True,
        )
        assert b"valid email" in resp.data


# ── Login ─────────────────────────────────────────────────────────────────

class TestLogin:
    def test_login_page_loads(self, client):
        resp = client.get("/login")
        assert resp.status_code == 200

    def test_successful_login_redirects_to_dashboard(self, registered_client):
        # Log out first
        registered_client.get("/logout")
        resp = registered_client.post(
            "/login",
            data={"email": "test@example.com", "password": "password123"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Dashboard" in resp.data or b"Test User" in resp.data

    def test_wrong_password_shows_error(self, registered_client):
        registered_client.get("/logout")
        resp = registered_client.post(
            "/login",
            data={"email": "test@example.com", "password": "wrongpassword"},
            follow_redirects=True,
        )
        assert b"Invalid email or password" in resp.data

    def test_unknown_email_shows_error(self, client):
        resp = client.post(
            "/login",
            data={"email": "nobody@example.com", "password": "password123"},
            follow_redirects=True,
        )
        assert b"Invalid email or password" in resp.data

    def test_empty_fields_shows_error(self, client):
        resp = client.post(
            "/login",
            data={"email": "", "password": ""},
            follow_redirects=True,
        )
        assert b"email and password" in resp.data


# ── Dashboard protection ───────────────────────────────────────────────────

class TestDashboard:
    def test_dashboard_redirects_when_not_logged_in(self, client):
        resp = client.get("/dashboard", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_dashboard_accessible_when_logged_in(self, registered_client):
        resp = registered_client.get("/dashboard")
        assert resp.status_code == 200
        assert b"Dashboard" in resp.data

    def test_dashboard_shows_prep_sections(self, registered_client):
        resp = registered_client.get("/dashboard")
        assert b"DSA" in resp.data
        assert b"Aptitude" in resp.data
        assert b"Cloud" in resp.data
        assert b"Projects" in resp.data
        assert b"Interview" in resp.data


# ── Logout ────────────────────────────────────────────────────────────────

class TestLogout:
    def test_logout_clears_session(self, registered_client):
        # Confirm logged in
        resp = registered_client.get("/dashboard")
        assert resp.status_code == 200

        # Log out
        registered_client.get("/logout", follow_redirects=True)

        # Dashboard should now redirect
        resp = registered_client.get("/dashboard", follow_redirects=False)
        assert resp.status_code == 302

    def test_logout_redirects_to_index(self, registered_client):
        resp = registered_client.get("/logout", follow_redirects=True)
        assert resp.status_code == 200
        assert b"PrepMate" in resp.data
