"""
models/__init__.py
User model helpers — thin wrappers around raw sqlite3 queries.

We do NOT use 'with conn:' as a context manager here because for in-memory
testing the connection is a shared singleton and closing it would destroy
the database. Instead we commit/rollback explicitly.
"""

from werkzeug.security import generate_password_hash, check_password_hash
from database import get_db


def create_user(app, name: str, email: str, password: str):
    """
    Insert a new user. Returns the new row id.
    Raises sqlite3.IntegrityError if the email is already taken.
    """
    hashed = generate_password_hash(password)
    conn = get_db(app)
    try:
        cursor = conn.execute(
            "INSERT INTO users (name, email, password) VALUES (?, ?, ?)",
            (name.strip(), email.strip().lower(), hashed),
        )
        conn.commit()
        return cursor.lastrowid
    except Exception:
        conn.rollback()
        raise


def get_user_by_email(app, email: str):
    """Return a user Row by email, or None."""
    conn = get_db(app)
    return conn.execute(
        "SELECT * FROM users WHERE email = ?",
        (email.strip().lower(),),
    ).fetchone()


def get_user_by_id(app, user_id: int):
    """Return a user Row by primary key, or None."""
    conn = get_db(app)
    return conn.execute(
        "SELECT * FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()


def verify_password(stored_hash: str, password: str) -> bool:
    """Check a plaintext password against its stored hash."""
    return check_password_hash(stored_hash, password)
