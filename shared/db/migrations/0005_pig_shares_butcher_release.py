"""Add pig share mode, hanging-weight tracking, and butcher-release workflow tables/columns."""

from __future__ import annotations

import sqlite3

VERSION = 5
NAME = "pig_shares_butcher_release"


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
    """Extend batches/preorders/harvests for share mode and create harvest_shares table."""
    _ensure_columns(
        conn,
        "batches",
        [
            ("sale_unit", "TEXT NOT NULL DEFAULT 'whole'"),
            ("customer_pays_butcher", "INTEGER NOT NULL DEFAULT 0"),
            ("butcher_name", "TEXT NOT NULL DEFAULT ''"),
            ("butcher_phone", "TEXT NOT NULL DEFAULT ''"),
            ("butcher_email", "TEXT NOT NULL DEFAULT ''"),
        ],
    )

    _ensure_columns(
        conn,
        "batch_preorders",
        [
            ("share_fraction", "REAL NOT NULL DEFAULT 1.0"),
            ("share_label", "TEXT NOT NULL DEFAULT ''"),
        ],
    )

    _ensure_columns(
        conn,
        "batch_harvests",
        [
            ("animal_label", "TEXT NOT NULL DEFAULT ''"),
            ("live_weight_lbs", "REAL"),
            ("weight_basis", "TEXT NOT NULL DEFAULT 'dressed'"),
        ],
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS harvest_shares (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            harvest_id INTEGER NOT NULL,
            batch_id INTEGER NOT NULL,
            share_slot TEXT NOT NULL DEFAULT 'whole',
            share_fraction REAL NOT NULL DEFAULT 1.0,
            share_weight_lbs REAL,
            customer_id INTEGER,
            for_sale INTEGER NOT NULL DEFAULT 1,
            deposit_paid REAL NOT NULL DEFAULT 0.0,
            actual_sale_price REAL,
            customer_butcher_fee REAL NOT NULL DEFAULT 0.0,
            payment_status TEXT NOT NULL DEFAULT 'unpaid',
            payment_method TEXT NOT NULL DEFAULT '',
            paid_at TEXT NOT NULL DEFAULT '',
            butcher_notified_at TEXT NOT NULL DEFAULT '',
            pickup_status TEXT NOT NULL DEFAULT 'unassigned',
            claimed_at TEXT NOT NULL DEFAULT '',
            cut_sheet_notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (harvest_id) REFERENCES batch_harvests(id) ON DELETE CASCADE,
            FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE,
            FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_harvest_shares_batch ON harvest_shares(batch_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_harvest_shares_harvest ON harvest_shares(harvest_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_harvest_shares_customer ON harvest_shares(customer_id)"
    )
