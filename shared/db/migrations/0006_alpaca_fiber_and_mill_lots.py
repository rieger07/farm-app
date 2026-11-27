"""Create alpaca/fiber annual shearing logs, mill lots, raw-fleece inputs, and finished outputs tables."""

from __future__ import annotations

import sqlite3

VERSION = 6
NAME = "alpaca_fiber_and_mill_lots"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create fiber_shearings, fiber_mill_lots, fiber_mill_lot_inputs, and fiber_lot_outputs."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fiber_shearings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            herd_id INTEGER NOT NULL,
            animal_id INTEGER,
            shear_date TEXT NOT NULL,
            fleece_color TEXT NOT NULL DEFAULT '',
            prime_lbs REAL NOT NULL DEFAULT 0,
            seconds_lbs REAL NOT NULL DEFAULT 0,
            total_lbs REAL NOT NULL DEFAULT 0,
            shearing_fee REAL NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (herd_id) REFERENCES herds(id) ON DELETE CASCADE,
            FOREIGN KEY (animal_id) REFERENCES herd_animals(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fiber_shearings_user_date ON fiber_shearings(user_id, shear_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fiber_shearings_herd ON fiber_shearings(herd_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fiber_shearings_animal ON fiber_shearings(animal_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fiber_mill_lots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            herd_id INTEGER NOT NULL,
            lot_name TEXT NOT NULL,
            mill_name TEXT NOT NULL DEFAULT '',
            color_group TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'sent_to_mill',
            sent_date TEXT NOT NULL DEFAULT '',
            received_date TEXT NOT NULL DEFAULT '',
            shipping_out_cost REAL NOT NULL DEFAULT 0,
            milling_fee REAL NOT NULL DEFAULT 0,
            shipping_back_cost REAL NOT NULL DEFAULT 0,
            other_handling_fee REAL NOT NULL DEFAULT 0,
            include_in_tax_books INTEGER NOT NULL DEFAULT 1,
            receipt_url TEXT NOT NULL DEFAULT '',
            drive_file_id TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (herd_id) REFERENCES herds(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fiber_mill_lots_user ON fiber_mill_lots(user_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fiber_mill_lots_herd ON fiber_mill_lots(herd_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fiber_mill_lot_inputs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lot_id INTEGER NOT NULL,
            shearing_id INTEGER NOT NULL,
            raw_lbs_used REAL NOT NULL DEFAULT 0,
            FOREIGN KEY (lot_id) REFERENCES fiber_mill_lots(id) ON DELETE CASCADE,
            FOREIGN KEY (shearing_id) REFERENCES fiber_shearings(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fiber_lot_inputs_lot ON fiber_mill_lot_inputs(lot_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fiber_lot_inputs_shearing ON fiber_mill_lot_inputs(shearing_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fiber_lot_outputs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            lot_id INTEGER NOT NULL,
            product_type TEXT NOT NULL DEFAULT 'yarn_skein',
            product_name TEXT NOT NULL,
            unit TEXT NOT NULL DEFAULT 'skeins',
            quantity REAL NOT NULL DEFAULT 0,
            finished_weight_oz REAL NOT NULL DEFAULT 0,
            ledger_sku_id INTEGER,
            ledger_entry_id INTEGER,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (lot_id) REFERENCES fiber_mill_lots(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fiber_lot_outputs_lot ON fiber_lot_outputs(lot_id)"
    )
