"""Add separate fiber_grade and fleece_factors columns to herd_animals and fiber_shearings."""

from __future__ import annotations

import sqlite3

VERSION = 7
NAME = "alpaca_color_factors_and_grade"


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
    """Add fiber_grade and fleece_factors columns safely for live DB upgrades."""
    _add_column_if_missing(conn, "herd_animals", "fiber_grade", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "herd_animals", "fleece_factors", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "fiber_shearings", "fiber_grade", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "fiber_shearings", "fleece_factors", "TEXT NOT NULL DEFAULT ''")
