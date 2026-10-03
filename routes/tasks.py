"""
routes/tasks.py — Task management routes (login required).

URL map
-------
GET  /tasks                     – list all tasks for current user
GET  /tasks?category=<slug>     – list tasks filtered by category
POST /tasks/create              – create a new task
POST /tasks/<id>/complete       – mark task complete
POST /tasks/<id>/reopen         – mark task incomplete
POST /tasks/<id>/delete         – delete a task
"""

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
from models.tasks import (
    CATEGORIES,
    CATEGORY_SLUGS,
    create_task,
    get_tasks,
    get_task,
    set_task_complete,
    delete_task,
    validate_task,
)
from routes.dashboard import login_required
from utils.csrf import csrf_protected

tasks_bp = Blueprint("tasks", __name__)


def _current_user_id() -> int:
    return session["user_id"]


# ── List / filter ──────────────────────────────────────────────────────────

@tasks_bp.route("/tasks")
@login_required
def task_list():
    uid = _current_user_id()
    app = current_app._get_current_object()

    # ?category= accepts a slug (e.g. "dsa") or exact display name
    raw_cat = request.args.get("category", "").strip()
    active_category: str | None = None

    if raw_cat:
        # Try slug first, then exact match
        if raw_cat in CATEGORY_SLUGS:
            active_category = CATEGORY_SLUGS[raw_cat]
        elif raw_cat in CATEGORIES:
            active_category = raw_cat
        else:
            flash("Unknown category filter.", "warning")

    tasks = get_tasks(app, uid, category=active_category)

    return render_template(
        "tasks.html",
        tasks=tasks,
        categories=CATEGORIES,
        category_slugs=CATEGORY_SLUGS,
        active_category=active_category,
        raw_cat=raw_cat,
    )


# ── Create ─────────────────────────────────────────────────────────────────

@tasks_bp.route("/tasks/create", methods=["POST"])
@login_required
@csrf_protected
def task_create():
    uid = _current_user_id()
    app = current_app._get_current_object()

    title       = request.form.get("title", "").strip()
    category    = request.form.get("category", "").strip()
    description = request.form.get("description", "").strip()
    due_date    = request.form.get("due_date", "").strip()

    errors = validate_task(title, category, due_date)
    if errors:
        for e in errors:
            flash(e, "error")
    else:
        create_task(
            app,
            uid,
            title=title,
            category=category,
            description=description or None,
            due_date=due_date or None,
        )
        flash("Task created successfully.", "success")

    # Redirect back — preserve category filter if present
    cat_arg = request.form.get("redirect_category", "")
    if cat_arg:
        return redirect(url_for("tasks.task_list", category=cat_arg))
    return redirect(url_for("tasks.task_list"))


# ── Complete ───────────────────────────────────────────────────────────────

@tasks_bp.route("/tasks/<int:task_id>/complete", methods=["POST"])
@login_required
@csrf_protected
def task_complete(task_id: int):
    uid = _current_user_id()
    app = current_app._get_current_object()

    if not set_task_complete(app, task_id, uid, complete=True):
        abort(404)

    flash("Task marked as complete! 🎉", "success")
    return _redirect_back()


# ── Reopen ─────────────────────────────────────────────────────────────────

@tasks_bp.route("/tasks/<int:task_id>/reopen", methods=["POST"])
@login_required
@csrf_protected
def task_reopen(task_id: int):
    uid = _current_user_id()
    app = current_app._get_current_object()

    if not set_task_complete(app, task_id, uid, complete=False):
        abort(404)

    flash("Task reopened.", "info")
    return _redirect_back()


# ── Delete ─────────────────────────────────────────────────────────────────

@tasks_bp.route("/tasks/<int:task_id>/delete", methods=["POST"])
@login_required
@csrf_protected
def task_delete(task_id: int):
    uid = _current_user_id()
    app = current_app._get_current_object()

    if not delete_task(app, task_id, uid):
        abort(404)

    flash("Task deleted.", "info")
    return _redirect_back()


# ── Helper ─────────────────────────────────────────────────────────────────

def _redirect_back():
    """Redirect to referer if safe, otherwise fall back to task list."""
    cat_arg = request.form.get("redirect_category", "")
    if cat_arg:
        return redirect(url_for("tasks.task_list", category=cat_arg))
    return redirect(url_for("tasks.task_list"))
