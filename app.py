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

from streamlit_timeline import st_timeline

# Livestock Data Dictionary (Default Species Presets)
# days_to_birth: Gestation or Incubation days
# starter_weeks / starter_feed_lbs: Starter phase duration (weeks) & feed per animal (lbs)
# grower_weeks / grower_feed_lbs: Grower/Finisher phase duration (weeks) & feed per animal (lbs)
# starter_bag_price / grower_bag_price: Default price ($) per feed bag
# unit_cost: Default acquisition cost ($) per egg/chick/poult/piglet
# processing_cost: Default butcher/processing cost ($) per harvested animal
# expected_yield_lbs: Default dressed/packaged meat yield (lbs) per harvested animal
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
                active INTEGER NOT NULL DEFAULT 1,
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        # Migrate older databases if phased-feed / cost columns are missing
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
        ]
        for col_name, col_type in new_columns:
            if col_name not in existing_cols:
                conn.execute(
                    f"ALTER TABLE batches ADD COLUMN {col_name} {col_type}"
                )

        # Backfill any rows that were created before phased-feed columns existed
        legacy_rows = conn.execute(
            "SELECT * FROM batches WHERE starter_weeks IS NULL OR starter_feed_lbs IS NULL"
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
                    expected_yield_lbs = COALESCE(expected_yield_lbs, ?)
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
                    processing_cost = ?, expected_yield_lbs = ?, active = ?, notes = ?
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
                    processing_cost, expected_yield_lbs, active, notes, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            "DELETE FROM batch_losses WHERE batch_id = ?", (int(batch_id),)
        )
        conn.execute("DELETE FROM batches WHERE id = ?", (int(batch_id),))
        conn.commit()


def export_backup_json() -> str:
    batches = list_batches()
    losses_by_batch: dict[int, list[dict]] = {}
    for l in list_batch_losses():
        losses_by_batch.setdefault(int(l["batch_id"]), []).append(l)

    enriched_batches = []
    for b in batches:
        b_copy = dict(b)
        b_copy["losses"] = losses_by_batch.get(int(b["id"]), [])
        enriched_batches.append(b_copy)

    payload = {
        "version": 2,
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
            conn.execute("DELETE FROM batch_losses")
            conn.execute("DELETE FROM batches")
        count = 0
        for item in batches_to_import:
            norm = _normalize_batch_payload(item)
            cursor = conn.execute(
                """
                INSERT INTO batches (
                    name, species, quantity, start_date, end_date,
                    days_to_birth, weeks_to_proc, feed_total,
                    starter_weeks, starter_feed_lbs, grower_weeks, grower_feed_lbs,
                    starter_bag_price, grower_bag_price, unit_cost,
                    processing_cost, expected_yield_lbs, active, notes, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    batch_id = int(batch["id"])
    if losses is None:
        losses = list_batch_losses(batch_id)

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

    def surviving_on_date(d: date) -> int:
        lost_so_far = sum(
            item["count"] for item in parsed_losses if item["loss_date"] <= d
        )
        return max(0, initial_quantity - lost_so_far)

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

    # Day-by-day feed consumption adjusted for losses
    starter_days = max(0, (transition_date - proc_base).days)
    daily_starter_per_head = (
        starter_feed_per_animal / starter_days if starter_days > 0 else 0.0
    )
    starter_feed_actual = 0.0
    for day_offset in range(starter_days):
        d = proc_base + timedelta(days=day_offset)
        starter_feed_actual += surviving_on_date(d) * daily_starter_per_head
    starter_feed_actual = round(starter_feed_actual, 2)
    starter_feed_planned = round(
        initial_quantity * starter_feed_per_animal, 2
    )

    grower_days = max(0, (proc_date - transition_date).days)
    daily_grower_per_head = (
        grower_feed_per_animal / grower_days if grower_days > 0 else 0.0
    )
    grower_feed_actual = 0.0
    for day_offset in range(grower_days):
        d = transition_date + timedelta(days=day_offset)
        grower_feed_actual += surviving_on_date(d) * daily_grower_per_head
    grower_feed_actual = round(grower_feed_actual, 2)
    grower_feed_planned = round(initial_quantity * grower_feed_per_animal, 2)

    total_feed_planned = round(starter_feed_planned + grower_feed_planned, 2)
    total_feed = round(starter_feed_actual + grower_feed_actual, 2)
    feed_saved_by_losses = round(
        max(0.0, total_feed_planned - total_feed), 2
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

    # Budget & ROI Calculations
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

    # Phase 4: Processing Timeline Item
    timeline_items.append(
        {
            "id": f"b{batch_id}_proc",
            "group": batch_id,
            "content": f"🔪 Processing ({surviving_quantity} head)",
            "start": proc_date.isoformat(),
            "style": "background-color: #fee2e2; border-color: #dc2626; color: #7f1d1d;",
            "title": (
                f"{batch['name']} — Processing Date: {proc_date.isoformat()} | "
                f"Harvesting {surviving_quantity}/{initial_quantity} head (~{expected_meat_lbs:,.1f} lbs meat)"
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
        "initial_quantity": initial_quantity,
        "total_lost": total_lost,
        "surviving_quantity": surviving_quantity,
        "surviving_on_date": surviving_on_date,
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
        "timeline_items": timeline_items,
    }


def _batch_feed_on_day(s: dict, d: date) -> tuple[float, float]:
    """Return (starter_lbs, grower_lbs) consumed by schedule `s` on day `d`, adjusted for losses."""
    if s["proc_base"] <= d < s["transition_date"] and s["starter_weeks"] > 0:
        return s["surviving_on_date"](d) * s["daily_starter_per_head"], 0.0
    if s["transition_date"] <= d < s["proc_date"] and s["grower_weeks"] > 0:
        return 0.0, s["surviving_on_date"](d) * s["daily_grower_per_head"]
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
# Streamlit UI Application
# ---------------------------------------------------------------------------
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
        "form_active": True,
        "form_notes": "",
    }
    for k, v in default_map.items():
        if k not in st.session_state:
            st.session_state[k] = v


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

    # -----------------------------------------------------------------------
    # Sidebar: Add / Edit Batch, Global Storage & Bag Settings, JSON Backup
    # -----------------------------------------------------------------------
    with st.sidebar:
        st.header(
            "✏️ Edit Livestock Batch"
            if st.session_state.editing_batch_id is not None
            else "➕ Add Livestock Batch"
        )

        order_type = st.selectbox(
            "Select Livestock Type:",
            list(LIVESTOCK_DATA.keys()),
            key="form_species",
            on_change=on_species_change,
            format_func=lambda s: s.title(),
        )

        date_label = (
            "Expected Breeding Date (or Range)"
            if "pigs" in order_type
            else "Arrival Date (or Range)"
        )

        today = date.today()

        with st.form("batch_form", clear_on_submit=False):
            batch_name = st.text_input(
                "Batch Name (optional)",
                key="form_name",
                placeholder=f"e.g. Spring {order_type.title()}",
            )

            selected_dates = st.date_input(
                date_label,
                key="form_dates",
                help="Select either a single date (click same date twice) or a start and end date range. Past and future dates are supported.",
            )

            quantity = st.number_input(
                "Initial Quantity (head / eggs)",
                min_value=1,
                key="form_quantity",
                step=1,
            )

            with st.expander(
                "🌾 Phased Feed & Lifecycle Overrides", expanded=False
            ):
                days_to_birth = st.number_input(
                    "Gestation / Incubation (days)",
                    min_value=0,
                    max_value=400,
                    key="form_days_to_birth",
                    step=1,
                )
                st_c1, st_c2 = st.columns(2)
                starter_weeks = st_c1.number_input(
                    "Starter Phase (wks)",
                    min_value=0,
                    max_value=104,
                    key="form_starter_weeks",
                    step=1,
                )
                starter_feed_lbs = st_c2.number_input(
                    "Starter Feed/Head (lbs)",
                    min_value=0.0,
                    max_value=5000.0,
                    key="form_starter_feed_lbs",
                    step=1.0,
                )

                gr_c1, gr_c2 = st.columns(2)
                grower_weeks = gr_c1.number_input(
                    "Grower/Finisher (wks)",
                    min_value=0,
                    max_value=260,
                    key="form_grower_weeks",
                    step=1,
                )
                grower_feed_lbs = gr_c2.number_input(
                    "Grower Feed/Head (lbs)",
                    min_value=0.0,
                    max_value=10000.0,
                    key="form_grower_feed_lbs",
                    step=5.0,
                )
                if "pigs" in order_type:
                    st.caption(
                        "*Note: Kunekune feed needs vary based on pasture quality.*"
                    )

            with st.expander(
                "💰 Cost, Bag Pricing & Meat Yield Overrides", expanded=False
            ):
                p_c1, p_c2 = st.columns(2)
                starter_bag_price = p_c1.number_input(
                    f"Starter Bag (${bag_weight}lb)",
                    min_value=0.0,
                    max_value=500.0,
                    key="form_starter_bag_price",
                    step=1.0,
                )
                grower_bag_price = p_c2.number_input(
                    f"Grower Bag (${bag_weight}lb)",
                    min_value=0.0,
                    max_value=500.0,
                    key="form_grower_bag_price",
                    step=1.0,
                )

                b_c1, b_c2 = st.columns(2)
                unit_cost = b_c1.number_input(
                    "Acquisition $/Unit",
                    min_value=0.0,
                    max_value=5000.0,
                    key="form_unit_cost",
                    step=1.0,
                )
                processing_cost = b_c2.number_input(
                    "Processing $/Head",
                    min_value=0.0,
                    max_value=5000.0,
                    key="form_processing_cost",
                    step=1.0,
                )
                expected_yield_lbs = st.number_input(
                    "Expected Dressed Meat Yield (lbs/head)",
                    min_value=0.0,
                    max_value=2000.0,
                    key="form_expected_yield_lbs",
                    step=0.5,
                )

            notes = st.text_area(
                "Batch Notes (optional)",
                key="form_notes",
                placeholder="Breeder/hatchery source, pasture paddock, butcher info...",
                height=68,
            )

            active_flag = st.checkbox(
                "Include in active herd plan & timeline",
                key="form_active",
            )

            if st.session_state.editing_batch_id is not None:
                btn_col1, btn_col2 = st.columns(2)
                submitted = btn_col1.form_submit_button(
                    "💾 Update Batch", use_container_width=True, type="primary"
                )
                cancel_edit = btn_col2.form_submit_button(
                    "Cancel", use_container_width=True
                )
            else:
                submitted = st.form_submit_button(
                    "➕ Save Batch", use_container_width=True, type="primary"
                )
                cancel_edit = False

        if cancel_edit:
            st.session_state.pending_reset = True
            st.rerun()

        if submitted:
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

            if int(starter_weeks) + int(grower_weeks) < 1:
                grower_weeks = 1

            existing_count = len(list_batches())
            resolved_name = (
                batch_name.strip() or f"{order_type.title()} #{existing_count + 1}"
            )

            save_batch(
                {
                    "name": resolved_name,
                    "species": order_type,
                    "quantity": quantity,
                    "start_date": start_d.isoformat(),
                    "end_date": end_d.isoformat(),
                    "days_to_birth": days_to_birth,
                    "starter_weeks": starter_weeks,
                    "starter_feed_lbs": starter_feed_lbs,
                    "grower_weeks": grower_weeks,
                    "grower_feed_lbs": grower_feed_lbs,
                    "starter_bag_price": starter_bag_price,
                    "grower_bag_price": grower_bag_price,
                    "unit_cost": unit_cost,
                    "processing_cost": processing_cost,
                    "expected_yield_lbs": expected_yield_lbs,
                    "active": active_flag,
                    "notes": notes.strip(),
                },
                batch_id=st.session_state.editing_batch_id,
            )
            st.session_state.pending_reset = True
            st.rerun()

        st.divider()
        st.subheader("🛢️ Storage & Feed Bag Settings")
        s_col1, s_col2 = st.columns(2)
        new_barrel_cap = s_col1.number_input(
            "Barrel Size (lbs)",
            min_value=10,
            max_value=5000,
            value=barrel_capacity,
            step=25,
        )
        new_bag_weight = s_col2.number_input(
            "Feed Bag (lbs)",
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

        st.divider()
        with st.expander("💾 Backup & Restore (JSON)", expanded=False):
            backup_str = export_backup_json()
            st.download_button(
                "⬇️ Download Herd Backup (.json)",
                data=backup_str,
                file_name=f"homestead_plan_{date.today().isoformat()}.json",
                mime="application/json",
                use_container_width=True,
            )
            uploaded_file = st.file_uploader("Restore from JSON", type=["json"])
            import_mode = st.radio(
                "Import Mode",
                ["Merge with existing batches", "Replace existing batches"],
                index=0,
            )
            if uploaded_file is not None and st.button(
                "📥 Import JSON Backup", use_container_width=True
            ):
                try:
                    raw_content = uploaded_file.read().decode("utf-8")
                    imported_n = import_backup_json(
                        raw_content,
                        replace_existing=(import_mode == "Replace existing batches"),
                    )
                    st.success(f"Imported {imported_n} batch(es) successfully!")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Failed to import backup: {exc}")

    # -----------------------------------------------------------------------
    # Main Dashboard: Multi-Batch Overview, Shared Timeline & Feed/Cost/ROI
    # -----------------------------------------------------------------------
    st.title("🚜 Homestead Planner: Poultry & Pigs")
    st.caption(
        "Plan, compare, and persist multiple livestock batches with phased rations, loss tracking, purchasing schedules, and $/lb meat ROI."
    )

    all_batches = list_batches()
    if not all_batches:
        st.info(
            "👋 **No livestock batches saved yet.** Use the **➕ Add Livestock Batch** panel in the sidebar to add your first batch of poultry or pigs!"
        )
        return

    all_losses = list_batch_losses()
    losses_by_batch: dict[int, list[dict]] = {}
    for l_row in all_losses:
        losses_by_batch.setdefault(int(l_row["batch_id"]), []).append(l_row)

    all_schedules = [
        compute_batch_schedule(
            b,
            barrel_capacity=barrel_capacity,
            bag_weight=bag_weight,
            losses=losses_by_batch.get(int(b["id"]), []),
        )
        for b in all_batches
    ]
    active_schedules = [s for s in all_schedules if s["batch"]["active"]]

    # Scenario / Filter Bar + Calendar Export
    top_col1, top_col2 = st.columns([3, 1])
    with top_col1:
        species_present = sorted({b["species"] for b in all_batches})
        selected_species_filter = st.multiselect(
            "Filter Active View by Species",
            options=species_present,
            default=species_present,
            format_func=lambda s: s.title(),
        )
    with top_col2:
        filtered_active_schedules = [
            s
            for s in active_schedules
            if s["batch"]["species"] in selected_species_filter
        ]
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

    weekly_df, monthly_df, feed_stats = compute_combined_feed_schedule(
        filtered_active_schedules, barrel_capacity
    )
    purchasing_events_df, monthly_cash_df = compute_purchasing_schedule(
        filtered_active_schedules, bag_weight
    )
    roi_df = compute_roi_summary_table(filtered_active_schedules)

    # Top Summary Metrics
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
    blended_cost_per_lb = (
        combined_budget_cost / combined_meat_lbs
        if combined_meat_lbs > 0
        else 0.0
    )
    blended_feed_cost_per_lb = (
        combined_feed_cost / combined_meat_lbs if combined_meat_lbs > 0 else 0.0
    )

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric(
        "Active Herd (Surviving)",
        f"{total_surviving_head:,} / {total_initial_head:,}",
        delta=(
            f"-{total_lost_head} lost ({total_active_batches} batches)"
            if total_lost_head > 0
            else f"{total_active_batches} active batch(es)"
        ),
        delta_color="inverse" if total_lost_head > 0 else "off",
    )
    m2.metric(
        "Combined Feed Needed",
        f"{combined_feed_lbs:,.0f} lbs",
        delta=(
            f"-{combined_feed_saved:,.0f} lbs from losses"
            if combined_feed_saved > 0
            else f"{combined_bags} × {bag_weight}lb bags"
        ),
        delta_color="off",
    )
    m3.metric(
        f"Storage ({barrel_capacity}lb Barrels)",
        f"{combined_barrels} total",
        delta=f"Peak {feed_stats['peak_concurrent_barrels']} concurrent",
        delta_color="off",
        help="Total barrels over lifetime vs. peak concurrent barrels needed at once.",
    )
    m4.metric(
        "Estimated Total Budget",
        f"${combined_budget_cost:,.2f}",
        delta=f"${combined_feed_cost:,.2f} in feed",
        delta_color="off",
    )
    m5.metric(
        "Expected Meat Yield",
        f"{combined_meat_lbs:,.1f} lbs",
        delta=f"Across {total_surviving_head:,} harvested head",
        delta_color="off",
    )
    m6.metric(
        "Blended Cost / lb Meat",
        f"${blended_cost_per_lb:,.2f}/lb",
        delta=f"${blended_feed_cost_per_lb:,.2f}/lb feed only",
        delta_color="off",
    )

    if not filtered_active_schedules:
        st.warning(
            "No active batches match the current filter. Enable a batch below or adjust the species filter."
        )
    else:
        # 1. Shared Multi-Batch Swimlane Timeline
        st.subheader("🗓️ Shared Herd Lifecycle Timeline")
        timeline_groups = []
        timeline_items = []
        for s in filtered_active_schedules:
            b = s["batch"]
            loss_badge = (
                f" · ⚠️ -{s['total_lost']}" if s["total_lost"] > 0 else ""
            )
            timeline_groups.append(
                {
                    "id": b["id"],
                    "content": (
                        f"<b>{b['name']}</b><br>"
                        f"<small>{s['surviving_quantity']}/{s['initial_quantity']}x {b['species'].title()}{loss_badge}</small>"
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
            height="350px",
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
                st.info(
                    f"🔎 **Selected Timeline Event:** `{mb['name']}` "
                    f"({matched_schedule['surviving_quantity']}/{matched_schedule['initial_quantity']}x {mb['species'].title()}) — "
                    f"**Starter:** {matched_schedule['proc_base']} → {matched_schedule['transition_date']} "
                    f"({matched_schedule['starter_feed']:,.1f} lbs / {matched_schedule['starter_bags']} bags) | "
                    f"**Grower:** {matched_schedule['transition_date']} → {matched_schedule['proc_date']} "
                    f"({matched_schedule['grower_feed']:,.1f} lbs / {matched_schedule['grower_bags']} bags) | "
                    f"**Budget:** ${matched_schedule['total_budget_cost']:,.2f} "
                    f"(${matched_schedule['total_cost_per_lb_meat']:,.2f}/lb meat)"
                )

        # 2. Phased Feed, Storage, Purchasing & ROI Schedule Tabs
        st.subheader("📊 Phased Feed, Storage, Purchasing & ROI Schedule")
        if not weekly_df.empty:
            tab_feed, tab_storage, tab_purchase, tab_roi = st.tabs(
                [
                    "🌾 Weekly Feed Burn (Starter vs. Grower)",
                    "🛢️ Concurrent Barrel Storage Demand",
                    "🛒 Purchasing & Cash-Flow Schedule",
                    "💰 Cost, Yield & $/lb ROI Breakdown",
                ]
            )

            with tab_feed:
                st.caption(
                    "Weekly feed consumption (lbs/week) split by Starter vs. Grower/Finisher ration and adjusted dynamically as losses occur."
                )
                ration_chart_df = weekly_df.set_index("Week Starting")[
                    ["Starter Feed (lbs)", "Grower/Finisher Feed (lbs)"]
                ]
                st.bar_chart(ration_chart_df, height=300)

                with st.expander("View Weekly Feed Burn by Batch & Monthly Summary"):
                    st.markdown("**Monthly Feed & Storage Summary**")
                    st.dataframe(monthly_df, use_container_width=True, hide_index=True)
                    st.markdown("**Weekly Breakdown**")
                    st.dataframe(weekly_df, use_container_width=True, hide_index=True)

            with tab_storage:
                st.caption(
                    f"Number of {barrel_capacity}lb barrels required to store remaining active feed for overlapping batches in a given week."
                )
                barrel_chart_df = weekly_df.set_index("Week Starting")[
                    ["Concurrent Barrels Needed (Remaining Active)"]
                ]
                st.area_chart(barrel_chart_df, height=300)

            with tab_purchase:
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
                        )
                    with p_col2:
                        st.markdown("**Chronological Purchase & Milestone Schedule**")
                        st.dataframe(
                            purchasing_events_df,
                            use_container_width=True,
                            hide_index=True,
                        )
                else:
                    st.info("No purchasing events scheduled.")

            with tab_roi:
                st.caption(
                    "Full cost breakdown (Acquisition + Whole-Bag Feed Cost + Processing) vs. expected dressed meat yield and $/lb ROI."
                )
                st.dataframe(roi_df, use_container_width=True, hide_index=True)

    # 3. Saved Batches Management, Milestones & Inline Loss Tracking
    st.subheader("📋 Saved Livestock Batches & Loss Tracking")
    for s in all_schedules:
        b = s["batch"]
        status_badge = "🟢 Active" if b["active"] else "⚪ Inactive (Excluded)"
        loss_summary = (
            f" · ⚠️ -{s['total_lost']} lost" if s["total_lost"] > 0 else ""
        )
        header_label = (
            f"{status_badge} | **{b['name']}** — "
            f"{s['surviving_quantity']}/{s['initial_quantity']}x {b['species'].title()}{loss_summary} | "
            f"Feed: {s['total_feed']:,.0f} lbs ({s['total_bags']} bags / {s['barrels_needed']} barrels) | "
            f"Budget: ${s['total_budget_cost']:,.2f} (${s['total_cost_per_lb_meat']:,.2f}/lb meat)"
        )

        with st.expander(header_label, expanded=False):
            info_c1, info_c2, info_c3 = st.columns(3)
            with info_c1:
                st.markdown(f"**📥 {s['initial_label']} Window:**")
                if s["start_date"] == s["end_date"]:
                    st.write(f"{s['start_date']}")
                else:
                    st.write(f"{s['start_date']} to {s['end_date']}")

                if s["birth_start"] and s["birth_end"]:
                    st.markdown(f"**🐣 Estimated {s['birth_label']} Window:**")
                    st.write(f"{s['birth_start']} to {s['birth_end']}")
                    birth_gcal = create_gcal_link(
                        f"{b['name']} - {s['birth_label']} Window",
                        s["birth_start"],
                        s["birth_end"],
                        f"Look out for {b['name']} ({s['initial_quantity']}x {b['species']})!",
                    )
                    st.link_button(
                        f"📅 Add {s['birth_label']} Window to Google Calendar",
                        birth_gcal,
                    )
                else:
                    st.caption("📦 Immediate start (already hatched/born).")

                if s["starter_weeks"] > 0 and s["grower_weeks"] > 0:
                    st.markdown("**🌾 Ration Transition Date (Starter → Grower):**")
                    st.write(f"{s['transition_date']}")
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
                        "📅 Add Ration Switch to Google Calendar",
                        trans_gcal,
                    )

            with info_c2:
                st.markdown("**🔪 Approximate Processing Date:**")
                if s["proc_window_start"] != s["proc_date"]:
                    st.write(
                        f"{s['proc_date']} *(Window: {s['proc_window_start']} to {s['proc_date']})*"
                    )
                else:
                    st.write(f"{s['proc_date']}")

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
                    "📅 Add Processing Date to Google Calendar",
                    proc_gcal,
                )

                st.markdown("**🌾 Phased Feed Summary:**")
                st.write(
                    f"- **Starter ({s['starter_weeks']} wks):** {s['starter_feed']:,.1f} lbs "
                    f"→ **{s['starter_bags']} bags** (${s['starter_cost_bags']:,.2f})"
                )
                st.write(
                    f"- **Grower ({s['grower_weeks']} wks):** {s['grower_feed']:,.1f} lbs "
                    f"→ **{s['grower_bags']} bags** (${s['grower_cost_bags']:,.2f})"
                )
                if s["feed_saved_by_losses"] > 0:
                    st.caption(
                        f"📉 *Feed reduced by **{s['feed_saved_by_losses']:,.1f} lbs** due to {s['total_lost']} logged loss(es) "
                        f"(originally {s['total_feed_planned']:,.1f} lbs).*"
                    )

            with info_c3:
                st.markdown("**💰 Budget, Yield & ROI:**")
                st.write(
                    f"- **Acquisition ({s['initial_quantity']} × ${s['unit_cost']:,.2f}):** ${s['acquisition_cost']:,.2f}"
                )
                st.write(
                    f"- **Feed ({s['total_bags']} × {bag_weight}lb bags):** ${s['total_feed_cost_bags']:,.2f} "
                    f"*(exact consumed: ${s['total_feed_cost_exact']:,.2f})*"
                )
                st.write(
                    f"- **Processing ({s['surviving_quantity']} × ${s['processing_cost']:,.2f}):** ${s['total_processing_cost']:,.2f}"
                )
                st.write(
                    f"- **Total Batch Cost:** **${s['total_budget_cost']:,.2f}**"
                )
                st.write(
                    f"- **Expected Meat Yield:** **{s['expected_meat_lbs']:,.1f} lbs** "
                    f"({s['expected_yield_per_head']:,.1f} lbs/head)"
                )
                st.write(
                    f"- **Cost per lb of Meat:** **${s['total_cost_per_lb_meat']:,.2f}/lb** "
                    f"*(Feed only: ${s['feed_cost_per_lb_meat']:,.2f}/lb)*"
                )
                if b.get("notes"):
                    st.markdown(f"**📝 Notes:** {b['notes']}")

            st.divider()
            # Inline Loss Tracking Section
            st.markdown(
                f"#### 📉 Log / Manage Batch Losses — Surviving: **{s['surviving_quantity']} / {s['initial_quantity']}** "
                f"*(Lost: {s['total_lost']} head · Feed Reduced: {s['feed_saved_by_losses']:,.1f} lbs)*"
            )
            loss_col_form, loss_col_list = st.columns([1, 1])

            with loss_col_form:
                if s["surviving_quantity"] > 0:
                    default_loss_date = min(
                        max(date.today(), s["start_date"]), s["proc_date"]
                    )
                    with st.form(f"loss_form_{b['id']}", clear_on_submit=True):
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
                                min(int(loss_cnt_input), s["surviving_quantity"]),
                                loss_reason_input,
                            )
                            st.rerun()
                else:
                    st.warning("All animals in this batch have been recorded as lost.")

            with loss_col_list:
                if s["losses"]:
                    st.markdown("**Recorded Losses:**")
                    for l_entry in s["losses"]:
                        # Determine which stage the loss occurred in
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

            st.divider()
            act_c1, act_c2, act_c3, act_c4 = st.columns(4)
            toggle_label = (
                "⏸️ Exclude from Plan" if b["active"] else "▶️ Include in Plan"
            )
            if act_c1.button(
                toggle_label, key=f"toggle_{b['id']}", use_container_width=True
            ):
                set_batch_active(b["id"], not bool(b["active"]))
                st.rerun()

            if act_c2.button(
                "✏️ Edit in Sidebar",
                key=f"edit_{b['id']}",
                use_container_width=True,
            ):
                st.session_state.pending_edit_batch = b
                st.rerun()

            if act_c3.button(
                "📋 Duplicate", key=f"dup_{b['id']}", use_container_width=True
            ):
                save_batch(
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
                        "active": b["active"],
                        "notes": b.get("notes", ""),
                    }
                )
                st.rerun()

            if act_c4.button(
                "🗑️ Delete", key=f"del_{b['id']}", use_container_width=True
            ):
                if st.session_state.editing_batch_id == b["id"]:
                    st.session_state.pending_reset = True
                delete_batch(b["id"])
                st.rerun()


if os.environ.get("FARM_APP_TESTING") != "1":
    render_app()
