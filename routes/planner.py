"""
routes/planner.py — AI-powered preparation planner.

URL map
-------
GET  /planner                       – list saved roadmaps
GET  /planner/new                   – roadmap input form
POST /planner/generate              – validate inputs, call LLM, save draft, redirect to review
GET  /planner/<id>/review           – review a draft roadmap
POST /planner/<id>/approve          – approve draft → saved
POST /planner/<id>/delete           – delete roadmap (draft or saved)
POST /planner/<id>/import           – import selected milestones as tasks
"""

from __future__ import annotations
import json
import re
from datetime import date, timedelta

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    abort,
    current_app,
)

from routes.dashboard import login_required
from utils.csrf import csrf_protected
from services.llm import generate_roadmap
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
from models.tasks import CATEGORIES, create_task

planner_bp = Blueprint("planner", __name__)

# ── Input validation ───────────────────────────────────────────────────────

_ROLE_RE = re.compile(r"^[\w\s&,./()\-+]{2,100}$")


def _validate_inputs(
    target_role: str,
    skills: str,
    hours_str: str,
    weeks_str: str,
) -> list[str]:
    errors: list[str] = []

    target_role = target_role.strip()
    if not target_role or not _ROLE_RE.match(target_role):
        errors.append("Target role must be 2–100 characters (letters, spaces, and basic punctuation).")

    skills = skills.strip()
    if not skills or len(skills) < 2:
        errors.append("Please describe your current skills (at least 2 characters).")
    elif len(skills) > 500:
        errors.append("Skills description must be 500 characters or fewer.")

    try:
        hours = float(hours_str)
        if not (0.5 <= hours <= 16):
            raise ValueError
    except (TypeError, ValueError):
        errors.append("Study hours per day must be between 0.5 and 16.")

    try:
        weeks = int(weeks_str)
        if not (1 <= weeks <= 52):
            raise ValueError
    except (TypeError, ValueError):
        errors.append("Preparation duration must be between 1 and 52 weeks.")

    return errors


# ── List saved roadmaps ────────────────────────────────────────────────────

@planner_bp.route("/planner")
@login_required
def planner_list():
    app = current_app._get_current_object()
    uid = session["user_id"]
    roadmaps = get_roadmaps_for_user(app, uid, include_drafts=False)
    return render_template("planner_list.html", roadmaps=roadmaps)


# ── Input form ─────────────────────────────────────────────────────────────

@planner_bp.route("/planner/new")
@login_required
def planner_new():
    return render_template("planner_form.html")


# ── Generate (POST) ────────────────────────────────────────────────────────

@planner_bp.route("/planner/generate", methods=["POST"])
@login_required
@csrf_protected
def planner_generate():
    app = current_app._get_current_object()
    uid = session["user_id"]

    target_role = request.form.get("target_role", "").strip()
    skills      = request.form.get("skills", "").strip()
    hours_str   = request.form.get("hours_per_day", "").strip()
    weeks_str   = request.form.get("duration_weeks", "").strip()

    errors = _validate_inputs(target_role, skills, hours_str, weeks_str)
    if errors:
        for e in errors:
            flash(e, "error")
        return render_template(
            "planner_form.html",
            target_role=target_role,
            skills=skills,
            hours_per_day=hours_str,
            duration_weeks=weeks_str,
        )

    hours_per_day  = float(hours_str)
    duration_weeks = int(weeks_str)

    # Call LLM (or fallback) — never raises
    roadmap_data = generate_roadmap(app, target_role, skills, hours_per_day, duration_weeks)

    # Persist immediately as a draft (associated with authenticated user)
    roadmap_id = save_roadmap(
        app,
        user_id=uid,
        target_role=target_role,
        skills=skills,
        hours_per_day=hours_per_day,
        duration_weeks=duration_weeks,
        title=roadmap_data["title"],
        source=roadmap_data["source"],
        milestones=roadmap_data["milestones"],
        is_draft=True,
        failure_reason=roadmap_data.get("failure_reason"),
    )

    flash(
        "Your roadmap has been generated. Review it below and approve to save it.",
        "info",
    )
    return redirect(url_for("planner.planner_review", roadmap_id=roadmap_id))


# ── Review draft ───────────────────────────────────────────────────────────

@planner_bp.route("/planner/<int:roadmap_id>/review")
@login_required
def planner_review(roadmap_id: int):
    app = current_app._get_current_object()
    uid = session["user_id"]

    roadmap = get_roadmap(app, roadmap_id, uid)
    if roadmap is None:
        abort(404)

    milestones = get_milestones(app, roadmap_id)
    # Deserialise daily_tasks JSON for each milestone
    milestones_with_tasks = _deserialise_milestones(milestones)

    return render_template(
        "planner_review.html",
        roadmap=roadmap,
        milestones=milestones_with_tasks,
        is_draft=bool(roadmap["is_draft"]),
    )


# ── Approve draft → saved ──────────────────────────────────────────────────

@planner_bp.route("/planner/<int:roadmap_id>/approve", methods=["POST"])
@login_required
@csrf_protected
def planner_approve(roadmap_id: int):
    app = current_app._get_current_object()
    uid = session["user_id"]

    if not approve_roadmap(app, roadmap_id, uid):
        abort(404)

    flash("Roadmap saved! You can now import milestones as tasks.", "success")
    return redirect(url_for("planner.planner_review", roadmap_id=roadmap_id))


# ── Delete ─────────────────────────────────────────────────────────────────

@planner_bp.route("/planner/<int:roadmap_id>/delete", methods=["POST"])
@login_required
@csrf_protected
def planner_delete(roadmap_id: int):
    app = current_app._get_current_object()
    uid = session["user_id"]

    if not delete_roadmap(app, roadmap_id, uid):
        abort(404)

    flash("Roadmap deleted.", "info")
    return redirect(url_for("planner.planner_list"))


# ── Import milestones as tasks ─────────────────────────────────────────────

@planner_bp.route("/planner/<int:roadmap_id>/import", methods=["POST"])
@login_required
@csrf_protected
def planner_import(roadmap_id: int):
    app = current_app._get_current_object()
    uid = session["user_id"]

    # Verify roadmap ownership
    roadmap = get_roadmap(app, roadmap_id, uid)
    if roadmap is None:
        abort(404)

    if roadmap["is_draft"]:
        flash("Approve the roadmap before importing tasks.", "warning")
        return redirect(url_for("planner.planner_review", roadmap_id=roadmap_id))

    selected_ids_raw = request.form.getlist("milestone_ids")
    if not selected_ids_raw:
        flash("No milestones selected.", "warning")
        return redirect(url_for("planner.planner_review", roadmap_id=roadmap_id))

    imported = 0
    skipped  = 0
    today    = date.today()

    for raw_id in selected_ids_raw:
        try:
            mid = int(raw_id)
        except (ValueError, TypeError):
            continue

        m = get_milestone(app, mid)
        if m is None or m["roadmap_id"] != roadmap_id:
            # Milestone doesn't exist or belongs to a different roadmap
            continue

        if milestone_already_imported(app, mid, uid):
            skipped += 1
            continue

        # Compute due date: today + week_number * 7 days
        due_date = (today + timedelta(weeks=int(m["week_number"]))).isoformat()

        # Validate category (safety net — should always be valid from LLM/fallback)
        category = m["category"] if m["category"] in CATEGORIES else CATEGORIES[0]

        task_id = create_task(
            app,
            uid,
            title=m["title"],
            category=category,
            description=m["description"] or None,
            due_date=due_date,
        )
        record_milestone_import(app, mid, task_id)
        imported += 1

    if imported:
        flash(f"{imported} milestone(s) added to your task list.", "success")
    if skipped:
        flash(f"{skipped} milestone(s) skipped (already imported).", "info")

    return redirect(url_for("planner.planner_review", roadmap_id=roadmap_id))


# ── Helpers ────────────────────────────────────────────────────────────────

def _deserialise_milestones(milestones) -> list[dict]:
    """Convert sqlite Row objects to plain dicts with daily_tasks as list."""
    result = []
    for m in milestones:
        d = dict(m)
        try:
            d["daily_tasks"] = json.loads(d["daily_tasks"]) if d["daily_tasks"] else []
        except (json.JSONDecodeError, TypeError):
            d["daily_tasks"] = []
        result.append(d)
    return result
