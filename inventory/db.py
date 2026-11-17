"""SQLite connection and schema for the inventory service.

Shares the same database file (``FARM_DB_PATH``) and ``users`` table as the
Streamlit planner. All inventory tables are keyed by ``merchant_id``.
"""

import sqlite3

from homestead.database import get_db_path, init_db

SCHEMA = """
CREATE TABLE IF NOT EXISTS merchants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merchant_members (
    merchant_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner', 'manager', 'staff')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (merchant_id, user_id),
    FOREIGN KEY (merchant_id) REFERENCES merchants(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_members_user ON merchant_members(user_id);

-- One Zettle connection per merchant (not per user). Tokens are encrypted at rest.
CREATE TABLE IF NOT EXISTS zettle_connections (
    merchant_id INTEGER PRIMARY KEY,
    zettle_org_uuid TEXT,
    access_token TEXT,
    refresh_token TEXT,
    expires_at TEXT,
    scopes TEXT,
    connected_at TEXT NOT NULL,
    FOREIGN KEY (merchant_id) REFERENCES merchants(id) ON DELETE CASCADE
);

-- Catalog mirrored read-only from Zettle. Stock fields live in stock_ledger.
CREATE TABLE IF NOT EXISTS skus (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant_id INTEGER NOT NULL,
    zettle_product_uuid TEXT NOT NULL,
    zettle_variant_uuid TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    variant_name TEXT NOT NULL DEFAULT '',
    sku_code TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    price_cents INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'USD',
    active INTEGER NOT NULL DEFAULT 1,
    low_stock_threshold REAL NOT NULL DEFAULT 10,
    show_in_quick_sale INTEGER NOT NULL DEFAULT 1,
    sell_mode TEXT NOT NULL DEFAULT 'unit',
    updated_at TEXT NOT NULL,
    UNIQUE (merchant_id, zettle_product_uuid, zettle_variant_uuid),
    FOREIGN KEY (merchant_id) REFERENCES merchants(id) ON DELETE CASCADE
);

-- Unified sales: Zettle purchases and cash quick-sales.
-- external_id = Zettle purchase UUID, or the client-generated UUID for cash.
CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant_id INTEGER NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('zettle', 'cash')),
    external_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    total_cents INTEGER NOT NULL,
    cash_received_cents INTEGER,
    created_by INTEGER,
    status TEXT NOT NULL DEFAULT 'completed',
    created_at TEXT NOT NULL,
    UNIQUE (merchant_id, source, external_id),
    FOREIGN KEY (merchant_id) REFERENCES merchants(id) ON DELETE CASCADE,
    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_sales_time ON sales(merchant_id, occurred_at);

CREATE TABLE IF NOT EXISTS sale_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id INTEGER NOT NULL,
    sku_id INTEGER,
    name_snapshot TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    unit_price_cents INTEGER NOT NULL,
    FOREIGN KEY (sale_id) REFERENCES sales(id) ON DELETE CASCADE,
    FOREIGN KEY (sku_id) REFERENCES skus(id) ON DELETE SET NULL
);

-- Append-only stock ledger. On hand = SUM(delta). Negative totals are allowed.
CREATE TABLE IF NOT EXISTS stock_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant_id INTEGER NOT NULL,
    sku_id INTEGER NOT NULL,
    delta INTEGER NOT NULL,
    reason TEXT NOT NULL CHECK (reason IN ('initial', 'restock', 'sale', 'adjustment')),
    sale_item_id INTEGER,
    note TEXT NOT NULL DEFAULT '',
    created_by INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (merchant_id) REFERENCES merchants(id) ON DELETE CASCADE,
    FOREIGN KEY (sku_id) REFERENCES skus(id) ON DELETE CASCADE,
    FOREIGN KEY (sale_item_id) REFERENCES sale_items(id) ON DELETE CASCADE,
    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_sku ON stock_ledger(merchant_id, sku_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_sale_item
    ON stock_ledger(sale_item_id) WHERE sale_item_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS sync_state (
    merchant_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (merchant_id, key),
    FOREIGN KEY (merchant_id) REFERENCES merchants(id) ON DELETE CASCADE
);

-- One-time onboarding invite tokens (either create a new merchant, or join an existing merchant).
CREATE TABLE IF NOT EXISTS merchant_invites (
    token TEXT PRIMARY KEY,
    merchant_id INTEGER,
    default_merchant_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL CHECK (role IN ('owner', 'manager', 'staff')),
    note TEXT NOT NULL DEFAULT '',
    created_by INTEGER,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    used_by INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (merchant_id) REFERENCES merchants(id) ON DELETE CASCADE,
    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (used_by) REFERENCES users(id) ON DELETE SET NULL
);
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_inventory_db() -> None:
    """Create planner tables (for ``users``) plus all inventory tables.

    Enables WAL so the sync worker and web service can share the file.
    """
    init_db()
    conn = get_connection()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sales)")}
        if "payment_type" not in cols:  # added after the first schema version
            conn.execute("ALTER TABLE sales ADD COLUMN payment_type TEXT")
        sku_cols = {r["name"] for r in conn.execute("PRAGMA table_info(skus)")}
        if "show_in_quick_sale" not in sku_cols:
            conn.execute("ALTER TABLE skus ADD COLUMN show_in_quick_sale INTEGER NOT NULL DEFAULT 1")
        if "sell_mode" not in sku_cols:
            conn.execute("ALTER TABLE skus ADD COLUMN sell_mode TEXT NOT NULL DEFAULT 'unit'")
        conn.commit()
    finally:
        conn.close()
