"""
models/tasks.py
Task model helpers — raw sqlite3 queries, per-user isolation enforced here.
All write operations commit/rollback explicitly to stay compatible with the
shared in-memory connection used during testing.
"""

from __future__ import annotations
import re
from datetime import date
from database import get_db

# ── Constants ─────────────────────────────────────────────────────────────

CATEGORIES = [
    "DSA & Programming",
    "Aptitude",
    "Cloud & Networking",
    "Projects",
    "Interview Preparation",
]

# Mapping from URL-friendly slug → display name
CATEGORY_SLUGS = {
    "dsa":       "DSA & Programming",
    "aptitude":  "Aptitude",
    "cloud":     "Cloud & Networking",
    "projects":  "Projects",
    "interview": "Interview Preparation",
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ── Validation ─────────────────────────────────────────────────────────────

def validate_task(title: str, category: str, due_date: str) -> list[str]:
    """Return a list of validation error strings, or empty list if valid."""
    errors: list[str] = []

    title = title.strip() if title else ""
    if not title:
        errors.append("Task title is required.")
    elif len(title) > 200:
        errors.append("Task title must be 200 characters or fewer.")

    if category not in CATEGORIES:
        errors.append("Invalid category selected.")

    if due_date:
        if not _DATE_RE.match(due_date):
            errors.append("Due date must be in YYYY-MM-DD format.")
        else:
            try:
                date.fromisoformat(due_date)
            except ValueError:
                errors.append("Due date is not a valid calendar date.")

    return errors


# ── CRUD ───────────────────────────────────────────────────────────────────

def create_task(
    app,
    user_id: int,
    title: str,
    category: str,
    description: str | None = None,
    due_date: str | None = None,
) -> int:
    """Insert a new task and return its id."""
    conn = get_db(app)
    try:
        cursor = conn.execute(
            """
            INSERT INTO tasks (user_id, title, category, description, due_date)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user_id,
                title.strip(),
                category,
                description.strip() if description else None,
                due_date or None,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    except Exception:
        conn.rollback()
        raise


def get_tasks(
    app,
    user_id: int,
    category: str | None = None,
    completed: bool | None = None,
) -> list:
    """Return tasks for a user, optionally filtered by category and/or completion."""
    params: list = [user_id]
    query = "SELECT * FROM tasks WHERE user_id = ?"

    if category is not None:
        query += " AND category = ?"
        params.append(category)

    if completed is not None:
        query += " AND is_complete = ?"
        params.append(1 if completed else 0)

    query += " ORDER BY is_complete ASC, due_date ASC NULLS LAST, created_at DESC"
    conn = get_db(app)
    return conn.execute(query, params).fetchall()


def get_task(app, task_id: int, user_id: int):
    """Return a single task row only if it belongs to user_id, else None."""
    conn = get_db(app)
    return conn.execute(
        "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
        (task_id, user_id),
    ).fetchone()


def set_task_complete(app, task_id: int, user_id: int, complete: bool) -> bool:
    """Mark a task complete or incomplete. Returns True if a row was updated."""
    conn = get_db(app)
    try:
        result = conn.execute(
            "UPDATE tasks SET is_complete = ? WHERE id = ? AND user_id = ?",
            (1 if complete else 0, task_id, user_id),
        )
        conn.commit()
        return result.rowcount > 0
    except Exception:
        conn.rollback()
        raise


def delete_task(app, task_id: int, user_id: int) -> bool:
    """Delete a task if it belongs to user_id. Returns True if deleted."""
    conn = get_db(app)
    try:
        result = conn.execute(
            "DELETE FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        )
        conn.commit()
        return result.rowcount > 0
    except Exception:
        conn.rollback()
        raise


# ── Statistics ─────────────────────────────────────────────────────────────

def get_task_stats(app, user_id: int) -> dict:
    """Return total, completed, pending, and completion_pct for a user."""
    conn = get_db(app)
    row = conn.execute(
        """
        SELECT
            COUNT(*)                              AS total,
            SUM(CASE WHEN is_complete=1 THEN 1 ELSE 0 END) AS completed
        FROM tasks
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()

    total     = row["total"] or 0
    completed = row["completed"] or 0
    pending   = total - completed
    pct       = round((completed / total) * 100) if total > 0 else 0

    return {
        "total":          total,
        "completed":      completed,
        "pending":        pending,
        "completion_pct": pct,
    }
