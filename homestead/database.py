"""SQLite persistence layer, multi-user authentication, species presets, automatic schema migrations, and JSON backup/restore."""

from datetime import date, datetime, timedelta
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import streamlit as st

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
        ]
        for col_name, col_type in new_columns:
            if col_name not in existing_cols:
                conn.execute(
                    f"ALTER TABLE batches ADD COLUMN {col_name} {col_type}"
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
) -> int:
    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO batch_harvests (
                batch_id, harvest_date, head_count, dressed_lbs, processing_cost_total, notes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(batch_id),
                str(harvest_date),
                max(1, int(head_count)),
                max(0.0, float(dressed_lbs)),
                max(0.0, float(processing_cost_total)),
                str(notes).strip(),
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
                    processing_cost, expected_yield_lbs, sale_price_per_lb, active, notes, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
