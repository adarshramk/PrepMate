"""
routes/dashboard.py — Student dashboard (login required).
"""

from functools import wraps
from flask import (
    Blueprint,
    render_template,
    redirect,
    url_for,
    session,
    flash,
    current_app,
)
from models import get_user_by_id
from models.tasks import get_task_stats, CATEGORY_SLUGS

dashboard_bp = Blueprint("dashboard", __name__)


def login_required(f):
    """Decorator: redirect to login if not authenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to access your dashboard.", "info")
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated


@dashboard_bp.route("/dashboard")
@login_required
def dashboard():
    app = current_app._get_current_object()
    user = get_user_by_id(app, session["user_id"])
    if user is None:
        session.clear()
        flash("Session expired. Please log in again.", "info")
        return redirect(url_for("auth.login"))

    stats = get_task_stats(app, session["user_id"])

    sections = [
        {
            "id":          "dsa",
            "slug":        "dsa",
            "title":       "DSA & Programming",
            "icon":        "💻",
            "description": "Data structures, algorithms, and competitive programming prep.",
            "topics":      ["Arrays & Strings", "Linked Lists", "Trees & Graphs", "Dynamic Programming", "Sorting & Searching"],
        },
        {
            "id":          "aptitude",
            "slug":        "aptitude",
            "title":       "Aptitude",
            "icon":        "🧮",
            "description": "Quantitative aptitude, logical reasoning, and verbal ability.",
            "topics":      ["Quantitative Math", "Logical Reasoning", "Verbal Ability", "Data Interpretation", "Mock Tests"],
        },
        {
            "id":          "cloud",
            "slug":        "cloud",
            "title":       "Cloud & Networking",
            "icon":        "☁️",
            "description": "AWS, Azure, GCP fundamentals and networking concepts.",
            "topics":      ["AWS Core Services", "Networking Basics", "Linux CLI", "Docker & Containers", "System Design"],
        },
        {
            "id":          "projects",
            "slug":        "projects",
            "title":       "Projects",
            "icon":        "🚀",
            "description": "Guided project ideas to strengthen your portfolio.",
            "topics":      ["Web Apps", "CLI Tools", "API Projects", "ML Demos", "Open Source Contributions"],
        },
        {
            "id":          "interview",
            "slug":        "interview",
            "title":       "Interview Preparation",
            "icon":        "🎯",
            "description": "HR questions, technical interviews, and mock sessions.",
            "topics":      ["Behavioural Questions", "Technical HR", "Resume Tips", "Mock Interviews", "Salary Negotiation"],
        },
    ]

    return render_template(
        "dashboard.html",
        user=user,
        sections=sections,
        stats=stats,
    )
