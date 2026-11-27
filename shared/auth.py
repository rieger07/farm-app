"""Shared user authentication and session management backed by Core Shared SQLite tables."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import hmac
import secrets
import sqlite3

from shared.db import get_connection


def hash_password(
    password: str, salt_hex: str | None = None
) -> tuple[str, str]:
    """Return (password_hash_hex, salt_hex) using PBKDF2-HMAC-SHA256."""
    if not salt_hex:
        salt_hex = secrets.token_hex(16)
    salt_bytes = bytes.fromhex(salt_hex)
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt_bytes, 200_000
    )
    return derived.hex(), salt_hex


def verify_password(password: str, password_hash: str, salt_hex: str) -> bool:
    """Verify a password against its stored hash and salt."""
    computed_hash, _ = hash_password(password, salt_hex)
    return hmac.compare_digest(computed_hash, password_hash)


def count_users() -> int:
    """Return the total number of registered user accounts."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()
        return int(row["cnt"]) if row else 0
    finally:
        conn.close()


def list_users() -> list[dict]:
    """List all users ordered by ID ascending."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, username, display_name, is_admin, created_at FROM users ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_user_by_id(user_id: int) -> dict | None:
    """Return a user record by primary key ID, or None if not found."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, username, display_name, is_admin, created_at FROM users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_by_username(username: str) -> dict | None:
    """Return a user record by case-insensitive username, or None if not found."""
    clean_username = str(username).strip()
    if not clean_username:
        return None
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, username, display_name, is_admin, created_at FROM users WHERE username = ? COLLATE NOCASE",
            (clean_username,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_user(
    username: str,
    password: str,
    display_name: str = "",
    is_admin: bool | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Create a user in the shared ``users`` table and return its public fields."""
    clean_username = str(username).strip()
    if not clean_username:
        raise ValueError("Username cannot be empty.")
    if len(str(password)) < 4:
        raise ValueError("Password must be at least 4 characters.")

    clean_display = str(display_name).strip() or clean_username
    pw_hash, pw_salt = hash_password(str(password))
    now_iso = datetime.utcnow().isoformat(timespec="seconds")

    owns_conn = conn is None
    c = conn if conn is not None else get_connection()
    try:
        existing_cnt = int(
            c.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()["cnt"]
        )
        first_user = existing_cnt == 0
        resolved_admin = (
            1
            if (is_admin if is_admin is not None else first_user)
            else 0
        )
        try:
            cursor = c.execute(
                """
                INSERT INTO users (username, display_name, password_hash, password_salt, is_admin, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_username,
                    clean_display,
                    pw_hash,
                    pw_salt,
                    resolved_admin,
                    now_iso,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                f"Username '{clean_username}' is already taken."
            ) from exc

        new_uid = int(cursor.lastrowid)
        if owns_conn:
            c.commit()
        return {
            "id": new_uid,
            "username": clean_username,
            "display_name": clean_display,
            "is_admin": bool(resolved_admin),
            "created_at": now_iso,
        }
    finally:
        if owns_conn:
            c.close()


def authenticate_user(username: str, password: str) -> dict | None:
    """Verify username and password, returning the user dict or None."""
    clean_username = str(username).strip()
    if not clean_username or not password:
        return None
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
            (clean_username,),
        ).fetchone()
        if not row:
            return None
        if not verify_password(
            str(password), row["password_hash"], row["password_salt"]
        ):
            return None
        return {
            "id": int(row["id"]),
            "username": row["username"],
            "display_name": row["display_name"],
            "is_admin": bool(row["is_admin"]),
            "created_at": row["created_at"],
        }
    finally:
        conn.close()


def update_user_display_name(user_id: int, display_name: str) -> str:
    """Update a user's display name and return the cleaned value."""
    clean = str(display_name or "").strip()[:120]
    if not clean:
        raise ValueError("Display name cannot be empty.")
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
        if not row:
            raise ValueError("User not found.")
        conn.execute(
            "UPDATE users SET display_name = ? WHERE id = ?",
            (clean, int(user_id)),
        )
        conn.commit()
        return clean
    finally:
        conn.close()


def update_user_password(user_id: int, new_password: str) -> None:
    """Update a user's stored password hash and salt."""
    if len(str(new_password)) < 4:
        raise ValueError("Password must be at least 4 characters.")
    pw_hash, pw_salt = hash_password(str(new_password))
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?",
            (pw_hash, pw_salt, int(user_id)),
        )
        conn.commit()
    finally:
        conn.close()


def set_user_admin(user_id: int, is_admin: bool) -> None:
    """Promote or demote a platform Super Admin, preserving at least one admin."""
    conn = get_connection()
    try:
        if not is_admin:
            admins = conn.execute(
                "SELECT COUNT(*) FROM users WHERE is_admin = 1"
            ).fetchone()[0]
            target = conn.execute(
                "SELECT is_admin FROM users WHERE id = ?", (int(user_id),)
            ).fetchone()
            if target and target["is_admin"] and admins <= 1:
                raise ValueError("Cannot remove admin privileges from the last Super Admin.")
        conn.execute(
            "UPDATE users SET is_admin = ? WHERE id = ?",
            (1 if is_admin else 0, int(user_id)),
        )
        conn.commit()
    finally:
        conn.close()


def _hash_session_token(raw_token: str) -> str:
    return hashlib.sha256(str(raw_token).encode("utf-8")).hexdigest()


def create_user_session(user_id: int, days_valid: int = 30) -> str:
    """Create a persistent hashed session token for ``user_id``."""
    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_session_token(raw_token)
    now = datetime.utcnow()
    expires = now + timedelta(days=max(1, int(days_valid)))
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM user_sessions WHERE expires_at < ?",
            (now.isoformat(timespec="seconds"),),
        )
        conn.execute(
            """
            INSERT INTO user_sessions (token, user_id, expires_at, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                token_hash,
                int(user_id),
                expires.isoformat(timespec="seconds"),
                now.isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return raw_token


def get_user_by_session_token(token: str) -> dict | None:
    """Look up an active session token and return the associated user dict."""
    if not token:
        return None
    token_hash = _hash_session_token(str(token))
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT u.id, u.username, u.display_name, u.is_admin, u.created_at
            FROM user_sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token = ? AND s.expires_at >= ?
            """,
            (token_hash, now_iso),
        ).fetchone()
        if not row:
            return None
        return {
            "id": int(row["id"]),
            "username": row["username"],
            "display_name": row["display_name"],
            "is_admin": bool(row["is_admin"]),
            "created_at": row["created_at"],
        }
    finally:
        conn.close()


def delete_user_session(token: str) -> None:
    """Invalidate a session token."""
    if not token:
        return
    token_hash = _hash_session_token(str(token))
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM user_sessions WHERE token = ? OR token = ?",
            (token_hash, str(token)),
        )
        conn.commit()
    finally:
        conn.close()
