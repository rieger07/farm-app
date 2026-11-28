"""JSON backup export and restore helpers for Homestead Planner."""

from datetime import datetime
import json

from homestead.db.batches import (
    _normalize_batch_payload,
    list_batch_harvests,
    list_batch_losses,
    list_batch_sales,
    list_batches,
)
from homestead.db.core import (
    _resolve_user_id,
    get_bag_weight,
    get_barrel_capacity,
    get_db_connection,
    get_species_catalog,
    save_species_preset,
    set_bag_weight,
    set_barrel_capacity,
)


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
                        batch_id, harvest_date, head_count, dressed_lbs, processing_cost_total,
                        notes, payment_method, paid_at, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        str(harv_item.get("payment_method", "") or ""),
                        str(harv_item.get("paid_at", "") or ""),
                        str(harv_item.get("created_at") or now_iso),
                    ),
                )
            for sale_item in item.get("sales", []):
                alloc_t = str(sale_item.get("allocation_type", "Customer Sale"))
                min_wt = 0.0 if alloc_t == "Live Sale" else 0.01
                cid_raw = sale_item.get("customer_id")
                cid_val = int(cid_raw) if cid_raw not in (None, "", 0, "0") else None
                conn.execute(
                    """
                    INSERT INTO batch_sales (
                        batch_id, entry_date, allocation_type, weight_lbs, revenue,
                        payment_method, head_count, customer_id, notes, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_batch_id,
                        str(sale_item.get("entry_date", norm["end_date"])),
                        alloc_t,
                        max(min_wt, float(sale_item.get("weight_lbs", min_wt))),
                        max(0.0, float(sale_item.get("revenue", 0.0))),
                        str(sale_item.get("payment_method", "") or ""),
                        max(0, int(sale_item.get("head_count", 0) or 0)),
                        cid_val,
                        str(sale_item.get("notes", "")),
                        str(sale_item.get("created_at") or now_iso),
                    ),
                )
            count += 1
        conn.commit()
    return count
