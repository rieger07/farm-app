"""Add vendor and split-receipt group ID columns to feed and expense logs."""

from __future__ import annotations

import sqlite3

VERSION = 2
NAME = "receipt_vendor_and_grouping"


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
    """Add vendor and receipt_group_id columns for unified Schedule F receipts."""
    _ensure_columns(
        conn,
        "batch_feed_logs",
        [
            ("vendor", "TEXT DEFAULT ''"),
            ("receipt_group_id", "TEXT DEFAULT ''"),
        ],
    )
    _ensure_columns(
        conn,
        "layer_feed_logs",
        [
            ("vendor", "TEXT DEFAULT ''"),
            ("receipt_group_id", "TEXT DEFAULT ''"),
        ],
    )
    _ensure_columns(
        conn,
        "farm_expenses",
        [
            ("receipt_group_id", "TEXT DEFAULT ''"),
        ],
    )
