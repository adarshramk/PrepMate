"""
utils/csrf.py
Minimal session-based CSRF protection — no external library required.

Usage:
  Template:    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  Route:       validate_csrf()   # aborts 400 if token missing/invalid
  Decorator:   @csrf_protected   # wraps a route function
"""

import secrets
from functools import wraps
from flask import session, request, abort, current_app


def generate_csrf_token() -> str:
    """Return (and cache in session) a CSRF token for this session."""
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_hex(32)
    return session["_csrf_token"]


def validate_csrf() -> None:
    """Abort with 400 if the submitted CSRF token does not match the session token.
    Skipped entirely when WTF_CSRF_ENABLED is False (test config).
    """
    if not current_app.config.get("WTF_CSRF_ENABLED", True):
        return

    token_in_session = session.get("_csrf_token")
    token_in_form    = request.form.get("csrf_token", "")

    if not token_in_session or not secrets.compare_digest(
        token_in_session, token_in_form
    ):
        abort(400, description="CSRF validation failed.")


def csrf_protected(f):
    """Decorator: validate CSRF token on POST requests."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == "POST":
            validate_csrf()
        return f(*args, **kwargs)
    return decorated
