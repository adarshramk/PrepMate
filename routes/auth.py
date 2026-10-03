"""
routes/auth.py — Registration, login, and logout.
"""

import sqlite3
import re
from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    current_app,
)
from models import create_user, get_user_by_email, verify_password

auth_bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _validate_register(name, email, password, confirm):
    """Return a list of validation error strings, or empty list if valid."""
    errors = []
    if not name or len(name.strip()) < 2:
        errors.append("Name must be at least 2 characters.")
    if not email or not EMAIL_RE.match(email):
        errors.append("Enter a valid email address.")
    if not password or len(password) < 8:
        errors.append("Password must be at least 8 characters.")
    if password != confirm:
        errors.append("Passwords do not match.")
    return errors


# ── Register ────────────────────────────────────────────────────────────────

@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("dashboard.dashboard"))

    if request.method == "POST":
        name     = request.form.get("name", "").strip()
        email    = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")

        errors = _validate_register(name, email, password, confirm)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "register.html", name=name, email=email
            )

        try:
            user_id = create_user(current_app._get_current_object(), name, email, password)
            session["user_id"] = user_id
            session["user_name"] = name
            flash("Account created! Welcome to PrepMate.", "success")
            return redirect(url_for("dashboard.dashboard"))
        except sqlite3.IntegrityError:
            flash("An account with that email already exists. Please log in.", "error")
            return render_template("register.html", name=name, email=email)

    return render_template("register.html")


# ── Login ────────────────────────────────────────────────────────────────────

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard.dashboard"))

    if request.method == "POST":
        email    = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        if not email or not password:
            flash("Please enter both email and password.", "error")
            return render_template("login.html", email=email)

        user = get_user_by_email(current_app._get_current_object(), email)
        if user is None or not verify_password(user["password"], password):
            flash("Invalid email or password.", "error")
            return render_template("login.html", email=email)

        session["user_id"]   = user["id"]
        session["user_name"] = user["name"]
        flash(f"Welcome back, {user['name']}!", "success")
        return redirect(url_for("dashboard.dashboard"))

    return render_template("login.html")


# ── Logout ───────────────────────────────────────────────────────────────────

@auth_bp.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("main.index"))
