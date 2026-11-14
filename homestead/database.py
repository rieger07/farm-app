"""SQLite persistence layer, automatic schema migrations, and JSON backup/restore."""

from datetime import date, datetime
import json
import os
import sqlite3

from homestead.config import (
    DEFAULT_BAG_WEIGHT,
    DEFAULT_BARREL_CAPACITY,
    get_species_defaults,
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
