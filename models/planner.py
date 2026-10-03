"""
models/planner.py
Roadmap model helpers — raw sqlite3 queries, per-user isolation enforced here.
Consistent with models/tasks.py: explicit commit/rollback, no context-manager
close on shared in-memory connections used during testing.
"""

from __future__ import annotations
import json
from database import get_db


# ── Save ───────────────────────────────────────────────────────────────────

def save_roadmap(
    app,
    user_id: int,
    target_role: str,
    skills: str,
    hours_per_day: float,
    duration_weeks: int,
    title: str,
    source: str,          # 'ai' or 'fallback'
    milestones: list[dict],
    is_draft: bool = True,
    failure_reason: str | None = None,
) -> int:
    """
    Insert a roadmap row and its milestones.
    Returns the new roadmap id.
    milestones is a list of dicts with keys:
        week_number, title, priority, category, description, daily_tasks, position
    failure_reason is None when source='ai', or a category string when Bedrock failed.
    """
    conn = get_db(app)
    try:
        cur = conn.execute(
            """
            INSERT INTO roadmaps
                (user_id, target_role, skills, hours_per_day, duration_weeks,
                 title, source, is_draft, failure_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                target_role.strip(),
                skills.strip(),
                hours_per_day,
                duration_weeks,
                title.strip(),
                source,
                1 if is_draft else 0,
                failure_reason,
            ),
        )
        roadmap_id = cur.lastrowid

        for pos, m in enumerate(milestones):
            daily_tasks_json = (
                json.dumps(m["daily_tasks"])
                if isinstance(m["daily_tasks"], list)
                else m["daily_tasks"]
            )
            conn.execute(
                """
                INSERT INTO roadmap_milestones
                    (roadmap_id, week_number, title, priority, category,
                     description, daily_tasks, position)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    roadmap_id,
                    m["week_number"],
                    m["title"],
                    m.get("priority", "medium"),
                    m["category"],
                    m.get("description", ""),
                    daily_tasks_json,
                    m.get("position", pos),
                ),
            )

        conn.commit()
        return roadmap_id
    except Exception:
        conn.rollback()
        raise


# ── Approve draft (promote to saved) ──────────────────────────────────────

def approve_roadmap(app, roadmap_id: int, user_id: int) -> bool:
    """Set is_draft=0. Returns True if the row existed and belonged to user."""
    conn = get_db(app)
    try:
        result = conn.execute(
            "UPDATE roadmaps SET is_draft = 0 WHERE id = ? AND user_id = ?",
            (roadmap_id, user_id),
        )
        conn.commit()
        return result.rowcount > 0
    except Exception:
        conn.rollback()
        raise


# ── Read ───────────────────────────────────────────────────────────────────

def get_roadmap(app, roadmap_id: int, user_id: int):
    """Return roadmap row only if it belongs to user_id, else None."""
    conn = get_db(app)
    return conn.execute(
        "SELECT * FROM roadmaps WHERE id = ? AND user_id = ?",
        (roadmap_id, user_id),
    ).fetchone()


def get_roadmaps_for_user(app, user_id: int, include_drafts: bool = False) -> list:
    """Return all roadmaps for a user, newest first. Drafts excluded by default."""
    conn = get_db(app)
    if include_drafts:
        return conn.execute(
            "SELECT * FROM roadmaps WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM roadmaps WHERE user_id = ? AND is_draft = 0 ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()


def get_milestones(app, roadmap_id: int) -> list:
    """Return milestones for a roadmap ordered by position."""
    conn = get_db(app)
    return conn.execute(
        "SELECT * FROM roadmap_milestones WHERE roadmap_id = ? ORDER BY position ASC",
        (roadmap_id,),
    ).fetchall()


def get_milestone(app, milestone_id: int) -> object:
    """Return a single milestone row by id (caller checks roadmap ownership)."""
    conn = get_db(app)
    return conn.execute(
        "SELECT * FROM roadmap_milestones WHERE id = ?",
        (milestone_id,),
    ).fetchone()


# ── Delete ─────────────────────────────────────────────────────────────────

def delete_roadmap(app, roadmap_id: int, user_id: int) -> bool:
    """Delete a roadmap (and its milestones via CASCADE). Returns True if deleted."""
    conn = get_db(app)
    try:
        result = conn.execute(
            "DELETE FROM roadmaps WHERE id = ? AND user_id = ?",
            (roadmap_id, user_id),
        )
        conn.commit()
        return result.rowcount > 0
    except Exception:
        conn.rollback()
        raise


# ── Duplicate-import guard ─────────────────────────────────────────────────

def record_milestone_import(app, milestone_id: int, task_id: int) -> None:
    """Record that a milestone was imported as a task (idempotent on duplicate)."""
    conn = get_db(app)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO milestone_imports (milestone_id, task_id) VALUES (?, ?)",
            (milestone_id, task_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def milestone_already_imported(app, milestone_id: int, user_id: int) -> bool:
    """
    Return True if this milestone was already imported as a task by this user.
    Checks via JOIN to ensure the task belongs to the user.
    """
    conn = get_db(app)
    row = conn.execute(
        """
        SELECT 1
        FROM milestone_imports mi
        JOIN tasks t ON t.id = mi.task_id
        WHERE mi.milestone_id = ? AND t.user_id = ?
        LIMIT 1
        """,
        (milestone_id, user_id),
    ).fetchone()
    return row is not None
