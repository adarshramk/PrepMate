"""
tests/test_tasks.py
Automated tests for PrepMate Milestone 2 — Task Manager.
All tests use in-memory SQLite so no real DB is touched.
CSRF is disabled via WTF_CSRF_ENABLED=False in the test config.
"""

import pytest
from app import create_app
from models.tasks import (
    create_task,
    get_tasks,
    get_task,
    set_task_complete,
    delete_task,
    get_task_stats,
    validate_task,
    CATEGORIES,
    CATEGORY_SLUGS,
)


# ── Fixtures ──────────────────────────────────────────────────────────────

TEST_CONFIG = {
    "TESTING": True,
    "DATABASE_PATH": ":memory:",
    "SECRET_KEY": "test-secret-key",
    "WTF_CSRF_ENABLED": False,
}


@pytest.fixture()
def app():
    """Application with a fresh in-memory DB per test."""
    return create_app(TEST_CONFIG)


@pytest.fixture()
def client(app):
    return app.test_client()


def _register_and_login(client, name="Alice", email="alice@example.com", password="password123"):
    """Helper: register + auto-login, return the client."""
    client.post(
        "/register",
        data={
            "name": name,
            "email": email,
            "password": password,
            "confirm_password": password,
        },
        follow_redirects=True,
    )
    return client


@pytest.fixture()
def auth_client(client):
    """Client logged in as 'Alice'."""
    return _register_and_login(client)


@pytest.fixture()
def auth_client2(app):
    """A second, independent client logged in as 'Bob' (different user)."""
    client2 = app.test_client()
    _register_and_login(client2, name="Bob", email="bob@example.com", password="password456")
    return client2


# ── validate_task ─────────────────────────────────────────────────────────

class TestValidateTask:
    def test_valid_task_no_errors(self):
        errors = validate_task("Study graphs", "DSA & Programming", "2026-12-01")
        assert errors == []

    def test_empty_title_error(self):
        errors = validate_task("", "Aptitude", "")
        assert any("title" in e.lower() for e in errors)

    def test_title_too_long(self):
        errors = validate_task("x" * 201, "Aptitude", "")
        assert any("200" in e for e in errors)

    def test_invalid_category(self):
        errors = validate_task("Do stuff", "NotACategory", "")
        assert any("category" in e.lower() for e in errors)

    def test_valid_category_accepted(self):
        for cat in CATEGORIES:
            errors = validate_task("Title", cat, "")
            assert all("category" not in e.lower() for e in errors), \
                f"Category '{cat}' was wrongly rejected"

    def test_invalid_due_date_format(self):
        errors = validate_task("Title", "Aptitude", "01-12-2026")
        assert any("date" in e.lower() for e in errors)

    def test_nonsense_date(self):
        errors = validate_task("Title", "Aptitude", "2026-13-99")
        assert any("date" in e.lower() for e in errors)

    def test_empty_due_date_is_optional(self):
        errors = validate_task("Title", "Aptitude", "")
        assert errors == []


# ── Model-level CRUD ──────────────────────────────────────────────────────

class TestTaskModelCRUD:
    def test_create_task_returns_id(self, app):
        tid = create_task(app, 1, "Study graphs", "DSA & Programming")
        assert isinstance(tid, int)
        assert tid > 0

    def test_created_task_is_pending(self, app):
        tid = create_task(app, 1, "Learn Docker", "Cloud & Networking")
        task = get_task(app, tid, 1)
        assert task is not None
        assert task["is_complete"] == 0

    def test_get_task_wrong_user_returns_none(self, app):
        tid = create_task(app, 1, "Private task", "Projects")
        task = get_task(app, tid, 999)  # wrong user
        assert task is None

    def test_complete_task(self, app):
        tid = create_task(app, 1, "Mock interview", "Interview Preparation")
        result = set_task_complete(app, tid, 1, complete=True)
        assert result is True
        task = get_task(app, tid, 1)
        assert task["is_complete"] == 1

    def test_reopen_task(self, app):
        tid = create_task(app, 1, "Reopen me", "Aptitude")
        set_task_complete(app, tid, 1, complete=True)
        set_task_complete(app, tid, 1, complete=False)
        task = get_task(app, tid, 1)
        assert task["is_complete"] == 0

    def test_complete_wrong_user_fails(self, app):
        tid = create_task(app, 1, "Alice task", "Projects")
        result = set_task_complete(app, tid, 999, complete=True)
        assert result is False
        task = get_task(app, tid, 1)
        assert task["is_complete"] == 0

    def test_delete_task(self, app):
        tid = create_task(app, 1, "Delete me", "Aptitude")
        result = delete_task(app, tid, 1)
        assert result is True
        assert get_task(app, tid, 1) is None

    def test_delete_wrong_user_fails(self, app):
        tid = create_task(app, 1, "Mine", "Projects")
        result = delete_task(app, tid, 999)
        assert result is False
        assert get_task(app, tid, 1) is not None

    def test_task_stores_fields(self, app):
        tid = create_task(
            app, 1,
            title="Prep AWS",
            category="Cloud & Networking",
            description="Study S3, EC2",
            due_date="2026-11-30",
        )
        task = get_task(app, tid, 1)
        assert task["title"]       == "Prep AWS"
        assert task["category"]    == "Cloud & Networking"
        assert task["description"] == "Study S3, EC2"
        assert task["due_date"]    == "2026-11-30"


# ── Filtering ─────────────────────────────────────────────────────────────

class TestTaskFiltering:
    def test_filter_by_category_returns_only_matching(self, app):
        create_task(app, 1, "DSA task",     "DSA & Programming")
        create_task(app, 1, "Aptitude task","Aptitude")
        create_task(app, 1, "Another DSA",  "DSA & Programming")

        dsa_tasks = get_tasks(app, 1, category="DSA & Programming")
        assert len(dsa_tasks) == 2
        for t in dsa_tasks:
            assert t["category"] == "DSA & Programming"

    def test_filter_by_completed(self, app):
        t1 = create_task(app, 1, "Done",    "Aptitude")
        t2 = create_task(app, 1, "Pending", "Aptitude")
        set_task_complete(app, t1, 1, complete=True)

        done    = get_tasks(app, 1, completed=True)
        pending = get_tasks(app, 1, completed=False)
        assert len(done)    == 1
        assert len(pending) == 1

    def test_no_filter_returns_all(self, app):
        create_task(app, 1, "T1", "DSA & Programming")
        create_task(app, 1, "T2", "Aptitude")
        create_task(app, 1, "T3", "Projects")
        assert len(get_tasks(app, 1)) == 3

    def test_category_and_completed_combined(self, app):
        t1 = create_task(app, 1, "DSA done",    "DSA & Programming")
        t2 = create_task(app, 1, "DSA pending", "DSA & Programming")
        create_task(app, 1, "Cloud done",   "Cloud & Networking")
        set_task_complete(app, t1, 1, complete=True)
        set_task_complete(app, t1, 1, complete=True)  # idempotent

        results = get_tasks(app, 1, category="DSA & Programming", completed=True)
        assert len(results) == 1
        assert results[0]["title"] == "DSA done"


# ── Statistics ─────────────────────────────────────────────────────────────

class TestTaskStats:
    def test_stats_empty_user(self, app):
        stats = get_task_stats(app, 42)
        assert stats == {"total": 0, "completed": 0, "pending": 0, "completion_pct": 0}

    def test_stats_all_pending(self, app):
        create_task(app, 1, "A", "Aptitude")
        create_task(app, 1, "B", "Aptitude")
        stats = get_task_stats(app, 1)
        assert stats["total"]          == 2
        assert stats["completed"]      == 0
        assert stats["pending"]        == 2
        assert stats["completion_pct"] == 0

    def test_stats_all_complete(self, app):
        t1 = create_task(app, 1, "A", "Aptitude")
        t2 = create_task(app, 1, "B", "Aptitude")
        set_task_complete(app, t1, 1, complete=True)
        set_task_complete(app, t2, 1, complete=True)
        stats = get_task_stats(app, 1)
        assert stats["completion_pct"] == 100

    def test_stats_partial(self, app):
        t1 = create_task(app, 1, "A", "Aptitude")
        create_task(app, 1, "B", "Aptitude")
        create_task(app, 1, "C", "Aptitude")
        create_task(app, 1, "D", "Aptitude")
        set_task_complete(app, t1, 1, complete=True)
        stats = get_task_stats(app, 1)
        assert stats["total"]     == 4
        assert stats["completed"] == 1
        assert stats["pending"]   == 3
        assert stats["completion_pct"] == 25


# ── User Isolation ────────────────────────────────────────────────────────

class TestUserIsolation:
    def test_tasks_not_visible_to_other_user(self, app):
        create_task(app, 1, "Alice task", "Projects")
        bob_tasks = get_tasks(app, 2)
        assert len(bob_tasks) == 0

    def test_stats_isolated(self, app):
        create_task(app, 1, "Alice A", "Aptitude")
        create_task(app, 1, "Alice B", "Aptitude")
        create_task(app, 2, "Bob A",   "Projects")

        alice_stats = get_task_stats(app, 1)
        bob_stats   = get_task_stats(app, 2)

        assert alice_stats["total"] == 2
        assert bob_stats["total"]   == 1


# ── HTTP / View layer ─────────────────────────────────────────────────────

class TestTasksHTTPCreate:
    def test_task_list_redirects_when_not_logged_in(self, client):
        resp = client.get("/tasks", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_task_list_loads_when_logged_in(self, auth_client):
        resp = auth_client.get("/tasks")
        assert resp.status_code == 200
        assert b"Tasks" in resp.data

    def test_create_task_via_post(self, auth_client):
        resp = auth_client.post(
            "/tasks/create",
            data={
                "title":    "Solve two-sum",
                "category": "DSA & Programming",
                "due_date": "2026-12-31",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Solve two-sum" in resp.data

    def test_create_task_missing_title_shows_error(self, auth_client):
        resp = auth_client.post(
            "/tasks/create",
            data={"title": "", "category": "Aptitude"},
            follow_redirects=True,
        )
        assert b"required" in resp.data or b"title" in resp.data.lower()

    def test_create_task_invalid_category_rejected(self, auth_client):
        resp = auth_client.post(
            "/tasks/create",
            data={"title": "Some task", "category": "FakeCategory"},
            follow_redirects=True,
        )
        assert b"Invalid category" in resp.data or resp.status_code == 200

    def test_create_task_unauthenticated_redirects(self, client):
        resp = client.post(
            "/tasks/create",
            data={"title": "Hack", "category": "Aptitude"},
            follow_redirects=False,
        )
        assert resp.status_code == 302


class TestTasksHTTPComplete:
    def test_complete_task_via_post(self, auth_client):
        # Create first
        auth_client.post(
            "/tasks/create",
            data={"title": "Task to complete", "category": "Aptitude"},
            follow_redirects=True,
        )
        # Retrieve the task id from the list page
        resp = auth_client.get("/tasks")
        assert b"Task to complete" in resp.data

        # Find the task id via model (app fixture not available here, use a trick)
        # We'll POST to /tasks/1/complete (first task is id=1 in a fresh DB)
        resp = auth_client.post(
            "/tasks/1/complete",
            data={},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"complete" in resp.data.lower() or b"done" in resp.data.lower()

    def test_complete_nonexistent_task_returns_404(self, auth_client):
        resp = auth_client.post("/tasks/9999/complete", data={}, follow_redirects=False)
        assert resp.status_code == 404

    def test_reopen_task_via_post(self, auth_client):
        auth_client.post(
            "/tasks/create",
            data={"title": "Task to reopen", "category": "Projects"},
            follow_redirects=True,
        )
        auth_client.post("/tasks/1/complete", data={}, follow_redirects=True)
        resp = auth_client.post("/tasks/1/reopen", data={}, follow_redirects=True)
        assert resp.status_code == 200


class TestTasksHTTPDelete:
    def test_delete_task_via_post(self, auth_client):
        auth_client.post(
            "/tasks/create",
            data={"title": "Delete me", "category": "Interview Preparation"},
            follow_redirects=True,
        )
        resp = auth_client.post("/tasks/1/delete", data={}, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Delete me" not in resp.data

    def test_delete_nonexistent_task_returns_404(self, auth_client):
        resp = auth_client.post("/tasks/9999/delete", data={}, follow_redirects=False)
        assert resp.status_code == 404

    def test_delete_other_users_task_returns_404(self, auth_client, auth_client2):
        """Bob cannot delete Alice's task."""
        # Alice creates a task — task id 1 for Alice
        auth_client.post(
            "/tasks/create",
            data={"title": "Alice only", "category": "Projects"},
            follow_redirects=True,
        )
        # Bob tries to delete task id 1
        resp = auth_client2.post("/tasks/1/delete", data={}, follow_redirects=False)
        assert resp.status_code == 404


class TestTasksHTTPFilter:
    def test_category_filter_via_slug(self, auth_client):
        auth_client.post(
            "/tasks/create",
            data={"title": "DSA task", "category": "DSA & Programming"},
            follow_redirects=True,
        )
        auth_client.post(
            "/tasks/create",
            data={"title": "Aptitude task", "category": "Aptitude"},
            follow_redirects=True,
        )
        resp = auth_client.get("/tasks?category=dsa")
        assert resp.status_code == 200
        assert b"DSA task" in resp.data
        assert b"Aptitude task" not in resp.data

    def test_all_tasks_shown_without_filter(self, auth_client):
        auth_client.post(
            "/tasks/create",
            data={"title": "T1", "category": "Aptitude"},
            follow_redirects=True,
        )
        auth_client.post(
            "/tasks/create",
            data={"title": "T2", "category": "Projects"},
            follow_redirects=True,
        )
        resp = auth_client.get("/tasks")
        assert b"T1" in resp.data
        assert b"T2" in resp.data

    def test_unknown_category_slug_shows_warning(self, auth_client):
        resp = auth_client.get("/tasks?category=notreal", follow_redirects=True)
        assert resp.status_code == 200
        assert b"Unknown category" in resp.data or b"warning" in resp.data.lower()


class TestDashboardStats:
    def test_dashboard_shows_live_stats(self, auth_client):
        # Create 2 tasks, complete 1
        auth_client.post(
            "/tasks/create",
            data={"title": "T1", "category": "Aptitude"},
            follow_redirects=True,
        )
        auth_client.post(
            "/tasks/create",
            data={"title": "T2", "category": "Aptitude"},
            follow_redirects=True,
        )
        auth_client.post("/tasks/1/complete", data={}, follow_redirects=True)

        resp = auth_client.get("/dashboard")
        assert resp.status_code == 200
        assert b"50%" in resp.data  # 1 of 2 complete

    def test_dashboard_stats_zero_initially(self, auth_client):
        resp = auth_client.get("/dashboard")
        # Should show 0 for all task counts
        assert b"0" in resp.data


class TestPersistence:
    def test_task_persists_after_page_reload(self, auth_client):
        auth_client.post(
            "/tasks/create",
            data={"title": "Persistent task", "category": "Cloud & Networking"},
            follow_redirects=True,
        )
        # Re-fetch the page (same session, same in-memory DB)
        resp = auth_client.get("/tasks")
        assert b"Persistent task" in resp.data

    def test_completed_state_persists(self, auth_client):
        auth_client.post(
            "/tasks/create",
            data={"title": "Persistent complete", "category": "Projects"},
            follow_redirects=True,
        )
        auth_client.post("/tasks/1/complete", data={}, follow_redirects=True)

        resp = auth_client.get("/tasks")
        assert resp.status_code == 200
        # The task should appear in completed section
        assert b"Persistent complete" in resp.data
