"""
wsgi.py — WSGI entry point for Gunicorn and AWS Elastic Beanstalk.

Gunicorn usage:
    gunicorn --bind 0.0.0.0:8000 --workers 2 wsgi:application

This file is intentionally separate from app.py so that:
  - Tests import from app.py (create_app factory) without triggering
    a module-level create_app() call and without touching the real DB.
  - Gunicorn points here and gets a fully-initialised WSGI application.
"""

import sys

try:
    from app import create_app
    application = create_app()
except RuntimeError as exc:
    # A missing or insecure FLASK_SECRET_KEY raises RuntimeError at startup.
    # Print a clear message so the error shows in Gunicorn / EB logs rather than
    # a silent worker crash.
    print(
        f"[PrepMate] STARTUP ERROR — application could not be created: {exc}\n"
        "Set the FLASK_SECRET_KEY environment variable to a strong random value.",
        file=sys.stderr,
        flush=True,
    )
    sys.exit(1)
