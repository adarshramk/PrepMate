"""
routes/main.py — Public landing page and infrastructure endpoints.
"""

from flask import Blueprint, render_template, redirect, url_for, session, jsonify, current_app
from database import get_db

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def index():
    # Redirect logged-in users straight to dashboard
    if "user_id" in session:
        return redirect(url_for("dashboard.dashboard"))
    return render_template("index.html")


@main_bp.route("/health")
def health():
    """
    Health check endpoint for load balancers, Docker HEALTHCHECK, and AWS ALB.

    Returns HTTP 200 with a JSON body when the application is ready to serve
    requests.  Probes the database to confirm connectivity.

    Returns HTTP 503 if the database is unreachable.
    """
    app = current_app._get_current_object()
    try:
        conn = get_db(app)
        conn.execute("SELECT 1").fetchone()
        db_ok = True
    except Exception:
        db_ok = False

    status = "ok" if db_ok else "degraded"
    http_code = 200 if db_ok else 503

    return jsonify({
        "status":   status,
        "db":       "ok" if db_ok else "error",
        "app":      "PrepMate",
    }), http_code
