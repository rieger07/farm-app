"""Core SQLite connection/init helpers, Homestead users, species presets, and settings."""

from datetime import datetime
import sqlite3

from homestead.config import (
    DEFAULT_BAG_WEIGHT,
    DEFAULT_BARREL_CAPACITY,
    LIFECYCLE_TYPES,
    LIVESTOCK_DATA,
    infer_lifecycle_type,
)
from shared.auth import (
    authenticate_user,
    count_users,
    create_user as _shared_create_user,
    create_user_session,
    delete_user_session,
    get_user_by_id,
    get_user_by_session_token,
    get_user_by_username,
    hash_password,
    list_users,
    update_user_password,
    verify_password,
)
from shared.db import (
    get_connection as _shared_get_connection,
    get_db_path,
    migrate as _shared_migrate,
)


def get_db_connection() -> sqlite3.Connection:
    return _shared_get_connection()


def _resolve_user_id(user_id: int | None = None) -> int | None:
    return int(user_id) if user_id is not None else None


def init_db() -> None:
    _shared_migrate()


# ---------------------------------------------------------------------------
# User Authentication & Session Management (core delegated to shared.auth)
# ---------------------------------------------------------------------------


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
    with get_db_connection() as conn:
        existing_cnt = int(
            conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()["cnt"]
        )
        first_user = existing_cnt == 0
        user = _shared_create_user(
            username,
            password,
            display_name=display_name,
            is_admin=is_admin,
            conn=conn,
        )
        new_uid = int(user["id"])
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
                            user["created_at"],
                        ),
                    )

        conn.commit()
        return user


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
