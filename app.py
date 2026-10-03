"""
app.py — PrepMate Flask application factory.

Production entry point:  wsgi.py  (used by Gunicorn)
Development entry point: python app.py
Test entry point:        pytest (imports create_app directly)
"""

from flask import Flask
from config import Config, _INSECURE_KEY
from database import init_db


def create_app(config: dict | None = None) -> Flask:
    """
    Application factory.  Pass a config dict to override any Config value
    (used by tests and wsgi.py).  The dict is applied *after* Config, so
    test values always win.
    """
    app = Flask(__name__)
    app.config.from_object(Config)

    if config:
        app.config.update(config)

    # ── Production safety guard ─────────────────────────────────────────
    # Refuse to start in production with the placeholder secret key.
    # Tests and development are excluded via DEBUG=True / TESTING=True.
    if (
        not app.config.get("DEBUG")
        and not app.config.get("TESTING")
        and app.config.get("SECRET_KEY") == _INSECURE_KEY
    ):
        raise RuntimeError(
            "FLASK_SECRET_KEY is not set. "
            "Set a strong random key in the environment before running in production."
        )

    # ── Database ────────────────────────────────────────────────────────
    init_db(app)

    # ── Jinja globals ───────────────────────────────────────────────────
    from utils.csrf import generate_csrf_token
    app.jinja_env.globals["csrf_token"] = generate_csrf_token

    # ── Blueprints ──────────────────────────────────────────────────────
    from routes.auth import auth_bp
    from routes.dashboard import dashboard_bp
    from routes.main import main_bp
    from routes.tasks import tasks_bp
    from routes.planner import planner_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(planner_bp)

    return app


if __name__ == "__main__":
    # Development server only — never use in production.
    _app = create_app()
    _app.run(debug=_app.config["DEBUG"])
