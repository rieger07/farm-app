"""Livestock batches, mortality losses, harvests, sales, feed logs, and milestones."""

from datetime import date, datetime
import secrets

from homestead.config import LIFECYCLE_TYPES, infer_lifecycle_type
from homestead.db.core import (
    _resolve_user_id,
    get_db_connection,
    get_species_defaults,
)
from shared.bridge import normalize_homestead_payment_method


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
    payment_method: str = "",
) -> int:
    resolved_type = (
        "Home Freezer"
        if "freezer" in str(allocation_type).lower()
        else "Customer Sale"
    )
    pm = "" if resolved_type == "Home Freezer" else normalize_homestead_payment_method(payment_method)
    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO batch_sales (
                batch_id, entry_date, allocation_type, weight_lbs, revenue, payment_method, notes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(batch_id),
                str(entry_date),
                resolved_type,
                max(0.01, float(weight_lbs)),
                0.0 if resolved_type == "Home Freezer" else max(0.0, float(revenue)),
                pm,
                str(notes).strip(),
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def update_batch_sale_payment_method(
    batch_id: int, sale_id: int, payment_method: str
) -> bool:
    """Update payment_method on a batch Customer Sale row."""
    pm = normalize_homestead_payment_method(payment_method)
    with get_db_connection() as conn:
        cur = conn.execute(
            "UPDATE batch_sales SET payment_method = ? WHERE id = ? AND batch_id = ? AND allocation_type = 'Customer Sale'",
            (pm, int(sale_id), int(batch_id)),
        )
        conn.commit()
        return cur.rowcount > 0


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
    vendor: str = "",
    receipt_group_id: str = "",
) -> int:
    ft = "starter" if str(feed_type).strip().lower() == "starter" else "grower"
    with get_db_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO batch_feed_logs (
                batch_id, feed_date, feed_type, bags, feed_lbs, cost_total,
                notes, receipt_url, drive_file_id, vendor, receipt_group_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(batch_id),
                str(feed_date),
                ft,
                round(float(bags), 2),
                round(float(feed_lbs), 2),
                round(float(cost_total), 2),
                str(notes or "").strip(),
                str(receipt_url or "").strip(),
                str(drive_file_id or "").strip(),
                str(vendor or "").strip(),
                str(receipt_group_id or "").strip(),
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def attach_batch_feed_receipt(
    log_id: int,
    user_id: int,
    receipt_url: str,
    drive_file_id: str = "",
) -> bool:
    clean_url = str(receipt_url or "").strip()
    if not clean_url:
        return False
    with get_db_connection() as conn:
        cur = conn.execute(
            """
            UPDATE batch_feed_logs
            SET receipt_url = ?,
                drive_file_id = COALESCE(NULLIF(?, ''), drive_file_id)
            WHERE id = ?
              AND batch_id IN (SELECT id FROM batches WHERE user_id = ?)
            """,
            (clean_url, str(drive_file_id or "").strip(), int(log_id), int(user_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def attach_batch_harvest_receipt(
    harvest_id: int,
    user_id: int,
    receipt_url: str,
    drive_file_id: str = "",
) -> bool:
    clean_url = str(receipt_url or "").strip()
    if not clean_url:
        return False
    with get_db_connection() as conn:
        cur = conn.execute(
            """
            UPDATE batch_harvests
            SET receipt_url = ?,
                drive_file_id = COALESCE(NULLIF(?, ''), drive_file_id)
            WHERE id = ?
              AND batch_id IN (SELECT id FROM batches WHERE user_id = ?)
            """,
            (clean_url, str(drive_file_id or "").strip(), int(harvest_id), int(user_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_batch_feed_log(log_id: int) -> None:
    with get_db_connection() as conn:
        conn.execute("DELETE FROM batch_feed_logs WHERE id = ?", (int(log_id),))
        conn.commit()


def delete_batch_feed_log_for_user(log_id: int, user_id: int) -> bool:
    with get_db_connection() as conn:
        cur = conn.execute(
            """
            DELETE FROM batch_feed_logs
            WHERE id = ?
              AND batch_id IN (SELECT id FROM batches WHERE user_id = ?)
            """,
            (int(log_id), int(user_id)),
        )
        conn.commit()
        return cur.rowcount > 0


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
