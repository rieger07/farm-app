"""Create farm_notes and farm_note_comments tables for Farm -> Team Notes."""

from __future__ import annotations

import sqlite3

VERSION = 8
NAME = "farm_team_notes"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create shared team notes & threaded comment tables and indexes."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS farm_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT 'general',
            pinned INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0,
            batch_id INTEGER,
            herd_id INTEGER,
            attachment_url TEXT NOT NULL DEFAULT '',
            drive_file_id TEXT NOT NULL DEFAULT '',
            created_by INTEGER,
            updated_by INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE SET NULL,
            FOREIGN KEY (herd_id) REFERENCES herds(id) ON DELETE SET NULL,
            FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL,
            FOREIGN KEY (updated_by) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_farm_notes_user_arch_pin "
        "ON farm_notes(user_id, archived, pinned, updated_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_farm_notes_batch ON farm_notes(batch_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_farm_notes_herd ON farm_notes(herd_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS farm_note_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            author_id INTEGER,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (note_id) REFERENCES farm_notes(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (author_id) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_farm_note_comments_note "
        "ON farm_note_comments(note_id, id)"
    )
