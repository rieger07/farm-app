"""Create perennial herds, named animals, and herd feed/hay log tables."""

from __future__ import annotations

import sqlite3

VERSION = 4
NAME = "perennial_herds"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create herds, herd_animals, and herd_feed_logs tables and indexes."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS herds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            species TEXT NOT NULL DEFAULT 'pig',
            herd_type TEXT NOT NULL DEFAULT 'breeding',
            head_count INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_herds_user ON herds(user_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS herd_animals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            herd_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            sex_role TEXT NOT NULL DEFAULT '',
            color TEXT NOT NULL DEFAULT '',
            birth_year INTEGER,
            active INTEGER NOT NULL DEFAULT 1,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (herd_id) REFERENCES herds(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_herd_animals_herd ON herd_animals(herd_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS herd_feed_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            herd_id INTEGER NOT NULL,
            feed_date TEXT NOT NULL,
            feed_category TEXT NOT NULL DEFAULT 'grain',
            quantity REAL NOT NULL DEFAULT 0,
            unit TEXT NOT NULL DEFAULT 'bags',
            weight_lbs REAL NOT NULL DEFAULT 0,
            cost_total REAL NOT NULL DEFAULT 0,
            vendor TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            receipt_url TEXT NOT NULL DEFAULT '',
            drive_file_id TEXT NOT NULL DEFAULT '',
            receipt_group_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (herd_id) REFERENCES herds(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_herd_feed_user_date ON herd_feed_logs(user_id, feed_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_herd_feed_herd ON herd_feed_logs(herd_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_herd_feed_group ON herd_feed_logs(receipt_group_id)"
    )
