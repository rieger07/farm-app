"""Add head_count and customer_id columns to batch_sales for live/pet animal batch sales."""

from __future__ import annotations

import sqlite3

VERSION = 9
NAME = "live_batch_sales"


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, ddl_type: str
) -> None:
    cols = {
        str(row["name"]).lower()
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column.lower() not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


def upgrade(conn: sqlite3.Connection) -> None:
    """Add head_count and customer_id columns to batch_sales safely for live DB upgrades."""
    _add_column_if_missing(conn, "batch_sales", "head_count", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "batch_sales", "customer_id", "INTEGER")
