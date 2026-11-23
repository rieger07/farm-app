"""SQLite persistence layer, multi-user authentication, species presets, automatic schema migrations, and JSON backup/restore."""

from datetime import date, datetime, timedelta
import hashlib
import hmac
import json
import os
import secrets
import sqlite3

try:
    import streamlit as st
except ModuleNotFoundError:
    st = None  # type: ignore[assignment]

from homestead.config import (
    DEFAULT_BAG_WEIGHT,
    DEFAULT_BARREL_CAPACITY,
    LIFECYCLE_TYPES,
    LIVESTOCK_DATA,
    get_species_defaults as _get_static_species_defaults,
    infer_lifecycle_type,
)


def get_db_path() -> str:
    return os.environ.get("FARM_DB_PATH", os.path.join("data", "homestead.db"))


def get_db_connection() -> sqlite3.Connection:
    db_path = get_db_path()
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_active_user_id() -> int | None:
    """Return the currently authenticated user_id from Streamlit session_state if present."""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        if get_script_run_ctx(suppress_warning=True) is None:
            return None
        uid = st.session_state.get("current_user_id")
        return int(uid) if uid is not None else None
    except Exception:
        return None


def _resolve_user_id(user_id: int | None = None) -> int | None:
    if user_id is not None:
        return int(user_id)
    return get_active_user_id()


def init_db() -> None:
    with get_db_connection() as conn:
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
                active INTEGER NOT NULL DEFAULT 1,
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        # Migrate older databases if phased-feed / cost / reconciliation / user_id / lifecycle_type columns are missing
        existing_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(batches)").fetchall()
        }
        new_columns = [
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
        ]
        for col_name, col_type in new_columns:
            if col_name not in existing_cols:
                conn.execute(
                    f"ALTER TABLE batches ADD COLUMN {col_name} {col_type}"
                )
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

        # Backfill any rows that were created before phased-feed / sale_price / lifecycle_type columns existed
        legacy_rows = conn.execute(
            "SELECT * FROM batches WHERE starter_weeks IS NULL OR starter_feed_lbs IS NULL OR sale_price_per_lb IS NULL OR lifecycle_type IS NULL"
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
                created_at TEXT NOT NULL,
                FOREIGN KEY (batch_id) REFERENCES batches(id) ON DELETE CASCADE
            )
            """
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_layer_flock_user_date ON layer_flock_events(user_id, event_date)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS egg_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                log_date TEXT NOT NULL,
                total_collected INTEGER NOT NULL DEFAULT 0,
                cracked_unusable INTEGER NOT NULL DEFAULT 0,
                home_used INTEGER NOT NULL DEFAULT 0,
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_egg_logs_user_date ON egg_logs(user_id, log_date)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS egg_pack_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                pack_date TEXT NOT NULL,
                dozens_packed INTEGER NOT NULL DEFAULT 1,
                eggs_used INTEGER NOT NULL DEFAULT 12,
                ledger_sku_id INTEGER,
                ledger_entry_id INTEGER,
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_layer_feed_user_date ON layer_feed_logs(user_id, feed_date)"
        )
        lf_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(layer_feed_logs)").fetchall()
        }
        if "feed_type" not in lf_cols:
            conn.execute("ALTER TABLE layer_feed_logs ADD COLUMN feed_type TEXT NOT NULL DEFAULT 'layer'")
        for tbl in ("batch_feed_logs", "batch_harvests"):
            cols = {
                row["name"]
                for row in conn.execute(f"PRAGMA table_info({tbl})").fetchall()
            }
            if "receipt_url" not in cols:
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN receipt_url TEXT DEFAULT ''")
            if "drive_file_id" not in cols:
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN drive_file_id TEXT DEFAULT ''")
        hv_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(batch_harvests)").fetchall()
        }
        for col_name, col_type in (
            ("customer_id", "INTEGER"),
            ("for_sale", "INTEGER NOT NULL DEFAULT 1"),
            ("deposit_paid", "REAL NOT NULL DEFAULT 0"),
            ("payment_status", "TEXT NOT NULL DEFAULT 'unpaid'"),
            ("pickup_status", "TEXT NOT NULL DEFAULT 'unassigned'"),
            ("claimed_at", "TEXT DEFAULT ''"),
            ("actual_sale_price", "REAL"),
        ):
            if col_name not in hv_cols:
                conn.execute(
                    f"ALTER TABLE batch_harvests ADD COLUMN {col_name} {col_type}"
                )
        conn.commit()


# ---------------------------------------------------------------------------
# User Authentication & Session Management
# ---------------------------------------------------------------------------
def hash_password(
    password: str, salt_hex: str | None = None
) -> tuple[str, str]:
    """Return (password_hash_hex, salt_hex) using PBKDF2-HMAC-SHA256."""
    if not salt_hex:
        salt_hex = secrets.token_hex(16)
    salt_bytes = bytes.fromhex(salt_hex)
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt_bytes, 200_000
    )
    return derived.hex(), salt_hex


def verify_password(password: str, password_hash: str, salt_hex: str) -> bool:
    computed_hash, _ = hash_password(password, salt_hex)
    return hmac.compare_digest(computed_hash, password_hash)


def count_users() -> int:
    with get_db_connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()
        return int(row["cnt"]) if row else 0


def list_users() -> list[dict]:
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT id, username, display_name, is_admin, created_at FROM users ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_user_by_id(user_id: int) -> dict | None:
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT id, username, display_name, is_admin, created_at FROM users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
        return dict(row) if row else None


def get_user_by_username(username: str) -> dict | None:
    clean_username = str(username).strip()
    if not clean_username:
        return None
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT id, username, display_name, is_admin, created_at FROM users WHERE username = ? COLLATE NOCASE",
            (clean_username,),
        ).fetchone()
        return dict(row) if row else None


def seed_user_species_presets(
    user_id: int,
    reset_defaults: bool = False,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Seed the 4 default species presets for a user."""
    now_iso = datetime.utcnow().isoformat(timespec="seconds")

    def _run(c: sqlite3.Connection) -> None:
        for sp_name, d in LIVESTOCK_DATA.items():
            lc_type = str(
                d.get(
                    "lifecycle_type",
                    infer_lifecycle_type(sp_name, int(d["days_to_birth"])),
                )
            )
            if reset_defaults:
                c.execute(
                    """
                    INSERT INTO species_presets (
                        user_id, species, lifecycle_type, days_to_birth,
                        starter_weeks, starter_feed_lbs, grower_weeks, grower_feed_lbs,
                        starter_bag_price, grower_bag_price, unit_cost,
                        processing_cost, expected_yield_lbs, sale_price_per_lb, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, species) DO UPDATE SET
                        lifecycle_type = excluded.lifecycle_type,
                        days_to_birth = excluded.days_to_birth,
                        starter_weeks = excluded.starter_weeks,
                        starter_feed_lbs = excluded.starter_feed_lbs,
                        grower_weeks = excluded.grower_weeks,
                        grower_feed_lbs = excluded.grower_feed_lbs,
                        starter_bag_price = excluded.starter_bag_price,
                        grower_bag_price = excluded.grower_bag_price,
                        unit_cost = excluded.unit_cost,
                        processing_cost = excluded.processing_cost,
                        expected_yield_lbs = excluded.expected_yield_lbs,
                        sale_price_per_lb = excluded.sale_price_per_lb
                    """,
                    (
                        int(user_id),
                        sp_name.lower(),
                        lc_type,
                        int(d["days_to_birth"]),
                        int(d["starter_weeks"]),
                        float(d["starter_feed_lbs"]),
                        int(d["grower_weeks"]),
                        float(d["grower_feed_lbs"]),
                        float(d["starter_bag_price"]),
                        float(d["grower_bag_price"]),
                        float(d["unit_cost"]),
                        float(d["processing_cost"]),
                        float(d["expected_yield_lbs"]),
                        float(d["sale_price_per_lb"]),
                        now_iso,
                    ),
                )
            else:
                c.execute(
                    """
                    INSERT OR IGNORE INTO species_presets (
                        user_id, species, lifecycle_type, days_to_birth,
                        starter_weeks, starter_feed_lbs, grower_weeks, grower_feed_lbs,
                        starter_bag_price, grower_bag_price, unit_cost,
                        processing_cost, expected_yield_lbs, sale_price_per_lb, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(user_id),
                        sp_name.lower(),
                        lc_type,
                        int(d["days_to_birth"]),
                        int(d["starter_weeks"]),
                        float(d["starter_feed_lbs"]),
                        int(d["grower_weeks"]),
                        float(d["grower_feed_lbs"]),
                        float(d["starter_bag_price"]),
                        float(d["grower_bag_price"]),
                        float(d["unit_cost"]),
                        float(d["processing_cost"]),
                        float(d["expected_yield_lbs"]),
                        float(d["sale_price_per_lb"]),
                        now_iso,
                    ),
                )

    if conn is not None:
        _run(conn)
    else:
        with get_db_connection() as c:
            _run(c)
            c.commit()


def create_user(
    username: str,
    password: str,
    display_name: str = "",
    is_admin: bool | None = None,
    claim_unowned_data: bool = True,
) -> dict:
    clean_username = str(username).strip()
    if not clean_username:
        raise ValueError("Username cannot be empty.")
    if len(str(password)) < 4:
        raise ValueError("Password must be at least 4 characters.")

    clean_display = str(display_name).strip() or clean_username
    pw_hash, pw_salt = hash_password(str(password))
    now_iso = datetime.utcnow().isoformat(timespec="seconds")

    with get_db_connection() as conn:
        existing_cnt = int(
            conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()["cnt"]
        )
        first_user = existing_cnt == 0
        resolved_admin = (
            1
            if (is_admin if is_admin is not None else first_user)
            else 0
        )
        try:
            cursor = conn.execute(
                """
                INSERT INTO users (username, display_name, password_hash, password_salt, is_admin, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_username,
                    clean_display,
                    pw_hash,
                    pw_salt,
                    resolved_admin,
                    now_iso,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                f"Username '{clean_username}' is already taken."
            ) from exc

        new_uid = int(cursor.lastrowid)
        seed_user_species_presets(new_uid, reset_defaults=False, conn=conn)

        if first_user and claim_unowned_data:
            conn.execute(
                "UPDATE batches SET user_id = ? WHERE user_id IS NULL",
                (new_uid,),
            )
            for setting_key in ("barrel_capacity", "bag_weight"):
                row = conn.execute(
                    "SELECT value FROM settings WHERE key = ?", (setting_key,)
                ).fetchone()
                if row:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO user_settings (user_id, key, value)
                        VALUES (?, ?, ?)
                        """,
                        (new_uid, setting_key, str(row["value"])),
                    )
            # Ensure any custom species on claimed batches exist in species_presets
            claimed_batches = conn.execute(
                "SELECT * FROM batches WHERE user_id = ?", (new_uid,)
            ).fetchall()
            for cb in claimed_batches:
                sp_key = str(cb["species"]).strip().lower()
                if sp_key:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO species_presets (
                            user_id, species, lifecycle_type, days_to_birth,
                            starter_weeks, starter_feed_lbs, grower_weeks, grower_feed_lbs,
                            starter_bag_price, grower_bag_price, unit_cost,
                            processing_cost, expected_yield_lbs, sale_price_per_lb, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            new_uid,
                            sp_key,
                            cb["lifecycle_type"]
                            or infer_lifecycle_type(
                                sp_key, int(cb["days_to_birth"] or 0)
                            ),
                            int(cb["days_to_birth"] or 0),
                            int(cb["starter_weeks"] or 3),
                            float(cb["starter_feed_lbs"] or 3.0),
                            int(cb["grower_weeks"] or 5),
                            float(cb["grower_feed_lbs"] or 12.0),
                            float(cb["starter_bag_price"] or 24.0),
                            float(cb["grower_bag_price"] or 22.0),
                            float(cb["unit_cost"] or 4.0),
                            float(cb["processing_cost"] or 5.0),
                            float(cb["expected_yield_lbs"] or 4.5),
                            float(cb["sale_price_per_lb"] or 6.5),
                            now_iso,
                        ),
                    )

        conn.commit()
        return {
            "id": new_uid,
            "username": clean_username,
            "display_name": clean_display,
            "is_admin": bool(resolved_admin),
            "created_at": now_iso,
        }


def authenticate_user(username: str, password: str) -> dict | None:
    clean_username = str(username).strip()
    if not clean_username or not password:
        return None
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
            (clean_username,),
        ).fetchone()
        if not row:
            return None
        if not verify_password(
            str(password), row["password_hash"], row["password_salt"]
        ):
            return None
        return {
            "id": int(row["id"]),
            "username": row["username"],
            "display_name": row["display_name"],
            "is_admin": bool(row["is_admin"]),
            "created_at": row["created_at"],
        }


def update_user_password(user_id: int, new_password: str) -> None:
    if len(str(new_password)) < 4:
        raise ValueError("Password must be at least 4 characters.")
    pw_hash, pw_salt = hash_password(str(new_password))
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?",
            (pw_hash, pw_salt, int(user_id)),
        )
        conn.commit()


def delete_user(user_id: int) -> None:
    with get_db_connection() as conn:
        user_batches = conn.execute(
            "SELECT id FROM batches WHERE user_id = ?", (int(user_id),)
        ).fetchall()
        for b in user_batches:
            bid = int(b["id"])
            conn.execute("DELETE FROM batch_sales WHERE batch_id = ?", (bid,))
            conn.execute(
                "DELETE FROM batch_harvests WHERE batch_id = ?", (bid,)
            )
            conn.execute("DELETE FROM batch_losses WHERE batch_id = ?", (bid,))
        conn.execute("DELETE FROM batches WHERE user_id = ?", (int(user_id),))
        conn.execute(
            "DELETE FROM species_presets WHERE user_id = ?", (int(user_id),)
        )
        conn.execute(
            "DELETE FROM user_settings WHERE user_id = ?", (int(user_id),)
        )
        conn.execute(
            "DELETE FROM user_sessions WHERE user_id = ?", (int(user_id),)
        )
        conn.execute("DELETE FROM users WHERE id = ?", (int(user_id),))
        conn.commit()


def _hash_session_token(raw_token: str) -> str:
    return hashlib.sha256(str(raw_token).encode("utf-8")).hexdigest()


def create_user_session(user_id: int, days_valid: int = 30) -> str:
    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_session_token(raw_token)
    now = datetime.utcnow()
    expires = now + timedelta(days=max(1, int(days_valid)))
    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM user_sessions WHERE expires_at < ?",
            (now.isoformat(timespec="seconds"),),
        )
        conn.execute(
            """
            INSERT INTO user_sessions (token, user_id, expires_at, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                token_hash,
                int(user_id),
                expires.isoformat(timespec="seconds"),
                now.isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    return raw_token


def get_user_by_session_token(token: str) -> dict | None:
    if not token:
        return None
    token_hash = _hash_session_token(str(token))
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT u.id, u.username, u.display_name, u.is_admin, u.created_at
            FROM user_sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token = ? AND s.expires_at >= ?
            """,
            (token_hash, now_iso),
        ).fetchone()
        if not row:
            return None
        return {
            "id": int(row["id"]),
            "username": row["username"],
            "display_name": row["display_name"],
            "is_admin": bool(row["is_admin"]),
            "created_at": row["created_at"],
        }


def delete_user_session(token: str) -> None:
    if not token:
        return
    token_hash = _hash_session_token(str(token))
    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM user_sessions WHERE token = ? OR token = ?",
            (token_hash, str(token)),
        )
        conn.commit()


def is_public_registration_enabled() -> bool:
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'allow_public_registration'"
        ).fetchone()
        if row:
            return str(row["value"]).lower() in ("1", "true", "yes")
    return False


def set_public_registration_enabled(enabled: bool) -> None:
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value)
            VALUES ('allow_public_registration', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            ("1" if enabled else "0",),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Species Presets Catalog (Per-User + Fallback)
# ---------------------------------------------------------------------------
def _row_to_species_preset(row: sqlite3.Row | dict) -> dict:
    st_wks = min(520, max(0, int(row["starter_weeks"])))
    gr_wks = min(520, max(0, int(row["grower_weeks"])))
    if st_wks + gr_wks < 1:
        gr_wks = 1
    st_fd = max(0.0, float(row["starter_feed_lbs"]))
    gr_fd = max(0.0, float(row["grower_feed_lbs"]))
    sp_name = str(row["species"]).strip().lower()
    days_b = min(3650, max(0, int(row["days_to_birth"])))
    lc_type = str(row["lifecycle_type"] or "").strip()
    if lc_type not in LIFECYCLE_TYPES:
        lc_type = infer_lifecycle_type(sp_name, days_b)
    return {
        "species": sp_name,
        "lifecycle_type": lc_type,
        "days_to_birth": days_b,
        "starter_weeks": st_wks,
        "starter_feed_lbs": st_fd,
        "grower_weeks": gr_wks,
        "grower_feed_lbs": gr_fd,
        "starter_bag_price": max(0.0, float(row["starter_bag_price"])),
        "grower_bag_price": max(0.0, float(row["grower_bag_price"])),
        "unit_cost": max(0.0, float(row["unit_cost"])),
        "processing_cost": max(0.0, float(row["processing_cost"])),
        "expected_yield_lbs": max(0.0, float(row["expected_yield_lbs"])),
        "sale_price_per_lb": max(0.0, float(row["sale_price_per_lb"])),
        "weeks_to_proc": st_wks + gr_wks,
        "feed_total": round(st_fd + gr_fd, 4),
    }


def get_species_catalog(user_id: int | None = None) -> dict[str, dict]:
    """Return an ordered dict of {species_lower: preset_dict} for the active user (or global defaults)."""
    uid = _resolve_user_id(user_id)
    if uid is not None:
        with get_db_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM species_presets WHERE user_id = ? ORDER BY id ASC",
                (uid,),
            ).fetchall()
            if not rows:
                # Seed default presets if user exists and has none yet
                user_exists = conn.execute(
                    "SELECT 1 FROM users WHERE id = ?", (uid,)
                ).fetchone()
                if user_exists:
                    seed_user_species_presets(uid, conn=conn)
                    conn.commit()
                    rows = conn.execute(
                        "SELECT * FROM species_presets WHERE user_id = ? ORDER BY id ASC",
                        (uid,),
                    ).fetchall()
            if rows:
                return {
                    str(r["species"]).strip().lower(): _row_to_species_preset(r)
                    for r in rows
                }
    return {k: dict(v) for k, v in LIVESTOCK_DATA.items()}


def get_species_defaults(
    species: str, user_id: int | None = None
) -> dict:
    """Return species defaults from the active user's catalog, falling back to LIVESTOCK_DATA."""
    sp_key = str(species).strip().lower()
    catalog = get_species_catalog(user_id=user_id)
    if sp_key in catalog:
        return dict(catalog[sp_key])
    if sp_key in LIVESTOCK_DATA:
        return dict(LIVESTOCK_DATA[sp_key])
    if catalog:
        first_key = next(iter(catalog))
        return dict(catalog[first_key])
    return dict(LIVESTOCK_DATA["chicken chicks"])


def save_species_preset(
    species: str,
    preset_data: dict,
    user_id: int | None = None,
    apply_to_active_batches: bool = False,
) -> dict:
    """Create or update a species preset for the active user (and optionally update active batches)."""
    sp_key = str(species).strip().lower()
    if not sp_key:
        raise ValueError("Species name cannot be empty.")

    uid = _resolve_user_id(user_id)
    days_to_birth = min(3650, max(0, int(preset_data.get("days_to_birth", 0))))
    lc_type = str(preset_data.get("lifecycle_type") or "").strip()
    if lc_type not in LIFECYCLE_TYPES:
        lc_type = infer_lifecycle_type(sp_key, days_to_birth)
    if lc_type == "direct_arrival":
        days_to_birth = 0

    st_wks = min(520, max(0, int(preset_data.get("starter_weeks", 3))))
    gr_wks = min(520, max(0, int(preset_data.get("grower_weeks", 5))))
    if st_wks + gr_wks < 1:
        gr_wks = 1
    st_fd = max(0.0, float(preset_data.get("starter_feed_lbs", 3.0)))
    gr_fd = max(0.0, float(preset_data.get("grower_feed_lbs", 12.0)))
    st_price = max(0.0, float(preset_data.get("starter_bag_price", 24.0)))
    gr_price = max(0.0, float(preset_data.get("grower_bag_price", 22.0)))
    u_cost = max(0.0, float(preset_data.get("unit_cost", 4.0)))
    p_cost = max(0.0, float(preset_data.get("processing_cost", 5.0)))
    y_lbs = max(0.0, float(preset_data.get("expected_yield_lbs", 4.5)))
    s_price = max(0.0, float(preset_data.get("sale_price_per_lb", 6.5)))

    normalized = {
        "species": sp_key,
        "lifecycle_type": lc_type,
        "days_to_birth": days_to_birth,
        "starter_weeks": st_wks,
        "starter_feed_lbs": st_fd,
        "grower_weeks": gr_wks,
        "grower_feed_lbs": gr_fd,
        "starter_bag_price": st_price,
        "grower_bag_price": gr_price,
        "unit_cost": u_cost,
        "processing_cost": p_cost,
        "expected_yield_lbs": y_lbs,
        "sale_price_per_lb": s_price,
        "weeks_to_proc": st_wks + gr_wks,
        "feed_total": round(st_fd + gr_fd, 4),
    }

    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        if uid is not None:
            conn.execute(
                """
                INSERT INTO species_presets (
                    user_id, species, lifecycle_type, days_to_birth,
                    starter_weeks, starter_feed_lbs, grower_weeks, grower_feed_lbs,
                    starter_bag_price, grower_bag_price, unit_cost,
                    processing_cost, expected_yield_lbs, sale_price_per_lb, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, species) DO UPDATE SET
                    lifecycle_type = excluded.lifecycle_type,
                    days_to_birth = excluded.days_to_birth,
                    starter_weeks = excluded.starter_weeks,
                    starter_feed_lbs = excluded.starter_feed_lbs,
                    grower_weeks = excluded.grower_weeks,
                    grower_feed_lbs = excluded.grower_feed_lbs,
                    starter_bag_price = excluded.starter_bag_price,
                    grower_bag_price = excluded.grower_bag_price,
                    unit_cost = excluded.unit_cost,
                    processing_cost = excluded.processing_cost,
                    expected_yield_lbs = excluded.expected_yield_lbs,
                    sale_price_per_lb = excluded.sale_price_per_lb
                """,
                (
                    uid,
                    sp_key,
                    lc_type,
                    days_to_birth,
                    st_wks,
                    st_fd,
                    gr_wks,
                    gr_fd,
                    st_price,
                    gr_price,
                    u_cost,
                    p_cost,
                    y_lbs,
                    s_price,
                    now_iso,
                ),
            )
        else:
            LIVESTOCK_DATA[sp_key] = dict(normalized)

        if apply_to_active_batches:
            if uid is not None:
                conn.execute(
                    """
                    UPDATE batches
                    SET lifecycle_type = ?, days_to_birth = ?, weeks_to_proc = ?, feed_total = ?,
                        starter_weeks = ?, starter_feed_lbs = ?, grower_weeks = ?, grower_feed_lbs = ?,
                        starter_bag_price = ?, grower_bag_price = ?, unit_cost = ?,
                        processing_cost = ?, expected_yield_lbs = ?, sale_price_per_lb = ?
                    WHERE user_id = ? AND active = 1 AND lower(species) = ?
                    """,
                    (
                        lc_type,
                        days_to_birth,
                        normalized["weeks_to_proc"],
                        normalized["feed_total"],
                        st_wks,
                        st_fd,
                        gr_wks,
                        gr_fd,
                        st_price,
                        gr_price,
                        u_cost,
                        p_cost,
                        y_lbs,
                        s_price,
                        uid,
                        sp_key,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE batches
                    SET lifecycle_type = ?, days_to_birth = ?, weeks_to_proc = ?, feed_total = ?,
                        starter_weeks = ?, starter_feed_lbs = ?, grower_weeks = ?, grower_feed_lbs = ?,
                        starter_bag_price = ?, grower_bag_price = ?, unit_cost = ?,
                        processing_cost = ?, expected_yield_lbs = ?, sale_price_per_lb = ?
                    WHERE active = 1 AND lower(species) = ?
                    """,
                    (
                        lc_type,
                        days_to_birth,
                        normalized["weeks_to_proc"],
                        normalized["feed_total"],
                        st_wks,
                        st_fd,
                        gr_wks,
                        gr_fd,
                        st_price,
                        gr_price,
                        u_cost,
                        p_cost,
                        y_lbs,
                        s_price,
                        sp_key,
                    ),
                )
        conn.commit()
    return normalized


def delete_species_preset(species: str, user_id: int | None = None) -> None:
    sp_key = str(species).strip().lower()
    uid = _resolve_user_id(user_id)
    if uid is not None:
        with get_db_connection() as conn:
            conn.execute(
                "DELETE FROM species_presets WHERE user_id = ? AND lower(species) = ?",
                (uid, sp_key),
            )
            conn.commit()
    elif sp_key in LIVESTOCK_DATA and len(LIVESTOCK_DATA) > 1:
        LIVESTOCK_DATA.pop(sp_key, None)


# ---------------------------------------------------------------------------
# Per-User & Global Settings
# ---------------------------------------------------------------------------
def get_int_setting(
    key: str, default_val: int, user_id: int | None = None
) -> int:
    uid = _resolve_user_id(user_id)
    with get_db_connection() as conn:
        if uid is not None:
            u_row = conn.execute(
                "SELECT value FROM user_settings WHERE user_id = ? AND key = ?",
                (uid, key),
            ).fetchone()
            if u_row:
                try:
                    return max(1, int(float(u_row["value"])))
                except (ValueError, TypeError):
                    return default_val
            return default_val
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if row:
            try:
                return max(1, int(float(row["value"])))
            except (ValueError, TypeError):
                return default_val
    return default_val


def set_int_setting(
    key: str, value: int, user_id: int | None = None
) -> None:
    uid = _resolve_user_id(user_id)
    clean_val = str(max(1, int(value)))
    with get_db_connection() as conn:
        if uid is not None:
            conn.execute(
                """
                INSERT INTO user_settings (user_id, key, value)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value
                """,
                (uid, key, clean_val),
            )
        else:
            conn.execute(
                """
                INSERT INTO settings (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, clean_val),
            )
        conn.commit()


def get_text_setting(
    key: str, default_val: str = "", user_id: int | None = None
) -> str:
    uid = _resolve_user_id(user_id)
    with get_db_connection() as conn:
        if uid is not None:
            row = conn.execute(
                "SELECT value FROM user_settings WHERE user_id = ? AND key = ?",
                (uid, key),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                (key,),
            ).fetchone()
        if row and row["value"] is not None:
            return str(row["value"])
    return default_val


def set_text_setting(
    key: str, value: str, user_id: int | None = None
) -> None:
    uid = _resolve_user_id(user_id)
    clean_val = str(value or "")
    with get_db_connection() as conn:
        if uid is not None:
            conn.execute(
                """
                INSERT INTO user_settings (user_id, key, value)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value
                """,
                (uid, key, clean_val),
            )
        else:
            conn.execute(
                """
                INSERT INTO settings (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, clean_val),
            )
        conn.commit()


def get_barrel_capacity(user_id: int | None = None) -> int:
    return get_int_setting(
        "barrel_capacity", DEFAULT_BARREL_CAPACITY, user_id=user_id
    )


def set_barrel_capacity(capacity: int, user_id: int | None = None) -> None:
    set_int_setting("barrel_capacity", capacity, user_id=user_id)


def get_bag_weight(user_id: int | None = None) -> int:
    return get_int_setting("bag_weight", DEFAULT_BAG_WEIGHT, user_id=user_id)


def set_bag_weight(weight: int, user_id: int | None = None) -> None:
    set_int_setting("bag_weight", weight, user_id=user_id)


# ---------------------------------------------------------------------------
# Batches & Logs CRUD (Scoped by User when authenticated)
# ---------------------------------------------------------------------------
def list_batches(user_id: int | None = None) -> list[dict]:
    uid = _resolve_user_id(user_id)
    with get_db_connection() as conn:
        if uid is not None:
            rows = conn.execute(
                "SELECT * FROM batches WHERE user_id = ? ORDER BY start_date ASC, id ASC",
                (uid,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM batches ORDER BY start_date ASC, id ASC"
            ).fetchall()
        return [dict(r) for r in rows]


def list_batch_losses(
    batch_id: int | None = None, user_id: int | None = None
) -> list[dict]:
    uid = _resolve_user_id(user_id)
    with get_db_connection() as conn:
        if batch_id is not None:
            rows = conn.execute(
                "SELECT * FROM batch_losses WHERE batch_id = ? ORDER BY loss_date ASC, id ASC",
                (int(batch_id),),
            ).fetchall()
        elif uid is not None:
            rows = conn.execute(
                """
                SELECT bl.* FROM batch_losses bl
                JOIN batches b ON b.id = bl.batch_id
                WHERE b.user_id = ?
                ORDER BY bl.loss_date ASC, bl.id ASC
                """,
                (uid,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM batch_losses ORDER BY loss_date ASC, id ASC"
            ).fetchall()
        return [dict(r) for r in rows]


def add_batch_loss(
    batch_id: int, loss_date: str, count: int, reason: str = ""
) -> int:
    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO batch_losses (batch_id, loss_date, count, reason, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                int(batch_id),
                str(loss_date),
                max(1, int(count)),
                str(reason).strip(),
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def delete_batch_loss(loss_id: int) -> None:
    with get_db_connection() as conn:
        conn.execute("DELETE FROM batch_losses WHERE id = ?", (int(loss_id),))
        conn.commit()


def list_batch_harvests(
    batch_id: int | None = None, user_id: int | None = None
) -> list[dict]:
    uid = _resolve_user_id(user_id)
    with get_db_connection() as conn:
        if batch_id is not None:
            rows = conn.execute(
                "SELECT * FROM batch_harvests WHERE batch_id = ? ORDER BY harvest_date ASC, id ASC",
                (int(batch_id),),
            ).fetchall()
        elif uid is not None:
            rows = conn.execute(
                """
                SELECT bh.* FROM batch_harvests bh
                JOIN batches b ON b.id = bh.batch_id
                WHERE b.user_id = ?
                ORDER BY bh.harvest_date ASC, bh.id ASC
                """,
                (uid,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM batch_harvests ORDER BY harvest_date ASC, id ASC"
            ).fetchall()
        return [dict(r) for r in rows]


def add_batch_harvest(
    batch_id: int,
    harvest_date: str,
    head_count: int,
    dressed_lbs: float,
    processing_cost_total: float,
    notes: str = "",
    receipt_url: str = "",
    drive_file_id: str = "",
    for_sale: bool = True,
) -> int:
    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO batch_harvests (
                batch_id, harvest_date, head_count, dressed_lbs, processing_cost_total,
                notes, receipt_url, drive_file_id, for_sale, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(batch_id),
                str(harvest_date),
                max(1, int(head_count)),
                max(0.0, float(dressed_lbs)),
                max(0.0, float(processing_cost_total)),
                str(notes).strip(),
                str(receipt_url or "").strip(),
                str(drive_file_id or "").strip(),
                1 if for_sale else 0,
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def delete_batch_harvest(harvest_id: int) -> None:
    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM batch_harvests WHERE id = ?", (int(harvest_id),)
        )
        conn.commit()


def list_batch_sales(
    batch_id: int | None = None, user_id: int | None = None
) -> list[dict]:
    uid = _resolve_user_id(user_id)
    with get_db_connection() as conn:
        if batch_id is not None:
            rows = conn.execute(
                "SELECT * FROM batch_sales WHERE batch_id = ? ORDER BY entry_date ASC, id ASC",
                (int(batch_id),),
            ).fetchall()
        elif uid is not None:
            rows = conn.execute(
                """
                SELECT bs.* FROM batch_sales bs
                JOIN batches b ON b.id = bs.batch_id
                WHERE b.user_id = ?
                ORDER BY bs.entry_date ASC, bs.id ASC
                """,
                (uid,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM batch_sales ORDER BY entry_date ASC, id ASC"
            ).fetchall()
        return [dict(r) for r in rows]


def add_batch_sale(
    batch_id: int,
    entry_date: str,
    allocation_type: str,
    weight_lbs: float,
    revenue: float,
    notes: str = "",
) -> int:
    resolved_type = (
        "Home Freezer"
        if "freezer" in str(allocation_type).lower()
        else "Customer Sale"
    )
    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO batch_sales (
                batch_id, entry_date, allocation_type, weight_lbs, revenue, notes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(batch_id),
                str(entry_date),
                resolved_type,
                max(0.01, float(weight_lbs)),
                0.0 if resolved_type == "Home Freezer" else max(0.0, float(revenue)),
                str(notes).strip(),
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def delete_batch_sale(sale_id: int) -> None:
    with get_db_connection() as conn:
        conn.execute("DELETE FROM batch_sales WHERE id = ?", (int(sale_id),))
        conn.commit()


def list_batch_feed_logs(batch_id: int) -> list[dict]:
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM batch_feed_logs WHERE batch_id = ? ORDER BY feed_date DESC, id DESC",
            (int(batch_id),),
        ).fetchall()
        return [dict(r) for r in rows]


def add_batch_feed_log(
    batch_id: int,
    feed_date: str,
    feed_type: str,
    bags: float,
    feed_lbs: float,
    cost_total: float,
    notes: str = "",
    receipt_url: str = "",
    drive_file_id: str = "",
) -> int:
    ft = "starter" if str(feed_type).strip().lower() == "starter" else "grower"
    with get_db_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO batch_feed_logs (
                batch_id, feed_date, feed_type, bags, feed_lbs, cost_total,
                notes, receipt_url, drive_file_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(batch_id),
                str(feed_date),
                ft,
                max(0.0, float(bags)),
                max(0.0, float(feed_lbs)),
                max(0.0, float(cost_total)),
                str(notes or "").strip(),
                str(receipt_url or "").strip(),
                str(drive_file_id or "").strip(),
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def delete_batch_feed_log(log_id: int) -> None:
    with get_db_connection() as conn:
        conn.execute("DELETE FROM batch_feed_logs WHERE id = ?", (int(log_id),))
        conn.commit()


def set_batch_actual_feed(
    batch_id: int,
    actual_feed_lbs: float | None,
    actual_feed_cost: float | None,
) -> None:
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE batches SET actual_feed_lbs = ?, actual_feed_cost = ? WHERE id = ?",
            (
                float(actual_feed_lbs) if actual_feed_lbs is not None else None,
                float(actual_feed_cost) if actual_feed_cost is not None else None,
                int(batch_id),
            ),
        )
        conn.commit()


def _normalize_batch_payload(
    batch_data: dict, user_id: int | None = None
) -> dict:
    species = str(batch_data.get("species", "chicken chicks")).strip().lower()
    defaults = get_species_defaults(species, user_id=user_id)

    has_phased = (
        "starter_weeks" in batch_data
        or "grower_weeks" in batch_data
        or "starter_feed_lbs" in batch_data
        or "grower_feed_lbs" in batch_data
    )

    if has_phased:
        starter_weeks = min(
            520,
            max(
                0,
                int(batch_data.get("starter_weeks", defaults["starter_weeks"])),
            ),
        )
        grower_weeks = min(
            520,
            max(
                0,
                int(batch_data.get("grower_weeks", defaults["grower_weeks"])),
            ),
        )
        if starter_weeks + grower_weeks < 1:
            grower_weeks = 1
        starter_feed_lbs = max(
            0.0,
            float(
                batch_data.get("starter_feed_lbs", defaults["starter_feed_lbs"])
            ),
        )
        grower_feed_lbs = max(
            0.0,
            float(
                batch_data.get("grower_feed_lbs", defaults["grower_feed_lbs"])
            ),
        )
        weeks_to_proc = starter_weeks + grower_weeks
        feed_total = round(starter_feed_lbs + grower_feed_lbs, 4)
    else:
        weeks_to_proc = min(
            1040,
            max(
                1,
                int(batch_data.get("weeks_to_proc", defaults["weeks_to_proc"])),
            ),
        )
        feed_total = max(
            0.0, float(batch_data.get("feed_total", defaults["feed_total"]))
        )
        default_wks = max(
            1, defaults["starter_weeks"] + defaults["grower_weeks"]
        )
        default_fd = max(
            0.001, defaults["starter_feed_lbs"] + defaults["grower_feed_lbs"]
        )
        starter_weeks = min(
            520,
            min(
                weeks_to_proc,
                max(
                    0,
                    round(
                        weeks_to_proc
                        * (defaults["starter_weeks"] / default_wks)
                    ),
                ),
            ),
        )
        grower_weeks = min(520, max(0, weeks_to_proc - starter_weeks))
        starter_feed_lbs = round(
            feed_total * (defaults["starter_feed_lbs"] / default_fd), 2
        )
        grower_feed_lbs = round(max(0.0, feed_total - starter_feed_lbs), 2)

    start_str = str(batch_data.get("start_date", date.today().isoformat()))
    end_str = str(batch_data.get("end_date", start_str))
    days_to_birth = min(
        3650,
        max(0, int(batch_data.get("days_to_birth", defaults["days_to_birth"]))),
    )
    lc_type = str(
        batch_data.get("lifecycle_type")
        or defaults.get("lifecycle_type")
        or infer_lifecycle_type(species, days_to_birth)
    )
    if lc_type not in LIFECYCLE_TYPES:
        lc_type = infer_lifecycle_type(species, days_to_birth)

    return {
        "name": str(batch_data.get("name") or f"{species.title()} Batch"),
        "species": species,
        "lifecycle_type": lc_type,
        "quantity": max(1, int(batch_data.get("quantity", 1))),
        "start_date": start_str,
        "end_date": end_str,
        "days_to_birth": days_to_birth,
        "weeks_to_proc": weeks_to_proc,
        "feed_total": feed_total,
        "starter_weeks": starter_weeks,
        "starter_feed_lbs": starter_feed_lbs,
        "grower_weeks": grower_weeks,
        "grower_feed_lbs": grower_feed_lbs,
        "starter_bag_price": max(
            0.0,
            float(
                batch_data.get(
                    "starter_bag_price", defaults["starter_bag_price"]
                )
            ),
        ),
        "grower_bag_price": max(
            0.0,
            float(
                batch_data.get("grower_bag_price", defaults["grower_bag_price"])
            ),
        ),
        "unit_cost": max(
            0.0, float(batch_data.get("unit_cost", defaults["unit_cost"]))
        ),
        "processing_cost": max(
            0.0,
            float(
                batch_data.get("processing_cost", defaults["processing_cost"])
            ),
        ),
        "expected_yield_lbs": max(
            0.0,
            float(
                batch_data.get(
                    "expected_yield_lbs", defaults["expected_yield_lbs"]
                )
            ),
        ),
        "sale_price_per_lb": max(
            0.0,
            float(
                batch_data.get(
                    "sale_price_per_lb", defaults["sale_price_per_lb"]
                )
            ),
        ),
        "active": 1 if batch_data.get("active", True) else 0,
        "notes": str(batch_data.get("notes", "")),
    }


def save_batch(
    batch_data: dict,
    batch_id: int | None = None,
    user_id: int | None = None,
) -> int:
    uid = _resolve_user_id(user_id)
    norm = _normalize_batch_payload(batch_data, user_id=uid)
    with get_db_connection() as conn:
        if batch_id is not None:
            conn.execute(
                """
                UPDATE batches
                SET name = ?, species = ?, lifecycle_type = ?, quantity = ?, start_date = ?, end_date = ?,
                    days_to_birth = ?, weeks_to_proc = ?, feed_total = ?,
                    starter_weeks = ?, starter_feed_lbs = ?, grower_weeks = ?, grower_feed_lbs = ?,
                    starter_bag_price = ?, grower_bag_price = ?, unit_cost = ?,
                    processing_cost = ?, expected_yield_lbs = ?, sale_price_per_lb = ?, active = ?, notes = ?
                WHERE id = ?
                """,
                (
                    norm["name"],
                    norm["species"],
                    norm["lifecycle_type"],
                    norm["quantity"],
                    norm["start_date"],
                    norm["end_date"],
                    norm["days_to_birth"],
                    norm["weeks_to_proc"],
                    norm["feed_total"],
                    norm["starter_weeks"],
                    norm["starter_feed_lbs"],
                    norm["grower_weeks"],
                    norm["grower_feed_lbs"],
                    norm["starter_bag_price"],
                    norm["grower_bag_price"],
                    norm["unit_cost"],
                    norm["processing_cost"],
                    norm["expected_yield_lbs"],
                    norm["sale_price_per_lb"],
                    norm["active"],
                    norm["notes"],
                    int(batch_id),
                ),
            )
            conn.commit()
            return int(batch_id)
        else:
            cursor = conn.execute(
                """
                INSERT INTO batches (
                    user_id, name, species, lifecycle_type, quantity, start_date, end_date,
                    days_to_birth, weeks_to_proc, feed_total,
                    starter_weeks, starter_feed_lbs, grower_weeks, grower_feed_lbs,
                    starter_bag_price, grower_bag_price, unit_cost,
                    processing_cost, expected_yield_lbs, sale_price_per_lb, active, notes,
                    picking_slug, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uid,
                    norm["name"],
                    norm["species"],
                    norm["lifecycle_type"],
                    norm["quantity"],
                    norm["start_date"],
                    norm["end_date"],
                    norm["days_to_birth"],
                    norm["weeks_to_proc"],
                    norm["feed_total"],
                    norm["starter_weeks"],
                    norm["starter_feed_lbs"],
                    norm["grower_weeks"],
                    norm["grower_feed_lbs"],
                    norm["starter_bag_price"],
                    norm["grower_bag_price"],
                    norm["unit_cost"],
                    norm["processing_cost"],
                    norm["expected_yield_lbs"],
                    norm["sale_price_per_lb"],
                    norm["active"],
                    norm["notes"],
                    secrets.token_urlsafe(10),
                    datetime.utcnow().isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)


def set_batch_active(batch_id: int, active: bool) -> None:
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE batches SET active = ? WHERE id = ?",
            (1 if active else 0, int(batch_id)),
        )
        conn.commit()


def list_acknowledged_milestones(batch_ids: list[int]) -> set[tuple[int, str]]:
    if not batch_ids:
        return set()
    marks = ",".join("?" * len(batch_ids))
    with get_db_connection() as conn:
        rows = conn.execute(
            f"SELECT batch_id, task FROM batch_milestones_ack WHERE batch_id IN ({marks})",
            [int(b) for b in batch_ids],
        ).fetchall()
    return {(int(r["batch_id"]), str(r["task"])) for r in rows}


def acknowledge_milestone(batch_id: int, task: str, done: bool = True) -> None:
    with get_db_connection() as conn:
        if done:
            conn.execute(
                """
                INSERT INTO batch_milestones_ack (batch_id, task, done_at)
                VALUES (?, ?, ?)
                ON CONFLICT(batch_id, task) DO UPDATE SET done_at = excluded.done_at
                """,
                (int(batch_id), str(task), datetime.utcnow().isoformat(timespec="seconds")),
            )
        else:
            conn.execute(
                "DELETE FROM batch_milestones_ack WHERE batch_id = ? AND task = ?",
                (int(batch_id), str(task)),
            )
        conn.commit()


def delete_batch(batch_id: int) -> None:
    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM batch_sales WHERE batch_id = ?", (int(batch_id),)
        )
        conn.execute(
            "DELETE FROM batch_harvests WHERE batch_id = ?", (int(batch_id),)
        )
        conn.execute(
            "DELETE FROM batch_losses WHERE batch_id = ?", (int(batch_id),)
        )
        conn.execute("DELETE FROM batches WHERE id = ?", (int(batch_id),))
        conn.commit()


def export_backup_json(user_id: int | None = None) -> str:
    uid = _resolve_user_id(user_id)
    batches = list_batches(user_id=uid)
    losses_by_batch: dict[int, list[dict]] = {}
    for l in list_batch_losses(user_id=uid):
        losses_by_batch.setdefault(int(l["batch_id"]), []).append(l)

    harvests_by_batch: dict[int, list[dict]] = {}
    for h in list_batch_harvests(user_id=uid):
        harvests_by_batch.setdefault(int(h["batch_id"]), []).append(h)

    sales_by_batch: dict[int, list[dict]] = {}
    for s_row in list_batch_sales(user_id=uid):
        sales_by_batch.setdefault(int(s_row["batch_id"]), []).append(s_row)

    enriched_batches = []
    for b in batches:
        b_copy = dict(b)
        bid = int(b["id"])
        b_copy["losses"] = losses_by_batch.get(bid, [])
        b_copy["harvests"] = harvests_by_batch.get(bid, [])
        b_copy["sales"] = sales_by_batch.get(bid, [])
        enriched_batches.append(b_copy)

    catalog = get_species_catalog(user_id=uid)
    payload = {
        "version": 4,
        "exported_at": datetime.utcnow().isoformat(timespec="seconds"),
        "settings": {
            "barrel_capacity": get_barrel_capacity(user_id=uid),
            "bag_weight": get_bag_weight(user_id=uid),
        },
        "species_presets": list(catalog.values()),
        "batches": enriched_batches,
    }
    return json.dumps(payload, indent=2)


def import_backup_json(
    raw_json: str,
    replace_existing: bool = False,
    user_id: int | None = None,
    merge_by_name: bool = False,
) -> int:
    uid = _resolve_user_id(user_id)
    data = json.loads(raw_json)
    if isinstance(data, list):
        batches_to_import = data
        settings_to_import = {}
        presets_to_import = []
    elif isinstance(data, dict):
        batches_to_import = data.get("batches", [])
        settings_to_import = data.get("settings", {})
        presets_to_import = data.get("species_presets", [])
    else:
        raise ValueError("Invalid JSON backup format.")

    if "barrel_capacity" in settings_to_import:
        set_barrel_capacity(
            int(settings_to_import["barrel_capacity"]), user_id=uid
        )
    if "bag_weight" in settings_to_import:
        set_bag_weight(int(settings_to_import["bag_weight"]), user_id=uid)

    if isinstance(presets_to_import, dict):
        for sp_key, p_val in presets_to_import.items():
            if isinstance(p_val, dict):
                save_species_preset(sp_key, p_val, user_id=uid)
    elif isinstance(presets_to_import, list):
        for p_val in presets_to_import:
            if isinstance(p_val, dict) and p_val.get("species"):
                save_species_preset(
                    str(p_val["species"]), p_val, user_id=uid
                )

    with get_db_connection() as conn:
        if replace_existing:
            if uid is not None:
                user_batches = conn.execute(
                    "SELECT id FROM batches WHERE user_id = ?", (uid,)
                ).fetchall()
                for ub in user_batches:
                    ub_id = int(ub["id"])
                    conn.execute(
                        "DELETE FROM batch_sales WHERE batch_id = ?", (ub_id,)
                    )
                    conn.execute(
                        "DELETE FROM batch_harvests WHERE batch_id = ?",
                        (ub_id,),
                    )
                    conn.execute(
                        "DELETE FROM batch_losses WHERE batch_id = ?", (ub_id,)
                    )
                conn.execute("DELETE FROM batches WHERE user_id = ?", (uid,))
            else:
                conn.execute("DELETE FROM batch_sales")
                conn.execute("DELETE FROM batch_harvests")
                conn.execute("DELETE FROM batch_losses")
                conn.execute("DELETE FROM batches")
        count = 0
        now_iso = datetime.utcnow().isoformat(timespec="seconds")
        for item in batches_to_import:
            norm = _normalize_batch_payload(item, user_id=uid)
            if uid is not None:
                # Ensure imported species exists in the user's species_presets
                conn.execute(
                    """
                    INSERT OR IGNORE INTO species_presets (
                        user_id, species, lifecycle_type, days_to_birth,
                        starter_weeks, starter_feed_lbs, grower_weeks, grower_feed_lbs,
                        starter_bag_price, grower_bag_price, unit_cost,
                        processing_cost, expected_yield_lbs, sale_price_per_lb, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uid,
                        norm["species"],
                        norm["lifecycle_type"],
                        norm["days_to_birth"],
                        norm["starter_weeks"],
                        norm["starter_feed_lbs"],
                        norm["grower_weeks"],
                        norm["grower_feed_lbs"],
                        norm["starter_bag_price"],
                        norm["grower_bag_price"],
                        norm["unit_cost"],
                        norm["processing_cost"],
                        norm["expected_yield_lbs"],
                        norm["sale_price_per_lb"],
                        now_iso,
                    ),
                )
            act_fd_lbs = (
                float(item["actual_feed_lbs"])
                if item.get("actual_feed_lbs") is not None
                else None
            )
            act_fd_cost = (
                float(item["actual_feed_cost"])
                if item.get("actual_feed_cost") is not None
                else None
            )
            existing_match_id: int | None = None
            if merge_by_name and not replace_existing:
                if uid is not None:
                    matches = conn.execute(
                        "SELECT id FROM batches WHERE user_id = ? AND LOWER(TRIM(name)) = LOWER(TRIM(?)) ORDER BY id ASC",
                        (uid, norm["name"]),
                    ).fetchall()
                else:
                    matches = conn.execute(
                        "SELECT id FROM batches WHERE LOWER(TRIM(name)) = LOWER(TRIM(?)) ORDER BY id ASC",
                        (norm["name"],),
                    ).fetchall()
                if matches:
                    existing_match_id = int(matches[0]["id"])
                    for m_row in matches:
                        m_id = int(m_row["id"])
                        conn.execute(
                            "DELETE FROM batch_sales WHERE batch_id = ?", (m_id,)
                        )
                        conn.execute(
                            "DELETE FROM batch_harvests WHERE batch_id = ?",
                            (m_id,),
                        )
                        conn.execute(
                            "DELETE FROM batch_losses WHERE batch_id = ?", (m_id,)
                        )
                    for extra_row in matches[1:]:
                        conn.execute(
                            "DELETE FROM batches WHERE id = ?",
                            (int(extra_row["id"]),),
                        )

            if existing_match_id is not None:
                conn.execute(
                    """
                    UPDATE batches
                    SET species = ?, lifecycle_type = ?, quantity = ?, start_date = ?, end_date = ?,
                        days_to_birth = ?, weeks_to_proc = ?, feed_total = ?,
                        starter_weeks = ?, starter_feed_lbs = ?, grower_weeks = ?, grower_feed_lbs = ?,
                        starter_bag_price = ?, grower_bag_price = ?, unit_cost = ?,
                        processing_cost = ?, expected_yield_lbs = ?, sale_price_per_lb = ?,
                        actual_feed_lbs = ?, actual_feed_cost = ?, active = ?, notes = ?
                    WHERE id = ?
                    """,
                    (
                        norm["species"],
                        norm["lifecycle_type"],
                        norm["quantity"],
                        norm["start_date"],
                        norm["end_date"],
                        norm["days_to_birth"],
                        norm["weeks_to_proc"],
                        norm["feed_total"],
                        norm["starter_weeks"],
                        norm["starter_feed_lbs"],
                        norm["grower_weeks"],
                        norm["grower_feed_lbs"],
                        norm["starter_bag_price"],
                        norm["grower_bag_price"],
                        norm["unit_cost"],
                        norm["processing_cost"],
                        norm["expected_yield_lbs"],
                        norm["sale_price_per_lb"],
                        act_fd_lbs,
                        act_fd_cost,
                        norm["active"],
                        norm["notes"],
                        existing_match_id,
                    ),
                )
                new_batch_id = existing_match_id
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO batches (
                        user_id, name, species, lifecycle_type, quantity, start_date, end_date,
                        days_to_birth, weeks_to_proc, feed_total,
                        starter_weeks, starter_feed_lbs, grower_weeks, grower_feed_lbs,
                        starter_bag_price, grower_bag_price, unit_cost,
                        processing_cost, expected_yield_lbs, sale_price_per_lb,
                        actual_feed_lbs, actual_feed_cost, active, notes, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uid,
                        norm["name"],
                        norm["species"],
                        norm["lifecycle_type"],
                        norm["quantity"],
                        norm["start_date"],
                        norm["end_date"],
                        norm["days_to_birth"],
                        norm["weeks_to_proc"],
                        norm["feed_total"],
                        norm["starter_weeks"],
                        norm["starter_feed_lbs"],
                        norm["grower_weeks"],
                        norm["grower_feed_lbs"],
                        norm["starter_bag_price"],
                        norm["grower_bag_price"],
                        norm["unit_cost"],
                        norm["processing_cost"],
                        norm["expected_yield_lbs"],
                        norm["sale_price_per_lb"],
                        act_fd_lbs,
                        act_fd_cost,
                        norm["active"],
                        norm["notes"],
                        str(item.get("created_at") or now_iso),
                    ),
                )
                new_batch_id = int(cursor.lastrowid)
            for loss_item in item.get("losses", []):
                conn.execute(
                    """
                    INSERT INTO batch_losses (batch_id, loss_date, count, reason, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        new_batch_id,
                        str(loss_item.get("loss_date", norm["start_date"])),
                        max(1, int(loss_item.get("count", 1))),
                        str(loss_item.get("reason", "")),
                        str(loss_item.get("created_at") or now_iso),
                    ),
                )
            for harv_item in item.get("harvests", []):
                conn.execute(
                    """
                    INSERT INTO batch_harvests (
                        batch_id, harvest_date, head_count, dressed_lbs, processing_cost_total, notes, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_batch_id,
                        str(harv_item.get("harvest_date", norm["end_date"])),
                        max(1, int(harv_item.get("head_count", 1))),
                        max(0.0, float(harv_item.get("dressed_lbs", 0.0))),
                        max(
                            0.0,
                            float(harv_item.get("processing_cost_total", 0.0)),
                        ),
                        str(harv_item.get("notes", "")),
                        str(harv_item.get("created_at") or now_iso),
                    ),
                )
            for sale_item in item.get("sales", []):
                conn.execute(
                    """
                    INSERT INTO batch_sales (
                        batch_id, entry_date, allocation_type, weight_lbs, revenue, notes, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_batch_id,
                        str(sale_item.get("entry_date", norm["end_date"])),
                        str(sale_item.get("allocation_type", "Customer Sale")),
                        max(0.01, float(sale_item.get("weight_lbs", 1.0))),
                        max(0.0, float(sale_item.get("revenue", 0.0))),
                        str(sale_item.get("notes", "")),
                        str(sale_item.get("created_at") or now_iso),
                    ),
                )
            count += 1
        conn.commit()
    return count


EXPENSE_CATEGORIES = (
    "Feed",
    "Livestock / Chicks",
    "Processing",
    "Supplies & Equipment",
    "Utilities & Fuel",
    "Repairs & Maintenance",
    "Other",
)


def get_google_drive_connection(user_id: int) -> dict | None:
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM google_drive_tokens WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
        return dict(row) if row else None


def save_google_drive_connection(
    user_id: int,
    google_email: str,
    access_token_enc: str,
    refresh_token_enc: str,
    token_expires_at: str,
    folder_id: str = "",
    folder_name: str = "Homestead Planner Receipts",
) -> None:
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO google_drive_tokens (
                user_id, google_email, access_token_enc, refresh_token_enc,
                token_expires_at, folder_id, folder_name, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                google_email = COALESCE(NULLIF(excluded.google_email, ''), google_drive_tokens.google_email),
                access_token_enc = excluded.access_token_enc,
                refresh_token_enc = COALESCE(NULLIF(excluded.refresh_token_enc, ''), google_drive_tokens.refresh_token_enc),
                token_expires_at = excluded.token_expires_at,
                folder_id = COALESCE(NULLIF(excluded.folder_id, ''), google_drive_tokens.folder_id),
                folder_name = COALESCE(NULLIF(excluded.folder_name, ''), google_drive_tokens.folder_name),
                updated_at = excluded.updated_at
            """,
            (
                int(user_id),
                str(google_email or "").strip(),
                str(access_token_enc or ""),
                str(refresh_token_enc or ""),
                str(token_expires_at or ""),
                str(folder_id or ""),
                str(folder_name or "Homestead Planner Receipts").strip() or "Homestead Planner Receipts",
                now_iso,
            ),
        )
        conn.commit()


def update_google_drive_tokens(
    user_id: int,
    access_token_enc: str,
    token_expires_at: str,
) -> None:
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE google_drive_tokens
            SET access_token_enc = ?, token_expires_at = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (
                str(access_token_enc or ""),
                str(token_expires_at or ""),
                datetime.utcnow().isoformat(timespec="seconds"),
                int(user_id),
            ),
        )
        conn.commit()


def update_google_drive_folder(user_id: int, folder_name: str) -> None:
    clean = str(folder_name or "").strip() or "Homestead Planner Receipts"
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE google_drive_tokens
            SET folder_name = ?, folder_id = '', updated_at = ?
            WHERE user_id = ?
            """,
            (clean, datetime.utcnow().isoformat(timespec="seconds"), int(user_id)),
        )
        conn.commit()


def disconnect_google_drive(user_id: int) -> None:
    with get_db_connection() as conn:
        conn.execute("DELETE FROM google_drive_tokens WHERE user_id = ?", (int(user_id),))
        conn.commit()


def list_farm_expenses(user_id: int) -> list[dict]:
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT e.*, b.name AS batch_name
            FROM farm_expenses e
            LEFT JOIN batches b ON b.id = e.batch_id
            WHERE e.user_id = ?
            ORDER BY e.expense_date DESC, e.id DESC
            """,
            (int(user_id),),
        ).fetchall()
        return [dict(r) for r in rows]


def add_farm_expense(
    user_id: int,
    expense_date: str,
    vendor: str,
    category: str,
    amount: float,
    batch_id: int | None = None,
    notes: str = "",
    receipt_url: str = "",
    drive_file_id: str = "",
) -> int:
    cat = str(category or "Other").strip() or "Other"
    with get_db_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO farm_expenses (
                user_id, batch_id, expense_date, vendor, category, amount,
                notes, receipt_url, drive_file_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(user_id),
                int(batch_id) if batch_id else None,
                str(expense_date),
                str(vendor or "").strip(),
                cat,
                max(0.0, float(amount)),
                str(notes or "").strip(),
                str(receipt_url or "").strip(),
                str(drive_file_id or "").strip(),
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def delete_farm_expense(expense_id: int, user_id: int) -> None:
    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM farm_expenses WHERE id = ? AND user_id = ?",
            (int(expense_id), int(user_id)),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Customers, Bird Assignments & Public Picking
# ---------------------------------------------------------------------------
PREFERRED_CONTACTS = ("sms", "email", "call")
PAYMENT_STATUSES = ("unpaid", "deposit", "paid")
PICKUP_STATUSES = ("unassigned", "assigned", "picked_up")


def _norm_phone(phone: str) -> str:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def list_customers(user_id: int, search: str = "") -> list[dict]:
    q = (search or "").strip().lower()
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT c.*,
                   COUNT(bh.id) AS bird_count,
                   COALESCE(SUM(bh.dressed_lbs), 0) AS total_lbs
            FROM customers c
            LEFT JOIN batch_harvests bh ON bh.customer_id = c.id
            WHERE c.user_id = ?
            GROUP BY c.id
            ORDER BY LOWER(c.name) ASC, c.id ASC
            """,
            (int(user_id),),
        ).fetchall()
    out = [dict(r) for r in rows]
    if q:
        out = [
            c for c in out
            if q in (c["name"] or "").lower()
            or q in (c["phone"] or "").lower()
            or q in (c["email"] or "").lower()
            or q in (c["notes"] or "").lower()
        ]
    return out


def get_customer(user_id: int, customer_id: int) -> dict | None:
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM customers WHERE user_id = ? AND id = ?",
            (int(user_id), int(customer_id)),
        ).fetchone()
        return dict(row) if row else None


def save_customer(
    user_id: int,
    name: str,
    phone: str = "",
    email: str = "",
    preferred_contact: str = "sms",
    notes: str = "",
    customer_id: int | None = None,
) -> int:
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("Customer name is required.")
    pref = preferred_contact if preferred_contact in PREFERRED_CONTACTS else "sms"
    with get_db_connection() as conn:
        if customer_id:
            conn.execute(
                """
                UPDATE customers
                SET name = ?, phone = ?, email = ?, preferred_contact = ?, notes = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    clean_name,
                    (phone or "").strip(),
                    (email or "").strip(),
                    pref,
                    (notes or "").strip(),
                    int(customer_id),
                    int(user_id),
                ),
            )
            conn.commit()
            return int(customer_id)
        cur = conn.execute(
            """
            INSERT INTO customers (user_id, name, phone, email, preferred_contact, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(user_id),
                clean_name,
                (phone or "").strip(),
                (email or "").strip(),
                pref,
                (notes or "").strip(),
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def delete_customer(user_id: int, customer_id: int) -> None:
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE batch_harvests SET customer_id = NULL, pickup_status = 'unassigned'"
            " WHERE customer_id = ? AND batch_id IN (SELECT id FROM batches WHERE user_id = ?)",
            (int(customer_id), int(user_id)),
        )
        conn.execute(
            "DELETE FROM customers WHERE id = ? AND user_id = ?",
            (int(customer_id), int(user_id)),
        )
        conn.commit()


def find_or_create_customer(
    user_id: int, name: str, contact: str
) -> dict:
    """Match an existing customer by normalized phone (handling +1/formatting), email, or exact name; backfill missing contact."""
    clean_name = (name or "").strip()
    clean_contact = (contact or "").strip()
    if not clean_name or not clean_contact:
        raise ValueError("Please enter your name and phone or email.")
    is_email = "@" in clean_contact
    norm_ph = _norm_phone(clean_contact)
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM customers WHERE user_id = ?", (int(user_id),)
        ).fetchall()
        matched = None
        for r in rows:
            if is_email and (r["email"] or "").strip().lower() == clean_contact.lower():
                matched = dict(r)
                break
            if not is_email and norm_ph and _norm_phone(r["phone"] or "") == norm_ph:
                matched = dict(r)
                break
        if matched is None:
            for r in rows:
                if (r["name"] or "").strip().lower() == clean_name.lower():
                    # Only auto-match by exact name if the other contact field wasn't conflicting
                    if is_email and not (r["email"] or "").strip():
                        matched = dict(r)
                        break
                    if not is_email and not _norm_phone(r["phone"] or ""):
                        matched = dict(r)
                        break
        if matched is not None:
            # Backfill missing phone or email if newly provided
            if is_email and not (matched.get("email") or "").strip():
                conn.execute(
                    "UPDATE customers SET email = ? WHERE id = ? AND user_id = ?",
                    (clean_contact, int(matched["id"]), int(user_id)),
                )
                conn.commit()
                matched["email"] = clean_contact
            elif not is_email and not _norm_phone(matched.get("phone") or ""):
                conn.execute(
                    "UPDATE customers SET phone = ? WHERE id = ? AND user_id = ?",
                    (clean_contact, int(matched["id"]), int(user_id)),
                )
                conn.commit()
                matched["phone"] = clean_contact
            return matched
    cid = save_customer(
        user_id=user_id,
        name=clean_name,
        phone="" if is_email else clean_contact,
        email=clean_contact if is_email else "",
        preferred_contact="email" if is_email else "sms",
    )
    return get_customer(user_id, cid) or {"id": cid, "name": clean_name}


def merge_customers(user_id: int, source_id: int, target_id: int) -> bool:
    """Merge source customer into target customer: move all birds, backfill contact/notes, delete source."""
    if int(source_id) == int(target_id):
        raise ValueError("Cannot merge a customer into themselves.")
    src = get_customer(user_id, source_id)
    tgt = get_customer(user_id, target_id)
    if not src or not tgt:
        raise ValueError("Both customers must exist.")
    merged_phone = (tgt.get("phone") or "").strip() or (src.get("phone") or "").strip()
    merged_email = (tgt.get("email") or "").strip() or (src.get("email") or "").strip()
    notes_parts = [p for p in ((tgt.get("notes") or "").strip(), (src.get("notes") or "").strip()) if p]
    merged_notes = " · ".join(dict.fromkeys(notes_parts))
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE batch_harvests SET customer_id = ?"
            " WHERE customer_id = ? AND batch_id IN (SELECT id FROM batches WHERE user_id = ?)",
            (int(target_id), int(source_id), int(user_id)),
        )
        conn.execute(
            "UPDATE batch_preorders SET customer_id = ?"
            " WHERE customer_id = ? AND user_id = ?",
            (int(target_id), int(source_id), int(user_id)),
        )
        conn.execute(
            "UPDATE customers SET phone = ?, email = ?, notes = ? WHERE id = ? AND user_id = ?",
            (merged_phone, merged_email, merged_notes, int(target_id), int(user_id)),
        )
        conn.execute(
            "DELETE FROM customers WHERE id = ? AND user_id = ?",
            (int(source_id), int(user_id)),
        )
        conn.commit()
    return True


def list_customer_birds(user_id: int, customer_id: int) -> list[dict]:
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT bh.*, b.name AS batch_name, b.species, b.sale_price_per_lb
            FROM batch_harvests bh
            JOIN batches b ON b.id = bh.batch_id
            WHERE b.user_id = ? AND bh.customer_id = ?
            ORDER BY bh.harvest_date DESC, bh.id DESC
            """,
            (int(user_id), int(customer_id)),
        ).fetchall()
        return [dict(r) for r in rows]


def update_harvest_assignment(
    batch_id: int,
    harvest_id: int,
    *,
    customer_id: int | None = None,
    for_sale: bool = True,
    deposit_paid: float = 0.0,
    actual_sale_price: float | None = None,
    payment_status: str = "unpaid",
    pickup_status: str = "unassigned",
    notes: str | None = None,
) -> bool:
    pay = payment_status if payment_status in PAYMENT_STATUSES else "unpaid"
    pstat = pickup_status if pickup_status in PICKUP_STATUSES else "unassigned"
    actual_val = round(max(0.0, float(actual_sale_price)), 2) if actual_sale_price is not None else None
    if not for_sale:
        customer_id = None
        pstat = "unassigned"
        actual_val = None
    elif customer_id is not None and pstat == "unassigned":
        pstat = "assigned"
    elif customer_id is None and pstat != "unassigned":
        pstat = "unassigned"
    claimed_at = datetime.utcnow().isoformat(timespec="seconds") if customer_id else ""
    with get_db_connection() as conn:
        if notes is not None:
            cur = conn.execute(
                """
                UPDATE batch_harvests
                SET customer_id = ?, for_sale = ?, deposit_paid = ?, actual_sale_price = ?,
                    payment_status = ?, pickup_status = ?,
                    claimed_at = CASE WHEN ? != '' AND COALESCE(claimed_at, '') = '' THEN ? WHEN ? = '' THEN '' ELSE claimed_at END,
                    notes = ?
                WHERE id = ? AND batch_id = ?
                """,
                (
                    int(customer_id) if customer_id else None,
                    1 if for_sale else 0,
                    max(0.0, float(deposit_paid or 0.0)),
                    actual_val,
                    pay,
                    pstat,
                    claimed_at,
                    claimed_at,
                    claimed_at,
                    str(notes).strip(),
                    int(harvest_id),
                    int(batch_id),
                ),
            )
        else:
            cur = conn.execute(
                """
                UPDATE batch_harvests
                SET customer_id = ?, for_sale = ?, deposit_paid = ?, actual_sale_price = ?,
                    payment_status = ?, pickup_status = ?,
                    claimed_at = CASE WHEN ? != '' AND COALESCE(claimed_at, '') = '' THEN ? WHEN ? = '' THEN '' ELSE claimed_at END
                WHERE id = ? AND batch_id = ?
                """,
                (
                    int(customer_id) if customer_id else None,
                    1 if for_sale else 0,
                    max(0.0, float(deposit_paid or 0.0)),
                    actual_val,
                    pay,
                    pstat,
                    claimed_at,
                    claimed_at,
                    claimed_at,
                    int(harvest_id),
                    int(batch_id),
                ),
            )
        conn.commit()
        return cur.rowcount > 0


def update_batch_picking_settings(
    user_id: int,
    batch_id: int,
    picking_enabled: bool,
    max_birds_per_customer: int | None = None,
    picking_preorder_only: bool | None = None,
) -> dict | None:
    limit_val = int(max_birds_per_customer) if max_birds_per_customer and int(max_birds_per_customer) > 0 else None
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT picking_slug, COALESCE(picking_preorder_only, 0) AS picking_preorder_only"
            " FROM batches WHERE id = ? AND user_id = ?",
            (int(batch_id), int(user_id)),
        ).fetchone()
        if not row:
            return None
        slug = row["picking_slug"] or secrets.token_urlsafe(10)
        po_only = (
            (1 if picking_preorder_only else 0)
            if picking_preorder_only is not None
            else int(row["picking_preorder_only"] or 0)
        )
        conn.execute(
            """
            UPDATE batches
            SET picking_enabled = ?, picking_slug = ?, max_birds_per_customer = ?,
                picking_preorder_only = ?
            WHERE id = ? AND user_id = ?
            """,
            (1 if picking_enabled else 0, slug, limit_val, po_only, int(batch_id), int(user_id)),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM batches WHERE id = ? AND user_id = ?",
            (int(batch_id), int(user_id)),
        ).fetchone()
        return dict(updated) if updated else None


def get_batch_by_picking_slug(slug: str) -> dict | None:
    clean = (slug or "").strip()
    if not clean:
        return None
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM batches WHERE picking_slug = ?", (clean,)
        ).fetchone()
        return dict(row) if row else None


def claim_bird_atomically(
    batch_id: int,
    harvest_id: int,
    customer_id: int,
    max_birds_per_customer: int | None = None,
    apply_preorder_deposit: bool = True,
) -> tuple[bool, str]:
    """Atomically claim a for-sale unassigned bird; returns (ok, error_reason).

    If ``apply_preorder_deposit`` is True and the customer has unapplied paid
    pre-order deposits on this batch, credits their per-bird deposit onto the
    claimed bird atomically without ever double-counting across birds.
    """
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        already_row = conn.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(deposit_paid), 0) AS applied_dep"
            " FROM batch_harvests"
            " WHERE batch_id = ? AND customer_id = ? AND COALESCE(for_sale, 1) = 1",
            (int(batch_id), int(customer_id)),
        ).fetchone()
        already = int(already_row["cnt"] or 0)
        applied_dep = float(already_row["applied_dep"] or 0.0)
        if max_birds_per_customer and int(max_birds_per_customer) > 0:
            if already >= int(max_birds_per_customer):
                conn.rollback()
                return False, "limit_reached"

        credit = 0.0
        if apply_preorder_deposit:
            po_row = conn.execute(
                "SELECT COALESCE(SUM(quantity), 0) AS qty,"
                "       COALESCE(SUM(deposit_paid), 0) AS paid_dep,"
                "       MAX(deposit_per_bird) AS per_bird"
                " FROM batch_preorders"
                " WHERE batch_id = ? AND customer_id = ? AND status = 'reserved'",
                (int(batch_id), int(customer_id)),
            ).fetchone()
            po_qty = int(po_row["qty"] or 0)
            po_paid = float(po_row["paid_dep"] or 0.0)
            po_per_bird = float(po_row["per_bird"] or 0.0)
            unapplied = round(max(0.0, po_paid - applied_dep), 2)
            if unapplied > 0.0:
                if po_qty > 0 and (already + 1) >= po_qty:
                    credit = unapplied
                elif po_per_bird > 0.0:
                    credit = round(min(unapplied, po_per_bird), 2)
                elif po_qty > 0:
                    credit = round(min(unapplied, po_paid / po_qty), 2)
                else:
                    credit = unapplied

        if credit > 0.0:
            cur = conn.execute(
                """
                UPDATE batch_harvests
                SET customer_id = ?, pickup_status = 'assigned', claimed_at = ?,
                    deposit_paid = ?,
                    payment_status = CASE WHEN payment_status = 'paid' THEN 'paid' ELSE 'deposit' END
                WHERE id = ? AND batch_id = ?
                  AND customer_id IS NULL
                  AND COALESCE(for_sale, 1) = 1
                """,
                (int(customer_id), now_iso, credit, int(harvest_id), int(batch_id)),
            )
        else:
            cur = conn.execute(
                """
                UPDATE batch_harvests
                SET customer_id = ?, pickup_status = 'assigned', claimed_at = ?
                WHERE id = ? AND batch_id = ?
                  AND customer_id IS NULL
                  AND COALESCE(for_sale, 1) = 1
                """,
                (int(customer_id), now_iso, int(harvest_id), int(batch_id)),
            )
        if cur.rowcount == 0:
            conn.rollback()
            return False, "already_claimed"
        conn.commit()
        return True, "ok"


def customer_insights_summary(user_id: int, search: str = "") -> dict:
    """Compute per-customer species counts, calculated vs actual paid, tips/overpayments, and overall KPIs."""
    customers = list_customers(user_id, search=search)
    with get_db_connection() as conn:
        bird_rows = conn.execute(
            """
            SELECT bh.*, b.name AS batch_name, b.species, b.sale_price_per_lb
            FROM batch_harvests bh
            JOIN batches b ON b.id = bh.batch_id
            WHERE b.user_id = ? AND bh.customer_id IS NOT NULL
            ORDER BY bh.harvest_date DESC, bh.id DESC
            """,
            (int(user_id),),
        ).fetchall()

    by_cust: dict[int, list[dict]] = {}
    for r in bird_rows:
        by_cust.setdefault(int(r["customer_id"]), []).append(dict(r))

    total_birds = 0
    total_lbs = 0.0
    total_calc = 0.0
    total_actual = 0.0
    repeat_customers = 0

    for c in customers:
        birds = by_cust.get(int(c["id"]), [])
        species_counts: dict[str, int] = {}
        batch_ids: set[int] = set()
        c_lbs = 0.0
        c_head = 0
        c_calc = 0.0
        c_actual = 0.0
        c_bal = 0.0
        for b in birds:
            hc = max(1, int(b.get("head_count") or 1))
            dl = float(b.get("dressed_lbs") or 0.0)
            sp = str(b.get("species") or "Livestock").title()
            species_counts[sp] = species_counts.get(sp, 0) + hc
            batch_ids.add(int(b["batch_id"]))
            c_head += hc
            c_lbs += dl
            calc_p = round(dl * float(b.get("sale_price_per_lb") or 0.0), 2)
            act_p = (
                round(float(b["actual_sale_price"]), 2)
                if b.get("actual_sale_price") is not None
                else calc_p
            )
            c_calc += calc_p
            c_actual += act_p
            if b.get("payment_status") != "paid":
                dep = float(b.get("deposit_paid") or 0.0)
                c_bal += max(0.0, act_p - dep)

        c["bird_count"] = c_head
        c["total_lbs"] = round(c_lbs, 2)
        c["avg_lbs"] = round(c_lbs / c_head, 2) if c_head else 0.0
        c["batch_count"] = len(batch_ids)
        c["species_counts"] = species_counts
        c["calculated_total"] = round(c_calc, 2)
        c["actual_total"] = round(c_actual, 2)
        c["tip_diff"] = round(c_actual - c_calc, 2)
        c["balance_due"] = round(c_bal, 2)

        total_birds += c_head
        total_lbs += c_lbs
        total_calc += c_calc
        total_actual += c_actual
        if len(batch_ids) >= 2:
            repeat_customers += 1

    return {
        "customers": customers,
        "total_customers": len(customers),
        "total_birds": total_birds,
        "total_lbs": round(total_lbs, 2),
        "total_calculated": round(total_calc, 2),
        "total_actual": round(total_actual, 2),
        "total_tips": round(total_actual - total_calc, 2),
        "repeat_customers": repeat_customers,
    }


def _clean_payment_handle(raw: str, kind: str) -> str:
    """Normalize a Venmo username or PayPal.Me handle (strips @ and URL prefixes)."""
    val = str(raw or "").strip()
    if not val:
        return ""
    low = val.lower()
    if kind == "venmo":
        for prefix in (
            "https://venmo.com/u/",
            "http://venmo.com/u/",
            "https://www.venmo.com/u/",
            "https://venmo.com/",
            "http://venmo.com/",
            "https://www.venmo.com/",
            "venmo.com/u/",
            "venmo.com/",
        ):
            if low.startswith(prefix):
                val = val[len(prefix):]
                break
    elif kind == "paypal":
        for prefix in (
            "https://www.paypal.me/",
            "http://www.paypal.me/",
            "https://paypal.me/",
            "http://paypal.me/",
            "www.paypal.me/",
            "paypal.me/",
        ):
            if low.startswith(prefix):
                val = val[len(prefix):]
                break
    val = val.split("?", 1)[0].split("#", 1)[0].strip().strip("/").lstrip("@")
    if "/" in val:
        val = val.split("/", 1)[0]
    return val[:64]


def get_farm_pickup_settings(user_id: int) -> dict[str, str]:
    return {
        "farm_name": get_text_setting("farm_name", "", user_id=user_id).strip(),
        "pickup_address": get_text_setting("pickup_address", "", user_id=user_id).strip(),
        "pickup_instructions": get_text_setting("pickup_instructions", "", user_id=user_id).strip(),
        "venmo_handle": get_text_setting("venmo_handle", "", user_id=user_id).strip(),
        "paypal_handle": get_text_setting("paypal_handle", "", user_id=user_id).strip(),
    }


def save_farm_pickup_settings(
    user_id: int,
    farm_name: str = "",
    pickup_address: str = "",
    pickup_instructions: str = "",
    venmo_handle: str = "",
    paypal_handle: str = "",
) -> dict[str, str]:
    clean = {
        "farm_name": str(farm_name or "").strip()[:120],
        "pickup_address": str(pickup_address or "").strip()[:240],
        "pickup_instructions": str(pickup_instructions or "").strip()[:400],
        "venmo_handle": _clean_payment_handle(venmo_handle, "venmo"),
        "paypal_handle": _clean_payment_handle(paypal_handle, "paypal"),
    }
    for k, v in clean.items():
        set_text_setting(k, v, user_id=user_id)
    return clean


def update_batch_preorder_settings(
    user_id: int,
    batch_id: int,
    preorder_enabled: bool,
    preorder_cap: int | None = None,
    deposit_per_bird: float = 0.0,
    preorder_notes: str = "",
    pickup_date: str = "",
    pickup_window: str = "",
    pickup_location: str = "",
) -> dict | None:
    cap_val = int(preorder_cap) if preorder_cap is not None and int(preorder_cap) > 0 else None
    dep_val = round(max(0.0, float(deposit_per_bird or 0.0)), 2)
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT picking_slug FROM batches WHERE id = ? AND user_id = ?",
            (int(batch_id), int(user_id)),
        ).fetchone()
        if not row:
            return None
        slug = row["picking_slug"] or secrets.token_urlsafe(10)
        conn.execute(
            """
            UPDATE batches
            SET preorder_enabled = ?,
                preorder_cap = ?,
                deposit_per_bird = ?,
                preorder_notes = ?,
                pickup_date = ?,
                pickup_window = ?,
                pickup_location = ?,
                picking_slug = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                1 if preorder_enabled else 0,
                cap_val,
                dep_val,
                str(preorder_notes or "").strip(),
                str(pickup_date or "").strip(),
                str(pickup_window or "").strip(),
                str(pickup_location or "").strip(),
                slug,
                int(batch_id),
                int(user_id),
            ),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM batches WHERE id = ? AND user_id = ?",
            (int(batch_id), int(user_id)),
        ).fetchone()
        return dict(updated) if updated else None


def list_batch_preorders(batch_id: int) -> list[dict]:
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT p.*,
                   c.name AS customer_name,
                   c.phone AS customer_phone,
                   c.email AS customer_email,
                   c.preferred_contact AS customer_pref
            FROM batch_preorders p
            JOIN customers c ON c.id = p.customer_id
            WHERE p.batch_id = ?
            ORDER BY CASE p.status WHEN 'reserved' THEN 0 WHEN 'waitlist' THEN 1 ELSE 2 END,
                     p.created_at ASC, p.id ASC
            """,
            (int(batch_id),),
        ).fetchall()
        return [dict(r) for r in rows]


def get_customer_batch_preorder_summary(batch_id: int, customer_id: int) -> dict:
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT status, quantity, deposit_per_bird, deposit_paid
            FROM batch_preorders
            WHERE batch_id = ? AND customer_id = ? AND status IN ('reserved', 'waitlist')
            """,
            (int(batch_id), int(customer_id)),
        ).fetchall()
        applied_row = conn.execute(
            """
            SELECT COUNT(*) AS claimed_cnt, COALESCE(SUM(deposit_paid), 0) AS applied_dep
            FROM batch_harvests
            WHERE batch_id = ? AND customer_id = ? AND COALESCE(for_sale, 1) = 1
            """,
            (int(batch_id), int(customer_id)),
        ).fetchone()
    reserved_qty = sum(int(r["quantity"] or 0) for r in rows if r["status"] == "reserved")
    waitlist_qty = sum(int(r["quantity"] or 0) for r in rows if r["status"] == "waitlist")
    total_paid = round(sum(float(r["deposit_paid"] or 0.0) for r in rows if r["status"] == "reserved"), 2)
    per_bird = max((float(r["deposit_per_bird"] or 0.0) for r in rows if r["status"] == "reserved"), default=0.0)
    applied_dep = round(float(applied_row["applied_dep"] or 0.0), 2)
    claimed_cnt = int(applied_row["claimed_cnt"] or 0)
    return {
        "has_reservation": reserved_qty > 0,
        "reserved_qty": reserved_qty,
        "waitlist_qty": waitlist_qty,
        "deposit_per_bird": round(per_bird, 2),
        "total_deposit_paid": total_paid,
        "already_applied_deposit": applied_dep,
        "remaining_unapplied_deposit": round(max(0.0, total_paid - applied_dep), 2),
        "claimed_count": claimed_cnt,
    }


def create_batch_preorder(
    user_id: int,
    batch_id: int,
    customer_id: int,
    quantity: int = 1,
    status: str = "reserved",
    deposit_per_bird: float = 0.0,
    deposit_paid: float = 0.0,
    deposit_status: str = "unpaid",
    notes: str = "",
) -> dict:
    qty = max(1, int(quantity or 1))
    st = status if status in ("reserved", "waitlist", "cancelled") else "reserved"
    dpb = round(max(0.0, float(deposit_per_bird or 0.0)), 2)
    dp = round(max(0.0, float(deposit_paid or 0.0)), 2)
    dst = "paid" if (deposit_status == "paid" or (dpb > 0 and dp >= round(dpb * qty, 2) and dp > 0)) else "unpaid"
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO batch_preorders (
                batch_id, user_id, customer_id, quantity, status,
                deposit_per_bird, deposit_paid, deposit_status, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(batch_id), int(user_id), int(customer_id), qty, st,
                dpb, dp, dst, str(notes or "").strip(), now_iso,
            ),
        )
        pid = int(cur.lastrowid)
        conn.commit()
        row = conn.execute("SELECT * FROM batch_preorders WHERE id = ?", (pid,)).fetchone()
        return dict(row)


def create_public_preorder_atomic(
    batch_id: int,
    user_id: int,
    customer_id: int,
    quantity: int,
    cap: int | None,
    deposit_per_bird: float = 0.0,
    notes: str = "",
) -> dict:
    """Atomically reserve quantity if under cap, else place on waitlist."""
    qty = max(1, min(50, int(quantity or 1)))
    dpb = round(max(0.0, float(deposit_per_bird or 0.0)), 2)
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        reserved_now = int(
            conn.execute(
                "SELECT COALESCE(SUM(quantity), 0) FROM batch_preorders"
                " WHERE batch_id = ? AND status = 'reserved'",
                (int(batch_id),),
            ).fetchone()[0]
        )
        if cap is not None and int(cap) > 0 and (reserved_now + qty) > int(cap):
            status = "waitlist"
        else:
            status = "reserved"
        cur = conn.execute(
            """
            INSERT INTO batch_preorders (
                batch_id, user_id, customer_id, quantity, status,
                deposit_per_bird, deposit_paid, deposit_status, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0.0, 'unpaid', ?, ?)
            """,
            (
                int(batch_id), int(user_id), int(customer_id), qty, status,
                dpb, str(notes or "").strip(), now_iso,
            ),
        )
        pid = int(cur.lastrowid)
        conn.commit()
        row = conn.execute("SELECT * FROM batch_preorders WHERE id = ?", (pid,)).fetchone()
        return dict(row)


def update_batch_preorder(
    user_id: int,
    batch_id: int,
    preorder_id: int,
    status: str | None = None,
    quantity: int | None = None,
    deposit_paid: float | None = None,
    deposit_status: str | None = None,
    notes: str | None = None,
) -> dict | None:
    with get_db_connection() as conn:
        existing = conn.execute(
            "SELECT * FROM batch_preorders WHERE id = ? AND batch_id = ? AND user_id = ?",
            (int(preorder_id), int(batch_id), int(user_id)),
        ).fetchone()
        if not existing:
            return None
        new_status = status if status in ("reserved", "waitlist", "cancelled") else existing["status"]
        new_qty = max(1, int(quantity)) if quantity is not None else int(existing["quantity"])
        new_dp = round(max(0.0, float(deposit_paid)), 2) if deposit_paid is not None else float(existing["deposit_paid"] or 0.0)
        new_dst = (
            deposit_status
            if deposit_status in ("unpaid", "paid")
            else existing["deposit_status"]
        )
        new_notes = str(notes).strip() if notes is not None else (existing["notes"] or "")
        conn.execute(
            """
            UPDATE batch_preorders
            SET status = ?, quantity = ?, deposit_paid = ?, deposit_status = ?, notes = ?
            WHERE id = ? AND batch_id = ? AND user_id = ?
            """,
            (
                new_status, new_qty, new_dp, new_dst, new_notes,
                int(preorder_id), int(batch_id), int(user_id),
            ),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM batch_preorders WHERE id = ?", (int(preorder_id),)
        ).fetchone()
        return dict(updated) if updated else None


def delete_batch_preorder(user_id: int, batch_id: int, preorder_id: int) -> bool:
    with get_db_connection() as conn:
        cur = conn.execute(
            "DELETE FROM batch_preorders WHERE id = ? AND batch_id = ? AND user_id = ?",
            (int(preorder_id), int(batch_id), int(user_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def batch_preorder_counts(user_id: int) -> dict[int, dict]:
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT batch_id,
                   SUM(CASE WHEN status = 'reserved' THEN quantity ELSE 0 END) AS reserved_qty,
                   SUM(CASE WHEN status = 'waitlist' THEN quantity ELSE 0 END) AS waitlist_qty,
                   SUM(CASE WHEN status = 'reserved' THEN deposit_paid ELSE 0 END) AS deposits_paid
            FROM batch_preorders
            WHERE user_id = ?
            GROUP BY batch_id
            """,
            (int(user_id),),
        ).fetchall()
    return {
        int(r["batch_id"]): {
            "reserved_qty": int(r["reserved_qty"] or 0),
            "waitlist_qty": int(r["waitlist_qty"] or 0),
            "deposits_paid": round(float(r["deposits_paid"] or 0.0), 2),
        }
        for r in rows
    }


def settle_customer_batch_pickup(
    user_id: int,
    batch_id: int,
    customer_id: int,
    collected_today: float | None = None,
    mark_paid: bool = True,
    mark_picked_up: bool = True,
) -> int:
    """Settle all for-sale birds assigned to customer_id in batch_id on Pickup Day.

    If ``collected_today`` is provided, computes ``new_total = total_deposits + collected_today``.
    When ``new_total`` differs from the birds' current effective total (e.g. a tip/round-up or
    adjustment), distributes ``new_total`` across the birds proportionally by dressed weight
    into ``actual_sale_price``.
    """
    with get_db_connection() as conn:
        batch = conn.execute(
            "SELECT sale_price_per_lb FROM batches WHERE id = ? AND user_id = ?",
            (int(batch_id), int(user_id)),
        ).fetchone()
        if not batch:
            return 0
        price_per_lb = float(batch["sale_price_per_lb"] or 0.0)
        birds = conn.execute(
            """
            SELECT id, dressed_lbs, deposit_paid, actual_sale_price, payment_status, pickup_status
            FROM batch_harvests
            WHERE batch_id = ? AND customer_id = ? AND COALESCE(for_sale, 1) = 1
            ORDER BY id ASC
            """,
            (int(batch_id), int(customer_id)),
        ).fetchall()
        if not birds:
            return 0

        bird_list = [dict(b) for b in birds]
        total_dep = round(sum(float(b.get("deposit_paid") or 0.0) for b in bird_list), 2)
        cur_eff = [
            round(float(b["actual_sale_price"]), 2)
            if b.get("actual_sale_price") is not None
            else round(float(b.get("dressed_lbs") or 0.0) * price_per_lb, 2)
            for b in bird_list
        ]
        cur_total = round(sum(cur_eff), 2)

        new_actuals: list[float | None] = [
            round(float(b["actual_sale_price"]), 2) if b.get("actual_sale_price") is not None else None
            for b in bird_list
        ]
        if collected_today is not None:
            target_total = round(max(0.0, total_dep + max(0.0, float(collected_today))), 2)
            if abs(target_total - cur_total) >= 0.01:
                weights = [max(0.01, float(b.get("dressed_lbs") or 0.0)) for b in bird_list]
                w_sum = sum(weights) or 1.0
                allocated = 0.0
                for idx, w in enumerate(weights):
                    if idx == len(weights) - 1:
                        share = round(max(0.0, target_total - allocated), 2)
                    else:
                        share = round(target_total * (w / w_sum), 2)
                        allocated = round(allocated + share, 2)
                    new_actuals[idx] = share

        for b, act_p in zip(bird_list, new_actuals):
            pay_stat = "paid" if mark_paid else (b.get("payment_status") or "unpaid")
            pick_stat = "picked_up" if mark_picked_up else (b.get("pickup_status") or "assigned")
            conn.execute(
                """
                UPDATE batch_harvests
                SET payment_status = ?, pickup_status = ?, actual_sale_price = ?
                WHERE id = ? AND batch_id = ?
                """,
                (pay_stat, pick_stat, act_p, int(b["id"]), int(batch_id)),
            )
        conn.commit()
        return len(bird_list)


# ---------------------------------------------------------------------------
# Eggs & Laying Flock + Iron Ledger Bridge
# ---------------------------------------------------------------------------
def get_layer_settings(user_id: int) -> dict:
    raw_rate = get_text_setting("layer_feed_lbs_per_hen_day", "0.25", user_id=user_id).strip()
    raw_scratch_rate = get_text_setting("layer_scratch_lbs_per_day", "0.0", user_id=user_id).strip()
    raw_price = get_text_setting("layer_bag_price", "22.00", user_id=user_id).strip()
    raw_scratch_price = get_text_setting("layer_scratch_bag_price", "16.00", user_id=user_id).strip()
    raw_sku = get_text_setting("layer_ledger_sku_id", "", user_id=user_id).strip()
    try:
        rate = max(0.01, float(raw_rate))
    except ValueError:
        rate = 0.25
    try:
        scratch_rate = max(0.0, float(raw_scratch_rate))
    except ValueError:
        scratch_rate = 0.0
    try:
        price = max(0.0, float(raw_price))
    except ValueError:
        price = 22.00
    try:
        scratch_price = max(0.0, float(raw_scratch_price))
    except ValueError:
        scratch_price = 16.00
    sku_id = int(raw_sku) if raw_sku.isdigit() and int(raw_sku) > 0 else None
    return {
        "feed_lbs_per_hen_day": round(rate, 3),
        "scratch_lbs_per_day": round(scratch_rate, 2),
        "bag_price": round(price, 2),
        "scratch_bag_price": round(scratch_price, 2),
        "ledger_sku_id": sku_id,
    }


def save_layer_settings(
    user_id: int,
    feed_lbs_per_hen_day: float = 0.25,
    bag_price: float = 22.00,
    ledger_sku_id: int | None = None,
    scratch_lbs_per_day: float = 0.0,
    scratch_bag_price: float = 16.00,
) -> dict:
    rate = round(max(0.01, float(feed_lbs_per_hen_day or 0.25)), 3)
    s_rate = round(max(0.0, float(scratch_lbs_per_day or 0.0)), 2)
    price = round(max(0.0, float(bag_price or 0.0)), 2)
    s_price = round(max(0.0, float(scratch_bag_price or 0.0)), 2)
    sku_str = str(int(ledger_sku_id)) if ledger_sku_id and int(ledger_sku_id) > 0 else ""
    set_text_setting("layer_feed_lbs_per_hen_day", str(rate), user_id=user_id)
    set_text_setting("layer_scratch_lbs_per_day", str(s_rate), user_id=user_id)
    set_text_setting("layer_bag_price", f"{price:.2f}", user_id=user_id)
    set_text_setting("layer_scratch_bag_price", f"{s_price:.2f}", user_id=user_id)
    set_text_setting("layer_ledger_sku_id", sku_str, user_id=user_id)
    return get_layer_settings(user_id)


def list_available_ledger_skus() -> list[dict]:
    """List active Iron Ledger SKUs from the shared SQLite DB (safe if Iron Ledger tables don't exist yet)."""
    with get_db_connection() as conn:
        has_skus = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'skus'"
        ).fetchone()
        if not has_skus:
            return []
        rows = conn.execute(
            """
            SELECT k.id, k.merchant_id, k.name, k.variant_name, k.price_cents,
                   COALESCE(m.name, 'Merchant #' || k.merchant_id) AS merchant_name,
                   COALESCE((SELECT SUM(delta) FROM stock_ledger l WHERE l.sku_id = k.id), 0) AS on_hand
            FROM skus k
            LEFT JOIN merchants m ON m.id = k.merchant_id
            WHERE k.active = 1 AND COALESCE(k.item_type, 'product') != 'fee'
            ORDER BY CASE WHEN LOWER(k.name) LIKE '%egg%' THEN 0 ELSE 1 END, k.name ASC, k.id ASC
            """
        ).fetchall()
        out = []
        for r in rows:
            label = r["name"] + (f" · {r['variant_name']}" if r["variant_name"] else "")
            out.append({
                "id": int(r["id"]),
                "merchant_id": int(r["merchant_id"]),
                "merchant_name": r["merchant_name"],
                "name": r["name"],
                "variant_name": r["variant_name"] or "",
                "label": label,
                "price_cents": int(r["price_cents"] or 0),
                "on_hand": float(r["on_hand"] or 0.0),
            })
        return out


def add_layer_flock_event(
    user_id: int,
    event_date: str,
    delta: int,
    reason: str = "purchase",
    source_batch_id: int | None = None,
    notes: str = "",
) -> int:
    clean_date = (event_date or date.today().isoformat()).strip()[:10]
    d_val = int(delta)
    if d_val == 0:
        raise ValueError("Headcount change cannot be zero.")
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO layer_flock_events (
                user_id, event_date, delta, reason, source_batch_id, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(user_id),
                clean_date,
                d_val,
                str(reason or "purchase").strip(),
                int(source_batch_id) if source_batch_id else None,
                str(notes or "").strip(),
                now_iso,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def delete_layer_flock_event(user_id: int, event_id: int) -> bool:
    with get_db_connection() as conn:
        cur = conn.execute(
            "DELETE FROM layer_flock_events WHERE id = ? AND user_id = ?",
            (int(event_id), int(user_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def list_layer_flock_events(user_id: int) -> list[dict]:
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT e.*, b.name AS batch_name
            FROM layer_flock_events e
            LEFT JOIN batches b ON b.id = e.source_batch_id
            WHERE e.user_id = ?
            ORDER BY e.event_date DESC, e.id DESC
            """,
            (int(user_id),),
        ).fetchall()
        return [dict(r) for r in rows]


def is_batch_graduated_to_layers(user_id: int, batch_id: int) -> bool:
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM layer_flock_events WHERE user_id = ? AND source_batch_id = ? LIMIT 1",
            (int(user_id), int(batch_id)),
        ).fetchone()
        return bool(row)


def graduate_batch_to_layers(
    user_id: int,
    batch_id: int,
    head_count: int,
    event_date: str | None = None,
    archive_batch: bool = True,
) -> int:
    hc = max(1, int(head_count or 1))
    with get_db_connection() as conn:
        b = conn.execute(
            "SELECT id, name FROM batches WHERE id = ? AND user_id = ?",
            (int(batch_id), int(user_id)),
        ).fetchone()
        if not b:
            raise ValueError("Batch not found.")
        already = conn.execute(
            "SELECT id FROM layer_flock_events WHERE user_id = ? AND source_batch_id = ?",
            (int(user_id), int(batch_id)),
        ).fetchone()
        if already:
            raise ValueError("This batch has already been graduated to the laying flock.")
    ev_id = add_layer_flock_event(
        user_id,
        event_date or date.today().isoformat(),
        delta=hc,
        reason="graduated_batch",
        source_batch_id=batch_id,
        notes=f"Graduated from {b['name']}",
    )
    if archive_batch:
        set_batch_active(batch_id, False)
    return ev_id


def add_egg_log(
    user_id: int,
    log_date: str,
    total_collected: int,
    cracked_unusable: int = 0,
    home_used: int = 0,
    notes: str = "",
) -> int:
    clean_date = (log_date or date.today().isoformat()).strip()[:10]
    tot = max(0, int(total_collected or 0))
    cracked = max(0, min(tot, int(cracked_unusable or 0)))
    home = max(0, min(tot - cracked, int(home_used or 0)))
    if tot == 0 and cracked == 0 and home == 0:
        raise ValueError("Total eggs collected must be greater than 0.")
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO egg_logs (
                user_id, log_date, total_collected, cracked_unusable, home_used, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (int(user_id), clean_date, tot, cracked, home, str(notes or "").strip(), now_iso),
        )
        conn.commit()
        return int(cur.lastrowid)


def delete_egg_log(user_id: int, log_id: int) -> bool:
    with get_db_connection() as conn:
        cur = conn.execute(
            "DELETE FROM egg_logs WHERE id = ? AND user_id = ?",
            (int(log_id), int(user_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def list_egg_logs(user_id: int, limit: int = 90) -> list[dict]:
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM egg_logs
            WHERE user_id = ?
            ORDER BY log_date DESC, id DESC
            LIMIT ?
            """,
            (int(user_id), int(limit)),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["good_packable"] = max(
                0,
                int(d["total_collected"] or 0)
                - int(d["cracked_unusable"] or 0)
                - int(d["home_used"] or 0),
            )
            out.append(d)
        return out


def pack_egg_cartons(
    user_id: int,
    pack_date: str,
    dozens_packed: int,
    notes: str = "",
    sync_to_ledger: bool = True,
) -> dict:
    dz = max(1, int(dozens_packed or 1))
    eggs_used = dz * 12
    clean_date = (pack_date or date.today().isoformat()).strip()[:10]
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    cfg = get_layer_settings(user_id)
    sku_id = cfg["ledger_sku_id"] if sync_to_ledger else None
    ledger_entry_id = None

    with get_db_connection() as conn:
        if sku_id:
            has_skus = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'skus'"
            ).fetchone()
            if has_skus:
                sku_row = conn.execute(
                    "SELECT id, merchant_id FROM skus WHERE id = ?", (int(sku_id),)
                ).fetchone()
                if sku_row:
                    l_cur = conn.execute(
                        """
                        INSERT INTO stock_ledger (
                            merchant_id, sku_id, delta, reason, note, created_by, created_at
                        ) VALUES (?, ?, ?, 'restock', ?, ?, ?)
                        """,
                        (
                            int(sku_row["merchant_id"]),
                            int(sku_id),
                            dz,
                            f"Packed {dz} dz from Homestead Egg Log ({clean_date})",
                            int(user_id),
                            now_iso,
                        ),
                    )
                    ledger_entry_id = int(l_cur.lastrowid)
                else:
                    sku_id = None
            else:
                sku_id = None

        p_cur = conn.execute(
            """
            INSERT INTO egg_pack_logs (
                user_id, pack_date, dozens_packed, eggs_used,
                ledger_sku_id, ledger_entry_id, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(user_id),
                clean_date,
                dz,
                eggs_used,
                sku_id,
                ledger_entry_id,
                str(notes or "").strip(),
                now_iso,
            ),
        )
        conn.commit()
        return {
            "pack_id": int(p_cur.lastrowid),
            "dozens_packed": dz,
            "eggs_used": eggs_used,
            "synced_to_ledger": bool(ledger_entry_id),
            "ledger_entry_id": ledger_entry_id,
        }


def delete_egg_pack_log(user_id: int, pack_id: int) -> bool:
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM egg_pack_logs WHERE id = ? AND user_id = ?",
            (int(pack_id), int(user_id)),
        ).fetchone()
        if not row:
            return False
        if row["ledger_entry_id"]:
            has_ledger = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'stock_ledger'"
            ).fetchone()
            if has_ledger:
                conn.execute(
                    "DELETE FROM stock_ledger WHERE id = ?",
                    (int(row["ledger_entry_id"]),),
                )
        conn.execute("DELETE FROM egg_pack_logs WHERE id = ? AND user_id = ?", (int(pack_id), int(user_id)))
        conn.commit()
        return True


def list_egg_pack_logs(user_id: int, limit: int = 30) -> list[dict]:
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM egg_pack_logs
            WHERE user_id = ?
            ORDER BY pack_date DESC, id DESC
            LIMIT ?
            """,
            (int(user_id), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]


def add_layer_feed_log(
    user_id: int,
    feed_date: str,
    bags: float,
    feed_lbs: float,
    cost_total: float,
    notes: str = "",
    receipt_url: str = "",
    drive_file_id: str = "",
    feed_type: str = "layer",
) -> int:
    clean_date = (feed_date or date.today().isoformat()).strip()[:10]
    ftype = feed_type if feed_type in ("layer", "scratch", "supplement") else "layer"
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO layer_feed_logs (
                user_id, feed_date, feed_type, bags, feed_lbs, cost_total,
                notes, receipt_url, drive_file_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(user_id),
                clean_date,
                ftype,
                max(0.0, float(bags or 0.0)),
                max(0.0, float(feed_lbs or 0.0)),
                max(0.0, float(cost_total or 0.0)),
                str(notes or "").strip(),
                str(receipt_url or "").strip(),
                str(drive_file_id or "").strip(),
                now_iso,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def delete_layer_feed_log(user_id: int, feed_id: int) -> bool:
    with get_db_connection() as conn:
        cur = conn.execute(
            "DELETE FROM layer_feed_logs WHERE id = ? AND user_id = ?",
            (int(feed_id), int(user_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def list_layer_feed_logs(user_id: int, limit: int = 50) -> list[dict]:
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM layer_feed_logs
            WHERE user_id = ?
            ORDER BY feed_date DESC, id DESC
            LIMIT ?
            """,
            (int(user_id), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]


def compute_egg_dashboard(user_id: int, bag_weight: int = 50) -> dict:
    """Compute laying flock headcount, daily egg metrics, loose eggs ready to pack,
    layer feed economics, and live Iron Ledger carton stock & sales revenue.
    """
    cfg = get_layer_settings(user_id)
    flock_events = list_layer_flock_events(user_id)
    egg_logs = list_egg_logs(user_id, limit=365)
    pack_logs = list_egg_pack_logs(user_id, limit=100)
    feed_logs = list_layer_feed_logs(user_id, limit=100)
    ledger_skus = list_available_ledger_skus()

    def _hens_on_date(d_iso: str) -> int:
        tot = sum(int(ev["delta"] or 0) for ev in flock_events if str(ev["event_date"]) <= d_iso)
        return max(0, tot)

    today = date.today()
    today_iso = today.isoformat()
    active_hens = _hens_on_date(today_iso)

    by_day: dict[str, dict] = {}
    total_collected_all = 0
    total_cracked_all = 0
    total_home_all = 0
    total_good_all = 0
    for lg in egg_logs:
        ds = str(lg["log_date"])[:10]
        c_val = int(lg["total_collected"] or 0)
        cr_val = int(lg["cracked_unusable"] or 0)
        hm_val = int(lg["home_used"] or 0)
        gd_val = int(lg["good_packable"] or 0)
        total_collected_all += c_val
        total_cracked_all += cr_val
        total_home_all += hm_val
        total_good_all += gd_val
        slot = by_day.setdefault(ds, {"collected": 0, "cracked": 0, "home": 0, "good": 0})
        slot["collected"] += c_val
        slot["cracked"] += cr_val
        slot["home"] += hm_val
        slot["good"] += gd_val

    with get_db_connection() as conn:
        all_egg_sums = conn.execute(
            """
            SELECT COALESCE(SUM(total_collected), 0) AS tot,
                   COALESCE(SUM(cracked_unusable), 0) AS cr,
                   COALESCE(SUM(home_used), 0) AS hm
            FROM egg_logs WHERE user_id = ?
            """,
            (int(user_id),),
        ).fetchone()
        total_packed_row = conn.execute(
            """
            SELECT COALESCE(SUM(dozens_packed), 0) AS dz,
                   COALESCE(SUM(eggs_used), 0) AS eggs
            FROM egg_pack_logs WHERE user_id = ?
            """,
            (int(user_id),),
        ).fetchone()

    lifetime_collected = int(all_egg_sums["tot"] or 0)
    lifetime_cracked = int(all_egg_sums["cr"] or 0)
    lifetime_home = int(all_egg_sums["hm"] or 0)
    lifetime_good = max(0, lifetime_collected - lifetime_cracked - lifetime_home)
    lifetime_packed_dz = int(total_packed_row["dz"] or 0)
    lifetime_packed_eggs = int(total_packed_row["eggs"] or 0)

    loose_eggs = max(0, lifetime_good - lifetime_packed_eggs)
    ready_dozens = loose_eggs // 12
    remainder_loose = loose_eggs % 12

    today_slot = by_day.get(today_iso, {"collected": 0, "cracked": 0, "home": 0, "good": 0})
    today_eggs = today_slot["collected"]
    today_lay_rate = round((today_eggs / active_hens) * 100, 1) if active_hens > 0 else 0.0

    # Last 7 days and 30 days series
    chart_days = []
    eggs_7d = 0
    eggs_30d = 0
    rate_sum_7d = 0.0
    rate_days_7d = 0
    for i in range(29, -1, -1):
        d_obj = today - timedelta(days=i)
        d_iso = d_obj.isoformat()
        slot = by_day.get(d_iso, {"collected": 0, "cracked": 0, "home": 0, "good": 0})
        hens_d = _hens_on_date(d_iso) or active_hens
        rate_d = round((slot["collected"] / hens_d) * 100, 1) if hens_d > 0 else 0.0
        eggs_30d += slot["collected"]
        if i < 7:
            eggs_7d += slot["collected"]
            if hens_d > 0:
                rate_sum_7d += rate_d
                rate_days_7d += 1
        chart_days.append({
            "date": d_iso,
            "label": d_obj.strftime("%b %-d"),
            "short_label": d_obj.strftime("%-d"),
            "collected": slot["collected"],
            "good": slot["good"],
            "hens": hens_d,
            "lay_rate_pct": rate_d,
        })
    chart_peak = max((c["collected"] for c in chart_days), default=0) or 1
    avg_7d_eggs = round(eggs_7d / 7.0, 1)
    avg_7d_lay_rate = round(rate_sum_7d / rate_days_7d, 1) if rate_days_7d > 0 else 0.0

    # Layer + Scratch feed economics, Barn Stockpile & Amortized Cost/Dozen
    daily_layer_burn_lbs = round(active_hens * cfg["feed_lbs_per_hen_day"], 2)
    daily_scratch_burn_lbs = round(cfg["scratch_lbs_per_day"], 2) if active_hens > 0 else 0.0
    daily_feed_burn_lbs = round(daily_layer_burn_lbs + daily_scratch_burn_lbs, 2)
    weekly_feed_lbs = round(daily_feed_burn_lbs * 7, 1)
    monthly_feed_lbs = round(daily_feed_burn_lbs * 30, 1)
    monthly_bags = round(monthly_feed_lbs / max(1, bag_weight), 1)

    layer_bought_lbs = round(
        sum(float(f["feed_lbs"] or 0.0) for f in feed_logs if (f.get("feed_type") or "layer") == "layer"), 1
    )
    layer_bought_cost = round(
        sum(float(f["cost_total"] or 0.0) for f in feed_logs if (f.get("feed_type") or "layer") == "layer"), 2
    )
    scratch_bought_lbs = round(
        sum(float(f["feed_lbs"] or 0.0) for f in feed_logs if f.get("feed_type") == "scratch"), 1
    )
    scratch_bought_cost = round(
        sum(float(f["cost_total"] or 0.0) for f in feed_logs if f.get("feed_type") == "scratch"), 2
    )
    supp_bought_cost = round(
        sum(float(f["cost_total"] or 0.0) for f in feed_logs if f.get("feed_type") == "supplement"), 2
    )

    logged_feed_lbs = round(sum(float(f["feed_lbs"] or 0.0) for f in feed_logs), 1)
    logged_feed_cost = round(sum(float(f["cost_total"] or 0.0) for f in feed_logs), 2)

    layer_price_per_lb = (
        (layer_bought_cost / layer_bought_lbs)
        if layer_bought_lbs > 0 and layer_bought_cost > 0
        else (cfg["bag_price"] / max(1, bag_weight))
    )
    scratch_price_per_lb = (
        (scratch_bought_cost / scratch_bought_lbs)
        if scratch_bought_lbs > 0 and scratch_bought_cost > 0
        else (cfg["scratch_bag_price"] / max(1, bag_weight))
    )

    # Barn stockpile remaining (lbs bought minus daily burn since earliest purchase date of that feed type)
    layer_dates = [str(f["feed_date"])[:10] for f in feed_logs if (f.get("feed_type") or "layer") == "layer"]
    layer_burned_since_buy = 0.0
    if layer_dates:
        try:
            d0 = date.fromisoformat(min(layer_dates))
        except ValueError:
            d0 = today
        span_days = max(1, min(365, (today - d0).days + 1))
        for idx in range(span_days):
            di = (d0 + timedelta(days=idx)).isoformat()
            h_i = _hens_on_date(di) or active_hens
            layer_burned_since_buy += h_i * cfg["feed_lbs_per_hen_day"]
    layer_remaining_lbs = round(max(0.0, layer_bought_lbs - layer_burned_since_buy), 1)
    layer_days_left = (
        round(layer_remaining_lbs / daily_layer_burn_lbs, 1)
        if daily_layer_burn_lbs > 0 and layer_bought_lbs > 0
        else None
    )

    scratch_dates = [str(f["feed_date"])[:10] for f in feed_logs if f.get("feed_type") == "scratch"]
    scratch_burned_since_buy = 0.0
    if scratch_dates:
        try:
            sd0 = date.fromisoformat(min(scratch_dates))
        except ValueError:
            sd0 = today
        s_span = max(1, min(365, (today - sd0).days + 1))
        scratch_burned_since_buy = s_span * cfg["scratch_lbs_per_day"]
    scratch_remaining_lbs = round(max(0.0, scratch_bought_lbs - scratch_burned_since_buy), 1)
    scratch_days_left = (
        round(scratch_remaining_lbs / daily_scratch_burn_lbs, 1)
        if daily_scratch_burn_lbs > 0 and scratch_bought_lbs > 0
        else None
    )

    # Amortized feed cost eaten on active egg-collection days (so buying 5 bags in bulk doesn't spike Day 1 cost/dz)
    consumed_layer_lbs = 0.0
    consumed_scratch_lbs = 0.0
    for d_iso in by_day:
        h_d = _hens_on_date(d_iso) or active_hens
        consumed_layer_lbs += h_d * cfg["feed_lbs_per_hen_day"]
        if h_d > 0:
            consumed_scratch_lbs += cfg["scratch_lbs_per_day"]
    consumed_feed_cost = round(
        consumed_layer_lbs * layer_price_per_lb
        + consumed_scratch_lbs * scratch_price_per_lb
        + supp_bought_cost,
        2,
    )
    if consumed_feed_cost > 0:
        reconciled_feed_cost = consumed_feed_cost
        feed_cost_source = "amortized from logged bags" if logged_feed_cost > 0 else "estimated daily burn"
    elif logged_feed_cost > 0:
        reconciled_feed_cost = logged_feed_cost
        feed_cost_source = "logged"
    else:
        reconciled_feed_cost = 0.0
        feed_cost_source = "estimated"

    lifetime_dozens_collected = round(lifetime_collected / 12.0, 2)
    cost_per_dozen = (
        round(reconciled_feed_cost / lifetime_dozens_collected, 2)
        if lifetime_dozens_collected > 0
        else 0.0
    )

    # Linked Iron Ledger SKU stats & realized sales revenue
    linked_sku = next((s for s in ledger_skus if s["id"] == cfg["ledger_sku_id"]), None)
    ledger_dozens_sold = 0.0
    ledger_revenue = 0.0
    if linked_sku:
        with get_db_connection() as conn:
            has_sales = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sale_items'"
            ).fetchone()
            if has_sales:
                s_row = conn.execute(
                    """
                    SELECT COALESCE(SUM(si.quantity), 0) AS qty,
                           COALESCE(SUM(si.quantity * si.unit_price_cents), 0) AS rev_cents
                    FROM sale_items si
                    JOIN sales s ON s.id = si.sale_id
                    WHERE si.sku_id = ? AND s.status = 'completed'
                    """,
                    (int(linked_sku["id"]),),
                ).fetchone()
                ledger_dozens_sold = round(float(s_row["qty"] or 0.0), 1)
                ledger_revenue = round(float(s_row["rev_cents"] or 0.0) / 100.0, 2)

    net_egg_profit = round(ledger_revenue - reconciled_feed_cost, 2)

    return {
        "settings": cfg,
        "active_hens": active_hens,
        "today_iso": today_iso,
        "today_eggs": today_eggs,
        "today_lay_rate": today_lay_rate,
        "eggs_7d": eggs_7d,
        "eggs_30d": eggs_30d,
        "avg_7d_eggs": avg_7d_eggs,
        "avg_7d_lay_rate": avg_7d_lay_rate,
        "lifetime_collected": lifetime_collected,
        "lifetime_dozens_collected": lifetime_dozens_collected,
        "lifetime_cracked": lifetime_cracked,
        "lifetime_home": lifetime_home,
        "lifetime_packed_dz": lifetime_packed_dz,
        "loose_eggs": loose_eggs,
        "ready_dozens": ready_dozens,
        "remainder_loose": remainder_loose,
        "daily_layer_burn_lbs": daily_layer_burn_lbs,
        "daily_scratch_burn_lbs": daily_scratch_burn_lbs,
        "daily_feed_burn_lbs": daily_feed_burn_lbs,
        "weekly_feed_lbs": weekly_feed_lbs,
        "monthly_feed_lbs": monthly_feed_lbs,
        "monthly_bags": monthly_bags,
        "layer_bought_lbs": layer_bought_lbs,
        "layer_bought_cost": layer_bought_cost,
        "layer_remaining_lbs": layer_remaining_lbs,
        "layer_days_left": layer_days_left,
        "scratch_bought_lbs": scratch_bought_lbs,
        "scratch_bought_cost": scratch_bought_cost,
        "scratch_remaining_lbs": scratch_remaining_lbs,
        "scratch_days_left": scratch_days_left,
        "logged_feed_lbs": logged_feed_lbs,
        "logged_feed_cost": logged_feed_cost,
        "reconciled_feed_cost": reconciled_feed_cost,
        "feed_cost_source": feed_cost_source,
        "cost_per_dozen": cost_per_dozen,
        "linked_sku": linked_sku,
        "ledger_skus": ledger_skus,
        "ledger_dozens_sold": ledger_dozens_sold,
        "ledger_revenue": ledger_revenue,
        "net_egg_profit": net_egg_profit,
        "chart_days": chart_days,
        "chart_peak": chart_peak,
        "flock_events": flock_events,
        "egg_logs": egg_logs[:60],
        "pack_logs": pack_logs,
        "feed_logs": feed_logs,
    }
