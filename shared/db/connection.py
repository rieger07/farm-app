"""Canonical SQLite connection factory shared by Homestead Planner and Iron Ledger."""

from __future__ import annotations

import os
import sqlite3


def get_db_path() -> str:
    """Return the path to the shared SQLite database file."""
    return os.environ.get("FARM_DB_PATH", os.path.join("data", "homestead.db"))


def get_connection(*, isolation_level: str | None = "") -> sqlite3.Connection:
    """Open a configured SQLite connection with foreign keys and busy_timeout enabled."""
    db_path = get_db_path()
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=isolation_level)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn
