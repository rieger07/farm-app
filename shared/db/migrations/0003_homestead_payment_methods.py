"""Add payment_method and paid_at columns to Homestead paid harvest and batch sale rows."""

from __future__ import annotations

import sqlite3

VERSION = 3
NAME = "homestead_payment_methods"


def _ensure_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: list[tuple[str, str]],
) -> None:
    """Add any missing columns to ``table`` idempotently."""
    existing = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for col_name, col_type in columns:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")


def upgrade(conn: sqlite3.Connection) -> None:
    """Add payment_method and paid_at tracking columns to batch_harvests and batch_sales."""
    _ensure_columns(
        conn,
        "batch_harvests",
        [
            ("payment_method", "TEXT NOT NULL DEFAULT ''"),
            ("paid_at", "TEXT NOT NULL DEFAULT ''"),
        ],
    )
    _ensure_columns(
        conn,
        "batch_sales",
        [
            ("payment_method", "TEXT NOT NULL DEFAULT ''"),
        ],
    )
