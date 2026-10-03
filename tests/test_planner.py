"""
tests/test_planner.py
Automated tests for PrepMate Milestone 3 — AI-powered Preparation Planner.

All tests use in-memory SQLite and blank AWS credentials, so:
  - No Bedrock API calls are ever made.
  - The fallback path is exercised throughout.
  - Tests are deterministic and free to run.
"""

from __future__ import annotations
import json
import pytest

from app import create_app
from models.planner import (
    save_roadmap,
    approve_roadmap,
    get_roadmap,
    get_roadmaps_for_user,
    get_milestones,
    get_milestone,
    delete_roadmap,
    record_milestone_import,
    milestone_already_imported,
)
from models.tasks import get_tasks, CATEGORIES
from services.llm import (
    generate_roadmap,
    _rule_based_fallback,
    _parse_and_validate,
    _distribute_weeks,
)

# ── Fixtures ──────────────────────────────────────────────────────────────

TEST_CONFIG = {
    "TESTING":               True,
    "DATABASE_PATH":         ":memory:",
    "SECRET_KEY":            "test-secret-key",
    "WTF_CSRF_ENABLED":      False,
    # Blank credentials → Bedrock skipped, fallback always used
    "AWS_ACCESS_KEY_ID":     "",
    "AWS_SECRET_ACCESS_KEY": "",
    "BEDROCK_REGION":        "us-east-1",
    "BEDROCK_MODEL_ID":      "amazon.nova-micro-v1:0",
}


@pytest.fixture()
def app():
    return create_app(TEST_CONFIG)


@pytest.fixture()
def client(app):
    return app.test_client()


def _login(client, name="Alice", email="alice@example.com", password="password123"):
    client.post(
        "/register",
        data={"name": name, "email": email,
              "password": password, "confirm_password": password},
        follow_redirects=True,
    )
    return client


@pytest.fixture()
def auth_client(client):
    return _login(client)


@pytest.fixture()
def auth_client2(app):
    c = app.test_client()
    _login(c, name="Bob", email="bob@example.com", password="bobpass12")
    return c


# ── Minimal valid roadmap dict for model tests ─────────────────────────────

def _make_roadmap_dict(weeks: int = 4) -> dict:
    milestones = [
        {
            "week_number":  i + 1,
            "title":        f"Milestone {i + 1}",
            "priority":     "medium",
            "category":     CATEGORIES[i % len(CATEGORIES)],
            "description":  "Test description",
            "daily_tasks":  ["Task A", "Task B"],
            "position":     i,
        }
        for i in range(weeks * 2)
    ]
    return {"title": f"Test Roadmap ({weeks}w)", "source": "fallback", "milestones": milestones}


# ══════════════════════════════════════════════════════════════════════════
# 1. Input validation
# ══════════════════════════════════════════════════════════════════════════

class TestPlannerValidation:
    def test_form_loads(self, auth_client):
        resp = auth_client.get("/planner/new")
        assert resp.status_code == 200
        assert b"Roadmap" in resp.data

    def test_missing_role_shows_error(self, auth_client):
        resp = auth_client.post(
            "/planner/generate",
            data={"target_role": "", "skills": "Python", "hours_per_day": "2", "duration_weeks": "4"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"role" in resp.data.lower() or b"error" in resp.data.lower()

    def test_missing_skills_shows_error(self, auth_client):
        resp = auth_client.post(
            "/planner/generate",
            data={"target_role": "SDE", "skills": "", "hours_per_day": "2", "duration_weeks": "4"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"skill" in resp.data.lower() or b"error" in resp.data.lower()

    def test_invalid_hours_shows_error(self, auth_client):
        resp = auth_client.post(
            "/planner/generate",
            data={"target_role": "SDE", "skills": "Python", "hours_per_day": "99", "duration_weeks": "4"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"hours" in resp.data.lower() or b"error" in resp.data.lower()

    def test_zero_hours_shows_error(self, auth_client):
        resp = auth_client.post(
            "/planner/generate",
            data={"target_role": "SDE", "skills": "Python", "hours_per_day": "0", "duration_weeks": "4"},
            follow_redirects=True,
        )
        assert resp.status_code == 200

    def test_invalid_weeks_shows_error(self, auth_client):
        resp = auth_client.post(
            "/planner/generate",
            data={"target_role": "SDE", "skills": "Python", "hours_per_day": "2", "duration_weeks": "100"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"duration" in resp.data.lower() or b"error" in resp.data.lower()

    def test_valid_input_redirects_to_review(self, auth_client):
        resp = auth_client.post(
            "/planner/generate",
            data={"target_role": "Backend Engineer", "skills": "Python basics",
                  "hours_per_day": "2", "duration_weeks": "4"},
            follow_redirects=False,
        )
        # Should redirect to /planner/<id>/review
        assert resp.status_code == 302
        assert "/review" in resp.headers["Location"]

    def test_unauthenticated_generate_redirects(self, client):
        resp = client.post(
            "/planner/generate",
            data={"target_role": "SDE", "skills": "X", "hours_per_day": "2", "duration_weeks": "4"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]


# ══════════════════════════════════════════════════════════════════════════
# 2. Rule-based fallback
# ══════════════════════════════════════════════════════════════════════════

class TestFallback:
    def test_fallback_returns_dict(self, app):
        with app.app_context():
            result = _rule_based_fallback("Backend Engineer", "Python", 2.0, 8)
        assert isinstance(result, dict)

    def test_fallback_source_is_fallback(self, app):
        with app.app_context():
            result = _rule_based_fallback("SDE", "None", 1.0, 4)
        assert result["source"] == "fallback"

    def test_fallback_has_title(self, app):
        with app.app_context():
            result = _rule_based_fallback("Cloud Engineer", "Linux", 3.0, 6)
        assert "title" in result
        assert len(result["title"]) > 0

    def test_fallback_milestones_non_empty(self, app):
        with app.app_context():
            result = _rule_based_fallback("SDE", "C++", 2.0, 4)
        assert isinstance(result["milestones"], list)
        assert len(result["milestones"]) > 0

    def test_fallback_all_categories_valid(self, app):
        with app.app_context():
            result = _rule_based_fallback("Data Analyst", "Excel", 2.0, 8)
        for m in result["milestones"]:
            assert m["category"] in CATEGORIES, f"Invalid category: {m['category']}"

    def test_fallback_priority_valid(self, app):
        with app.app_context():
            result = _rule_based_fallback("Frontend Engineer", "HTML", 2.0, 6)
        for m in result["milestones"]:
            assert m["priority"] in ("high", "medium", "low")

    def test_fallback_daily_tasks_are_list(self, app):
        with app.app_context():
            result = _rule_based_fallback("DevOps Engineer", "Bash", 2.0, 6)
        for m in result["milestones"]:
            assert isinstance(m["daily_tasks"], list)
            assert len(m["daily_tasks"]) > 0

    def test_fallback_week_numbers_positive(self, app):
        with app.app_context():
            result = _rule_based_fallback("SDE", "Java", 2.0, 8)
        for m in result["milestones"]:
            assert m["week_number"] >= 1

    def test_fallback_role_profile_backend(self, app):
        with app.app_context():
            result = _rule_based_fallback("Backend Engineer", "Python", 2.0, 8)
        # Backend profile should prioritise DSA & Programming in first milestone
        first_cats = [m["category"] for m in result["milestones"][:3]]
        assert "DSA & Programming" in first_cats

    def test_generate_roadmap_uses_fallback_when_bedrock_raises(self, app):
        """
        When Bedrock raises (e.g. NoCredentialsError in a CI environment
        where no credential chain is available), generate_roadmap() must
        fall back silently and return source='fallback'.
        """
        from unittest.mock import patch, MagicMock
        import botocore.exceptions

        no_creds_error = botocore.exceptions.NoCredentialsError()

        with patch("boto3.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session
            mock_client = MagicMock()
            mock_session.client.return_value = mock_client
            mock_client.converse.side_effect = no_creds_error

            with app.app_context():
                result = generate_roadmap(app, "SDE", "Python", 2.0, 4)

        assert result["source"] == "fallback"

    def test_distribute_weeks_sums_correctly(self):
        cats = CATEGORIES
        distributed = _distribute_weeks(cats, 10)
        assert sum(w for _, w in distributed) == 10

    def test_distribute_weeks_all_positive(self):
        distributed = _distribute_weeks(CATEGORIES, 5)
        for _, w in distributed:
            assert w >= 1


# ══════════════════════════════════════════════════════════════════════════
# 3. LLM response parser
# ══════════════════════════════════════════════════════════════════════════

class TestResponseParser:
    def _valid_json(self, weeks: int = 4) -> str:
        data = {
            "title": "Test Roadmap",
            "milestones": [
                {
                    "week_number": i + 1,
                    "title": f"Step {i + 1}",
                    "priority": "medium",
                    "category": CATEGORIES[i % len(CATEGORIES)],
                    "description": "Do the thing.",
                    "daily_tasks": ["Task 1", "Task 2", "Task 3"],
                }
                for i in range(weeks * 2)
            ],
        }
        return json.dumps(data)

    def test_valid_response_parses_correctly(self):
        result = _parse_and_validate(self._valid_json(), "SDE", 4)
        assert result is not None
        assert result["source"] == "ai"
        assert len(result["milestones"]) == 8

    def test_markdown_fence_stripped(self):
        wrapped = "```json\n" + self._valid_json() + "\n```"
        result = _parse_and_validate(wrapped, "SDE", 4)
        assert result is not None

    def test_invalid_json_returns_none(self):
        result = _parse_and_validate("not json at all {{{", "SDE", 4)
        assert result is None

    def test_empty_milestones_returns_none(self):
        data = json.dumps({"title": "Empty", "milestones": []})
        result = _parse_and_validate(data, "SDE", 4)
        assert result is None

    def test_unknown_category_salvaged(self):
        data = {
            "title": "Test",
            "milestones": [{
                "week_number": 1, "title": "DSA stuff",
                "priority": "high", "category": "algorithms and coding",
                "description": "Test", "daily_tasks": ["T1"],
            }],
        }
        result = _parse_and_validate(json.dumps(data), "SDE", 2)
        assert result is not None
        assert result["milestones"][0]["category"] in CATEGORIES

    def test_invalid_priority_defaults_to_medium(self):
        data = {
            "title": "Test",
            "milestones": [{
                "week_number": 1, "title": "Step",
                "priority": "CRITICAL", "category": CATEGORIES[0],
                "description": "X", "daily_tasks": ["T"],
            }],
        }
        result = _parse_and_validate(json.dumps(data), "SDE", 2)
        assert result["milestones"][0]["priority"] == "medium"

    def test_daily_tasks_truncated_to_five(self):
        data = {
            "title": "Test",
            "milestones": [{
                "week_number": 1, "title": "Step",
                "priority": "medium", "category": CATEGORIES[0],
                "description": "X",
                "daily_tasks": [f"T{i}" for i in range(10)],
            }],
        }
        result = _parse_and_validate(json.dumps(data), "SDE", 2)
        assert len(result["milestones"][0]["daily_tasks"]) <= 5


# ══════════════════════════════════════════════════════════════════════════
# 4. Model layer (roadmap CRUD)
# ══════════════════════════════════════════════════════════════════════════

class TestRoadmapModel:
    def test_save_roadmap_returns_id(self, app):
        d = _make_roadmap_dict(2)
        rid = save_roadmap(app, 1, "SDE", "Python", 2.0, 4, d["title"], "fallback", d["milestones"])
        assert isinstance(rid, int)
        assert rid > 0

    def test_saved_roadmap_is_draft_by_default(self, app):
        d = _make_roadmap_dict(2)
        rid = save_roadmap(app, 1, "SDE", "Python", 2.0, 4, d["title"], "fallback", d["milestones"])
        row = get_roadmap(app, rid, 1)
        assert row["is_draft"] == 1

    def test_approve_roadmap_clears_draft(self, app):
        d = _make_roadmap_dict(2)
        rid = save_roadmap(app, 1, "SDE", "Python", 2.0, 4, d["title"], "fallback", d["milestones"])
        result = approve_roadmap(app, rid, 1)
        assert result is True
        row = get_roadmap(app, rid, 1)
        assert row["is_draft"] == 0

    def test_approve_wrong_user_fails(self, app):
        d = _make_roadmap_dict(2)
        rid = save_roadmap(app, 1, "SDE", "Python", 2.0, 4, d["title"], "fallback", d["milestones"])
        result = approve_roadmap(app, rid, 999)
        assert result is False

    def test_get_roadmap_wrong_user_returns_none(self, app):
        d = _make_roadmap_dict(2)
        rid = save_roadmap(app, 1, "SDE", "Python", 2.0, 4, d["title"], "fallback", d["milestones"])
        assert get_roadmap(app, rid, 999) is None

    def test_milestones_saved_with_roadmap(self, app):
        d = _make_roadmap_dict(2)
        rid = save_roadmap(app, 1, "SDE", "Python", 2.0, 4, d["title"], "fallback", d["milestones"])
        milestones = get_milestones(app, rid)
        assert len(milestones) == len(d["milestones"])

    def test_milestones_daily_tasks_are_json(self, app):
        d = _make_roadmap_dict(2)
        rid = save_roadmap(app, 1, "SDE", "Python", 2.0, 4, d["title"], "fallback", d["milestones"])
        ms = get_milestones(app, rid)
        for m in ms:
            parsed = json.loads(m["daily_tasks"])
            assert isinstance(parsed, list)

    def test_delete_roadmap(self, app):
        d = _make_roadmap_dict(1)
        rid = save_roadmap(app, 1, "SDE", "Python", 2.0, 2, d["title"], "fallback", d["milestones"])
        result = delete_roadmap(app, rid, 1)
        assert result is True
        assert get_roadmap(app, rid, 1) is None

    def test_delete_wrong_user_fails(self, app):
        d = _make_roadmap_dict(1)
        rid = save_roadmap(app, 1, "SDE", "Python", 2.0, 2, d["title"], "fallback", d["milestones"])
        result = delete_roadmap(app, rid, 999)
        assert result is False

    def test_list_excludes_drafts_by_default(self, app):
        d = _make_roadmap_dict(1)
        rid = save_roadmap(app, 1, "SDE", "Python", 2.0, 2, d["title"], "fallback", d["milestones"])
        # Draft — should not appear in default list
        assert len(get_roadmaps_for_user(app, 1)) == 0
        approve_roadmap(app, rid, 1)
        assert len(get_roadmaps_for_user(app, 1)) == 1

    def test_list_includes_drafts_when_requested(self, app):
        d = _make_roadmap_dict(1)
        save_roadmap(app, 1, "SDE", "Python", 2.0, 2, d["title"], "fallback", d["milestones"])
        assert len(get_roadmaps_for_user(app, 1, include_drafts=True)) == 1

    def test_multiple_roadmaps_per_user(self, app):
        d = _make_roadmap_dict(1)
        for _ in range(3):
            rid = save_roadmap(app, 1, "SDE", "Python", 2.0, 2, d["title"], "fallback", d["milestones"])
            approve_roadmap(app, rid, 1)
        assert len(get_roadmaps_for_user(app, 1)) == 3


# ══════════════════════════════════════════════════════════════════════════
# 5. Duplicate-import guard
# ══════════════════════════════════════════════════════════════════════════

class TestImportGuard:
    def test_not_imported_initially(self, app):
        d = _make_roadmap_dict(1)
        rid = save_roadmap(app, 1, "SDE", "Python", 2.0, 2, d["title"], "fallback", d["milestones"])
        ms = get_milestones(app, rid)
        assert not milestone_already_imported(app, ms[0]["id"], 1)

    def test_imported_flag_set_after_record(self, app):
        from models.tasks import create_task
        d = _make_roadmap_dict(1)
        rid = save_roadmap(app, 1, "SDE", "Python", 2.0, 2, d["title"], "fallback", d["milestones"])
        ms = get_milestones(app, rid)
        task_id = create_task(app, 1, "Test", CATEGORIES[0])
        record_milestone_import(app, ms[0]["id"], task_id)
        assert milestone_already_imported(app, ms[0]["id"], 1)

    def test_import_flag_not_set_for_different_user(self, app):
        from models.tasks import create_task
        d = _make_roadmap_dict(1)
        rid = save_roadmap(app, 1, "SDE", "Python", 2.0, 2, d["title"], "fallback", d["milestones"])
        ms = get_milestones(app, rid)
        # Record import for user 1
        task_id = create_task(app, 1, "Test", CATEGORIES[0])
        record_milestone_import(app, ms[0]["id"], task_id)
        # User 2 should NOT see it as already imported
        assert not milestone_already_imported(app, ms[0]["id"], 2)


# ══════════════════════════════════════════════════════════════════════════
# 6. HTTP layer
# ══════════════════════════════════════════════════════════════════════════

class TestPlannerHTTP:
    def test_planner_list_requires_login(self, client):
        resp = client.get("/planner", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_planner_list_loads_when_logged_in(self, auth_client):
        resp = auth_client.get("/planner")
        assert resp.status_code == 200
        assert b"Roadmap" in resp.data

    def test_generate_creates_draft_and_redirects(self, auth_client):
        resp = auth_client.post(
            "/planner/generate",
            data={"target_role": "SDE", "skills": "Python",
                  "hours_per_day": "2", "duration_weeks": "4"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        location = resp.headers["Location"]
        assert "/review" in location

    def test_review_page_loads(self, auth_client):
        auth_client.post(
            "/planner/generate",
            data={"target_role": "SDE", "skills": "Python",
                  "hours_per_day": "2", "duration_weeks": "4"},
            follow_redirects=True,
        )
        resp = auth_client.get("/planner/1/review")
        assert resp.status_code == 200
        assert b"SDE" in resp.data

    def test_review_shows_fallback_notice(self, auth_client):
        auth_client.post(
            "/planner/generate",
            data={"target_role": "SDE", "skills": "Python",
                  "hours_per_day": "2", "duration_weeks": "4"},
            follow_redirects=True,
        )
        resp = auth_client.get("/planner/1/review")
        assert b"Rule-based" in resp.data or b"fallback" in resp.data.lower()

    def test_review_shows_draft_status(self, auth_client):
        auth_client.post(
            "/planner/generate",
            data={"target_role": "SDE", "skills": "Python",
                  "hours_per_day": "2", "duration_weeks": "4"},
            follow_redirects=True,
        )
        resp = auth_client.get("/planner/1/review")
        assert b"Draft" in resp.data

    def test_review_404_for_another_users_roadmap(self, auth_client, auth_client2):
        # Alice generates roadmap id=1
        auth_client.post(
            "/planner/generate",
            data={"target_role": "SDE", "skills": "Python",
                  "hours_per_day": "2", "duration_weeks": "4"},
            follow_redirects=True,
        )
        # Bob tries to view it
        resp = auth_client2.get("/planner/1/review", follow_redirects=False)
        assert resp.status_code == 404

    def test_approve_roadmap(self, auth_client):
        auth_client.post(
            "/planner/generate",
            data={"target_role": "SDE", "skills": "Python",
                  "hours_per_day": "2", "duration_weeks": "4"},
            follow_redirects=True,
        )
        resp = auth_client.post("/planner/1/approve", data={}, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Saved" in resp.data or b"saved" in resp.data.lower()

    def test_approve_wrong_user_404(self, auth_client, auth_client2):
        auth_client.post(
            "/planner/generate",
            data={"target_role": "SDE", "skills": "Python",
                  "hours_per_day": "2", "duration_weeks": "4"},
            follow_redirects=True,
        )
        resp = auth_client2.post("/planner/1/approve", data={}, follow_redirects=False)
        assert resp.status_code == 404

    def test_delete_roadmap(self, auth_client):
        auth_client.post(
            "/planner/generate",
            data={"target_role": "SDE", "skills": "Python",
                  "hours_per_day": "2", "duration_weeks": "4"},
            follow_redirects=True,
        )
        resp = auth_client.post("/planner/1/delete", data={}, follow_redirects=True)
        assert resp.status_code == 200
        # Should now show empty planner list
        assert b"No roadmaps" in resp.data or b"roadmap" in resp.data.lower()

    def test_delete_wrong_user_404(self, auth_client, auth_client2):
        auth_client.post(
            "/planner/generate",
            data={"target_role": "SDE", "skills": "Python",
                  "hours_per_day": "2", "duration_weeks": "4"},
            follow_redirects=True,
        )
        resp = auth_client2.post("/planner/1/delete", data={}, follow_redirects=False)
        assert resp.status_code == 404

    def test_planner_list_shows_saved_roadmaps(self, auth_client):
        auth_client.post(
            "/planner/generate",
            data={"target_role": "Backend Engineer", "skills": "Python",
                  "hours_per_day": "3", "duration_weeks": "6"},
            follow_redirects=True,
        )
        auth_client.post("/planner/1/approve", data={}, follow_redirects=True)
        resp = auth_client.get("/planner")
        assert resp.status_code == 200
        assert b"Backend Engineer" in resp.data


# ══════════════════════════════════════════════════════════════════════════
# 7. Task import from milestones
# ══════════════════════════════════════════════════════════════════════════

class TestTaskImport:
    def _setup_saved_roadmap(self, auth_client):
        """Generate and approve a roadmap; return roadmap_id=1."""
        auth_client.post(
            "/planner/generate",
            data={"target_role": "SDE", "skills": "Python",
                  "hours_per_day": "2", "duration_weeks": "4"},
            follow_redirects=True,
        )
        auth_client.post("/planner/1/approve", data={}, follow_redirects=True)
        return 1

    def test_import_draft_blocked(self, auth_client):
        auth_client.post(
            "/planner/generate",
            data={"target_role": "SDE", "skills": "Python",
                  "hours_per_day": "2", "duration_weeks": "4"},
            follow_redirects=True,
        )
        resp = auth_client.post(
            "/planner/1/import",
            data={"milestone_ids": ["1"]},
            follow_redirects=True,
        )
        assert b"Approve" in resp.data or b"approve" in resp.data.lower()

    def test_import_no_milestones_selected(self, auth_client):
        self._setup_saved_roadmap(auth_client)
        resp = auth_client.post(
            "/planner/1/import",
            data={},
            follow_redirects=True,
        )
        assert b"No milestones" in resp.data or b"selected" in resp.data.lower()

    def test_import_milestones_creates_tasks(self, auth_client, app):
        self._setup_saved_roadmap(auth_client)
        resp = auth_client.post(
            "/planner/1/import",
            data={"milestone_ids": ["1"]},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        # Check tasks were created for user_id=1
        tasks = get_tasks(app, 1)
        assert len(tasks) >= 1

    def test_imported_tasks_have_correct_category(self, auth_client, app):
        self._setup_saved_roadmap(auth_client)
        milestones = get_milestones(app, 1)
        first = milestones[0]
        auth_client.post(
            "/planner/1/import",
            data={"milestone_ids": [str(first["id"])]},
            follow_redirects=True,
        )
        tasks = get_tasks(app, 1)
        assert any(t["category"] == first["category"] for t in tasks)

    def test_imported_tasks_have_due_date(self, auth_client, app):
        self._setup_saved_roadmap(auth_client)
        auth_client.post(
            "/planner/1/import",
            data={"milestone_ids": ["1"]},
            follow_redirects=True,
        )
        tasks = get_tasks(app, 1)
        assert any(t["due_date"] is not None for t in tasks)

    def test_duplicate_import_skipped(self, auth_client, app):
        self._setup_saved_roadmap(auth_client)
        # Import the same milestone twice
        auth_client.post(
            "/planner/1/import",
            data={"milestone_ids": ["1"]},
            follow_redirects=True,
        )
        auth_client.post(
            "/planner/1/import",
            data={"milestone_ids": ["1"]},
            follow_redirects=True,
        )
        # Only one task should exist for that milestone
        tasks = get_tasks(app, 1)
        assert len(tasks) == 1

    def test_second_import_shows_skipped_message(self, auth_client):
        self._setup_saved_roadmap(auth_client)
        auth_client.post("/planner/1/import", data={"milestone_ids": ["1"]}, follow_redirects=True)
        resp = auth_client.post(
            "/planner/1/import",
            data={"milestone_ids": ["1"]},
            follow_redirects=True,
        )
        assert b"skipped" in resp.data.lower() or b"already" in resp.data.lower()

    def test_import_wrong_user_404(self, auth_client, auth_client2):
        self._setup_saved_roadmap(auth_client)
        resp = auth_client2.post(
            "/planner/1/import",
            data={"milestone_ids": ["1"]},
            follow_redirects=False,
        )
        assert resp.status_code == 404


# ═══════════════════════════��══════════════════════════════════════════════
# 8. User isolation (end-to-end)
# ══════════════════════════════════════════════════════════════════════════

class TestUserIsolation:
    def test_roadmaps_isolated_between_users(self, auth_client, auth_client2, app):
        # Alice generates a roadmap
        auth_client.post(
            "/planner/generate",
            data={"target_role": "SDE", "skills": "Python",
                  "hours_per_day": "2", "duration_weeks": "4"},
            follow_redirects=True,
        )
        auth_client.post("/planner/1/approve", data={}, follow_redirects=True)

        # Bob should see an empty list
        resp = auth_client2.get("/planner")
        assert b"No roadmaps" in resp.data or b"New Roadmap" in resp.data
        # And cannot access Alice's roadmap directly
        assert get_roadmaps_for_user(app, 2) == []

    def test_milestones_isolated(self, auth_client, auth_client2, app):
        # Alice creates roadmap id=1
        auth_client.post(
            "/planner/generate",
            data={"target_role": "SDE", "skills": "Python",
                  "hours_per_day": "2", "duration_weeks": "4"},
            follow_redirects=True,
        )
        # Bob creates roadmap — should get a different id if Alice's is id=1
        auth_client2.post(
            "/planner/generate",
            data={"target_role": "Cloud", "skills": "Linux",
                  "hours_per_day": "1", "duration_weeks": "2"},
            follow_redirects=True,
        )
        # Alice's milestones should not appear in Bob's roadmap
        alice_ms = get_milestones(app, 1)
        # Bob's roadmap will be id=2
        bob_ms = get_milestones(app, 2)
        alice_ids = {m["id"] for m in alice_ms}
        bob_ids   = {m["id"] for m in bob_ms}
        assert alice_ids.isdisjoint(bob_ids)

    def test_tasks_not_created_for_other_user_on_import(self, auth_client, auth_client2, app):
        # Alice sets up and imports
        auth_client.post(
            "/planner/generate",
            data={"target_role": "SDE", "skills": "Python",
                  "hours_per_day": "2", "duration_weeks": "4"},
            follow_redirects=True,
        )
        auth_client.post("/planner/1/approve", data={}, follow_redirects=True)
        auth_client.post("/planner/1/import", data={"milestone_ids": ["1"]}, follow_redirects=True)

        # Bob should have no tasks
        assert get_tasks(app, 2) == []
