"""Baseline schema and legacy-column migration for Homestead Planner and Iron Ledger."""

from __future__ import annotations

import secrets
import sqlite3

from homestead.config import (
    get_species_defaults as _get_static_species_defaults,
    infer_lifecycle_type,
)

VERSION = 1
NAME = "baseline"


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
    """Create Core, Homestead Planner, and Iron Ledger tables and apply legacy upgrades."""
    # =========================================================================
    # 1. Core Shared Tables (Users, Sessions, Workspaces / Merchants, Invites)
    # =========================================================================
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            display_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS merchants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            venmo_handle TEXT NOT NULL DEFAULT '',
            paypal_handle TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    _ensure_columns(
        conn,
        "merchants",
        [
            ("venmo_handle", "TEXT NOT NULL DEFAULT ''"),
            ("paypal_handle", "TEXT NOT NULL DEFAULT ''"),
        ],
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS merchant_members (
            merchant_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('owner', 'manager', 'staff')),
            created_at TEXT NOT NULL,
            PRIMARY KEY (merchant_id, user_id),
            FOREIGN KEY (merchant_id) REFERENCES merchants(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_members_user ON merchant_members(user_id)"
    )

    # One-time onboarding invite tokens (either create a new merchant, or join an existing merchant).
    conn.execute(
        """
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
        )
        """
    )

    # =========================================================================
    # 2. Homestead Planner Tables
    # =========================================================================
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (user_id, key),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS species_presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            species TEXT NOT NULL COLLATE NOCASE,
            lifecycle_type TEXT NOT NULL DEFAULT 'direct_arrival',
            days_to_birth INTEGER NOT NULL DEFAULT 0,
            starter_weeks INTEGER NOT NULL DEFAULT 3,
            starter_feed_lbs REAL NOT NULL DEFAULT 3.0,
            grower_weeks INTEGER NOT NULL DEFAULT 5,
            grower_feed_lbs REAL NOT NULL DEFAULT 12.0,
            starter_bag_price REAL NOT NULL DEFAULT 24.0,
            grower_bag_price REAL NOT NULL DEFAULT 22.0,
            unit_cost REAL NOT NULL DEFAULT 4.0,
            processing_cost REAL NOT NULL DEFAULT 5.0,
            expected_yield_lbs REAL NOT NULL DEFAULT 4.5,
            sale_price_per_lb REAL NOT NULL DEFAULT 6.5,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, species),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT NOT NULL,
            species TEXT NOT NULL,
            lifecycle_type TEXT,
            quantity INTEGER NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            days_to_birth INTEGER NOT NULL,
            weeks_to_proc INTEGER NOT NULL,
            feed_total REAL NOT NULL,
            starter_weeks INTEGER,
            starter_feed_lbs REAL,
            grower_weeks INTEGER,
            grower_feed_lbs REAL,
            starter_bag_price REAL,
            grower_bag_price REAL,
            unit_cost REAL,
            processing_cost REAL,
            expected_yield_lbs REAL,
            sale_price_per_lb REAL,
            actual_feed_lbs REAL,
            actual_feed_cost REAL,
            picking_enabled INTEGER DEFAULT 0,
            picking_slug TEXT,
            max_birds_per_customer INTEGER,
            preorder_enabled INTEGER DEFAULT 0,
            preorder_cap INTEGER,
            deposit_per_bird REAL DEFAULT 0.0,
            preorder_notes TEXT DEFAULT '',
            picking_preorder_only INTEGER DEFAULT 0,
            pickup_date TEXT DEFAULT '',
            pickup_window TEXT DEFAULT '',
            pickup_location TEXT DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    _ensure_columns(
        conn,
        "batches",
        [
            ("user_id", "INTEGER"),
            ("lifecycle_type", "TEXT"),
            ("starter_weeks", "INTEGER"),
            ("starter_feed_lbs", "REAL"),
            ("grower_weeks", "INTEGER"),
            ("grower_feed_lbs", "REAL"),
            ("starter_bag_price", "REAL"),
            ("grower_bag_price", "REAL"),
            ("unit_cost", "REAL"),
            ("processing_cost", "REAL"),
            ("expected_yield_lbs", "REAL"),
            ("sale_price_per_lb", "REAL"),
            ("actual_feed_lbs", "REAL"),
            ("actual_feed_cost", "REAL"),
            ("picking_enabled", "INTEGER DEFAULT 0"),
            ("picking_slug", "TEXT"),
            ("max_birds_per_customer", "INTEGER"),
            ("preorder_enabled", "INTEGER DEFAULT 0"),
            ("preorder_cap", "INTEGER"),
            ("deposit_per_bird", "REAL DEFAULT 0.0"),
            ("preorder_notes", "TEXT DEFAULT ''"),
            ("picking_preorder_only", "INTEGER DEFAULT 0"),
            ("pickup_date", "TEXT DEFAULT ''"),
            ("pickup_window", "TEXT DEFAULT ''"),
            ("pickup_location", "TEXT DEFAULT ''"),
        ],
    )
    # Never alter existing batches.picking_slug values; only populate missing/empty slugs.
    for row in conn.execute(
        "SELECT id FROM batches WHERE picking_slug IS NULL OR picking_slug = ''"
    ).fetchall():
        conn.execute(
            "UPDATE batches SET picking_slug = ? WHERE id = ?",
            (secrets.token_urlsafe(10), row["id"]),
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_batches_picking_slug ON batches(picking_slug)"
    )

    # Backfill legacy batches missing phased-feed / sale_price / lifecycle_type columns
    legacy_rows = conn.execute(
        "SELECT * FROM batches WHERE starter_weeks IS NULL OR starter_feed_lbs IS NULL"
        " OR sale_price_per_lb IS NULL OR lifecycle_type IS NULL"
    ).fetchall()
    for row in legacy_rows:
        d = _get_static_species_defaults(row["species"])
        total_wks = max(1, int(row["weeks_to_proc"] or d["weeks_to_proc"]))
        total_fd = max(0.0, float(row["feed_total"] or d["feed_total"]))
        default_wks = max(1, d["starter_weeks"] + d["grower_weeks"])
        default_fd = max(0.001, d["starter_feed_lbs"] + d["grower_feed_lbs"])

        st_wks = min(
            total_wks,
            max(0, round(total_wks * (d["starter_weeks"] / default_wks))),
        )
        gr_wks = max(0, total_wks - st_wks)
        st_fd = round(total_fd * (d["starter_feed_lbs"] / default_fd), 2)
        gr_fd = round(max(0.0, total_fd - st_fd), 2)
        lc_type = row["lifecycle_type"] or infer_lifecycle_type(
            row["species"], int(row["days_to_birth"] or 0)
        )

        conn.execute(
            """
            UPDATE batches
            SET lifecycle_type = COALESCE(lifecycle_type, ?),
                starter_weeks = COALESCE(starter_weeks, ?),
                starter_feed_lbs = COALESCE(starter_feed_lbs, ?),
                grower_weeks = COALESCE(grower_weeks, ?),
                grower_feed_lbs = COALESCE(grower_feed_lbs, ?),
                starter_bag_price = COALESCE(starter_bag_price, ?),
                grower_bag_price = COALESCE(grower_bag_price, ?),
                unit_cost = COALESCE(unit_cost, ?),
                processing_cost = COALESCE(processing_cost, ?),
                expected_yield_lbs = COALESCE(expected_yield_lbs, ?),
                sale_price_per_lb = COALESCE(sale_price_per_lb, ?)
            WHERE id = ?
            """,
            (
                lc_type,
                st_wks,
                st_fd,
                gr_wks,
                gr_fd,
                d["starter_bag_price"],
                d["grower_bag_price"],
                d["unit_cost"],
                d["processing_cost"],
                d["expected_yield_lbs"],
                d["sale_price_per_lb"],
                row["id"],
            ),
        )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS batch_losses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            loss_date TEXT NOT NULL,
            count INTEGER NOT NULL,
            reason TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS batch_harvests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            harvest_date TEXT NOT NULL,
            head_count INTEGER NOT NULL,
            dressed_lbs REAL NOT NULL,
            processing_cost_total REAL NOT NULL,
            notes TEXT DEFAULT '',
            receipt_url TEXT DEFAULT '',
            drive_file_id TEXT DEFAULT '',
            customer_id INTEGER,
            for_sale INTEGER NOT NULL DEFAULT 1,
            deposit_paid REAL NOT NULL DEFAULT 0,
            payment_status TEXT NOT NULL DEFAULT 'unpaid',
            pickup_status TEXT NOT NULL DEFAULT 'unassigned',
            claimed_at TEXT DEFAULT '',
            actual_sale_price REAL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
        )
        """
    )
    _ensure_columns(
        conn,
        "batch_harvests",
        [
            ("receipt_url", "TEXT DEFAULT ''"),
            ("drive_file_id", "TEXT DEFAULT ''"),
            ("customer_id", "INTEGER"),
            ("for_sale", "INTEGER NOT NULL DEFAULT 1"),
            ("deposit_paid", "REAL NOT NULL DEFAULT 0"),
            ("payment_status", "TEXT NOT NULL DEFAULT 'unpaid'"),
            ("pickup_status", "TEXT NOT NULL DEFAULT 'unassigned'"),
            ("claimed_at", "TEXT DEFAULT ''"),
            ("actual_sale_price", "REAL"),
        ],
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS batch_sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            entry_date TEXT NOT NULL,
            allocation_type TEXT NOT NULL,
            weight_lbs REAL NOT NULL,
            revenue REAL NOT NULL,
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telemetry_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT DEFAULT '',
            event_type TEXT NOT NULL,
            action TEXT NOT NULL,
            workspace TEXT DEFAULT '',
            duration_ms REAL,
            details TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_telemetry_created ON telemetry_events(id DESC)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS batch_milestones_ack (
            batch_id INTEGER NOT NULL,
            task TEXT NOT NULL,
            done_at TEXT NOT NULL,
            PRIMARY KEY (batch_id, task),
            FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS batch_feed_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            feed_date TEXT NOT NULL,
            feed_type TEXT NOT NULL DEFAULT 'grower',
            bags REAL NOT NULL DEFAULT 0,
            feed_lbs REAL NOT NULL DEFAULT 0,
            cost_total REAL NOT NULL DEFAULT 0,
            notes TEXT DEFAULT '',
            receipt_url TEXT DEFAULT '',
            drive_file_id TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
        )
        """
    )
    _ensure_columns(
        conn,
        "batch_feed_logs",
        [
            ("receipt_url", "TEXT DEFAULT ''"),
            ("drive_file_id", "TEXT DEFAULT ''"),
        ],
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS google_drive_tokens (
            user_id INTEGER PRIMARY KEY,
            google_email TEXT DEFAULT '',
            access_token_enc TEXT DEFAULT '',
            refresh_token_enc TEXT DEFAULT '',
            token_expires_at TEXT DEFAULT '',
            folder_id TEXT DEFAULT '',
            folder_name TEXT DEFAULT 'Homestead Planner Receipts',
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS farm_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            batch_id INTEGER,
            expense_date TEXT NOT NULL,
            vendor TEXT DEFAULT '',
            category TEXT NOT NULL DEFAULT 'Other',
            amount REAL NOT NULL DEFAULT 0,
            notes TEXT DEFAULT '',
            receipt_url TEXT DEFAULT '',
            drive_file_id TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE SET NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            phone TEXT DEFAULT '',
            email TEXT DEFAULT '',
            preferred_contact TEXT NOT NULL DEFAULT 'sms',
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_customers_user ON customers(user_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS batch_preorders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            customer_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'reserved',
            deposit_per_bird REAL NOT NULL DEFAULT 0.0,
            deposit_paid REAL NOT NULL DEFAULT 0.0,
            deposit_status TEXT NOT NULL DEFAULT 'unpaid',
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_batch_preorders_batch ON batch_preorders(batch_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_batch_preorders_cust ON batch_preorders(customer_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS layer_flock_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            species TEXT NOT NULL DEFAULT 'chicken',
            event_date TEXT NOT NULL,
            delta INTEGER NOT NULL,
            reason TEXT NOT NULL DEFAULT 'purchase',
            source_batch_id INTEGER,
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (source_batch_id) REFERENCES batches(id) ON DELETE SET NULL
        )
        """
    )
    _ensure_columns(
        conn,
        "layer_flock_events",
        [("species", "TEXT NOT NULL DEFAULT 'chicken'")],
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_layer_flock_user_date ON layer_flock_events(user_id, event_date)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS egg_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            species TEXT NOT NULL DEFAULT 'chicken',
            log_date TEXT NOT NULL,
            total_collected INTEGER NOT NULL DEFAULT 0,
            cracked_unusable INTEGER NOT NULL DEFAULT 0,
            home_used INTEGER NOT NULL DEFAULT 0,
            notes TEXT DEFAULT '',
            created_by INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    _ensure_columns(
        conn,
        "egg_logs",
        [
            ("created_by", "INTEGER"),
            ("species", "TEXT NOT NULL DEFAULT 'chicken'"),
        ],
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_egg_logs_user_date ON egg_logs(user_id, log_date)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS egg_pack_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            species TEXT NOT NULL DEFAULT 'chicken',
            pack_date TEXT NOT NULL,
            dozens_packed INTEGER NOT NULL DEFAULT 1,
            pack_size INTEGER NOT NULL DEFAULT 12,
            eggs_used INTEGER NOT NULL DEFAULT 12,
            ledger_sku_id INTEGER,
            ledger_entry_id INTEGER,
            notes TEXT DEFAULT '',
            created_by INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    _ensure_columns(
        conn,
        "egg_pack_logs",
        [
            ("created_by", "INTEGER"),
            ("species", "TEXT NOT NULL DEFAULT 'chicken'"),
            ("pack_size", "INTEGER NOT NULL DEFAULT 12"),
        ],
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_egg_pack_user_date ON egg_pack_logs(user_id, pack_date)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS layer_feed_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            feed_date TEXT NOT NULL,
            feed_type TEXT NOT NULL DEFAULT 'layer',
            species_tags TEXT NOT NULL DEFAULT 'all',
            bags REAL NOT NULL DEFAULT 1.0,
            feed_lbs REAL NOT NULL DEFAULT 50.0,
            cost_total REAL NOT NULL DEFAULT 0.0,
            notes TEXT DEFAULT '',
            receipt_url TEXT DEFAULT '',
            drive_file_id TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    _ensure_columns(
        conn,
        "layer_feed_logs",
        [
            ("feed_type", "TEXT NOT NULL DEFAULT 'layer'"),
            ("species_tags", "TEXT NOT NULL DEFAULT 'all'"),
        ],
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_layer_feed_user_date ON layer_feed_logs(user_id, feed_date)"
    )

    # =========================================================================
    # 3. Iron Ledger Tables
    # =========================================================================
    # One Zettle connection per merchant (not per user). Tokens are encrypted at rest.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS zettle_connections (
            merchant_id INTEGER PRIMARY KEY,
            zettle_org_uuid TEXT,
            access_token TEXT,
            refresh_token TEXT,
            expires_at TEXT,
            scopes TEXT,
            connected_at TEXT NOT NULL,
            FOREIGN KEY (merchant_id) REFERENCES merchants(id) ON DELETE CASCADE
        )
        """
    )

    # Catalog mirrored read-only from Zettle. Stock fields live in stock_ledger.
    conn.execute(
        """
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
            item_type TEXT NOT NULL DEFAULT 'product',
            updated_at TEXT NOT NULL,
            UNIQUE (merchant_id, zettle_product_uuid, zettle_variant_uuid),
            FOREIGN KEY (merchant_id) REFERENCES merchants(id) ON DELETE CASCADE
        )
        """
    )
    _ensure_columns(
        conn,
        "skus",
        [
            ("show_in_quick_sale", "INTEGER NOT NULL DEFAULT 1"),
            ("sell_mode", "TEXT NOT NULL DEFAULT 'unit'"),
            ("item_type", "TEXT NOT NULL DEFAULT 'product'"),
        ],
    )

    # Unified sales: Zettle purchases and cash quick-sales.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            merchant_id INTEGER NOT NULL,
            source TEXT NOT NULL CHECK (source IN ('zettle', 'cash')),
            external_id TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            total_cents INTEGER NOT NULL,
            cash_received_cents INTEGER,
            payment_type TEXT,
            created_by INTEGER,
            status TEXT NOT NULL DEFAULT 'completed',
            created_at TEXT NOT NULL,
            UNIQUE (merchant_id, source, external_id),
            FOREIGN KEY (merchant_id) REFERENCES merchants(id) ON DELETE CASCADE,
            FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    _ensure_columns(conn, "sales", [("payment_type", "TEXT")])
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sales_time ON sales(merchant_id, occurred_at)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sale_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL,
            sku_id INTEGER,
            name_snapshot TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            unit_price_cents INTEGER NOT NULL,
            FOREIGN KEY (sale_id) REFERENCES sales(id) ON DELETE CASCADE,
            FOREIGN KEY (sku_id) REFERENCES skus(id) ON DELETE SET NULL
        )
        """
    )

    # Append-only stock ledger. On hand = SUM(delta). Negative totals are allowed.
    conn.execute(
        """
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
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ledger_sku ON stock_ledger(merchant_id, sku_id)"
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_sale_item
            ON stock_ledger(sale_item_id) WHERE sale_item_id IS NOT NULL
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_state (
            merchant_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (merchant_id, key),
            FOREIGN KEY (merchant_id) REFERENCES merchants(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS merchant_subscriptions (
            merchant_id INTEGER PRIMARY KEY,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            plan_key TEXT NOT NULL DEFAULT 'lifetime',
            status TEXT NOT NULL DEFAULT 'comp' CHECK (status IN ('comp', 'trialing', 'active', 'past_due', 'canceled')),
            current_period_end TEXT,
            grace_until TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (merchant_id) REFERENCES merchants(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_subs_customer ON merchant_subscriptions(stripe_customer_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_subs_sub ON merchant_subscriptions(stripe_subscription_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS billing_checkout_invites (
            stripe_session_id TEXT PRIMARY KEY,
            invite_token TEXT NOT NULL,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            plan_key TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS marketplaces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            merchant_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            location TEXT NOT NULL DEFAULT '',
            booth_fee_cents INTEGER NOT NULL DEFAULT 0,
            travel_cost_cents INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY (merchant_id) REFERENCES merchants(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_marketplaces_merchant ON marketplaces(merchant_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_days (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            merchant_id INTEGER NOT NULL,
            market_date TEXT NOT NULL,
            marketplace_id INTEGER NOT NULL,
            booth_fee_cents INTEGER NOT NULL DEFAULT 0,
            travel_cost_cents INTEGER NOT NULL DEFAULT 0,
            starting_cash_cents INTEGER NOT NULL DEFAULT 0,
            counted_cash_cents INTEGER,
            status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
            started_at TEXT,
            closed_at TEXT,
            weather TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE (merchant_id, market_date),
            FOREIGN KEY (merchant_id) REFERENCES merchants(id) ON DELETE CASCADE,
            FOREIGN KEY (marketplace_id) REFERENCES marketplaces(id) ON DELETE CASCADE
        )
        """
    )
    _ensure_columns(
        conn,
        "market_days",
        [
            ("starting_cash_cents", "INTEGER NOT NULL DEFAULT 0"),
            ("counted_cash_cents", "INTEGER"),
            ("status", "TEXT NOT NULL DEFAULT 'open'"),
            ("started_at", "TEXT"),
            ("closed_at", "TEXT"),
        ],
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_days_merchant_date ON market_days(merchant_id, market_date)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_day_items (
            market_day_id INTEGER NOT NULL,
            sku_id INTEGER NOT NULL,
            brought_qty REAL NOT NULL DEFAULT 0,
            write_off_qty REAL NOT NULL DEFAULT 0,
            counted_left_qty REAL,
            closeout_ledger_id INTEGER,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (market_day_id, sku_id),
            FOREIGN KEY (market_day_id) REFERENCES market_days(id) ON DELETE CASCADE,
            FOREIGN KEY (sku_id) REFERENCES skus(id) ON DELETE CASCADE,
            FOREIGN KEY (closeout_ledger_id) REFERENCES stock_ledger(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mday_items_day ON market_day_items(market_day_id)"
    )
