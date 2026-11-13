from datetime import date, datetime, timedelta
import hashlib
import importlib.util
import json
import math
import os
import sqlite3
import sys
import types
import urllib.parse
import pandas as pd
import streamlit as st

# Provide a lightweight stdlib shim for pkg_resources (removed in setuptools>=82),
# which streamlit-vis-timeline imports to locate its frontend/build directory.
if "pkg_resources" not in sys.modules:
    try:
        import pkg_resources  # type: ignore # noqa: F401
    except ModuleNotFoundError:
        _pkg_res = types.ModuleType("pkg_resources")

        def _resource_filename(package_or_requirement: str, resource_name: str) -> str:
            spec = importlib.util.find_spec(package_or_requirement)
            if spec and spec.origin:
                return os.path.join(os.path.dirname(spec.origin), resource_name)
            raise ModuleNotFoundError(
                f"No module named {package_or_requirement!r}"
            )

        _pkg_res.resource_filename = _resource_filename  # type: ignore[attr-defined]
        sys.modules["pkg_resources"] = _pkg_res

try:
    from streamlit_timeline import st_timeline
except ModuleNotFoundError:

    def st_timeline(  # type: ignore[misc]
        items: list,
        groups: list | None = None,
        options: dict | None = None,
        height: str = "350px",
    ) -> dict | str | None:
        return st.session_state.get("_mock_selected_tl_item")

# Livestock Data Dictionary (Default Species Presets)
# days_to_birth: Gestation or Incubation days
# starter_weeks / starter_feed_lbs: Starter phase duration (weeks) & feed per animal (lbs)
# grower_weeks / grower_feed_lbs: Grower/Finisher phase duration (weeks) & feed per animal (lbs)
# starter_bag_price / grower_bag_price: Default price ($) per feed bag
# unit_cost: Default acquisition cost ($) per egg/chick/poult/piglet
# processing_cost: Default butcher/processing cost ($) per harvested animal
# expected_yield_lbs: Default dressed/packaged meat yield (lbs) per harvested animal
# sale_price_per_lb: Default target sale price ($/lb) of dressed meat
LIVESTOCK_DATA = {
    "chicken eggs": {
        "days_to_birth": 21,
        "starter_weeks": 3,
        "starter_feed_lbs": 3.0,
        "grower_weeks": 5,
        "grower_feed_lbs": 12.0,
        "starter_bag_price": 24.0,
        "grower_bag_price": 22.0,
        "unit_cost": 2.50,
        "processing_cost": 5.00,
        "expected_yield_lbs": 4.5,
        "sale_price_per_lb": 6.50,
        "weeks_to_proc": 8,
        "feed_total": 15.0,
    },
    "chicken chicks": {
        "days_to_birth": 0,
        "starter_weeks": 3,
        "starter_feed_lbs": 3.0,
        "grower_weeks": 5,
        "grower_feed_lbs": 12.0,
        "starter_bag_price": 24.0,
        "grower_bag_price": 22.0,
        "unit_cost": 4.00,
        "processing_cost": 5.00,
        "expected_yield_lbs": 4.5,
        "sale_price_per_lb": 6.50,
        "weeks_to_proc": 8,
        "feed_total": 15.0,
    },
    "turkey chicks": {
        "days_to_birth": 0,
        "starter_weeks": 6,
        "starter_feed_lbs": 12.0,
        "grower_weeks": 12,
        "grower_feed_lbs": 63.0,
        "starter_bag_price": 28.0,
        "grower_bag_price": 24.0,
        "unit_cost": 12.00,
        "processing_cost": 15.00,
        "expected_yield_lbs": 16.0,
        "sale_price_per_lb": 7.50,
        "weeks_to_proc": 18,
        "feed_total": 75.0,
    },
    "kunekune pigs": {
        "days_to_birth": 116,
        "starter_weeks": 8,
        "starter_feed_lbs": 50.0,
        "grower_weeks": 44,
        "grower_feed_lbs": 600.0,
        "starter_bag_price": 26.0,
        "grower_bag_price": 20.0,
        "unit_cost": 250.00,
        "processing_cost": 350.00,
        "expected_yield_lbs": 120.0,
        "sale_price_per_lb": 9.00,
        "weeks_to_proc": 52,
        "feed_total": 650.0,
    },
}

DEFAULT_BARREL_CAPACITY = 300  # lbs per barrel
DEFAULT_BAG_WEIGHT = 50  # lbs per feed bag


def get_db_path() -> str:
    return os.environ.get("FARM_DB_PATH", os.path.join("data", "homestead.db"))


def get_species_defaults(species: str) -> dict:
    return LIVESTOCK_DATA.get(
        str(species).lower(), LIVESTOCK_DATA["chicken chicks"]
    )


# ---------------------------------------------------------------------------
# Database Persistence Layer (SQLite + Automatic Migration + JSON Backup)
# ---------------------------------------------------------------------------
def get_db_connection() -> sqlite3.Connection:
    db_path = get_db_path()
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                species TEXT NOT NULL,
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
        # Migrate older databases if phased-feed / cost / reconciliation columns are missing
        existing_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(batches)").fetchall()
        }
        new_columns = [
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

        # Backfill any rows that were created before phased-feed / sale_price columns existed
        legacy_rows = conn.execute(
            "SELECT * FROM batches WHERE starter_weeks IS NULL OR starter_feed_lbs IS NULL OR sale_price_per_lb IS NULL"
        ).fetchall()
        for row in legacy_rows:
            d = get_species_defaults(row["species"])
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

            conn.execute(
                """
                UPDATE batches
                SET starter_weeks = COALESCE(starter_weeks, ?),
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
        conn.commit()


def get_int_setting(key: str, default_val: int) -> int:
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if row:
            try:
                return max(1, int(float(row["value"])))
            except (ValueError, TypeError):
                return default_val
    return default_val


def set_int_setting(key: str, value: int) -> None:
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(max(1, int(value)))),
        )
        conn.commit()


def get_barrel_capacity() -> int:
    return get_int_setting("barrel_capacity", DEFAULT_BARREL_CAPACITY)


def set_barrel_capacity(capacity: int) -> None:
    set_int_setting("barrel_capacity", capacity)


def get_bag_weight() -> int:
    return get_int_setting("bag_weight", DEFAULT_BAG_WEIGHT)


def set_bag_weight(weight: int) -> None:
    set_int_setting("bag_weight", weight)


def list_batches() -> list[dict]:
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM batches ORDER BY start_date ASC, id ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def list_batch_losses(batch_id: int | None = None) -> list[dict]:
    with get_db_connection() as conn:
        if batch_id is not None:
            rows = conn.execute(
                "SELECT * FROM batch_losses WHERE batch_id = ? ORDER BY loss_date ASC, id ASC",
                (int(batch_id),),
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


def list_batch_harvests(batch_id: int | None = None) -> list[dict]:
    with get_db_connection() as conn:
        if batch_id is not None:
            rows = conn.execute(
                "SELECT * FROM batch_harvests WHERE batch_id = ? ORDER BY harvest_date ASC, id ASC",
                (int(batch_id),),
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


def list_batch_sales(batch_id: int | None = None) -> list[dict]:
    with get_db_connection() as conn:
        if batch_id is not None:
            rows = conn.execute(
                "SELECT * FROM batch_sales WHERE batch_id = ? ORDER BY entry_date ASC, id ASC",
                (int(batch_id),),
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


def _normalize_batch_payload(batch_data: dict) -> dict:
    species = str(batch_data.get("species", "chicken chicks")).lower()
    defaults = get_species_defaults(species)

    has_phased = (
        "starter_weeks" in batch_data
        or "grower_weeks" in batch_data
        or "starter_feed_lbs" in batch_data
        or "grower_feed_lbs" in batch_data
    )

    if has_phased:
        starter_weeks = max(
            0, int(batch_data.get("starter_weeks", defaults["starter_weeks"]))
        )
        grower_weeks = max(
            0, int(batch_data.get("grower_weeks", defaults["grower_weeks"]))
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
        weeks_to_proc = max(
            1, int(batch_data.get("weeks_to_proc", defaults["weeks_to_proc"]))
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
            weeks_to_proc,
            max(
                0,
                round(weeks_to_proc * (defaults["starter_weeks"] / default_wks)),
            ),
        )
        grower_weeks = max(0, weeks_to_proc - starter_weeks)
        starter_feed_lbs = round(
            feed_total * (defaults["starter_feed_lbs"] / default_fd), 2
        )
        grower_feed_lbs = round(max(0.0, feed_total - starter_feed_lbs), 2)

    start_str = str(batch_data.get("start_date", date.today().isoformat()))
    end_str = str(batch_data.get("end_date", start_str))

    return {
        "name": str(batch_data.get("name") or f"{species.title()} Batch"),
        "species": species,
        "quantity": max(1, int(batch_data.get("quantity", 1))),
        "start_date": start_str,
        "end_date": end_str,
        "days_to_birth": max(
            0, int(batch_data.get("days_to_birth", defaults["days_to_birth"]))
        ),
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


def save_batch(batch_data: dict, batch_id: int | None = None) -> int:
    norm = _normalize_batch_payload(batch_data)
    with get_db_connection() as conn:
        if batch_id is not None:
            conn.execute(
                """
                UPDATE batches
                SET name = ?, species = ?, quantity = ?, start_date = ?, end_date = ?,
                    days_to_birth = ?, weeks_to_proc = ?, feed_total = ?,
                    starter_weeks = ?, starter_feed_lbs = ?, grower_weeks = ?, grower_feed_lbs = ?,
                    starter_bag_price = ?, grower_bag_price = ?, unit_cost = ?,
                    processing_cost = ?, expected_yield_lbs = ?, sale_price_per_lb = ?, active = ?, notes = ?
                WHERE id = ?
                """,
                (
                    norm["name"],
                    norm["species"],
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
                    name, species, quantity, start_date, end_date,
                    days_to_birth, weeks_to_proc, feed_total,
                    starter_weeks, starter_feed_lbs, grower_weeks, grower_feed_lbs,
                    starter_bag_price, grower_bag_price, unit_cost,
                    processing_cost, expected_yield_lbs, sale_price_per_lb, active, notes, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    norm["name"],
                    norm["species"],
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


def export_backup_json() -> str:
    batches = list_batches()
    losses_by_batch: dict[int, list[dict]] = {}
    for l in list_batch_losses():
        losses_by_batch.setdefault(int(l["batch_id"]), []).append(l)

    harvests_by_batch: dict[int, list[dict]] = {}
    for h in list_batch_harvests():
        harvests_by_batch.setdefault(int(h["batch_id"]), []).append(h)

    sales_by_batch: dict[int, list[dict]] = {}
    for s_row in list_batch_sales():
        sales_by_batch.setdefault(int(s_row["batch_id"]), []).append(s_row)

    enriched_batches = []
    for b in batches:
        b_copy = dict(b)
        bid = int(b["id"])
        b_copy["losses"] = losses_by_batch.get(bid, [])
        b_copy["harvests"] = harvests_by_batch.get(bid, [])
        b_copy["sales"] = sales_by_batch.get(bid, [])
        enriched_batches.append(b_copy)

    payload = {
        "version": 3,
        "exported_at": datetime.utcnow().isoformat(timespec="seconds"),
        "settings": {
            "barrel_capacity": get_barrel_capacity(),
            "bag_weight": get_bag_weight(),
        },
        "batches": enriched_batches,
    }
    return json.dumps(payload, indent=2)


def import_backup_json(raw_json: str, replace_existing: bool = False) -> int:
    data = json.loads(raw_json)
    if isinstance(data, list):
        batches_to_import = data
        settings_to_import = {}
    elif isinstance(data, dict):
        batches_to_import = data.get("batches", [])
        settings_to_import = data.get("settings", {})
    else:
        raise ValueError("Invalid JSON backup format.")

    if "barrel_capacity" in settings_to_import:
        set_barrel_capacity(int(settings_to_import["barrel_capacity"]))
    if "bag_weight" in settings_to_import:
        set_bag_weight(int(settings_to_import["bag_weight"]))

    with get_db_connection() as conn:
        if replace_existing:
            conn.execute("DELETE FROM batch_sales")
            conn.execute("DELETE FROM batch_harvests")
            conn.execute("DELETE FROM batch_losses")
            conn.execute("DELETE FROM batches")
        count = 0
        for item in batches_to_import:
            norm = _normalize_batch_payload(item)
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
            cursor = conn.execute(
                """
                INSERT INTO batches (
                    name, species, quantity, start_date, end_date,
                    days_to_birth, weeks_to_proc, feed_total,
                    starter_weeks, starter_feed_lbs, grower_weeks, grower_feed_lbs,
                    starter_bag_price, grower_bag_price, unit_cost,
                    processing_cost, expected_yield_lbs, sale_price_per_lb,
                    actual_feed_lbs, actual_feed_cost, active, notes, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    norm["name"],
                    norm["species"],
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
                    str(
                        item.get("created_at")
                        or datetime.utcnow().isoformat(timespec="seconds")
                    ),
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
                        str(
                            loss_item.get("created_at")
                            or datetime.utcnow().isoformat(timespec="seconds")
                        ),
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
                        str(
                            harv_item.get("created_at")
                            or datetime.utcnow().isoformat(timespec="seconds")
                        ),
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
                        str(
                            sale_item.get("created_at")
                            or datetime.utcnow().isoformat(timespec="seconds")
                        ),
                    ),
                )
            count += 1
        conn.commit()
    return count


# ---------------------------------------------------------------------------
# Calendar, Phased Lifecycle, Loss Adjustment & Budget/ROI Engine
# ---------------------------------------------------------------------------
def create_gcal_link(
    title: str, start_date: date, end_date_inclusive: date, description: str
) -> str:
    """Create a Google Calendar all-day template link (end date is exclusive in GCal)."""
    start_fmt = start_date.strftime("%Y%m%d")
    end_exclusive = end_date_inclusive + timedelta(days=1)
    end_fmt = end_exclusive.strftime("%Y%m%d")

    base_url = "https://www.google.com/calendar/render?action=TEMPLATE"
    params = {
        "text": title,
        "dates": f"{start_fmt}/{end_fmt}",
        "details": description,
        "src": 11,
    }
    return base_url + "&" + urllib.parse.urlencode(params)


def compute_batch_schedule(
    batch: dict,
    barrel_capacity: int = DEFAULT_BARREL_CAPACITY,
    bag_weight: int = DEFAULT_BAG_WEIGHT,
    losses: list[dict] | None = None,
    harvests: list[dict] | None = None,
    sales: list[dict] | None = None,
) -> dict:
    start_dt = date.fromisoformat(batch["start_date"])
    end_dt = date.fromisoformat(batch["end_date"])
    if end_dt < start_dt:
        start_dt, end_dt = end_dt, start_dt

    species = batch["species"]
    defaults = get_species_defaults(species)
    is_pigs = "pigs" in species.lower()
    days_to_birth = int(batch["days_to_birth"])
    initial_quantity = max(1, int(batch["quantity"]))

    starter_weeks = int(
        batch["starter_weeks"]
        if batch.get("starter_weeks") is not None
        else defaults["starter_weeks"]
    )
    grower_weeks = int(
        batch["grower_weeks"]
        if batch.get("grower_weeks") is not None
        else defaults["grower_weeks"]
    )
    if starter_weeks + grower_weeks < 1:
        grower_weeks = max(1, int(batch.get("weeks_to_proc", 8)))
    weeks_to_proc = max(1, starter_weeks + grower_weeks)

    starter_feed_per_animal = float(
        batch["starter_feed_lbs"]
        if batch.get("starter_feed_lbs") is not None
        else defaults["starter_feed_lbs"]
    )
    grower_feed_per_animal = float(
        batch["grower_feed_lbs"]
        if batch.get("grower_feed_lbs") is not None
        else defaults["grower_feed_lbs"]
    )
    starter_bag_price = float(
        batch["starter_bag_price"]
        if batch.get("starter_bag_price") is not None
        else defaults["starter_bag_price"]
    )
    grower_bag_price = float(
        batch["grower_bag_price"]
        if batch.get("grower_bag_price") is not None
        else defaults["grower_bag_price"]
    )
    unit_cost = float(
        batch["unit_cost"]
        if batch.get("unit_cost") is not None
        else defaults["unit_cost"]
    )
    processing_cost = float(
        batch["processing_cost"]
        if batch.get("processing_cost") is not None
        else defaults["processing_cost"]
    )
    expected_yield_per_head = float(
        batch["expected_yield_lbs"]
        if batch.get("expected_yield_lbs") is not None
        else defaults["expected_yield_lbs"]
    )
    sale_price_per_lb = float(
        batch["sale_price_per_lb"]
        if batch.get("sale_price_per_lb") is not None
        else defaults["sale_price_per_lb"]
    )

    batch_id = int(batch["id"])
    if losses is None:
        losses = list_batch_losses(batch_id)
    if harvests is None:
        harvests = list_batch_harvests(batch_id)
    if sales is None:
        sales = list_batch_sales(batch_id)

    parsed_losses = []
    for l in losses:
        try:
            l_dt = date.fromisoformat(str(l["loss_date"]))
        except ValueError:
            l_dt = start_dt
        parsed_losses.append(
            {
                "id": l.get("id", 0),
                "batch_id": batch_id,
                "loss_date": l_dt,
                "loss_date_str": l_dt.isoformat(),
                "count": max(1, int(l.get("count", 1))),
                "reason": str(l.get("reason", "")),
            }
        )
    parsed_losses.sort(key=lambda x: (x["loss_date"], x["id"]))

    parsed_harvests = []
    for h in harvests:
        try:
            h_dt = date.fromisoformat(str(h["harvest_date"]))
        except ValueError:
            h_dt = end_dt
        parsed_harvests.append(
            {
                "id": h.get("id", 0),
                "batch_id": batch_id,
                "harvest_date": h_dt,
                "harvest_date_str": h_dt.isoformat(),
                "head_count": max(1, int(h.get("head_count", 1))),
                "dressed_lbs": max(0.0, float(h.get("dressed_lbs", 0.0))),
                "processing_cost_total": max(
                    0.0, float(h.get("processing_cost_total", 0.0))
                ),
                "notes": str(h.get("notes", "")),
            }
        )
    parsed_harvests.sort(key=lambda x: (x["harvest_date"], x["id"]))

    parsed_sales = []
    for sl in sales:
        try:
            s_dt = date.fromisoformat(str(sl["entry_date"]))
        except ValueError:
            s_dt = end_dt
        parsed_sales.append(
            {
                "id": sl.get("id", 0),
                "batch_id": batch_id,
                "entry_date": s_dt,
                "entry_date_str": s_dt.isoformat(),
                "allocation_type": str(
                    sl.get("allocation_type", "Customer Sale")
                ),
                "weight_lbs": max(0.0, float(sl.get("weight_lbs", 0.0))),
                "revenue": max(0.0, float(sl.get("revenue", 0.0))),
                "notes": str(sl.get("notes", "")),
            }
        )
    parsed_sales.sort(key=lambda x: (x["entry_date"], x["id"]))

    def surviving_on_date(d: date) -> int:
        lost_so_far = sum(
            item["count"] for item in parsed_losses if item["loss_date"] <= d
        )
        return max(0, initial_quantity - lost_so_far)

    def feeding_on_date(d: date) -> int:
        """Animals still on feed on date `d` (subtracts losses up to `d` and harvests strictly before `d`)."""
        lost_so_far = sum(
            item["count"] for item in parsed_losses if item["loss_date"] <= d
        )
        harvested_before = sum(
            item["head_count"]
            for item in parsed_harvests
            if item["harvest_date"] < d
        )
        return max(0, initial_quantity - lost_so_far - harvested_before)

    total_lost = min(
        initial_quantity, sum(item["count"] for item in parsed_losses)
    )
    surviving_quantity = max(0, initial_quantity - total_lost)

    initial_label = "Breeding" if is_pigs else "Arrival"
    gestation_label = "Gestation" if is_pigs else "Incubation"
    birth_label = "Birth" if is_pigs else "Hatch"

    timeline_items = []

    # Phase 1: Breeding or Arrival
    if start_dt == end_dt:
        timeline_items.append(
            {
                "id": f"b{batch_id}_init",
                "group": batch_id,
                "content": f"📥 {initial_label}",
                "start": start_dt.isoformat(),
                "end": (end_dt + timedelta(days=1)).isoformat(),
                "style": "background-color: #dbeafe; border-color: #2563eb; color: #1e3a8a;",
                "title": f"{batch['name']} — {initial_label}: {start_dt.isoformat()}",
            }
        )
    else:
        timeline_items.append(
            {
                "id": f"b{batch_id}_init",
                "group": batch_id,
                "content": f"📥 {initial_label}",
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "style": "background-color: #dbeafe; border-color: #2563eb; color: #1e3a8a;",
                "title": f"{batch['name']} — {initial_label}: {start_dt.isoformat()} to {end_dt.isoformat()}",
            }
        )

    # Phase 2: Gestation / Incubation (if applicable)
    if days_to_birth > 0:
        birth_start = start_dt + timedelta(days=days_to_birth)
        birth_end = end_dt + timedelta(days=days_to_birth)
        timeline_items.append(
            {
                "id": f"b{batch_id}_gest",
                "group": batch_id,
                "content": f"⏳ {gestation_label}",
                "start": end_dt.isoformat(),
                "end": birth_end.isoformat(),
                "style": "background-color: #fef3c7; border-color: #d97706; color: #78350f;",
                "title": (
                    f"{batch['name']} — {gestation_label} ({days_to_birth}d) | "
                    f"{birth_label} Window: {birth_start.isoformat()} to {birth_end.isoformat()}"
                ),
            }
        )
        proc_base = birth_end
        proc_window_start = birth_start + timedelta(weeks=weeks_to_proc)
    else:
        birth_start = None
        birth_end = None
        proc_base = start_dt
        proc_window_start = start_dt + timedelta(weeks=weeks_to_proc)

    transition_date = proc_base + timedelta(weeks=starter_weeks)
    proc_date = transition_date + timedelta(weeks=grower_weeks)

    # Day-by-day feed consumption adjusted for losses AND early/split harvests
    starter_days = max(0, (transition_date - proc_base).days)
    daily_starter_per_head = (
        starter_feed_per_animal / starter_days if starter_days > 0 else 0.0
    )
    starter_feed_actual = 0.0
    starter_feed_loss_only = 0.0
    for day_offset in range(starter_days):
        d = proc_base + timedelta(days=day_offset)
        starter_feed_actual += feeding_on_date(d) * daily_starter_per_head
        starter_feed_loss_only += surviving_on_date(d) * daily_starter_per_head
    starter_feed_actual = round(starter_feed_actual, 2)
    starter_feed_loss_only = round(starter_feed_loss_only, 2)
    starter_feed_planned = round(
        initial_quantity * starter_feed_per_animal, 2
    )

    grower_days = max(0, (proc_date - transition_date).days)
    daily_grower_per_head = (
        grower_feed_per_animal / grower_days if grower_days > 0 else 0.0
    )
    grower_feed_actual = 0.0
    grower_feed_loss_only = 0.0
    for day_offset in range(grower_days):
        d = transition_date + timedelta(days=day_offset)
        grower_feed_actual += feeding_on_date(d) * daily_grower_per_head
        grower_feed_loss_only += surviving_on_date(d) * daily_grower_per_head
    grower_feed_actual = round(grower_feed_actual, 2)
    grower_feed_loss_only = round(grower_feed_loss_only, 2)
    grower_feed_planned = round(initial_quantity * grower_feed_per_animal, 2)

    total_feed_planned = round(starter_feed_planned + grower_feed_planned, 2)
    total_feed_loss_only = round(
        starter_feed_loss_only + grower_feed_loss_only, 2
    )
    total_feed = round(starter_feed_actual + grower_feed_actual, 2)
    feed_saved_by_losses = round(
        max(0.0, total_feed_planned - total_feed_loss_only), 2
    )
    feed_saved_by_early_harvest = round(
        max(0.0, total_feed_loss_only - total_feed), 2
    )

    starter_weekly_feed = (
        starter_feed_actual / starter_weeks if starter_weeks > 0 else 0.0
    )
    grower_weekly_feed = (
        grower_feed_actual / grower_weeks if grower_weeks > 0 else 0.0
    )
    weekly_feed = total_feed / weeks_to_proc

    barrels_needed = math.ceil(total_feed / max(1, barrel_capacity))
    safe_bag_wt = max(1, bag_weight)
    starter_bags = (
        math.ceil(starter_feed_actual / safe_bag_wt)
        if starter_feed_actual > 0
        else 0
    )
    grower_bags = (
        math.ceil(grower_feed_actual / safe_bag_wt)
        if grower_feed_actual > 0
        else 0
    )
    total_bags = starter_bags + grower_bags

    # Budget & ROI Calculations (Estimated / Planned Baseline)
    starter_cost_bags = round(starter_bags * starter_bag_price, 2)
    grower_cost_bags = round(grower_bags * grower_bag_price, 2)
    total_feed_cost_bags = round(starter_cost_bags + grower_cost_bags, 2)

    starter_cost_exact = round(
        (starter_feed_actual / safe_bag_wt) * starter_bag_price, 2
    )
    grower_cost_exact = round(
        (grower_feed_actual / safe_bag_wt) * grower_bag_price, 2
    )
    total_feed_cost_exact = round(starter_cost_exact + grower_cost_exact, 2)

    acquisition_cost = round(initial_quantity * unit_cost, 2)
    total_processing_cost = round(surviving_quantity * processing_cost, 2)
    total_budget_cost = round(
        acquisition_cost + total_feed_cost_bags + total_processing_cost, 2
    )

    expected_meat_lbs = round(surviving_quantity * expected_yield_per_head, 2)
    feed_cost_per_lb_meat = (
        round(total_feed_cost_bags / expected_meat_lbs, 2)
        if expected_meat_lbs > 0
        else 0.0
    )
    total_cost_per_lb_meat = (
        round(total_budget_cost / expected_meat_lbs, 2)
        if expected_meat_lbs > 0
        else 0.0
    )
    estimated_revenue = round(expected_meat_lbs * sale_price_per_lb, 2)
    estimated_profit = round(estimated_revenue - total_budget_cost, 2)
    profit_per_lb_meat = (
        round(sale_price_per_lb - total_cost_per_lb_meat, 2)
        if expected_meat_lbs > 0
        else 0.0
    )
    roi_margin_pct = (
        round((estimated_profit / total_budget_cost) * 100.0, 1)
        if total_budget_cost > 0
        else 0.0
    )

    # -----------------------------------------------------------------------
    # Actual Harvest, Feed Override & Sales / Home Freezer Reconciliation
    # -----------------------------------------------------------------------
    harvested_head = min(
        surviving_quantity,
        sum(item["head_count"] for item in parsed_harvests),
    )
    unharvested_surviving_head = max(0, surviving_quantity - harvested_head)
    is_fully_harvested = (
        surviving_quantity > 0 and unharvested_surviving_head == 0
    )

    actual_dressed_lbs = round(
        sum(item["dressed_lbs"] for item in parsed_harvests), 2
    )
    actual_avg_yield_per_head = (
        round(actual_dressed_lbs / harvested_head, 2)
        if harvested_head > 0
        else 0.0
    )
    actual_harvest_proc_cost = round(
        sum(item["processing_cost_total"] for item in parsed_harvests), 2
    )

    actual_feed_lbs_override = (
        float(batch["actual_feed_lbs"])
        if batch.get("actual_feed_lbs") is not None
        else None
    )
    actual_feed_cost_override = (
        float(batch["actual_feed_cost"])
        if batch.get("actual_feed_cost") is not None
        else None
    )
    reconciled_feed_lbs = (
        round(actual_feed_lbs_override, 2)
        if actual_feed_lbs_override is not None
        else total_feed
    )
    reconciled_feed_cost = (
        round(actual_feed_cost_override, 2)
        if actual_feed_cost_override is not None
        else total_feed_cost_bags
    )

    # Processing cost uses logged actual harvest processing fees + estimated fee for any remaining unharvested head
    reconciled_processing_cost = (
        round(
            actual_harvest_proc_cost
            + (unharvested_surviving_head * processing_cost),
            2,
        )
        if parsed_harvests
        else total_processing_cost
    )
    reconciled_total_cost = round(
        acquisition_cost + reconciled_feed_cost + reconciled_processing_cost, 2
    )

    # Total meat yield blends actual harvested dressed lbs + expected yield for any remaining unharvested animals
    reconciled_meat_lbs = (
        round(
            actual_dressed_lbs
            + (unharvested_surviving_head * expected_yield_per_head),
            2,
        )
        if parsed_harvests
        else expected_meat_lbs
    )
    reconciled_cost_per_lb_meat = (
        round(reconciled_total_cost / reconciled_meat_lbs, 2)
        if reconciled_meat_lbs > 0
        else 0.0
    )

    sold_lbs = round(
        sum(
            item["weight_lbs"]
            for item in parsed_sales
            if item["allocation_type"] == "Customer Sale"
        ),
        2,
    )
    actual_sales_revenue = round(
        sum(
            item["revenue"]
            for item in parsed_sales
            if item["allocation_type"] == "Customer Sale"
        ),
        2,
    )
    freezer_lbs = round(
        sum(
            item["weight_lbs"]
            for item in parsed_sales
            if item["allocation_type"] == "Home Freezer"
        ),
        2,
    )
    allocated_lbs = round(sold_lbs + freezer_lbs, 2)
    unallocated_harvested_lbs = round(
        max(0.0, actual_dressed_lbs - allocated_lbs), 2
    )

    imputed_freezer_value = round(freezer_lbs * sale_price_per_lb, 2)
    actual_cash_profit = round(actual_sales_revenue - reconciled_total_cost, 2)
    total_realized_value = round(
        actual_sales_revenue + imputed_freezer_value, 2
    )
    total_realized_profit = round(
        total_realized_value - reconciled_total_cost, 2
    )

    # Effective Cost per lb of Home Freezer Meat after offsetting batch costs with customer sales revenue
    net_cost_for_freezer = max(0.0, reconciled_total_cost - actual_sales_revenue)
    effective_freezer_cost_per_lb = (
        round(net_cost_for_freezer / freezer_lbs, 2)
        if freezer_lbs > 0
        else None
    )

    # Phase 3a: Starter Timeline Item
    if starter_weeks > 0:
        timeline_items.append(
            {
                "id": f"b{batch_id}_starter",
                "group": batch_id,
                "content": f"🌱 Starter ({starter_weekly_feed:.1f} lbs/wk)",
                "start": proc_base.isoformat(),
                "end": transition_date.isoformat(),
                "style": "background-color: #ecfccb; border-color: #65a30d; color: #365314;",
                "title": (
                    f"{batch['name']} — Starter Phase ({starter_weeks} wks): "
                    f"{proc_base.isoformat()} to {transition_date.isoformat()} | "
                    f"Feed: {starter_feed_actual:,.1f} lbs ({starter_bags} bags @ ${starter_bag_price:.2f})"
                ),
            }
        )

    # Phase 3b: Grower/Finisher Timeline Item
    if grower_weeks > 0:
        timeline_items.append(
            {
                "id": f"b{batch_id}_grow",
                "group": batch_id,
                "content": f"🌾 Grower/Finisher ({grower_weekly_feed:.1f} lbs/wk)",
                "start": transition_date.isoformat(),
                "end": proc_date.isoformat(),
                "style": "background-color: #dcfce7; border-color: #16a34a; color: #14532d;",
                "title": (
                    f"{batch['name']} — Grower/Finisher Phase ({grower_weeks} wks): "
                    f"{transition_date.isoformat()} to {proc_date.isoformat()} | "
                    f"Feed: {grower_feed_actual:,.1f} lbs ({grower_bags} bags @ ${grower_bag_price:.2f})"
                ),
            }
        )

    # Phase 4: Planned Processing Timeline Item (if unharvested animals remain or no harvest logged yet)
    if unharvested_surviving_head > 0 or not parsed_harvests:
        timeline_items.append(
            {
                "id": f"b{batch_id}_proc",
                "group": batch_id,
                "content": f"🔪 Target Processing ({unharvested_surviving_head} head)",
                "start": proc_date.isoformat(),
                "style": "background-color: #fee2e2; border-color: #dc2626; color: #7f1d1d;",
                "title": (
                    f"{batch['name']} — Planned Processing Date: {proc_date.isoformat()} | "
                    f"Remaining to harvest: {unharvested_surviving_head}/{surviving_quantity} head"
                ),
            }
        )

    # Actual Harvest Markers on Timeline Swimlane
    for h_item in parsed_harvests:
        note_suffix = f" ({h_item['notes']})" if h_item["notes"] else ""
        timeline_items.append(
            {
                "id": f"b{batch_id}_harvest_{h_item['id']}",
                "group": batch_id,
                "content": f"🥩 Harvested {h_item['head_count']} ({h_item['dressed_lbs']:,.1f} lbs)",
                "start": h_item["harvest_date_str"],
                "style": "background-color: #d1fae5; border-color: #059669; color: #065f46; font-weight: 600;",
                "title": (
                    f"{batch['name']} — Actual Harvest on {h_item['harvest_date_str']}: "
                    f"{h_item['head_count']} head → {h_item['dressed_lbs']:,.1f} lbs dressed "
                    f"(Processing fee: ${h_item['processing_cost_total']:,.2f}){note_suffix}"
                ),
            }
        )

    # Loss Markers on Timeline Swimlane
    for l_item in parsed_losses:
        reason_suffix = f" ({l_item['reason']})" if l_item["reason"] else ""
        timeline_items.append(
            {
                "id": f"b{batch_id}_loss_{l_item['id']}",
                "group": batch_id,
                "content": f"⚠️ -{l_item['count']} Lost",
                "start": l_item["loss_date_str"],
                "style": "background-color: #fef2f2; border-color: #ef4444; color: #991b1b; font-weight: 600;",
                "title": (
                    f"{batch['name']} — Loss on {l_item['loss_date_str']}: "
                    f"-{l_item['count']} head{reason_suffix}"
                ),
            }
        )

    return {
        "batch": batch,
        "losses": parsed_losses,
        "harvests": parsed_harvests,
        "sales": parsed_sales,
        "initial_quantity": initial_quantity,
        "total_lost": total_lost,
        "surviving_quantity": surviving_quantity,
        "harvested_head": harvested_head,
        "unharvested_surviving_head": unharvested_surviving_head,
        "is_fully_harvested": is_fully_harvested,
        "surviving_on_date": surviving_on_date,
        "feeding_on_date": feeding_on_date,
        "start_date": start_dt,
        "end_date": end_dt,
        "initial_label": initial_label,
        "gestation_label": gestation_label,
        "birth_label": birth_label,
        "birth_start": birth_start,
        "birth_end": birth_end,
        "proc_base": proc_base,
        "transition_date": transition_date,
        "proc_window_start": proc_window_start,
        "proc_date": proc_date,
        "starter_weeks": starter_weeks,
        "grower_weeks": grower_weeks,
        "weeks_to_proc": weeks_to_proc,
        "starter_feed_per_animal": starter_feed_per_animal,
        "grower_feed_per_animal": grower_feed_per_animal,
        "daily_starter_per_head": daily_starter_per_head,
        "daily_grower_per_head": daily_grower_per_head,
        "starter_feed_planned": starter_feed_planned,
        "grower_feed_planned": grower_feed_planned,
        "total_feed_planned": total_feed_planned,
        "starter_feed": starter_feed_actual,
        "grower_feed": grower_feed_actual,
        "total_feed": total_feed,
        "feed_saved_by_losses": feed_saved_by_losses,
        "feed_saved_by_early_harvest": feed_saved_by_early_harvest,
        "starter_weekly_feed": starter_weekly_feed,
        "grower_weekly_feed": grower_weekly_feed,
        "weekly_feed": weekly_feed,
        "barrels_needed": barrels_needed,
        "starter_bags": starter_bags,
        "grower_bags": grower_bags,
        "total_bags": total_bags,
        "starter_bag_price": starter_bag_price,
        "grower_bag_price": grower_bag_price,
        "starter_cost_bags": starter_cost_bags,
        "grower_cost_bags": grower_cost_bags,
        "total_feed_cost_bags": total_feed_cost_bags,
        "total_feed_cost_exact": total_feed_cost_exact,
        "unit_cost": unit_cost,
        "acquisition_cost": acquisition_cost,
        "processing_cost": processing_cost,
        "total_processing_cost": total_processing_cost,
        "total_budget_cost": total_budget_cost,
        "expected_yield_per_head": expected_yield_per_head,
        "expected_meat_lbs": expected_meat_lbs,
        "feed_cost_per_lb_meat": feed_cost_per_lb_meat,
        "total_cost_per_lb_meat": total_cost_per_lb_meat,
        "sale_price_per_lb": sale_price_per_lb,
        "estimated_revenue": estimated_revenue,
        "estimated_profit": estimated_profit,
        "profit_per_lb_meat": profit_per_lb_meat,
        "roi_margin_pct": roi_margin_pct,
        "actual_dressed_lbs": actual_dressed_lbs,
        "actual_avg_yield_per_head": actual_avg_yield_per_head,
        "actual_harvest_proc_cost": actual_harvest_proc_cost,
        "actual_feed_lbs_override": actual_feed_lbs_override,
        "actual_feed_cost_override": actual_feed_cost_override,
        "reconciled_feed_lbs": reconciled_feed_lbs,
        "reconciled_feed_cost": reconciled_feed_cost,
        "reconciled_processing_cost": reconciled_processing_cost,
        "reconciled_total_cost": reconciled_total_cost,
        "reconciled_meat_lbs": reconciled_meat_lbs,
        "reconciled_cost_per_lb_meat": reconciled_cost_per_lb_meat,
        "sold_lbs": sold_lbs,
        "actual_sales_revenue": actual_sales_revenue,
        "freezer_lbs": freezer_lbs,
        "allocated_lbs": allocated_lbs,
        "unallocated_harvested_lbs": unallocated_harvested_lbs,
        "imputed_freezer_value": imputed_freezer_value,
        "actual_cash_profit": actual_cash_profit,
        "total_realized_value": total_realized_value,
        "total_realized_profit": total_realized_profit,
        "net_cost_for_freezer": net_cost_for_freezer,
        "effective_freezer_cost_per_lb": effective_freezer_cost_per_lb,
        "timeline_items": timeline_items,
    }


def _batch_feed_on_day(s: dict, d: date) -> tuple[float, float]:
    """Return (starter_lbs, grower_lbs) consumed by schedule `s` on day `d`, adjusted for losses and early harvests."""
    head_fn = s.get("feeding_on_date", s["surviving_on_date"])
    if s["proc_base"] <= d < s["transition_date"] and s["starter_weeks"] > 0:
        return head_fn(d) * s["daily_starter_per_head"], 0.0
    if s["transition_date"] <= d < s["proc_date"] and s["grower_weeks"] > 0:
        return 0.0, head_fn(d) * s["daily_grower_per_head"]
    return 0.0, 0.0


def compute_combined_feed_schedule(
    schedules: list[dict], barrel_capacity: int = DEFAULT_BARREL_CAPACITY
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """
    Build weekly and monthly time-phased feed demand across active batches,
    accounting for Starter vs. Grower/Finisher phases and dated batch losses.
    """
    if not schedules:
        empty_df = pd.DataFrame()
        return (
            empty_df,
            empty_df,
            {
                "peak_weekly_lbs": 0.0,
                "peak_concurrent_lbs": 0.0,
                "peak_concurrent_barrels": 0,
            },
        )

    min_grow_start = min(s["proc_base"] for s in schedules)
    max_grow_end = max(s["proc_date"] for s in schedules)

    # Precompute daily feed per schedule so remaining active feed is fast and exact
    daily_maps: dict[int, dict[date, tuple[float, float]]] = {}
    for s in schedules:
        bid = int(s["batch"]["id"])
        d_map: dict[date, tuple[float, float]] = {}
        curr_d = s["proc_base"]
        while curr_d < s["proc_date"]:
            d_map[curr_d] = _batch_feed_on_day(s, curr_d)
            curr_d += timedelta(days=1)
        daily_maps[bid] = d_map

    first_monday = min_grow_start - timedelta(days=min_grow_start.weekday())
    records = []

    curr_week_start = first_monday
    while curr_week_start < max_grow_end:
        curr_week_end = curr_week_start + timedelta(days=7)
        row: dict[str, float | int | str] = {
            "Week Starting": curr_week_start.isoformat(),
            "Starter Feed (lbs)": 0.0,
            "Grower/Finisher Feed (lbs)": 0.0,
            "Total Weekly Feed (lbs)": 0.0,
            "Remaining Active Batch Feed (lbs)": 0.0,
        }

        for s in schedules:
            bid = int(s["batch"]["id"])
            b_name = f"{s['batch']['name']} (#{bid})"
            d_map = daily_maps[bid]

            st_week = 0.0
            gr_week = 0.0
            for day_offset in range(7):
                d = curr_week_start + timedelta(days=day_offset)
                if d in d_map:
                    st_d, gr_d = d_map[d]
                    st_week += st_d
                    gr_week += gr_d

            batch_week_total = st_week + gr_week
            row[b_name] = round(batch_week_total, 2)
            row["Starter Feed (lbs)"] = float(row["Starter Feed (lbs)"]) + st_week
            row["Grower/Finisher Feed (lbs)"] = (
                float(row["Grower/Finisher Feed (lbs)"]) + gr_week
            )
            row["Total Weekly Feed (lbs)"] = (
                float(row["Total Weekly Feed (lbs)"]) + batch_week_total
            )

            # If this batch is actively growing during this week, sum remaining feed from overlap_start to proc_date
            overlap_start = max(curr_week_start, s["proc_base"])
            overlap_end = min(curr_week_end, s["proc_date"])
            if overlap_start < overlap_end:
                rem_feed = sum(
                    st_d + gr_d
                    for d_key, (st_d, gr_d) in d_map.items()
                    if d_key >= overlap_start
                )
                row["Remaining Active Batch Feed (lbs)"] = (
                    float(row["Remaining Active Batch Feed (lbs)"]) + rem_feed
                )

        row["Starter Feed (lbs)"] = round(float(row["Starter Feed (lbs)"]), 2)
        row["Grower/Finisher Feed (lbs)"] = round(
            float(row["Grower/Finisher Feed (lbs)"]), 2
        )
        row["Total Weekly Feed (lbs)"] = round(
            float(row["Total Weekly Feed (lbs)"]), 2
        )
        row["Remaining Active Batch Feed (lbs)"] = round(
            float(row["Remaining Active Batch Feed (lbs)"]), 2
        )
        row["Concurrent Barrels Needed (Remaining Active)"] = math.ceil(
            float(row["Remaining Active Batch Feed (lbs)"])
            / max(1, barrel_capacity)
        )
        records.append(row)
        curr_week_start = curr_week_end

    weekly_df = pd.DataFrame(records)
    if weekly_df.empty:
        return (
            weekly_df,
            pd.DataFrame(),
            {
                "peak_weekly_lbs": 0.0,
                "peak_concurrent_lbs": 0.0,
                "peak_concurrent_barrels": 0,
            },
        )

    monthly_df = weekly_df.copy()
    monthly_df["Month"] = monthly_df["Week Starting"].str.slice(0, 7)
    monthly_summary = (
        monthly_df.groupby("Month", as_index=False)
        .agg(
            Starter_Feed_lbs=("Starter Feed (lbs)", "sum"),
            Grower_Feed_lbs=("Grower/Finisher Feed (lbs)", "sum"),
            Monthly_Feed_lbs=("Total Weekly Feed (lbs)", "sum"),
            Peak_Concurrent_Barrels=(
                "Concurrent Barrels Needed (Remaining Active)",
                "max",
            ),
        )
        .rename(
            columns={
                "Starter_Feed_lbs": "Starter Feed (lbs)",
                "Grower_Feed_lbs": "Grower/Finisher Feed (lbs)",
                "Monthly_Feed_lbs": "Monthly Feed Consumed (lbs)",
                "Peak_Concurrent_Barrels": "Peak Concurrent Barrels in Month",
            }
        )
    )
    for col in [
        "Starter Feed (lbs)",
        "Grower/Finisher Feed (lbs)",
        "Monthly Feed Consumed (lbs)",
    ]:
        monthly_summary[col] = monthly_summary[col].round(1)
    monthly_summary["Barrels Consumed in Month"] = monthly_summary[
        "Monthly Feed Consumed (lbs)"
    ].apply(lambda lbs: round(lbs / max(1, barrel_capacity), 2))

    stats = {
        "peak_weekly_lbs": float(weekly_df["Total Weekly Feed (lbs)"].max()),
        "peak_concurrent_lbs": float(
            weekly_df["Remaining Active Batch Feed (lbs)"].max()
        ),
        "peak_concurrent_barrels": int(
            weekly_df["Concurrent Barrels Needed (Remaining Active)"].max()
        ),
    }
    return weekly_df, monthly_summary, stats


def compute_purchasing_schedule(
    schedules: list[dict], bag_weight: int = DEFAULT_BAG_WEIGHT
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Build an itemized chronological Purchasing & Cash-Flow Schedule and a Monthly Cash-Flow Summary
    showing when to buy animals, Starter feed bags, Grower/Finisher feed bags, and pay processing fees.
    """
    if not schedules:
        return pd.DataFrame(), pd.DataFrame()

    events = []
    for s in schedules:
        b = s["batch"]
        b_label = f"{b['name']} (#{b['id']})"

        if s["acquisition_cost"] > 0:
            events.append(
                {
                    "Date": s["start_date"].isoformat(),
                    "Month": s["start_date"].isoformat()[:7],
                    "Batch": b_label,
                    "Category": "🐣 Animal / Egg Acquisition",
                    "Details": f"{s['initial_quantity']} × {b['species'].title()} @ ${s['unit_cost']:,.2f}",
                    "Feed Bags": 0,
                    "Feed (lbs)": 0.0,
                    "Cost ($)": round(s["acquisition_cost"], 2),
                }
            )

        if s["starter_bags"] > 0:
            events.append(
                {
                    "Date": s["proc_base"].isoformat(),
                    "Month": s["proc_base"].isoformat()[:7],
                    "Batch": b_label,
                    "Category": "🌱 Starter Feed Purchase",
                    "Details": (
                        f"{s['starter_bags']} × {bag_weight}lb bags "
                        f"({s['starter_feed']:,.1f} lbs needed) @ ${s['starter_bag_price']:,.2f}/bag"
                    ),
                    "Feed Bags": int(s["starter_bags"]),
                    "Feed (lbs)": round(s["starter_feed"], 1),
                    "Cost ($)": round(s["starter_cost_bags"], 2),
                }
            )

        if s["grower_bags"] > 0:
            events.append(
                {
                    "Date": s["transition_date"].isoformat(),
                    "Month": s["transition_date"].isoformat()[:7],
                    "Batch": b_label,
                    "Category": "🌾 Grower/Finisher Feed Purchase",
                    "Details": (
                        f"{s['grower_bags']} × {bag_weight}lb bags "
                        f"({s['grower_feed']:,.1f} lbs needed) @ ${s['grower_bag_price']:,.2f}/bag"
                    ),
                    "Feed Bags": int(s["grower_bags"]),
                    "Feed (lbs)": round(s["grower_feed"], 1),
                    "Cost ($)": round(s["grower_cost_bags"], 2),
                }
            )

        if s["total_processing_cost"] > 0:
            events.append(
                {
                    "Date": s["proc_date"].isoformat(),
                    "Month": s["proc_date"].isoformat()[:7],
                    "Batch": b_label,
                    "Category": "🔪 Processing / Butcher Fee",
                    "Details": (
                        f"{s['surviving_quantity']} harvested head @ ${s['processing_cost']:,.2f}/head"
                    ),
                    "Feed Bags": 0,
                    "Feed (lbs)": 0.0,
                    "Cost ($)": round(s["total_processing_cost"], 2),
                }
            )

    events_df = pd.DataFrame(events)
    if events_df.empty:
        return events_df, pd.DataFrame()

    events_df = events_df.sort_values(by=["Date", "Batch", "Category"]).reset_index(
        drop=True
    )

    monthly_cash_df = (
        events_df.groupby("Month", as_index=False)
        .agg(
            Total_Bags=("Feed Bags", "sum"),
            Total_Feed_lbs=("Feed (lbs)", "sum"),
            Total_Cost=("Cost ($)", "sum"),
        )
        .rename(
            columns={
                "Total_Bags": f"Feed Bags Purchased ({bag_weight}lb)",
                "Total_Feed_lbs": "Feed Purchased (lbs)",
                "Total_Cost": "Total Monthly Outlay ($)",
            }
        )
    )
    monthly_cash_df["Total Monthly Outlay ($)"] = monthly_cash_df[
        "Total Monthly Outlay ($)"
    ].round(2)
    return events_df, monthly_cash_df


def compute_roi_summary_table(schedules: list[dict]) -> pd.DataFrame:
    if not schedules:
        return pd.DataFrame()

    rows = []
    for s in schedules:
        b = s["batch"]
        rows.append(
            {
                "Batch": f"{b['name']} (#{b['id']})",
                "Species": b["species"].title(),
                "Head (Surviving / Initial)": f"{s['surviving_quantity']} / {s['initial_quantity']}",
                "Starter Feed": f"{s['starter_feed']:,.1f} lbs ({s['starter_bags']} bags)",
                "Grower Feed": f"{s['grower_feed']:,.1f} lbs ({s['grower_bags']} bags)",
                "Feed Saved (Losses)": f"{s['feed_saved_by_losses']:,.1f} lbs",
                "Acquisition ($)": round(s["acquisition_cost"], 2),
                "Feed Cost - Bags ($)": round(s["total_feed_cost_bags"], 2),
                "Feed Cost - Exact ($)": round(s["total_feed_cost_exact"], 2),
                "Processing ($)": round(s["total_processing_cost"], 2),
                "Total Cost ($)": round(s["total_budget_cost"], 2),
                "Expected Meat (lbs)": round(s["expected_meat_lbs"], 1),
                "Feed $/lb Meat": round(s["feed_cost_per_lb_meat"], 2),
                "Total $/lb Meat": round(s["total_cost_per_lb_meat"], 2),
                "Sale $/lb": round(s["sale_price_per_lb"], 2),
                "Est. Revenue ($)": round(s["estimated_revenue"], 2),
                "Est. Profit ($)": round(s["estimated_profit"], 2),
                "Profit $/lb": round(s["profit_per_lb_meat"], 2),
                "ROI (%)": f"{s['roi_margin_pct']:.1f}%",
            }
        )
    return pd.DataFrame(rows)


def compute_reconciliation_table(schedules: list[dict]) -> pd.DataFrame:
    """Build an Estimated vs. Actual Harvest, Home Freezer & Sales Reconciliation summary table."""
    if not schedules:
        return pd.DataFrame()

    rows = []
    for s in schedules:
        b = s["batch"]
        if s["effective_freezer_cost_per_lb"] is None:
            freezer_cost_str = "—"
        elif s["effective_freezer_cost_per_lb"] == 0.0:
            freezer_cost_str = "🎉 $0.00/lb (Free Meat!)"
        else:
            freezer_cost_str = f"${s['effective_freezer_cost_per_lb']:,.2f}/lb"

        rows.append(
            {
                "Batch": f"{b['name']} (#{b['id']})",
                "Harvested Head": f"{s['harvested_head']} / {s['surviving_quantity']}",
                "Est. Meat (lbs)": round(s["expected_meat_lbs"], 1),
                "Actual Harvested (lbs)": round(s["actual_dressed_lbs"], 1),
                "Est. Cost ($)": round(s["total_budget_cost"], 2),
                "Reconciled Cost ($)": round(s["reconciled_total_cost"], 2),
                "Reconciled $/lb": round(s["reconciled_cost_per_lb_meat"], 2),
                "Sold (lbs)": round(s["sold_lbs"], 1),
                "Cash Sales ($)": round(s["actual_sales_revenue"], 2),
                "Home Freezer (lbs)": round(s["freezer_lbs"], 1),
                "Unallocated (lbs)": round(s["unallocated_harvested_lbs"], 1),
                "Effective Freezer $/lb": freezer_cost_str,
                "Cash Net ($)": round(s["actual_cash_profit"], 2),
                "Total Realized Profit ($)": round(s["total_realized_profit"], 2),
            }
        )
    return pd.DataFrame(rows)


def generate_ics_calendar(schedules: list[dict]) -> str:
    """Generate an RFC 5545 .ics calendar file for all active livestock batches."""
    dtstamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Homestead Planner//Poultry & Pigs//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]

    def add_event(
        uid_seed: str, summary: str, start_d: date, end_d_inc: date, desc: str
    ) -> None:
        uid = hashlib.md5(uid_seed.encode("utf-8")).hexdigest() + "@homesteadplanner"
        dtstart = start_d.strftime("%Y%m%d")
        dtend = (end_d_inc + timedelta(days=1)).strftime("%Y%m%d")
        clean_desc = desc.replace("\n", "\\n")
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{dtstamp}",
                f"DTSTART;VALUE=DATE:{dtstart}",
                f"DTEND;VALUE=DATE:{dtend}",
                f"SUMMARY:{summary}",
                f"DESCRIPTION:{clean_desc}",
                "END:VEVENT",
            ]
        )

    for s in schedules:
        b = s["batch"]
        b_title = f"{b['name']} ({s['surviving_quantity']}/{s['initial_quantity']}x {b['species'].title()})"
        add_event(
            f"batch-{b['id']}-init-{s['start_date']}",
            f"🚜 {b['name']}: {s['initial_label']}",
            s["start_date"],
            s["end_date"],
            f"{s['initial_label']} window for {b_title}. Notes: {b.get('notes', '')}",
        )
        if s["birth_start"] and s["birth_end"]:
            add_event(
                f"batch-{b['id']}-birth-{s['birth_start']}",
                f"🐣 {b['name']}: {s['birth_label']} Window",
                s["birth_start"],
                s["birth_end"],
                f"Estimated {s['birth_label'].lower()} window for {b_title}.",
            )
        if s["starter_weeks"] > 0 and s["grower_weeks"] > 0:
            add_event(
                f"batch-{b['id']}-transition-{s['transition_date']}",
                f"🌾 {b['name']}: Switch to Grower/Finisher Feed",
                s["transition_date"],
                s["transition_date"],
                (
                    f"Transition {b_title} from Starter ration to Grower/Finisher ration. "
                    f"Grower feed needed: {s['grower_feed']:,.1f} lbs ({s['grower_bags']} bags)."
                ),
            )
        add_event(
            f"batch-{b['id']}-proc-{s['proc_date']}",
            f"🔪 {b['name']}: Processing Date",
            s["proc_date"],
            s["proc_date"],
            (
                f"Approximate processing date for {b_title}. "
                f"Total feed: {s['total_feed']:,.1f} lbs ({s['barrels_needed']} barrels). "
                f"Expected meat yield: {s['expected_meat_lbs']:,.1f} lbs (${s['total_cost_per_lb_meat']:,.2f}/lb)."
            ),
        )

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


# ---------------------------------------------------------------------------
# Streamlit UI Application & Workspace Helpers
# ---------------------------------------------------------------------------
WORKSPACES = [
    "🗓️ Dashboard & Timeline",
    "🐾 Manage Batches & Logs",
    "🌾 Feed & Storage Schedule",
    "💰 Financials & Reconciliation",
    "⚙️ Settings & Backup",
]

STATUS_OPTIONS = [
    "🟢 Active",
    "🥩 Harvested",
    "⚪ Excluded",
]


def get_batch_status_badge(schedule: dict) -> str:
    b = schedule["batch"]
    if not b["active"]:
        return "⚪ Excluded"
    if schedule["is_fully_harvested"]:
        return "🥩 Harvested"
    return "🟢 Active"


def _format_relative_days_badge(
    start_d: date, end_d: date, today: date
) -> tuple[str, int]:
    """Return a human-friendly relative day badge and integer days_away."""
    days_away = (start_d - today).days
    if start_d <= today <= end_d:
        return "🟢 Today / Active Now", 0
    if days_away == 1:
        return "⏳ Tomorrow", 1
    if 1 < days_away <= 14:
        return f"⏳ In {days_away} days", days_away
    if days_away > 14:
        return f"📅 In {days_away} days", days_away
    days_ago = (today - end_d).days
    if days_ago == 1:
        return "🕓 Yesterday", days_away
    return f"🕓 {days_ago}d ago", days_away


def compute_upcoming_milestones(
    schedules: list[dict], today: date | None = None
) -> list[dict]:
    """
    Build a chronological list of farm milestones (Arrival/Breeding, Hatch/Birth,
    Starter -> Grower Ration Switch, and Processing Dates) across schedules.
    Prioritizes upcoming/active milestones (end_date >= today), falling back to
    chronological order if all milestones are historical.
    """
    if today is None:
        today = date.today()

    milestones: list[dict] = []
    for s in schedules:
        b = s["batch"]
        bid = int(b["id"])
        b_name = b["name"]
        sp_title = b["species"].title()

        # 1. Arrival / Breeding Window
        init_badge, init_days = _format_relative_days_badge(
            s["start_date"], s["end_date"], today
        )
        init_date_str = (
            s["start_date"].isoformat()
            if s["start_date"] == s["end_date"]
            else f"{s['start_date'].isoformat()} → {s['end_date'].isoformat()}"
        )
        milestones.append(
            {
                "batch_id": bid,
                "batch_name": b_name,
                "species": sp_title,
                "icon": "📥",
                "task": f"{s['initial_label']} Window",
                "event_date": s["start_date"],
                "end_date": s["end_date"],
                "date_display": init_date_str,
                "badge": init_badge,
                "days_away": init_days,
                "details": f"{s['initial_quantity']} × {sp_title} (${s['acquisition_cost']:,.2f})",
                "gcal_url": create_gcal_link(
                    f"{b_name} - {s['initial_label']} Window",
                    s["start_date"],
                    s["end_date"],
                    f"{s['initial_label']} window for {b_name} ({s['initial_quantity']}x {sp_title}).",
                ),
            }
        )

        # 2. Hatch / Birth Window
        if s["birth_start"] and s["birth_end"]:
            b_badge, b_days = _format_relative_days_badge(
                s["birth_start"], s["birth_end"], today
            )
            b_date_str = (
                s["birth_start"].isoformat()
                if s["birth_start"] == s["birth_end"]
                else f"{s['birth_start'].isoformat()} → {s['birth_end'].isoformat()}"
            )
            milestones.append(
                {
                    "batch_id": bid,
                    "batch_name": b_name,
                    "species": sp_title,
                    "icon": "🐣",
                    "task": f"{s['birth_label']} Window",
                    "event_date": s["birth_start"],
                    "end_date": s["birth_end"],
                    "date_display": b_date_str,
                    "badge": b_badge,
                    "days_away": b_days,
                    "details": f"Start {s['starter_bags']} Starter bag(s) ({s['starter_feed']:,.0f} lbs)",
                    "gcal_url": create_gcal_link(
                        f"{b_name} - {s['birth_label']} Window",
                        s["birth_start"],
                        s["birth_end"],
                        f"Estimated {s['birth_label'].lower()} window for {b_name} ({s['initial_quantity']}x {sp_title}).",
                    ),
                }
            )

        # 3. Starter -> Grower Ration Switch
        if s["starter_weeks"] > 0 and s["grower_weeks"] > 0:
            tr_badge, tr_days = _format_relative_days_badge(
                s["transition_date"], s["transition_date"], today
            )
            milestones.append(
                {
                    "batch_id": bid,
                    "batch_name": b_name,
                    "species": sp_title,
                    "icon": "🌾",
                    "task": "Starter → Grower Switch",
                    "event_date": s["transition_date"],
                    "end_date": s["transition_date"],
                    "date_display": s["transition_date"].isoformat(),
                    "badge": tr_badge,
                    "days_away": tr_days,
                    "details": f"Switch to {s['grower_bags']} Grower bag(s) ({s['grower_feed']:,.0f} lbs)",
                    "gcal_url": create_gcal_link(
                        f"{b_name} - Switch to Grower/Finisher Feed",
                        s["transition_date"],
                        s["transition_date"],
                        (
                            f"Switch {b_name} from Starter to Grower/Finisher feed. "
                            f"Need {s['grower_bags']} bags ({s['grower_feed']:,.1f} lbs)."
                        ),
                    ),
                }
            )

        # 4. Target Processing Date
        if not s["is_fully_harvested"]:
            pr_badge, pr_days = _format_relative_days_badge(
                s["proc_date"], s["proc_date"], today
            )
            milestones.append(
                {
                    "batch_id": bid,
                    "batch_name": b_name,
                    "species": sp_title,
                    "icon": "🔪",
                    "task": "Target Processing Date",
                    "event_date": s["proc_date"],
                    "end_date": s["proc_date"],
                    "date_display": s["proc_date"].isoformat(),
                    "badge": pr_badge,
                    "days_away": pr_days,
                    "details": (
                        f"{s['unharvested_surviving_head']} head → "
                        f"~{s['expected_meat_lbs']:,.1f} lbs meat"
                    ),
                    "gcal_url": create_gcal_link(
                        f"{b_name} - Processing Date",
                        s["proc_date"],
                        s["proc_date"],
                        (
                            f"Processing date for {b_name} ({s['surviving_quantity']} surviving head). "
                            f"Expected meat yield: {s['expected_meat_lbs']:,.1f} lbs."
                        ),
                    ),
                }
            )

    milestones.sort(key=lambda m: (m["event_date"], m["batch_id"], m["task"]))
    upcoming = [m for m in milestones if m["end_date"] >= today]
    return upcoming if upcoming else milestones


def on_species_change() -> None:
    selected = st.session_state.form_species
    defaults = get_species_defaults(selected)
    st.session_state.form_days_to_birth = int(defaults["days_to_birth"])
    st.session_state.form_starter_weeks = int(defaults["starter_weeks"])
    st.session_state.form_starter_feed_lbs = float(defaults["starter_feed_lbs"])
    st.session_state.form_grower_weeks = int(defaults["grower_weeks"])
    st.session_state.form_grower_feed_lbs = float(defaults["grower_feed_lbs"])
    st.session_state.form_starter_bag_price = float(
        defaults["starter_bag_price"]
    )
    st.session_state.form_grower_bag_price = float(defaults["grower_bag_price"])
    st.session_state.form_unit_cost = float(defaults["unit_cost"])
    st.session_state.form_processing_cost = float(defaults["processing_cost"])
    st.session_state.form_expected_yield_lbs = float(
        defaults["expected_yield_lbs"]
    )
    st.session_state.form_sale_price_per_lb = float(
        defaults["sale_price_per_lb"]
    )


def apply_pending_editor_transitions() -> None:
    today = date.today()
    if "editing_batch_id" not in st.session_state:
        st.session_state.editing_batch_id = None
    if "form_species" not in st.session_state:
        st.session_state.form_species = list(LIVESTOCK_DATA.keys())[0]

    if st.session_state.get("pending_reset", False):
        st.session_state.editing_batch_id = None
        default_sp = st.session_state.form_species
        defaults = get_species_defaults(default_sp)
        st.session_state.form_name = ""
        st.session_state.form_quantity = 1
        st.session_state.form_dates = (
            today + timedelta(days=7),
            today + timedelta(days=14),
        )
        st.session_state.form_days_to_birth = int(defaults["days_to_birth"])
        st.session_state.form_starter_weeks = int(defaults["starter_weeks"])
        st.session_state.form_starter_feed_lbs = float(
            defaults["starter_feed_lbs"]
        )
        st.session_state.form_grower_weeks = int(defaults["grower_weeks"])
        st.session_state.form_grower_feed_lbs = float(
            defaults["grower_feed_lbs"]
        )
        st.session_state.form_starter_bag_price = float(
            defaults["starter_bag_price"]
        )
        st.session_state.form_grower_bag_price = float(
            defaults["grower_bag_price"]
        )
        st.session_state.form_unit_cost = float(defaults["unit_cost"])
        st.session_state.form_processing_cost = float(
            defaults["processing_cost"]
        )
        st.session_state.form_expected_yield_lbs = float(
            defaults["expected_yield_lbs"]
        )
        st.session_state.form_sale_price_per_lb = float(
            defaults["sale_price_per_lb"]
        )
        st.session_state.form_active = True
        st.session_state.form_notes = ""
        st.session_state.pending_reset = False

    pending_batch = st.session_state.get("pending_edit_batch")
    if pending_batch is not None:
        sp = (
            pending_batch["species"]
            if pending_batch["species"] in LIVESTOCK_DATA
            else list(LIVESTOCK_DATA.keys())[0]
        )
        defaults = get_species_defaults(sp)
        st.session_state.editing_batch_id = pending_batch["id"]
        st.session_state.form_name = pending_batch["name"]
        st.session_state.form_species = sp
        st.session_state.form_quantity = int(pending_batch["quantity"])
        st.session_state.form_dates = (
            date.fromisoformat(pending_batch["start_date"]),
            date.fromisoformat(pending_batch["end_date"]),
        )
        st.session_state.form_days_to_birth = int(
            pending_batch["days_to_birth"]
        )
        st.session_state.form_starter_weeks = int(
            pending_batch.get("starter_weeks")
            if pending_batch.get("starter_weeks") is not None
            else defaults["starter_weeks"]
        )
        st.session_state.form_starter_feed_lbs = float(
            pending_batch.get("starter_feed_lbs")
            if pending_batch.get("starter_feed_lbs") is not None
            else defaults["starter_feed_lbs"]
        )
        st.session_state.form_grower_weeks = int(
            pending_batch.get("grower_weeks")
            if pending_batch.get("grower_weeks") is not None
            else defaults["grower_weeks"]
        )
        st.session_state.form_grower_feed_lbs = float(
            pending_batch.get("grower_feed_lbs")
            if pending_batch.get("grower_feed_lbs") is not None
            else defaults["grower_feed_lbs"]
        )
        st.session_state.form_starter_bag_price = float(
            pending_batch.get("starter_bag_price")
            if pending_batch.get("starter_bag_price") is not None
            else defaults["starter_bag_price"]
        )
        st.session_state.form_grower_bag_price = float(
            pending_batch.get("grower_bag_price")
            if pending_batch.get("grower_bag_price") is not None
            else defaults["grower_bag_price"]
        )
        st.session_state.form_unit_cost = float(
            pending_batch.get("unit_cost")
            if pending_batch.get("unit_cost") is not None
            else defaults["unit_cost"]
        )
        st.session_state.form_processing_cost = float(
            pending_batch.get("processing_cost")
            if pending_batch.get("processing_cost") is not None
            else defaults["processing_cost"]
        )
        st.session_state.form_expected_yield_lbs = float(
            pending_batch.get("expected_yield_lbs")
            if pending_batch.get("expected_yield_lbs") is not None
            else defaults["expected_yield_lbs"]
        )
        st.session_state.form_sale_price_per_lb = float(
            pending_batch.get("sale_price_per_lb")
            if pending_batch.get("sale_price_per_lb") is not None
            else defaults["sale_price_per_lb"]
        )
        st.session_state.form_active = bool(pending_batch["active"])
        st.session_state.form_notes = pending_batch.get("notes", "")
        st.session_state.pending_edit_batch = None

    defaults = get_species_defaults(st.session_state.form_species)
    default_map = {
        "form_name": "",
        "form_quantity": 1,
        "form_dates": (today + timedelta(days=7), today + timedelta(days=14)),
        "form_days_to_birth": int(defaults["days_to_birth"]),
        "form_starter_weeks": int(defaults["starter_weeks"]),
        "form_starter_feed_lbs": float(defaults["starter_feed_lbs"]),
        "form_grower_weeks": int(defaults["grower_weeks"]),
        "form_grower_feed_lbs": float(defaults["grower_feed_lbs"]),
        "form_starter_bag_price": float(defaults["starter_bag_price"]),
        "form_grower_bag_price": float(defaults["grower_bag_price"]),
        "form_unit_cost": float(defaults["unit_cost"]),
        "form_processing_cost": float(defaults["processing_cost"]),
        "form_expected_yield_lbs": float(defaults["expected_yield_lbs"]),
        "form_sale_price_per_lb": float(defaults["sale_price_per_lb"]),
        "form_active": True,
        "form_notes": "",
    }
    for k, v in default_map.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _dismiss_batch_modal() -> None:
    st.session_state.show_batch_modal = False


def _save_batch_modal_callback() -> None:
    today = date.today()
    order_type = str(st.session_state.get("form_species", "chicken chicks"))
    batch_name = str(st.session_state.get("form_name", "")).strip()
    quantity = int(st.session_state.get("form_quantity", 1))
    selected_dates = st.session_state.get("form_dates", (today, today))
    days_to_birth = int(st.session_state.get("form_days_to_birth", 0))
    starter_weeks = int(st.session_state.get("form_starter_weeks", 3))
    starter_feed_lbs = float(st.session_state.get("form_starter_feed_lbs", 3.0))
    grower_weeks = int(st.session_state.get("form_grower_weeks", 5))
    grower_feed_lbs = float(st.session_state.get("form_grower_feed_lbs", 12.0))
    starter_bag_price = float(
        st.session_state.get("form_starter_bag_price", 24.0)
    )
    grower_bag_price = float(
        st.session_state.get("form_grower_bag_price", 22.0)
    )
    unit_cost = float(st.session_state.get("form_unit_cost", 4.0))
    processing_cost = float(st.session_state.get("form_processing_cost", 5.0))
    expected_yield_lbs = float(
        st.session_state.get("form_expected_yield_lbs", 4.5)
    )
    sale_price_per_lb = float(
        st.session_state.get("form_sale_price_per_lb", 6.5)
    )
    active_flag = bool(st.session_state.get("form_active", True))
    notes = str(st.session_state.get("form_notes", "")).strip()

    if isinstance(selected_dates, (list, tuple)):
        if len(selected_dates) == 2:
            start_d, end_d = selected_dates[0], selected_dates[1]
        elif len(selected_dates) == 1:
            start_d = end_d = selected_dates[0]
        else:
            start_d = end_d = today
    elif isinstance(selected_dates, date):
        start_d = end_d = selected_dates
    else:
        start_d = end_d = today

    if end_d < start_d:
        start_d, end_d = end_d, start_d

    eff_grower_weeks = (
        1 if (starter_weeks + grower_weeks < 1) else grower_weeks
    )
    existing_count = len(list_batches())
    resolved_name = (
        batch_name or f"{order_type.title()} #{existing_count + 1}"
    )

    saved_id = save_batch(
        {
            "name": resolved_name,
            "species": order_type,
            "quantity": quantity,
            "start_date": start_d.isoformat(),
            "end_date": end_d.isoformat(),
            "days_to_birth": days_to_birth,
            "starter_weeks": starter_weeks,
            "starter_feed_lbs": starter_feed_lbs,
            "grower_weeks": eff_grower_weeks,
            "grower_feed_lbs": grower_feed_lbs,
            "starter_bag_price": starter_bag_price,
            "grower_bag_price": grower_bag_price,
            "unit_cost": unit_cost,
            "processing_cost": processing_cost,
            "expected_yield_lbs": expected_yield_lbs,
            "sale_price_per_lb": sale_price_per_lb,
            "active": active_flag,
            "notes": notes,
        },
        batch_id=st.session_state.get("editing_batch_id"),
    )
    st.session_state.selected_batch_id = saved_id
    st.session_state.show_batch_modal = False
    st.session_state.pending_reset = True


def _cancel_batch_modal_callback() -> None:
    st.session_state.show_batch_modal = False
    st.session_state.pending_reset = True


@st.dialog(
    "🚜 Livestock Batch Editor",
    width="large",
    on_dismiss=_dismiss_batch_modal,
)
def render_batch_editor_modal(barrel_capacity: int, bag_weight: int) -> None:
    if not st.session_state.get("show_batch_modal", False):
        st.rerun(scope="app")

    is_editing = st.session_state.editing_batch_id is not None
    st.markdown(
        f"### {'✏️ Edit Livestock Batch' if is_editing else '➕ Add Livestock Batch'}"
    )
    st.caption(
        "Configure species defaults, phased Starter/Grower rations, and unit/meat pricing. "
        "The live preview banner below updates automatically before you save."
    )

    tab_basics, tab_rations, tab_pricing = st.tabs(
        [
            "1. Basics & Dates",
            "2. Phased Feed Rations",
            "3. Pricing & Meat Yield",
        ]
    )

    today = date.today()

    with tab_basics:
        b_col1, b_col2 = st.columns(2)
        with b_col1:
            order_type = st.selectbox(
                "Select Livestock Type:",
                list(LIVESTOCK_DATA.keys()),
                key="form_species",
                on_change=on_species_change,
                format_func=lambda s: s.title(),
            )
            batch_name = st.text_input(
                "Batch Name (optional)",
                key="form_name",
                placeholder=f"e.g. Spring {order_type.title()}",
            )
            quantity = st.number_input(
                "Initial Quantity (head / eggs)",
                min_value=1,
                key="form_quantity",
                step=1,
            )
        with b_col2:
            date_label = (
                "Expected Breeding Date (or Range)"
                if "pigs" in order_type
                else "Arrival Date (or Range)"
            )
            selected_dates = st.date_input(
                date_label,
                key="form_dates",
                help="Select either a single date (click same date twice) or a start and end date range. Past and future dates are supported.",
            )
            days_to_birth = st.number_input(
                "Gestation / Incubation (days)",
                min_value=0,
                max_value=400,
                key="form_days_to_birth",
                step=1,
            )
            active_flag = st.checkbox(
                "Include in active herd plan & timeline",
                key="form_active",
            )

        notes = st.text_area(
            "Batch Notes (optional)",
            key="form_notes",
            placeholder="Breeder/hatchery source, pasture paddock, butcher info...",
            height=68,
        )

    with tab_rations:
        r_col1, r_col2 = st.columns(2)
        with r_col1:
            with st.container(border=True):
                st.markdown("**🌱 Starter Phase Ration**")
                starter_weeks = st.number_input(
                    "Starter Phase (wks)",
                    min_value=0,
                    max_value=104,
                    key="form_starter_weeks",
                    step=1,
                )
                starter_feed_lbs = st.number_input(
                    "Starter Feed/Head (lbs)",
                    min_value=0.0,
                    max_value=5000.0,
                    key="form_starter_feed_lbs",
                    step=1.0,
                )
        with r_col2:
            with st.container(border=True):
                st.markdown("**🌾 Grower / Finisher Phase Ration**")
                grower_weeks = st.number_input(
                    "Grower/Finisher (wks)",
                    min_value=0,
                    max_value=260,
                    key="form_grower_weeks",
                    step=1,
                )
                grower_feed_lbs = st.number_input(
                    "Grower Feed/Head (lbs)",
                    min_value=0.0,
                    max_value=10000.0,
                    key="form_grower_feed_lbs",
                    step=5.0,
                )
        if "pigs" in order_type:
            st.caption(
                "*Note: Kunekune pig feed demand varies significantly based on pasture quality and seasonal forage.*"
            )

    with tab_pricing:
        p_col1, p_col2, p_col3 = st.columns(3)
        with p_col1:
            with st.container(border=True):
                st.markdown("**🌾 Feed Bag Pricing**")
                starter_bag_price = st.number_input(
                    f"Starter Bag (${bag_weight}lb)",
                    min_value=0.0,
                    max_value=500.0,
                    key="form_starter_bag_price",
                    step=1.0,
                )
                grower_bag_price = st.number_input(
                    f"Grower Bag (${bag_weight}lb)",
                    min_value=0.0,
                    max_value=500.0,
                    key="form_grower_bag_price",
                    step=1.0,
                )
        with p_col2:
            with st.container(border=True):
                st.markdown("**🐣 Acquisition & Butcher**")
                unit_cost = st.number_input(
                    "Acquisition $/Unit",
                    min_value=0.0,
                    max_value=5000.0,
                    key="form_unit_cost",
                    step=1.0,
                )
                processing_cost = st.number_input(
                    "Processing $/Head",
                    min_value=0.0,
                    max_value=5000.0,
                    key="form_processing_cost",
                    step=1.0,
                )
        with p_col3:
            with st.container(border=True):
                st.markdown("**🥩 Meat Yield & Pricing**")
                expected_yield_lbs = st.number_input(
                    "Meat Yield (lbs/head)",
                    min_value=0.0,
                    max_value=2000.0,
                    key="form_expected_yield_lbs",
                    step=0.5,
                )
                sale_price_per_lb = st.number_input(
                    "Target Sale Price ($/lb)",
                    min_value=0.0,
                    max_value=500.0,
                    key="form_sale_price_per_lb",
                    step=0.5,
                )

    # Resolve dates & compute live preview
    if isinstance(selected_dates, (list, tuple)):
        if len(selected_dates) == 2:
            start_d, end_d = selected_dates[0], selected_dates[1]
        elif len(selected_dates) == 1:
            start_d = end_d = selected_dates[0]
        else:
            start_d = end_d = today
    elif isinstance(selected_dates, date):
        start_d = end_d = selected_dates
    else:
        start_d = end_d = today

    if end_d < start_d:
        start_d, end_d = end_d, start_d

    eff_grower_weeks = (
        1 if (int(starter_weeks) + int(grower_weeks) < 1) else int(grower_weeks)
    )
    preview_payload = _normalize_batch_payload(
        {
            "name": batch_name.strip() or f"{order_type.title()} Preview",
            "species": order_type,
            "quantity": quantity,
            "start_date": start_d.isoformat(),
            "end_date": end_d.isoformat(),
            "days_to_birth": days_to_birth,
            "starter_weeks": int(starter_weeks),
            "starter_feed_lbs": float(starter_feed_lbs),
            "grower_weeks": eff_grower_weeks,
            "grower_feed_lbs": float(grower_feed_lbs),
            "starter_bag_price": float(starter_bag_price),
            "grower_bag_price": float(grower_bag_price),
            "unit_cost": float(unit_cost),
            "processing_cost": float(processing_cost),
            "expected_yield_lbs": float(expected_yield_lbs),
            "sale_price_per_lb": float(sale_price_per_lb),
            "active": active_flag,
            "notes": notes.strip(),
        }
    )
    preview_payload["id"] = st.session_state.editing_batch_id or 0
    preview_sched = compute_batch_schedule(
        preview_payload,
        barrel_capacity=barrel_capacity,
        bag_weight=bag_weight,
        losses=[],
        harvests=[],
        sales=[],
    )

    with st.container(border=True):
        st.markdown("##### 📊 Live Batch Preview (Before Saving)")
        pv1, pv2, pv3, pv4 = st.columns(4)
        if preview_sched["birth_start"] and preview_sched["birth_end"]:
            hatch_str = f"{preview_sched['birth_label']}: {preview_sched['birth_start']} → {preview_sched['birth_end']}"
        else:
            hatch_str = f"{preview_sched['initial_label']}: {preview_sched['start_date']}"
        pv1.metric(
            "🔪 Butcher Date",
            f"{preview_sched['proc_date']}",
            delta=f"Switch: {preview_sched['transition_date']}",
            delta_color="off",
        )
        pv1.caption(hatch_str)

        pv2.metric(
            "🌾 Total Feed & Bags",
            f"{preview_sched['total_feed']:,.0f} lbs ({preview_sched['total_bags']} bags)",
            delta=f"{preview_sched['starter_bags']} Starter · {preview_sched['grower_bags']} Grower",
            delta_color="off",
        )
        pv2.caption(
            f"Storage: {preview_sched['barrels_needed']} × {barrel_capacity}lb barrel(s)"
        )

        pv3.metric(
            "💰 Est. Total Cost",
            f"${preview_sched['total_budget_cost']:,.2f}",
            delta=f"${preview_sched['total_cost_per_lb_meat']:,.2f}/lb dressed meat",
            delta_color="off",
        )
        pv3.caption(
            f"Meat Yield: {preview_sched['expected_meat_lbs']:,.1f} lbs ({quantity} head)"
        )

        pv4.metric(
            "💵 Est. Net Profit",
            f"${preview_sched['estimated_profit']:,.2f}",
            delta=f"Rev: ${preview_sched['estimated_revenue']:,.2f} ({preview_sched['roi_margin_pct']:.1f}% ROI)",
            delta_color="normal",
        )
        pv4.caption(
            f"Net Margin: ${preview_sched['profit_per_lb_meat']:+.2f}/lb @ ${preview_sched['sale_price_per_lb']:,.2f}/lb"
        )

    btn_c1, btn_c2 = st.columns([2, 1])
    save_label = "💾 Update Batch" if is_editing else "➕ Save Batch"
    btn_c1.button(
        save_label,
        key="modal_save_batch_btn",
        type="primary",
        use_container_width=True,
        on_click=_save_batch_modal_callback,
    )
    btn_c2.button(
        "Cancel",
        key="modal_cancel_batch_btn",
        use_container_width=True,
        on_click=_cancel_batch_modal_callback,
    )


def render_app() -> None:
    st.set_page_config(
        page_title="Homestead Planner: Poultry & Pigs",
        page_icon="🚜",
        layout="wide",
    )

    init_db()
    apply_pending_editor_transitions()

    barrel_capacity = get_barrel_capacity()
    bag_weight = get_bag_weight()

    all_batches = list_batches()
    all_losses = list_batch_losses()
    losses_by_batch: dict[int, list[dict]] = {}
    for l_row in all_losses:
        losses_by_batch.setdefault(int(l_row["batch_id"]), []).append(l_row)

    all_harvests = list_batch_harvests()
    harvests_by_batch: dict[int, list[dict]] = {}
    for h_row in all_harvests:
        harvests_by_batch.setdefault(int(h_row["batch_id"]), []).append(h_row)

    all_sales = list_batch_sales()
    sales_by_batch: dict[int, list[dict]] = {}
    for s_row in all_sales:
        sales_by_batch.setdefault(int(s_row["batch_id"]), []).append(s_row)

    all_schedules = [
        compute_batch_schedule(
            b,
            barrel_capacity=barrel_capacity,
            bag_weight=bag_weight,
            losses=losses_by_batch.get(int(b["id"]), []),
            harvests=harvests_by_batch.get(int(b["id"]), []),
            sales=sales_by_batch.get(int(b["id"]), []),
        )
        for b in all_batches
    ]

    # -----------------------------------------------------------------------
    # Minimal Sidebar: Prominent Add Batch Button, Filters & .ics Export
    # -----------------------------------------------------------------------
    with st.sidebar:
        st.markdown("## 🚜 Homestead Planner")
        st.caption("Poultry & Pigs Lifecycle, Feed & ROI")

        if st.button(
            "➕ Add Livestock Batch",
            key="sidebar_add_batch_btn",
            type="primary",
            use_container_width=True,
        ):
            st.session_state.pending_reset = True
            st.session_state.show_batch_modal = True
            st.rerun()

        st.divider()
        st.markdown("##### 🔍 Herd Filters")
        species_present = sorted({b["species"] for b in all_batches})
        if species_present:
            selected_species_filter = st.multiselect(
                "Filter by Species",
                options=species_present,
                default=species_present,
                format_func=lambda s: s.title(),
            )
        else:
            selected_species_filter = []

        selected_status_filter = st.multiselect(
            "Filter by Batch Status",
            options=STATUS_OPTIONS,
            default=STATUS_OPTIONS,
        )

        filtered_schedules = [
            s
            for s in all_schedules
            if s["batch"]["species"] in selected_species_filter
            and get_batch_status_badge(s) in selected_status_filter
        ]
        filtered_active_schedules = [
            s for s in filtered_schedules if s["batch"]["active"]
        ]

        st.divider()
        st.markdown("##### 📅 Calendar Export")
        if filtered_active_schedules:
            ics_content = generate_ics_calendar(filtered_active_schedules)
            st.download_button(
                "📅 Export Active Plan (.ics)",
                data=ics_content,
                file_name="homestead_schedule.ics",
                mime="text/calendar",
                use_container_width=True,
                help="Download an iCalendar (.ics) file with Arrival/Breeding, Birth/Hatch, Ration Transition, and Processing dates for all active batches.",
            )
        else:
            st.caption("Enable an active batch to export `.ics` calendar events.")

        st.divider()
        st.caption(
            f"🛢️ **Barrel Size:** {barrel_capacity} lbs · **Feed Bag:** {bag_weight} lbs\n\n"
            f"*(Configure in ⚙️ Settings & Backup)*"
        )

    # Render Add/Edit Batch Modal if open
    if st.session_state.get("show_batch_modal", False):
        render_batch_editor_modal(barrel_capacity, bag_weight)

    # -----------------------------------------------------------------------
    # Main Header & Top-Level Workspace Navigation
    # -----------------------------------------------------------------------
    st.title("🚜 Homestead Planner: Poultry & Pigs")
    st.caption(
        "Plan, track, and reconcile livestock batches across lifecycle timelines, phased rations, storage barrels, and freezer/sales ROI."
    )

    if "workspace_nav" not in st.session_state:
        st.session_state.workspace_nav = WORKSPACES[0]
    if st.session_state.get("jump_to_workspace"):
        st.session_state.workspace_nav = st.session_state.pop(
            "jump_to_workspace"
        )

    active_workspace = st.segmented_control(
        "Workspace Navigation",
        options=WORKSPACES,
        key="workspace_nav",
        selection_mode="single",
        label_visibility="collapsed",
        width="stretch",
    )
    if not active_workspace:
        active_workspace = WORKSPACES[0]

    # Precompute aggregated tables & metrics for active plan
    weekly_df, monthly_df, feed_stats = compute_combined_feed_schedule(
        filtered_active_schedules, barrel_capacity
    )
    purchasing_events_df, monthly_cash_df = compute_purchasing_schedule(
        filtered_active_schedules, bag_weight
    )
    roi_df = compute_roi_summary_table(filtered_active_schedules)
    reconciliation_df = compute_reconciliation_table(filtered_active_schedules)

    total_active_batches = len(filtered_active_schedules)
    total_initial_head = sum(
        s["initial_quantity"] for s in filtered_active_schedules
    )
    total_surviving_head = sum(
        s["surviving_quantity"] for s in filtered_active_schedules
    )
    total_lost_head = sum(s["total_lost"] for s in filtered_active_schedules)

    combined_feed_lbs = sum(s["total_feed"] for s in filtered_active_schedules)
    combined_feed_saved = sum(
        s["feed_saved_by_losses"] for s in filtered_active_schedules
    )
    combined_barrels = math.ceil(combined_feed_lbs / max(1, barrel_capacity))
    combined_bags = sum(s["total_bags"] for s in filtered_active_schedules)

    combined_budget_cost = sum(
        s["total_budget_cost"] for s in filtered_active_schedules
    )
    combined_feed_cost = sum(
        s["total_feed_cost_bags"] for s in filtered_active_schedules
    )
    combined_meat_lbs = sum(
        s["expected_meat_lbs"] for s in filtered_active_schedules
    )
    combined_revenue = sum(
        s["estimated_revenue"] for s in filtered_active_schedules
    )
    combined_profit = sum(
        s["estimated_profit"] for s in filtered_active_schedules
    )
    blended_cost_per_lb = (
        combined_budget_cost / combined_meat_lbs
        if combined_meat_lbs > 0
        else 0.0
    )
    blended_sale_per_lb = (
        combined_revenue / combined_meat_lbs if combined_meat_lbs > 0 else 0.0
    )
    blended_profit_per_lb = blended_sale_per_lb - blended_cost_per_lb
    blended_feed_cost_per_lb = (
        combined_feed_cost / combined_meat_lbs if combined_meat_lbs > 0 else 0.0
    )

    tot_harvested_head = sum(
        s["harvested_head"] for s in filtered_active_schedules
    )
    tot_actual_dressed = sum(
        s["actual_dressed_lbs"] for s in filtered_active_schedules
    )
    tot_reconciled_cost = sum(
        s["reconciled_total_cost"] for s in filtered_active_schedules
    )
    tot_sold_lbs = sum(s["sold_lbs"] for s in filtered_active_schedules)
    tot_sales_rev = sum(
        s["actual_sales_revenue"] for s in filtered_active_schedules
    )
    tot_freezer_lbs = sum(s["freezer_lbs"] for s in filtered_active_schedules)
    tot_cash_net = tot_sales_rev - tot_reconciled_cost
    tot_net_cost_for_freezer = max(0.0, tot_reconciled_cost - tot_sales_rev)
    tot_eff_freezer_per_lb = (
        tot_net_cost_for_freezer / tot_freezer_lbs
        if tot_freezer_lbs > 0
        else None
    )

    # Empty state if no batches exist and user is not in Settings & Backup
    if not all_batches and active_workspace != "⚙️ Settings & Backup":
        with st.container(border=True):
            st.info(
                "👋 **No livestock batches saved yet.** Click **➕ Add Livestock Batch** in the sidebar (or below) to create your first batch, or restore a saved herd plan in **⚙️ Settings & Backup**."
            )
            e_c1, e_c2 = st.columns(2)
            if e_c1.button(
                "➕ Add Your First Livestock Batch",
                key="empty_add_batch_btn",
                type="primary",
                use_container_width=True,
            ):
                st.session_state.pending_reset = True
                st.session_state.show_batch_modal = True
                st.rerun()
            if e_c2.button(
                "⚙️ Open Settings & Restore Backup",
                key="empty_open_settings_btn",
                use_container_width=True,
            ):
                st.session_state.jump_to_workspace = "⚙️ Settings & Backup"
                st.rerun()
        return

    # =======================================================================
    # WORKSPACE 1: 🗓️ Dashboard & Timeline
    # =======================================================================
    if active_workspace == "🗓️ Dashboard & Timeline":
        # 4 Clean KPI Cards in Bordered Containers
        kpi1, kpi2, kpi3, kpi4 = st.columns(4)
        with kpi1:
            with st.container(border=True):
                st.metric(
                    "🐾 Active Herd",
                    f"{total_surviving_head:,} / {total_initial_head:,} head",
                    delta=(
                        f"-{total_lost_head} lost ({total_active_batches} batches)"
                        if total_lost_head > 0
                        else f"{total_active_batches} active batch(es)"
                    ),
                    delta_color="inverse" if total_lost_head > 0 else "off",
                )
                st.caption(
                    f"Harvested: **{tot_harvested_head}** head · Remaining: **{max(0, total_surviving_head - tot_harvested_head)}** head"
                )
        with kpi2:
            with st.container(border=True):
                st.metric(
                    "🌾 Feed & Barrel Storage",
                    f"{combined_feed_lbs:,.0f} lbs",
                    delta=f"{combined_bags} bags · Peak {feed_stats['peak_concurrent_barrels']} barrel(s)",
                    delta_color="off",
                )
                st.caption(
                    f"Total storage: **{combined_barrels}** × {barrel_capacity}lb barrels · Peak burn: **{feed_stats['peak_weekly_lbs']:,.1f} lbs/wk**"
                )
        with kpi3:
            with st.container(border=True):
                st.metric(
                    "💰 Reconciled Cost vs. Est. Revenue",
                    f"${tot_reconciled_cost:,.2f} / ${combined_revenue:,.2f}",
                    delta=f"Cost: ${blended_cost_per_lb:,.2f}/lb · Target Sale: ${blended_sale_per_lb:,.2f}/lb",
                    delta_color="off",
                )
                st.caption(
                    f"Feed Cost: **${combined_feed_cost:,.2f}** (${blended_feed_cost_per_lb:,.2f}/lb) · Cash Sales: **${tot_sales_rev:,.2f}**"
                )
        with kpi4:
            with st.container(border=True):
                freezer_delta = (
                    f"❄️ {tot_freezer_lbs:,.1f} lbs freezer ($0.00/lb Free!)"
                    if tot_eff_freezer_per_lb == 0.0
                    else (
                        f"❄️ {tot_freezer_lbs:,.1f} lbs freezer (${tot_eff_freezer_per_lb:,.2f}/lb)"
                        if tot_eff_freezer_per_lb is not None
                        else f"{'+' if blended_profit_per_lb >= 0 else ''}${blended_profit_per_lb:,.2f}/lb est. net"
                    )
                )
                st.metric(
                    "🥩 Net Profit & Home Freezer Meat",
                    f"${combined_profit:,.2f}",
                    delta=freezer_delta,
                    delta_color="normal",
                )
                st.caption(
                    f"Est. Yield: **{combined_meat_lbs:,.1f} lbs** · Harvested: **{tot_actual_dressed:,.1f} lbs** · Freezer: **{tot_freezer_lbs:,.1f} lbs**"
                )

        if not filtered_active_schedules:
            st.warning(
                "No active batches match the current Species / Status filters in the sidebar."
            )
        else:
            # 🔔 Upcoming Milestones & Action Items Strip
            st.subheader("🔔 Upcoming Milestones & Action Items")
            upcoming_milestones = compute_upcoming_milestones(
                filtered_active_schedules
            )
            if upcoming_milestones:
                top_milestones = upcoming_milestones[:4]
                m_cols = st.columns(len(top_milestones))
                for idx, ms in enumerate(top_milestones):
                    with m_cols[idx]:
                        with st.container(border=True):
                            st.markdown(
                                f"**{ms['icon']} {ms['task']}** · `{ms['badge']}`"
                            )
                            st.markdown(
                                f"**{ms['batch_name']}** *({ms['species']})*\n\n"
                                f"📅 **{ms['date_display']}**\n\n"
                                f"<small>{ms['details']}</small>",
                                unsafe_allow_html=True,
                            )
                            st.link_button(
                                "📅 Add to Google Calendar",
                                ms["gcal_url"],
                                use_container_width=True,
                            )

                if len(upcoming_milestones) > 4:
                    with st.expander(
                        f"View all {len(upcoming_milestones)} chronological farm milestones"
                    ):
                        ms_df = pd.DataFrame(
                            [
                                {
                                    "Status": m["badge"],
                                    "Date": m["date_display"],
                                    "Batch": m["batch_name"],
                                    "Milestone": f"{m['icon']} {m['task']}",
                                    "Action Details": m["details"],
                                }
                                for m in upcoming_milestones
                            ]
                        )
                        st.dataframe(
                            ms_df, use_container_width=True, hide_index=True
                        )

            # 🗓️ Shared Herd Lifecycle Timeline
            st.subheader("🗓️ Shared Herd Lifecycle Timeline")
            st.caption(
                "Click any phase, loss, or harvest bar on the swimlane timeline to inspect the batch or jump straight to Batch Manager / Edit Modal."
            )
            timeline_groups = []
            timeline_items = []
            for s in filtered_active_schedules:
                b = s["batch"]
                loss_badge = (
                    f" · ⚠️ -{s['total_lost']}" if s["total_lost"] > 0 else ""
                )
                harvest_badge = (
                    f" · 🥩 {s['harvested_head']} harvested"
                    if s["harvested_head"] > 0
                    else ""
                )
                timeline_groups.append(
                    {
                        "id": b["id"],
                        "content": (
                            f"<b>{b['name']}</b><br>"
                            f"<small>{s['surviving_quantity']}/{s['initial_quantity']}x {b['species'].title()}{loss_badge}{harvest_badge}</small>"
                        ),
                    }
                )
                timeline_items.extend(s["timeline_items"])

            selected_tl_item = st_timeline(
                timeline_items,
                groups=timeline_groups,
                options={
                    "selectable": True,
                    "stack": False,
                    "zoomable": True,
                    "verticalScroll": True,
                    "orientation": "top",
                    "margin": {"item": 8, "axis": 6},
                },
                height="360px",
            )

            if selected_tl_item:
                item_id = (
                    selected_tl_item.get("id", "")
                    if isinstance(selected_tl_item, dict)
                    else str(selected_tl_item)
                )
                matched_schedule = None
                for s in filtered_active_schedules:
                    if item_id.startswith(f"b{s['batch']['id']}_"):
                        matched_schedule = s
                        break
                if matched_schedule:
                    mb = matched_schedule["batch"]
                    with st.container(border=True):
                        tl_c1, tl_c2, tl_c3 = st.columns([4, 1.3, 1.1])
                        with tl_c1:
                            st.markdown(
                                f"🔎 **Selected Timeline Batch:** **{mb['name']}** "
                                f"(`{matched_schedule['surviving_quantity']}/{matched_schedule['initial_quantity']}x {mb['species'].title()}`)  \n"
                                f"🌱 **Starter:** {matched_schedule['proc_base']} → {matched_schedule['transition_date']} "
                                f"({matched_schedule['starter_feed']:,.1f} lbs / {matched_schedule['starter_bags']} bags) · "
                                f"🌾 **Grower:** {matched_schedule['transition_date']} → {matched_schedule['proc_date']} "
                                f"({matched_schedule['grower_feed']:,.1f} lbs / {matched_schedule['grower_bags']} bags) · "
                                f"💰 **Reconciled Cost:** ${matched_schedule['reconciled_total_cost']:,.2f} "
                                f"(${matched_schedule['reconciled_cost_per_lb_meat']:,.2f}/lb)"
                            )
                        with tl_c2:
                            if st.button(
                                "🐾 Open in Batch Manager",
                                key=f"tl_open_mgr_{mb['id']}",
                                type="primary",
                                use_container_width=True,
                            ):
                                st.session_state.selected_batch_id = int(
                                    mb["id"]
                                )
                                st.session_state.jump_to_workspace = (
                                    "🐾 Manage Batches & Logs"
                                )
                                st.rerun()
                        with tl_c3:
                            if st.button(
                                "✏️ Edit Batch",
                                key=f"tl_edit_batch_{mb['id']}",
                                use_container_width=True,
                            ):
                                st.session_state.pending_edit_batch = mb
                                st.session_state.show_batch_modal = True
                                st.rerun()

    # =======================================================================
    # WORKSPACE 2: 🐾 Manage Batches & Logs (Master-Detail Workspace)
    # =======================================================================
    elif active_workspace == "🐾 Manage Batches & Logs":
        st.subheader("🐾 Manage Batches & Logs (Master-Detail Workspace)")

        # Use filtered_schedules if non-empty, otherwise fall back to all_schedules
        selectable_schedules = (
            filtered_schedules if filtered_schedules else all_schedules
        )
        if not filtered_schedules and all_schedules:
            st.caption(
                "*Showing all batches because current sidebar filters matched 0 batches.*"
            )

        schedule_by_id = {
            int(s["batch"]["id"]): s for s in selectable_schedules
        }
        batch_ids = list(schedule_by_id.keys())

        if (
            "selected_batch_id" not in st.session_state
            or st.session_state.selected_batch_id not in schedule_by_id
        ):
            st.session_state.selected_batch_id = batch_ids[0]

        def _format_batch_selector(bid: int) -> str:
            sc = schedule_by_id[bid]
            b_row = sc["batch"]
            badge = get_batch_status_badge(sc)
            return (
                f"{badge} | {b_row['name']} (#{bid}) — "
                f"{sc['surviving_quantity']}/{sc['initial_quantity']} {b_row['species'].title()} · "
                f"Feed: {sc['reconciled_feed_lbs']:,.0f} lbs · "
                f"Est. Profit: ${sc['estimated_profit']:,.2f}"
            )

        # Master Selector & Action Toolbar
        with st.container(border=True):
            sel_col, act_col1, act_col2, act_col3, act_col4 = st.columns(
                [3.4, 1.1, 1.1, 1.2, 0.9]
            )
            with sel_col:
                selected_bid = st.selectbox(
                    "Select Batch to Inspect & Log",
                    options=batch_ids,
                    index=batch_ids.index(st.session_state.selected_batch_id),
                    format_func=_format_batch_selector,
                    label_visibility="collapsed",
                )
                st.session_state.selected_batch_id = int(selected_bid)

            s = schedule_by_id[int(selected_bid)]
            b = s["batch"]

            with act_col1:
                if st.button(
                    "✏️ Edit Batch",
                    key=f"edit_{b['id']}",
                    type="primary",
                    use_container_width=True,
                ):
                    st.session_state.pending_edit_batch = b
                    st.session_state.show_batch_modal = True
                    st.rerun()

            with act_col2:
                if st.button(
                    "📋 Duplicate",
                    key=f"dup_{b['id']}",
                    use_container_width=True,
                ):
                    new_id = save_batch(
                        {
                            "name": f"{b['name']} (Copy)",
                            "species": b["species"],
                            "quantity": b["quantity"],
                            "start_date": b["start_date"],
                            "end_date": b["end_date"],
                            "days_to_birth": b["days_to_birth"],
                            "starter_weeks": b.get("starter_weeks"),
                            "starter_feed_lbs": b.get("starter_feed_lbs"),
                            "grower_weeks": b.get("grower_weeks"),
                            "grower_feed_lbs": b.get("grower_feed_lbs"),
                            "starter_bag_price": b.get("starter_bag_price"),
                            "grower_bag_price": b.get("grower_bag_price"),
                            "unit_cost": b.get("unit_cost"),
                            "processing_cost": b.get("processing_cost"),
                            "expected_yield_lbs": b.get("expected_yield_lbs"),
                            "sale_price_per_lb": b.get("sale_price_per_lb"),
                            "active": b["active"],
                            "notes": b.get("notes", ""),
                        }
                    )
                    st.session_state.selected_batch_id = new_id
                    st.rerun()

            with act_col3:
                toggle_label = (
                    "⏸️ Exclude" if b["active"] else "▶️ Include"
                )
                if st.button(
                    toggle_label,
                    key=f"toggle_{b['id']}",
                    use_container_width=True,
                    help="Include or exclude this batch from the active plan & timeline",
                ):
                    set_batch_active(b["id"], not bool(b["active"]))
                    st.rerun()

            with act_col4:
                if st.button(
                    "🗑️ Delete",
                    key=f"del_{b['id']}",
                    use_container_width=True,
                ):
                    if st.session_state.editing_batch_id == b["id"]:
                        st.session_state.pending_reset = True
                    delete_batch(b["id"])
                    st.rerun()

            # Quick Google Calendar Milestone Links Row
            gcal_cols = st.columns(3)
            with gcal_cols[0]:
                if s["birth_start"] and s["birth_end"]:
                    birth_gcal = create_gcal_link(
                        f"{b['name']} - {s['birth_label']} Window",
                        s["birth_start"],
                        s["birth_end"],
                        f"Look out for {b['name']} ({s['initial_quantity']}x {b['species']})!",
                    )
                    st.link_button(
                        f"📅 {s['birth_label']} Window ({s['birth_start']}) → Google Cal",
                        birth_gcal,
                        use_container_width=True,
                    )
                else:
                    init_gcal = create_gcal_link(
                        f"{b['name']} - {s['initial_label']} Date",
                        s["start_date"],
                        s["end_date"],
                        f"{s['initial_label']} for {b['name']} ({s['initial_quantity']}x {b['species']})!",
                    )
                    st.link_button(
                        f"📅 {s['initial_label']} ({s['start_date']}) → Google Cal",
                        init_gcal,
                        use_container_width=True,
                    )
            with gcal_cols[1]:
                if s["starter_weeks"] > 0 and s["grower_weeks"] > 0:
                    trans_gcal = create_gcal_link(
                        f"{b['name']} - Switch to Grower/Finisher Feed",
                        s["transition_date"],
                        s["transition_date"],
                        (
                            f"Switch {b['name']} from Starter to Grower/Finisher feed. "
                            f"Need {s['grower_bags']} bags ({s['grower_feed']:,.1f} lbs)."
                        ),
                    )
                    st.link_button(
                        f"📅 Ration Switch ({s['transition_date']}) → Google Cal",
                        trans_gcal,
                        use_container_width=True,
                    )
                else:
                    st.caption("Single-phase ration (no transition event)")
            with gcal_cols[2]:
                proc_gcal = create_gcal_link(
                    f"{b['name']} - Processing Date",
                    s["proc_date"],
                    s["proc_date"],
                    (
                        f"Processing date for {b['name']} ({s['surviving_quantity']} surviving head). "
                        f"Expected meat yield: {s['expected_meat_lbs']:,.1f} lbs."
                    ),
                )
                st.link_button(
                    f"📅 Processing Date ({s['proc_date']}) → Google Cal",
                    proc_gcal,
                    use_container_width=True,
                )

        # Focused Batch Detail Panel: 4 Clean Bordered Metric Cards
        status_badge = get_batch_status_badge(s)
        dc1, dc2, dc3, dc4 = st.columns(4)
        with dc1:
            with st.container(border=True):
                st.metric(
                    f"{status_badge} · Herd & Lifecycle",
                    f"{s['surviving_quantity']} / {s['initial_quantity']} head",
                    delta=(
                        f"-{s['total_lost']} lost · {s['harvested_head']} harvested"
                        if s["total_lost"] > 0 or s["harvested_head"] > 0
                        else f"{b['species'].title()}"
                    ),
                    delta_color="off",
                )
                birth_line = (
                    f"**{s['birth_label']}:** {s['birth_start']} → {s['birth_end']} · "
                    if s["birth_start"] and s["birth_end"]
                    else ""
                )
                st.caption(
                    f"**{s['initial_label']}:** {s['start_date']} · {birth_line}"
                    f"**Switch:** {s['transition_date']} · **Butcher:** {s['proc_date']}"
                )
        with dc2:
            with st.container(border=True):
                st.metric(
                    "🌾 Phased Feed & Storage",
                    f"{s['reconciled_feed_lbs']:,.1f} lbs",
                    delta=f"{s['total_bags']} bags ({s['barrels_needed']} × {barrel_capacity}lb barrels)",
                    delta_color="off",
                )
                st.caption(
                    f"**Starter ({s['starter_weeks']}w):** {s['starter_feed']:,.1f} lbs ({s['starter_bags']} bags) · "
                    f"**Grower ({s['grower_weeks']}w):** {s['grower_feed']:,.1f} lbs ({s['grower_bags']} bags)"
                )
                if s["feed_saved_by_losses"] > 0 or s["feed_saved_by_early_harvest"] > 0:
                    st.caption(
                        f"📉 Saved: {s['feed_saved_by_losses']:,.1f} lbs (losses) · "
                        f"{s['feed_saved_by_early_harvest']:,.1f} lbs (early harvest)"
                    )
        with dc3:
            with st.container(border=True):
                st.metric(
                    "💰 Batch Cost & Meat Yield",
                    f"${s['reconciled_total_cost']:,.2f}",
                    delta=f"${s['reconciled_cost_per_lb_meat']:,.2f}/lb · Est. Cost: ${s['total_budget_cost']:,.2f}",
                    delta_color="off",
                )
                st.caption(
                    f"Acq: **${s['acquisition_cost']:,.2f}** · Feed: **${s['reconciled_feed_cost']:,.2f}** · "
                    f"Butcher: **${s['reconciled_processing_cost']:,.2f}** · "
                    f"Meat: **{s['actual_dressed_lbs']:,.1f} / {s['expected_meat_lbs']:,.1f} lbs**"
                )
        with dc4:
            with st.container(border=True):
                st.metric(
                    "🥩 Profit, Sales & Freezer",
                    f"${s['estimated_profit']:,.2f} est.",
                    delta=f"ROI: {s['roi_margin_pct']:.1f}% (@ ${s['sale_price_per_lb']:,.2f}/lb)",
                    delta_color="normal",
                )
                eff_str = (
                    "🎉 $0.00/lb Free!"
                    if s["effective_freezer_cost_per_lb"] == 0.0
                    else (
                        f"${s['effective_freezer_cost_per_lb']:,.2f}/lb"
                        if s["effective_freezer_cost_per_lb"] is not None
                        else "—"
                    )
                )
                st.caption(
                    f"Sold: **{s['sold_lbs']:,.1f} lbs** (**${s['actual_sales_revenue']:,.2f}**) · "
                    f"Freezer: **{s['freezer_lbs']:,.1f} lbs** (Eff: **{eff_str}**)"
                )

        if b.get("notes"):
            st.info(f"📝 **Batch Notes:** {b['notes']}")

        # 3 Spacious Tabs for the Selected Batch
        subtab_losses, subtab_harvest, subtab_sales = st.tabs(
            [
                f"📉 Batch Losses ({s['total_lost']} lost)",
                f"🔪 Harvest Log & Actual Feed Override ({s['harvested_head']}/{s['surviving_quantity']} head)",
                f"💵 Sales & Home Freezer Log ({s['sold_lbs']:,.1f} lbs sold · {s['freezer_lbs']:,.1f} lbs freezer)",
            ]
        )

        with subtab_losses:
            st.markdown(
                f"#### 📉 Log / Manage Batch Losses — Surviving: **{s['surviving_quantity']} / {s['initial_quantity']}** "
                f"*(Lost: {s['total_lost']} head · Feed Reduced: {s['feed_saved_by_losses']:,.1f} lbs)*"
            )
            loss_col_form, loss_col_list = st.columns([1, 1])

            with loss_col_form:
                with st.container(border=True):
                    if s["surviving_quantity"] > 0:
                        default_loss_date = min(
                            max(date.today(), s["start_date"]), s["proc_date"]
                        )
                        with st.form(
                            f"loss_form_{b['id']}", clear_on_submit=True
                        ):
                            lf_c1, lf_c2 = st.columns(2)
                            loss_dt_input = lf_c1.date_input(
                                "Date of Loss",
                                value=default_loss_date,
                                min_value=s["start_date"],
                                max_value=s["proc_date"],
                                key=f"loss_dt_{b['id']}",
                            )
                            loss_cnt_input = lf_c2.number_input(
                                "Quantity Lost",
                                min_value=1,
                                max_value=max(1, s["surviving_quantity"]),
                                value=1,
                                step=1,
                                key=f"loss_cnt_{b['id']}",
                            )
                            loss_reason_input = st.text_input(
                                "Reason / Stage (optional)",
                                placeholder="e.g. Unhatched egg, brooder loss, predator...",
                                key=f"loss_reason_{b['id']}",
                            )
                            if st.form_submit_button(
                                "➕ Record Loss & Recalculate Feed",
                                use_container_width=True,
                            ):
                                resolved_loss_dt = (
                                    loss_dt_input[0]
                                    if isinstance(loss_dt_input, (list, tuple))
                                    else loss_dt_input
                                )
                                add_batch_loss(
                                    b["id"],
                                    resolved_loss_dt.isoformat(),
                                    min(
                                        int(loss_cnt_input),
                                        s["surviving_quantity"],
                                    ),
                                    loss_reason_input,
                                )
                                st.rerun()
                    else:
                        st.warning(
                            "All animals in this batch have been recorded as lost."
                        )

            with loss_col_list:
                with st.container(border=True):
                    if s["losses"]:
                        st.markdown("**Recorded Losses:**")
                        for l_entry in s["losses"]:
                            l_d = l_entry["loss_date"]
                            if l_d < s["proc_base"]:
                                stage_tag = s["gestation_label"]
                            elif l_d < s["transition_date"]:
                                stage_tag = "Starter Phase"
                            else:
                                stage_tag = "Grower/Finisher Phase"

                            row_c1, row_c2 = st.columns([4, 1])
                            reason_txt = (
                                f" — *{l_entry['reason']}*"
                                if l_entry["reason"]
                                else ""
                            )
                            row_c1.markdown(
                                f"- **{l_entry['loss_date_str']}** ({stage_tag}): "
                                f"**-{l_entry['count']} head**{reason_txt}"
                            )
                            if row_c2.button(
                                "🗑️",
                                key=f"del_loss_{l_entry['id']}",
                                help="Remove this loss entry",
                            ):
                                delete_batch_loss(l_entry["id"])
                                st.rerun()
                    else:
                        st.caption(
                            "No losses recorded for this batch. Recording a loss adjusts feed demand from that date forward."
                        )

        with subtab_harvest:
            st.markdown(
                f"#### 🔪 Actual Harvest & Butcher Log — Harvested: **{s['harvested_head']} / {s['surviving_quantity']} head** "
                f"*(Actual Dressed Yield: **{s['actual_dressed_lbs']:,.1f} lbs** vs. Est. {s['expected_meat_lbs']:,.1f} lbs)*"
            )
            harv_col_form, harv_col_list = st.columns([1, 1])

            with harv_col_form:
                with st.container(border=True):
                    if s["unharvested_surviving_head"] > 0:
                        default_harv_date = max(date.today(), s["proc_base"])
                        default_head_to_harv = s["unharvested_surviving_head"]
                        default_dressed = round(
                            default_head_to_harv * s["expected_yield_per_head"],
                            1,
                        )
                        default_proc_total = round(
                            default_head_to_harv * s["processing_cost"], 2
                        )
                        with st.form(
                            f"harvest_form_{b['id']}", clear_on_submit=True
                        ):
                            hf_c1, hf_c2 = st.columns(2)
                            harv_dt_input = hf_c1.date_input(
                                "Harvest / Butcher Date",
                                value=default_harv_date,
                                key=f"harv_dt_{b['id']}",
                            )
                            harv_head_input = hf_c2.number_input(
                                "Head Harvested",
                                min_value=1,
                                max_value=max(
                                    1, s["unharvested_surviving_head"]
                                ),
                                value=default_head_to_harv,
                                step=1,
                                key=f"harv_head_{b['id']}",
                            )
                            hf_c3, hf_c4 = st.columns(2)
                            harv_lbs_input = hf_c3.number_input(
                                "Total Dressed Hanging/Packaged Weight (lbs)",
                                min_value=0.0,
                                max_value=50000.0,
                                value=float(default_dressed),
                                step=1.0,
                                key=f"harv_lbs_{b['id']}",
                            )
                            harv_proc_input = hf_c4.number_input(
                                "Total Butcher / Processing Fee ($)",
                                min_value=0.0,
                                max_value=50000.0,
                                value=float(default_proc_total),
                                step=5.0,
                                key=f"harv_proc_{b['id']}",
                            )
                            harv_notes_input = st.text_input(
                                "Carcass / Butcher Notes (optional)",
                                placeholder="e.g. Barrow #1 hanging weight, processor invoice #...",
                                key=f"harv_notes_{b['id']}",
                            )
                            if st.form_submit_button(
                                "➕ Log Harvest Event",
                                use_container_width=True,
                            ):
                                resolved_harv_dt = (
                                    harv_dt_input[0]
                                    if isinstance(harv_dt_input, (list, tuple))
                                    else harv_dt_input
                                )
                                add_batch_harvest(
                                    b["id"],
                                    resolved_harv_dt.isoformat(),
                                    min(
                                        int(harv_head_input),
                                        s["unharvested_surviving_head"],
                                    ),
                                    float(harv_lbs_input),
                                    float(harv_proc_input),
                                    harv_notes_input,
                                )
                                st.rerun()
                    else:
                        st.success(
                            "✅ All surviving animals in this batch have been logged as harvested."
                        )

            with harv_col_list:
                with st.container(border=True):
                    if s["harvests"]:
                        st.markdown("**Recorded Harvest Events:**")
                        for h_entry in s["harvests"]:
                            avg_h = (
                                h_entry["dressed_lbs"] / h_entry["head_count"]
                                if h_entry["head_count"] > 0
                                else 0.0
                            )
                            h_note = (
                                f" — *{h_entry['notes']}*"
                                if h_entry["notes"]
                                else ""
                            )
                            hc1, hc2 = st.columns([4, 1])
                            hc1.markdown(
                                f"- **{h_entry['harvest_date_str']}**: **{h_entry['head_count']} head** → "
                                f"**{h_entry['dressed_lbs']:,.1f} lbs** ({avg_h:,.1f} lbs/head) | "
                                f"Fee: **${h_entry['processing_cost_total']:,.2f}**{h_note}"
                            )
                            if hc2.button(
                                "🗑️",
                                key=f"del_harv_{h_entry['id']}",
                                help="Remove this harvest entry",
                            ):
                                delete_batch_harvest(h_entry["id"])
                                st.rerun()
                    else:
                        st.caption(
                            "No harvest events logged yet. Logging an early or split harvest also reduces remaining feed demand for harvested animals after their harvest date."
                        )

                with st.container(border=True):
                    st.markdown(
                        "**⚖️ Actual Feed Used Override (End-of-Batch Reconciliation)**"
                    )
                    st.caption(
                        f"Calculated feed (adjusted for losses & harvest dates): **{s['total_feed']:,.1f} lbs** / **${s['total_feed_cost_bags']:,.2f}** ({s['total_bags']} bags)."
                    )
                    with st.form(
                        f"actual_feed_form_{b['id']}", clear_on_submit=False
                    ):
                        af_c1, af_c2 = st.columns(2)
                        af_lbs_val = af_c1.number_input(
                            "Actual Total Feed Used (lbs)",
                            min_value=0.0,
                            max_value=500000.0,
                            value=float(s["reconciled_feed_lbs"]),
                            step=10.0,
                            key=f"af_lbs_{b['id']}",
                        )
                        af_cost_val = af_c2.number_input(
                            "Actual Total Feed Cost ($)",
                            min_value=0.0,
                            max_value=500000.0,
                            value=float(s["reconciled_feed_cost"]),
                            step=5.0,
                            key=f"af_cost_{b['id']}",
                        )
                        af_btn1, af_btn2 = st.columns(2)
                        save_af = af_btn1.form_submit_button(
                            "💾 Save Actual Feed Override",
                            use_container_width=True,
                        )
                        clear_af = af_btn2.form_submit_button(
                            "↩️ Reset to Calculated Feed",
                            use_container_width=True,
                        )
                        if save_af:
                            set_batch_actual_feed(
                                b["id"], float(af_lbs_val), float(af_cost_val)
                            )
                            st.rerun()
                        if clear_af:
                            set_batch_actual_feed(b["id"], None, None)
                            st.rerun()

        with subtab_sales:
            if s["effective_freezer_cost_per_lb"] is None:
                eff_freezer_badge = (
                    "Log Home Freezer lbs to calculate effective $/lb"
                )
            elif s["effective_freezer_cost_per_lb"] == 0.0:
                eff_freezer_badge = "🎉 **$0.00/lb (Free Meat! Customer sales covered 100% of batch costs)**"
            else:
                eff_freezer_badge = (
                    f"**${s['effective_freezer_cost_per_lb']:,.2f}/lb** "
                    f"*(after offsetting ${s['reconciled_total_cost']:,.2f} cost with ${s['actual_sales_revenue']:,.2f} sales)*"
                )

            st.markdown(
                f"#### 💵 Customer Sales & Home Freezer Allocation\n"
                f"- **Harvested Dressed Meat:** **{s['actual_dressed_lbs']:,.1f} lbs** "
                f"(Allocated: **{s['allocated_lbs']:,.1f} lbs** · Unallocated: **{s['unallocated_harvested_lbs']:,.1f} lbs**)\n"
                f"- **Customer Sales:** **{s['sold_lbs']:,.1f} lbs** → **${s['actual_sales_revenue']:,.2f}** cash revenue "
                f"(Net Cash vs. Reconciled Cost: **${s['actual_cash_profit']:,.2f}**)\n"
                f"- **Home Freezer Allocation:** **{s['freezer_lbs']:,.1f} lbs** "
                f"(Imputed value @ ${s['sale_price_per_lb']:,.2f}/lb: **${s['imputed_freezer_value']:,.2f}**)\n"
                f"- **Effective Cost of Home Freezer Meat:** {eff_freezer_badge}"
            )

            sale_col_form, sale_col_list = st.columns([1, 1])
            with sale_col_form:
                with st.container(border=True):
                    default_sale_wt = (
                        s["unallocated_harvested_lbs"]
                        if s["unallocated_harvested_lbs"] > 0
                        else 10.0
                    )
                    default_sale_rev = round(
                        default_sale_wt * s["sale_price_per_lb"], 2
                    )
                    with st.form(
                        f"sale_form_{b['id']}", clear_on_submit=True
                    ):
                        sf_c1, sf_c2 = st.columns(2)
                        sale_dt_input = sf_c1.date_input(
                            "Date",
                            value=date.today(),
                            key=f"sale_dt_{b['id']}",
                        )
                        sale_type_input = sf_c2.selectbox(
                            "Allocation Type",
                            options=["Customer Sale", "Home Freezer"],
                            key=f"sale_type_{b['id']}",
                        )
                        sf_c3, sf_c4 = st.columns(2)
                        sale_wt_input = sf_c3.number_input(
                            "Weight (lbs)",
                            min_value=0.1,
                            max_value=50000.0,
                            value=float(max(0.1, default_sale_wt)),
                            step=1.0,
                            key=f"sale_wt_{b['id']}",
                        )
                        sale_rev_input = sf_c4.number_input(
                            "Total Sale Revenue ($) — ignored for Home Freezer",
                            min_value=0.0,
                            max_value=500000.0,
                            value=float(default_sale_rev),
                            step=5.0,
                            key=f"sale_rev_{b['id']}",
                        )
                        sale_notes_input = st.text_input(
                            "Customer / Cut / Freezer Notes (optional)",
                            placeholder="e.g. Half hog to neighbor, 10 whole broilers, chest freezer #1...",
                            key=f"sale_notes_{b['id']}",
                        )
                        if st.form_submit_button(
                            "➕ Log Sale / Freezer Allocation",
                            use_container_width=True,
                        ):
                            resolved_sale_dt = (
                                sale_dt_input[0]
                                if isinstance(sale_dt_input, (list, tuple))
                                else sale_dt_input
                            )
                            add_batch_sale(
                                b["id"],
                                resolved_sale_dt.isoformat(),
                                sale_type_input,
                                float(sale_wt_input),
                                (
                                    0.0
                                    if sale_type_input == "Home Freezer"
                                    else float(sale_rev_input)
                                ),
                                sale_notes_input,
                            )
                            st.rerun()

            with sale_col_list:
                with st.container(border=True):
                    if s["sales"]:
                        st.markdown("**Recorded Sales & Freezer Entries:**")
                        for sl_entry in s["sales"]:
                            sl_note = (
                                f" — *{sl_entry['notes']}*"
                                if sl_entry["notes"]
                                else ""
                            )
                            if sl_entry["allocation_type"] == "Home Freezer":
                                badge = "❄️ Home Freezer"
                                price_info = f"(Imputed value: ${sl_entry['weight_lbs'] * s['sale_price_per_lb']:,.2f})"
                            else:
                                badge = "💵 Customer Sale"
                                realized_per_lb = (
                                    sl_entry["revenue"] / sl_entry["weight_lbs"]
                                    if sl_entry["weight_lbs"] > 0
                                    else 0.0
                                )
                                price_info = f"→ **${sl_entry['revenue']:,.2f}** (${realized_per_lb:,.2f}/lb)"

                            sc1, sc2 = st.columns([4, 1])
                            sc1.markdown(
                                f"- **{sl_entry['entry_date_str']}** [{badge}]: "
                                f"**{sl_entry['weight_lbs']:,.1f} lbs** {price_info}{sl_note}"
                            )
                            if sc2.button(
                                "🗑️",
                                key=f"del_sale_{sl_entry['id']}",
                                help="Remove this sale/freezer entry",
                            ):
                                delete_batch_sale(sl_entry["id"])
                                st.rerun()
                    else:
                        st.caption(
                            "No customer sales or home freezer allocations logged yet."
                        )

    # =======================================================================
    # WORKSPACE 3: 🌾 Feed & Storage Schedule
    # =======================================================================
    elif active_workspace == "🌾 Feed & Storage Schedule":
        st.subheader("🌾 Phased Feed Burn & Barrel Storage Schedule")
        if weekly_df.empty:
            st.warning(
                "No active batches match the current filters to compute a feed schedule."
            )
        else:
            fs1, fs2, fs3, fs4 = st.columns(4)
            with fs1:
                with st.container(border=True):
                    st.metric(
                        "🌾 Total Active Feed",
                        f"{combined_feed_lbs:,.0f} lbs",
                        delta=f"{combined_bags} × {bag_weight}lb bags",
                        delta_color="off",
                    )
            with fs2:
                with st.container(border=True):
                    st.metric(
                        "🔥 Peak Weekly Burn",
                        f"{feed_stats['peak_weekly_lbs']:,.1f} lbs/wk",
                        delta="Across overlapping batches",
                        delta_color="off",
                    )
            with fs3:
                with st.container(border=True):
                    st.metric(
                        "🛢️ Peak Concurrent Barrels",
                        f"{feed_stats['peak_concurrent_barrels']} barrel(s)",
                        delta=f"Peak {feed_stats['peak_concurrent_lbs']:,.0f} lbs active",
                        delta_color="off",
                    )
            with fs4:
                with st.container(border=True):
                    st.metric(
                        "📉 Feed Saved by Losses",
                        f"{combined_feed_saved:,.1f} lbs",
                        delta=f"{total_lost_head} total loss(es) logged",
                        delta_color="off",
                    )

            # Side-by-Side Charts
            chart_col1, chart_col2 = st.columns(2)
            with chart_col1:
                with st.container(border=True):
                    st.markdown("##### 🌾 Weekly Feed Burn (Starter vs. Grower)")
                    st.caption(
                        "Weekly feed consumption (lbs/wk) split by Starter vs. Grower/Finisher ration."
                    )
                    ration_chart_df = weekly_df.set_index("Week Starting")[
                        ["Starter Feed (lbs)", "Grower/Finisher Feed (lbs)"]
                    ]
                    st.bar_chart(ration_chart_df, height=320)

            with chart_col2:
                with st.container(border=True):
                    st.markdown("##### 🛢️ Concurrent Barrel Storage Demand")
                    st.caption(
                        f"Number of {barrel_capacity}lb barrels required to store remaining active feed in each week."
                    )
                    barrel_chart_df = weekly_df.set_index("Week Starting")[
                        ["Concurrent Barrels Needed (Remaining Active)"]
                    ]
                    st.area_chart(barrel_chart_df, height=320)

            # Formatted Monthly & Weekly Tables
            st.markdown("##### 📅 Monthly Feed & Barrel Summary")
            st.dataframe(
                monthly_df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Starter Feed (lbs)": st.column_config.NumberColumn(
                        format="%.1f lbs"
                    ),
                    "Grower/Finisher Feed (lbs)": st.column_config.NumberColumn(
                        format="%.1f lbs"
                    ),
                    "Monthly Feed Consumed (lbs)": st.column_config.NumberColumn(
                        format="%.1f lbs"
                    ),
                    "Barrels Consumed in Month": st.column_config.NumberColumn(
                        format="%.2f barrels"
                    ),
                },
            )

            st.markdown("##### 📆 Weekly Feed Breakdown by Batch")
            st.dataframe(
                weekly_df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Starter Feed (lbs)": st.column_config.NumberColumn(
                        format="%.1f lbs"
                    ),
                    "Grower/Finisher Feed (lbs)": st.column_config.NumberColumn(
                        format="%.1f lbs"
                    ),
                    "Total Weekly Feed (lbs)": st.column_config.NumberColumn(
                        format="%.1f lbs"
                    ),
                    "Remaining Active Batch Feed (lbs)": st.column_config.NumberColumn(
                        format="%.1f lbs"
                    ),
                },
            )

    # =======================================================================
    # WORKSPACE 4: 💰 Financials & Reconciliation
    # =======================================================================
    elif active_workspace == "💰 Financials & Reconciliation":
        st.subheader("💰 Financials, ROI & Harvest Reconciliation")
        if not filtered_active_schedules:
            st.warning(
                "No active batches match the current filters to compute financials."
            )
        else:
            fin_tab_reconcile, fin_tab_roi, fin_tab_purchase = st.tabs(
                [
                    "🥩 Harvest, Freezer & Sales Reconciliation",
                    "💰 Estimated Cost, Yield & $/lb ROI",
                    "🛒 Purchasing & Cash-Flow Schedule",
                ]
            )

            with fin_tab_reconcile:
                st.caption(
                    "Compare estimated projections against actual butcher harvest weights, actual feed/butcher costs, customer sales revenue, and home freezer allocation."
                )
                rc1, rc2, rc3, rc4, rc5 = st.columns(5)
                with rc1:
                    with st.container(border=True):
                        st.metric(
                            "Actual Harvested Yield",
                            f"{tot_actual_dressed:,.1f} lbs",
                            delta=f"{tot_harvested_head} / {total_surviving_head} head harvested",
                            delta_color="off",
                        )
                with rc2:
                    with st.container(border=True):
                        st.metric(
                            "Reconciled Batch Cost",
                            f"${tot_reconciled_cost:,.2f}",
                            delta=f"Est: ${combined_budget_cost:,.2f}",
                            delta_color="off",
                        )
                with rc3:
                    with st.container(border=True):
                        st.metric(
                            "Customer Sales Revenue",
                            f"${tot_sales_rev:,.2f}",
                            delta=f"{tot_sold_lbs:,.1f} lbs sold",
                            delta_color="off",
                        )
                with rc4:
                    with st.container(border=True):
                        st.metric(
                            "Home Freezer Meat",
                            f"{tot_freezer_lbs:,.1f} lbs",
                            delta=(
                                "🎉 $0.00/lb (Free Meat!)"
                                if tot_eff_freezer_per_lb == 0.0
                                else (
                                    f"Effective: ${tot_eff_freezer_per_lb:,.2f}/lb"
                                    if tot_eff_freezer_per_lb is not None
                                    else "Log freezer lbs in Batch Manager"
                                )
                            ),
                            delta_color="off",
                        )
                with rc5:
                    with st.container(border=True):
                        st.metric(
                            "Actual Net Cash Position",
                            f"${tot_cash_net:,.2f}",
                            delta="Customer sales minus total cost",
                            delta_color="normal",
                        )

                st.dataframe(
                    reconciliation_df,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Est. Meat (lbs)": st.column_config.NumberColumn(
                            format="%.1f lbs"
                        ),
                        "Actual Harvested (lbs)": st.column_config.NumberColumn(
                            format="%.1f lbs"
                        ),
                        "Est. Cost ($)": st.column_config.NumberColumn(
                            format="$%.2f"
                        ),
                        "Reconciled Cost ($)": st.column_config.NumberColumn(
                            format="$%.2f"
                        ),
                        "Reconciled $/lb": st.column_config.NumberColumn(
                            format="$%.2f"
                        ),
                        "Sold (lbs)": st.column_config.NumberColumn(
                            format="%.1f lbs"
                        ),
                        "Cash Sales ($)": st.column_config.NumberColumn(
                            format="$%.2f"
                        ),
                        "Home Freezer (lbs)": st.column_config.NumberColumn(
                            format="%.1f lbs"
                        ),
                        "Unallocated (lbs)": st.column_config.NumberColumn(
                            format="%.1f lbs"
                        ),
                        "Cash Net ($)": st.column_config.NumberColumn(
                            format="$%.2f"
                        ),
                        "Total Realized Profit ($)": st.column_config.NumberColumn(
                            format="$%.2f"
                        ),
                    },
                )

            with fin_tab_roi:
                st.caption(
                    "Full cost breakdown (Acquisition + Whole-Bag Feed Cost + Processing) vs. expected dressed meat yield and $/lb ROI."
                )
                roi_c1, roi_c2, roi_c3, roi_c4 = st.columns(4)
                with roi_c1:
                    with st.container(border=True):
                        st.metric(
                            "Planned Total Cost",
                            f"${combined_budget_cost:,.2f}",
                            delta=f"${combined_feed_cost:,.2f} in whole feed bags",
                            delta_color="off",
                        )
                with roi_c2:
                    with st.container(border=True):
                        st.metric(
                            "Expected Meat Yield",
                            f"{combined_meat_lbs:,.1f} lbs",
                            delta=f"Cost: ${blended_cost_per_lb:,.2f}/lb",
                            delta_color="off",
                        )
                with roi_c3:
                    with st.container(border=True):
                        st.metric(
                            "Estimated Gross Revenue",
                            f"${combined_revenue:,.2f}",
                            delta=f"Avg Sale: ${blended_sale_per_lb:,.2f}/lb",
                            delta_color="off",
                        )
                with roi_c4:
                    with st.container(border=True):
                        st.metric(
                            "Estimated Net Profit",
                            f"${combined_profit:,.2f}",
                            delta=f"{'+' if blended_profit_per_lb >= 0 else ''}${blended_profit_per_lb:,.2f}/lb net",
                            delta_color="normal",
                        )

                st.dataframe(
                    roi_df,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Acquisition ($)": st.column_config.NumberColumn(
                            format="$%.2f"
                        ),
                        "Feed Cost - Bags ($)": st.column_config.NumberColumn(
                            format="$%.2f"
                        ),
                        "Feed Cost - Exact ($)": st.column_config.NumberColumn(
                            format="$%.2f"
                        ),
                        "Processing ($)": st.column_config.NumberColumn(
                            format="$%.2f"
                        ),
                        "Total Cost ($)": st.column_config.NumberColumn(
                            format="$%.2f"
                        ),
                        "Expected Meat (lbs)": st.column_config.NumberColumn(
                            format="%.1f lbs"
                        ),
                        "Feed $/lb Meat": st.column_config.NumberColumn(
                            format="$%.2f"
                        ),
                        "Total $/lb Meat": st.column_config.NumberColumn(
                            format="$%.2f"
                        ),
                        "Sale $/lb": st.column_config.NumberColumn(
                            format="$%.2f"
                        ),
                        "Est. Revenue ($)": st.column_config.NumberColumn(
                            format="$%.2f"
                        ),
                        "Est. Profit ($)": st.column_config.NumberColumn(
                            format="$%.2f"
                        ),
                        "Profit $/lb": st.column_config.NumberColumn(
                            format="$%.2f"
                        ),
                    },
                )

            with fin_tab_purchase:
                st.caption(
                    f"Time-phased purchasing and cash-flow schedule using whole {bag_weight}lb feed bags (`ceil(lbs / {bag_weight})`)."
                )
                if not monthly_cash_df.empty:
                    p_col1, p_col2 = st.columns([1, 2])
                    with p_col1:
                        st.markdown("**Monthly Outlay Summary**")
                        st.dataframe(
                            monthly_cash_df,
                            use_container_width=True,
                            hide_index=True,
                            column_config={
                                "Feed Purchased (lbs)": st.column_config.NumberColumn(
                                    format="%.1f lbs"
                                ),
                                "Total Monthly Outlay ($)": st.column_config.NumberColumn(
                                    format="$%.2f"
                                ),
                            },
                        )
                    with p_col2:
                        st.markdown(
                            "**Chronological Purchase & Milestone Schedule**"
                        )
                        st.dataframe(
                            purchasing_events_df,
                            use_container_width=True,
                            hide_index=True,
                            column_config={
                                "Feed (lbs)": st.column_config.NumberColumn(
                                    format="%.1f lbs"
                                ),
                                "Cost ($)": st.column_config.NumberColumn(
                                    format="$%.2f"
                                ),
                            },
                        )
                else:
                    st.info("No purchasing events scheduled.")

    # =======================================================================
    # WORKSPACE 5: ⚙️ Settings & Backup
    # =======================================================================
    elif active_workspace == "⚙️ Settings & Backup":
        st.subheader("⚙️ Global Storage Settings & JSON Herd Backup")
        set_col1, set_col2 = st.columns(2)

        with set_col1:
            with st.container(border=True):
                st.markdown("#### 🛢️ Storage & Feed Bag Settings")
                st.caption(
                    "Adjust your homestead's standard storage barrel capacity and feed bag weight. "
                    "All bag counts (`ceil(lbs / bag_weight)`) and barrel storage curves update automatically."
                )
                s_col1, s_col2 = st.columns(2)
                new_barrel_cap = s_col1.number_input(
                    "Barrel Size (lbs)",
                    min_value=10,
                    max_value=5000,
                    value=barrel_capacity,
                    step=25,
                )
                new_bag_weight = s_col2.number_input(
                    "Feed Bag Weight (lbs)",
                    min_value=5,
                    max_value=2000,
                    value=bag_weight,
                    step=5,
                )
                if int(new_barrel_cap) != barrel_capacity:
                    set_barrel_capacity(int(new_barrel_cap))
                    st.rerun()
                if int(new_bag_weight) != bag_weight:
                    set_bag_weight(int(new_bag_weight))
                    st.rerun()

        with set_col2:
            with st.container(border=True):
                st.markdown("#### 💾 JSON Herd Backup & Restore")
                st.caption(
                    "Export your entire SQLite database (batches, losses, harvest logs, sales/freezer entries, and settings) to a portable JSON file or restore from backup."
                )
                backup_str = export_backup_json()
                st.download_button(
                    "⬇️ Download Herd Backup (.json)",
                    data=backup_str,
                    file_name=f"homestead_plan_{date.today().isoformat()}.json",
                    mime="application/json",
                    use_container_width=True,
                )
                st.divider()
                uploaded_file = st.file_uploader(
                    "Restore from JSON", type=["json"]
                )
                import_mode = st.radio(
                    "Import Mode",
                    ["Merge with existing batches", "Replace existing batches"],
                    index=0,
                )
                if uploaded_file is not None and st.button(
                    "📥 Import JSON Backup",
                    type="primary",
                    use_container_width=True,
                ):
                    try:
                        raw_content = uploaded_file.read().decode("utf-8")
                        imported_n = import_backup_json(
                            raw_content,
                            replace_existing=(
                                import_mode == "Replace existing batches"
                            ),
                        )
                        st.success(
                            f"Imported {imported_n} batch(es) successfully!"
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Failed to import backup: {exc}")


if os.environ.get("FARM_APP_TESTING") != "1":
    render_app()
