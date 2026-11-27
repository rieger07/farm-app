"""Pig/beef share mode persistence: carcass halves & whole shares, hanging weights, and butcher release."""

from __future__ import annotations

from datetime import datetime
import sqlite3

from homestead.db.core import get_db_connection
from shared.bridge import normalize_homestead_payment_method


SALE_UNITS = ("whole", "shares")
SHARE_SLOTS = ("whole", "half_a", "half_b")
PAYMENT_STATUSES = ("unpaid", "deposit", "paid")
PICKUP_STATUSES = ("unassigned", "assigned", "picked_up")


def normalize_sale_unit(value: str | None) -> str:
    clean = str(value or "").strip().lower()
    return "shares" if "share" in clean else "whole"


def _is_halves_mode(value: str | None) -> bool:
    clean = str(value or "").strip().lower()
    return "half" in clean or "halv" in clean or "1/2" in clean


def normalize_share_size(value: str | float | None) -> tuple[float, str]:
    """Return (share_fraction, share_label) for 'half'/'1/2'/0.5 vs 'whole'/1.0."""
    if value is None:
        return 1.0, "whole"
    raw = str(value).strip().lower()
    if _is_halves_mode(raw):
        return 0.5, "half"
    try:
        num = float(raw)
        if 0 < num <= 0.6:
            return 0.5, "half"
    except ValueError:
        pass
    return 1.0, "whole"


def share_slot_label(slot: str | None) -> str:
    clean = str(slot or "").strip().lower()
    if clean == "half_a":
        return "Half A"
    if clean == "half_b":
        return "Half B"
    return "Whole"


def effective_share_weight(share: dict, carcass_lbs: float) -> float:
    """Return explicit share override weight if set, else carcass_lbs * share_fraction."""
    if share.get("share_weight_lbs") is not None:
        return round(max(0.0, float(share["share_weight_lbs"])), 2)
    frac = float(share.get("share_fraction") or 1.0)
    return round(max(0.0, float(carcass_lbs or 0.0)) * frac, 2)


def _now_utc_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def backfill_batch_harvest_shares(
    batch_id: int,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Create 1 'whole' harvest_shares row for any legacy harvest in batch_id lacking share rows."""
    owns_conn = conn is None
    c = conn if conn is not None else get_db_connection()
    now_iso = _now_utc_iso()
    created = 0
    try:
        rows = c.execute(
            """
            SELECT bh.*
            FROM batch_harvests bh
            WHERE bh.batch_id = ?
              AND NOT EXISTS (
                SELECT 1 FROM harvest_shares hs WHERE hs.harvest_id = bh.id
              )
            ORDER BY bh.id ASC
            """,
            (int(batch_id),),
        ).fetchall()
        for r in rows:
            for_sale = 1 if (r["for_sale"] is None or int(r["for_sale"]) != 0) else 0
            cid = int(r["customer_id"]) if r["customer_id"] else None
            c.execute(
                """
                INSERT INTO harvest_shares (
                    harvest_id, batch_id, share_slot, share_fraction, share_weight_lbs,
                    customer_id, for_sale, deposit_paid, actual_sale_price, customer_butcher_fee,
                    payment_status, payment_method, paid_at, butcher_notified_at,
                    pickup_status, claimed_at, cut_sheet_notes, created_at
                ) VALUES (?, ?, 'whole', 1.0, NULL, ?, ?, ?, ?, 0.0, ?, ?, ?, '', ?, ?, ?, ?)
                """,
                (
                    int(r["id"]),
                    int(batch_id),
                    cid if for_sale else None,
                    for_sale,
                    round(max(0.0, float(r["deposit_paid"] or 0.0)), 2) if for_sale else 0.0,
                    round(max(0.0, float(r["actual_sale_price"])), 2)
                    if for_sale and r["actual_sale_price"] is not None
                    else None,
                    str(r["payment_status"] or "unpaid") if for_sale else "unpaid",
                    str(r["payment_method"] or "") if for_sale else "",
                    str(r["paid_at"] or "") if for_sale else "",
                    str(r["pickup_status"] or ("assigned" if cid else "unassigned"))
                    if for_sale
                    else "unassigned",
                    str(r["claimed_at"] or "") if for_sale and cid else "",
                    str(r["notes"] or "").strip(),
                    str(r["created_at"] or now_iso),
                ),
            )
            created += 1
        if owns_conn and created:
            c.commit()
        return created
    finally:
        if owns_conn:
            c.close()


def update_batch_share_settings(
    user_id: int,
    batch_id: int,
    *,
    sale_unit: str,
    customer_pays_butcher: bool = False,
    butcher_name: str = "",
    butcher_phone: str = "",
    butcher_email: str = "",
) -> dict | None:
    unit = normalize_sale_unit(sale_unit)
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM batches WHERE id = ? AND user_id = ?",
            (int(batch_id), int(user_id)),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            """
            UPDATE batches
            SET sale_unit = ?,
                customer_pays_butcher = ?,
                butcher_name = ?,
                butcher_phone = ?,
                butcher_email = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                unit,
                1 if customer_pays_butcher else 0,
                str(butcher_name or "").strip()[:120],
                str(butcher_phone or "").strip()[:60],
                str(butcher_email or "").strip()[:120],
                int(batch_id),
                int(user_id),
            ),
        )
        if unit == "shares":
            backfill_batch_harvest_shares(int(batch_id), conn=conn)
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM batches WHERE id = ? AND user_id = ?",
            (int(batch_id), int(user_id)),
        ).fetchone()
        return dict(updated) if updated else None


def list_batch_harvest_shares(
    batch_id: int | None = None,
    user_id: int | None = None,
) -> list[dict]:
    """Return harvest share rows enriched with parent carcass & batch metadata."""
    clauses: list[str] = []
    params: list[object] = []
    if batch_id is not None:
        clauses.append("hs.batch_id = ?")
        params.append(int(batch_id))
    if user_id is not None:
        clauses.append("b.user_id = ?")
        params.append(int(user_id))
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT hs.*,
                   bh.harvest_date,
                   bh.dressed_lbs AS carcass_lbs,
                   COALESCE(bh.animal_label, '') AS animal_label,
                   bh.live_weight_lbs,
                   COALESCE(bh.weight_basis, 'dressed') AS weight_basis,
                   bh.processing_cost_total,
                   COALESCE(bh.notes, '') AS carcass_notes,
                   b.name AS batch_name,
                   b.species,
                   b.sale_price_per_lb,
                   COALESCE(b.sale_unit, 'whole') AS sale_unit,
                   COALESCE(b.customer_pays_butcher, 0) AS customer_pays_butcher,
                   COALESCE(b.pickup_date, '') AS pickup_date
            FROM harvest_shares hs
            JOIN batch_harvests bh ON bh.id = hs.harvest_id
            JOIN batches b ON b.id = hs.batch_id
            {where_sql}
            ORDER BY bh.harvest_date ASC, bh.id ASC,
                     CASE hs.share_slot WHEN 'whole' THEN 0 WHEN 'half_a' THEN 1 WHEN 'half_b' THEN 2 ELSE 3 END,
                     hs.id ASC
            """,
            tuple(params),
        ).fetchall()
    return [dict(r) for r in rows]


def create_harvest_with_shares(
    batch_id: int,
    harvest_date: str,
    *,
    dressed_lbs: float = 0.0,
    processing_cost_total: float = 0.0,
    notes: str = "",
    animal_label: str = "",
    live_weight_lbs: float | None = None,
    split_mode: str = "halves",
    for_sale: bool = True,
    weight_basis: str = "hanging",
) -> int:
    """Create 1 physical carcass harvest row and its child Whole or Half A / Half B share rows."""
    now_iso = _now_utc_iso()
    lbs_val = round(max(0.0, float(dressed_lbs or 0.0)), 2)
    proc_val = round(max(0.0, float(processing_cost_total or 0.0)), 2)
    live_val = (
        round(max(0.0, float(live_weight_lbs)), 2)
        if live_weight_lbs is not None and float(live_weight_lbs) > 0
        else None
    )
    basis = "hanging" if str(weight_basis or "").strip().lower() == "hanging" else "dressed"
    fs_int = 1 if for_sale else 0
    is_halves = _is_halves_mode(split_mode or "halves")
    slots = [("half_a", 0.5), ("half_b", 0.5)] if is_halves else [("whole", 1.0)]

    with get_db_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO batch_harvests (
                batch_id, harvest_date, head_count, dressed_lbs, processing_cost_total,
                notes, for_sale, animal_label, live_weight_lbs, weight_basis, created_at
            ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(batch_id),
                str(harvest_date or "").strip(),
                lbs_val,
                proc_val,
                str(notes or "").strip(),
                fs_int,
                str(animal_label or "").strip()[:80],
                live_val,
                basis,
                now_iso,
            ),
        )
        harvest_id = int(cur.lastrowid)
        for slot, frac in slots:
            conn.execute(
                """
                INSERT INTO harvest_shares (
                    harvest_id, batch_id, share_slot, share_fraction,
                    for_sale, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (harvest_id, int(batch_id), slot, frac, fs_int, now_iso),
            )
        conn.commit()
        return harvest_id


def update_carcass_details(
    user_id: int,
    batch_id: int,
    harvest_id: int,
    *,
    dressed_lbs: float | None = None,
    live_weight_lbs: float | None = None,
    clear_live_weight: bool = False,
    processing_cost_total: float | None = None,
    animal_label: str | None = None,
    harvest_date: str | None = None,
    notes: str | None = None,
) -> bool:
    """Update carcass hanging weight, live weight, label, processing fee, date, or notes."""
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT bh.*
            FROM batch_harvests bh
            JOIN batches b ON b.id = bh.batch_id
            WHERE bh.id = ? AND bh.batch_id = ? AND b.user_id = ?
            """,
            (int(harvest_id), int(batch_id), int(user_id)),
        ).fetchone()
        if not row:
            return False

        new_dressed = (
            round(max(0.0, float(dressed_lbs)), 2)
            if dressed_lbs is not None
            else float(row["dressed_lbs"] or 0.0)
        )
        if clear_live_weight:
            new_live = None
        elif live_weight_lbs is not None:
            lv = round(max(0.0, float(live_weight_lbs)), 2)
            new_live = lv if lv > 0 else None
        else:
            new_live = row["live_weight_lbs"]
        new_proc = (
            round(max(0.0, float(processing_cost_total)), 2)
            if processing_cost_total is not None
            else float(row["processing_cost_total"] or 0.0)
        )
        new_label = (
            str(animal_label).strip()[:80]
            if animal_label is not None
            else str(row["animal_label"] or "")
        )
        new_date = (
            str(harvest_date).strip()[:10]
            if harvest_date is not None and str(harvest_date).strip()
            else str(row["harvest_date"])
        )
        new_notes = (
            str(notes).strip()
            if notes is not None
            else str(row["notes"] or "")
        )

        conn.execute(
            """
            UPDATE batch_harvests
            SET dressed_lbs = ?, live_weight_lbs = ?, processing_cost_total = ?,
                animal_label = ?, harvest_date = ?, notes = ?
            WHERE id = ? AND batch_id = ?
            """,
            (
                new_dressed,
                new_live,
                new_proc,
                new_label,
                new_date,
                new_notes,
                int(harvest_id),
                int(batch_id),
            ),
        )
        conn.commit()
        return True


def set_carcass_split_mode(
    user_id: int,
    batch_id: int,
    harvest_id: int,
    split_mode: str,
) -> bool:
    """Switch a carcass between 1 whole share and Half A + Half B shares."""
    want_halves = _is_halves_mode(split_mode)
    now_iso = _now_utc_iso()
    with get_db_connection() as conn:
        hv = conn.execute(
            """
            SELECT bh.id, bh.for_sale
            FROM batch_harvests bh
            JOIN batches b ON b.id = bh.batch_id
            WHERE bh.id = ? AND bh.batch_id = ? AND b.user_id = ?
            """,
            (int(harvest_id), int(batch_id), int(user_id)),
        ).fetchone()
        if not hv:
            return False

        backfill_batch_harvest_shares(int(batch_id), conn=conn)
        shares = conn.execute(
            "SELECT * FROM harvest_shares WHERE harvest_id = ? AND batch_id = ? ORDER BY id ASC",
            (int(harvest_id), int(batch_id)),
        ).fetchall()
        if not shares:
            return False

        if want_halves:
            if len(shares) >= 2:
                return True
            first = dict(shares[0])
            conn.execute(
                """
                UPDATE harvest_shares
                SET share_slot = 'half_a', share_fraction = 0.5, share_weight_lbs = NULL
                WHERE id = ? AND batch_id = ?
                """,
                (int(first["id"]), int(batch_id)),
            )
            conn.execute(
                """
                INSERT INTO harvest_shares (
                    harvest_id, batch_id, share_slot, share_fraction, for_sale, created_at
                ) VALUES (?, ?, 'half_b', 0.5, 1, ?)
                """,
                (int(harvest_id), int(batch_id), now_iso),
            )
            conn.commit()
            return True

        # Merging to 'whole'
        if len(shares) == 1:
            only = dict(shares[0])
            if only["share_slot"] != "whole" or float(only["share_fraction"] or 1.0) != 1.0:
                conn.execute(
                    """
                    UPDATE harvest_shares
                    SET share_slot = 'whole', share_fraction = 1.0, share_weight_lbs = NULL
                    WHERE id = ? AND batch_id = ?
                    """,
                    (int(only["id"]), int(batch_id)),
                )
                conn.commit()
            return True

        share_dicts = [dict(s) for s in shares]
        assigned_cids = [int(s["customer_id"]) for s in share_dicts if s.get("customer_id")]
        if len(set(assigned_cids)) > 1:
            raise ValueError("Unassign one half before merging back into a whole carcass.")

        primary = next((s for s in share_dicts if s.get("customer_id")), share_dicts[0])
        others = [s for s in share_dicts if int(s["id"]) != int(primary["id"])]

        merged_cid = primary.get("customer_id")
        merged_for_sale = (
            1
            if (merged_cid is not None or any(bool(s.get("for_sale", 1)) for s in share_dicts))
            else 0
        )
        merged_dep = round(sum(float(s.get("deposit_paid") or 0.0) for s in share_dicts), 2)
        act_vals = [float(s["actual_sale_price"]) for s in share_dicts if s.get("actual_sale_price") is not None]
        merged_actual = round(sum(act_vals), 2) if act_vals else None
        merged_bfee = round(sum(float(s.get("customer_butcher_fee") or 0.0) for s in share_dicts), 2)
        notes_parts = [
            str(s.get("cut_sheet_notes") or "").strip()
            for s in share_dicts
            if str(s.get("cut_sheet_notes") or "").strip()
        ]
        merged_notes = " · ".join(dict.fromkeys(notes_parts))

        conn.execute(
            """
            UPDATE harvest_shares
            SET share_slot = 'whole',
                share_fraction = 1.0,
                share_weight_lbs = NULL,
                customer_id = ?,
                for_sale = ?,
                deposit_paid = ?,
                actual_sale_price = ?,
                customer_butcher_fee = ?,
                cut_sheet_notes = ?
            WHERE id = ? AND batch_id = ?
            """,
            (
                int(merged_cid) if merged_cid else None,
                merged_for_sale,
                merged_dep,
                merged_actual,
                merged_bfee,
                merged_notes,
                int(primary["id"]),
                int(batch_id),
            ),
        )
        for ot in others:
            conn.execute(
                "DELETE FROM harvest_shares WHERE id = ? AND batch_id = ?",
                (int(ot["id"]), int(batch_id)),
            )
        conn.commit()
        return True


def update_share_assignment(
    user_id: int,
    batch_id: int,
    share_id: int,
    *,
    customer_id: int | None = None,
    for_sale: bool = True,
    share_weight_lbs: float | None = None,
    deposit_paid: float = 0.0,
    actual_sale_price: float | None = None,
    customer_butcher_fee: float = 0.0,
    payment_status: str = "unpaid",
    payment_method: str | None = None,
    pickup_status: str = "unassigned",
    butcher_notified: bool | None = None,
    cut_sheet_notes: str | None = None,
) -> bool:
    """Update customer assignment, weight override, payment, butcher release, and cut-sheet notes."""
    pay = payment_status if payment_status in PAYMENT_STATUSES else "unpaid"
    pstat = pickup_status if pickup_status in PICKUP_STATUSES else "unassigned"
    actual_val = round(max(0.0, float(actual_sale_price)), 2) if actual_sale_price is not None else None
    sw_val = (
        round(max(0.01, float(share_weight_lbs)), 2)
        if share_weight_lbs is not None and float(share_weight_lbs) > 0
        else None
    )
    dep_val = round(max(0.0, float(deposit_paid or 0.0)), 2)
    bfee_val = round(max(0.0, float(customer_butcher_fee or 0.0)), 2)
    pm_val = (
        normalize_homestead_payment_method(payment_method)
        if payment_method is not None
        else None
    )

    if not for_sale:
        customer_id = None
        pstat = "unassigned"
        pay = "unpaid"
        dep_val = 0.0
        actual_val = None
        pm_val = ""
        butcher_notified = False
    elif customer_id is not None and pstat == "unassigned":
        pstat = "assigned"
    elif customer_id is None and pstat != "unassigned":
        pstat = "unassigned"

    now_utc = _now_utc_iso()
    claimed_at = now_utc if customer_id else ""

    with get_db_connection() as conn:
        existing = conn.execute(
            """
            SELECT hs.id, hs.butcher_notified_at
            FROM harvest_shares hs
            JOIN batches b ON b.id = hs.batch_id
            WHERE hs.id = ? AND hs.batch_id = ? AND b.user_id = ?
            """,
            (int(share_id), int(batch_id), int(user_id)),
        ).fetchone()
        if not existing:
            return False

        if butcher_notified is True:
            bnot_val = str(existing["butcher_notified_at"] or "").strip() or now_utc
        elif butcher_notified is False:
            bnot_val = ""
        else:
            bnot_val = str(existing["butcher_notified_at"] or "").strip()

        set_parts = [
            "customer_id = ?",
            "for_sale = ?",
            "share_weight_lbs = ?",
            "deposit_paid = ?",
            "actual_sale_price = ?",
            "customer_butcher_fee = ?",
            "payment_status = ?",
            "pickup_status = ?",
            "claimed_at = CASE WHEN ? != '' AND COALESCE(claimed_at, '') = '' THEN ? WHEN ? = '' THEN '' ELSE claimed_at END",
            """paid_at = CASE
                WHEN ? = 'paid' AND COALESCE(paid_at, '') = '' THEN ?
                WHEN ? != 'paid' AND ? != 'picked_up' THEN ''
                ELSE COALESCE(paid_at, '')
            END""",
            "butcher_notified_at = ?",
        ]
        params: list[object] = [
            int(customer_id) if customer_id else None,
            1 if for_sale else 0,
            sw_val,
            dep_val,
            actual_val,
            bfee_val,
            pay,
            pstat,
            claimed_at,
            claimed_at,
            claimed_at,
            pay,
            now_utc,
            pay,
            pstat,
            bnot_val,
        ]
        if pm_val is not None:
            set_parts.append("payment_method = ?")
            params.append(pm_val)
        if cut_sheet_notes is not None:
            set_parts.append("cut_sheet_notes = ?")
            params.append(str(cut_sheet_notes).strip())

        params.extend([int(share_id), int(batch_id)])
        cur = conn.execute(
            f"UPDATE harvest_shares SET {', '.join(set_parts)} WHERE id = ? AND batch_id = ?",
            params,
        )
        conn.commit()
        return cur.rowcount > 0


def mark_shares_butcher_notified(
    user_id: int,
    batch_id: int,
    *,
    customer_id: int | None = None,
    share_ids: list[int] | None = None,
    notified: bool = True,
    only_paid: bool = False,
) -> int:
    """Mark or clear butcher release timestamps across owned shares in a batch."""
    now_utc = _now_utc_iso()
    clean_ids = [int(x) for x in (share_ids or []) if int(x) > 0]
    with get_db_connection() as conn:
        owned = conn.execute(
            "SELECT id FROM batches WHERE id = ? AND user_id = ?",
            (int(batch_id), int(user_id)),
        ).fetchone()
        if not owned:
            return 0
        clauses = ["batch_id = ?"]
        params: list[object] = [int(batch_id)]
        if customer_id is not None:
            clauses.append("customer_id = ?")
            params.append(int(customer_id))
        if clean_ids:
            marks = ",".join("?" * len(clean_ids))
            clauses.append(f"id IN ({marks})")
            params.extend(clean_ids)
        if only_paid:
            clauses.append(
                "COALESCE(for_sale, 1) = 1 AND customer_id IS NOT NULL "
                "AND LOWER(COALESCE(payment_status, '')) = 'paid' "
                "AND COALESCE(butcher_notified_at, '') = ''"
            )
        where_sql = " AND ".join(clauses)
        if notified:
            cur = conn.execute(
                f"""
                UPDATE harvest_shares
                SET butcher_notified_at = CASE
                    WHEN COALESCE(butcher_notified_at, '') = '' THEN ?
                    ELSE butcher_notified_at
                END
                WHERE {where_sql}
                """,
                [now_utc, *params],
            )
        else:
            cur = conn.execute(
                f"UPDATE harvest_shares SET butcher_notified_at = '' WHERE {where_sql}",
                params,
            )
        conn.commit()
        return int(cur.rowcount or 0)


def settle_customer_shares(
    user_id: int,
    batch_id: int,
    customer_id: int,
    *,
    collected_today: float | None = None,
    mark_paid: bool = True,
    mark_butcher_notified: bool = False,
    mark_picked_up: bool = False,
    share_ids: list[int] | None = None,
    payment_method: str | None = None,
) -> int:
    """Settle selected (or all) for-sale shares assigned to customer_id in batch_id."""
    clean_ids = [int(x) for x in (share_ids or []) if int(x) > 0]
    pm = (
        normalize_homestead_payment_method(payment_method)
        if payment_method is not None
        else None
    )
    now_utc = _now_utc_iso()
    with get_db_connection() as conn:
        batch = conn.execute(
            "SELECT sale_price_per_lb FROM batches WHERE id = ? AND user_id = ?",
            (int(batch_id), int(user_id)),
        ).fetchone()
        if not batch:
            return 0
        price_per_lb = float(batch["sale_price_per_lb"] or 0.0)
        if clean_ids:
            marks = ",".join("?" * len(clean_ids))
            shares = conn.execute(
                f"""
                SELECT hs.id, hs.share_fraction, hs.share_weight_lbs, bh.dressed_lbs,
                       hs.deposit_paid, hs.actual_sale_price, hs.payment_status,
                       hs.pickup_status, hs.butcher_notified_at
                FROM harvest_shares hs
                JOIN batch_harvests bh ON bh.id = hs.harvest_id
                WHERE hs.batch_id = ? AND hs.customer_id = ? AND COALESCE(hs.for_sale, 1) = 1
                  AND hs.id IN ({marks})
                ORDER BY hs.id ASC
                """,
                [int(batch_id), int(customer_id), *clean_ids],
            ).fetchall()
        else:
            shares = conn.execute(
                """
                SELECT hs.id, hs.share_fraction, hs.share_weight_lbs, bh.dressed_lbs,
                       hs.deposit_paid, hs.actual_sale_price, hs.payment_status,
                       hs.pickup_status, hs.butcher_notified_at
                FROM harvest_shares hs
                JOIN batch_harvests bh ON bh.id = hs.harvest_id
                WHERE hs.batch_id = ? AND hs.customer_id = ? AND COALESCE(hs.for_sale, 1) = 1
                ORDER BY hs.id ASC
                """,
                (int(batch_id), int(customer_id)),
            ).fetchall()
        if not shares:
            return 0

        share_list = [dict(s) for s in shares]
        weights_lbs = [effective_share_weight(s, float(s.get("dressed_lbs") or 0.0)) for s in share_list]
        total_dep = round(sum(float(s.get("deposit_paid") or 0.0) for s in share_list), 2)
        cur_eff = [
            round(float(s["actual_sale_price"]), 2)
            if s.get("actual_sale_price") is not None
            else round(w_lb * price_per_lb, 2)
            for s, w_lb in zip(share_list, weights_lbs)
        ]
        cur_total = round(sum(cur_eff), 2)

        new_actuals: list[float | None] = [
            round(float(s["actual_sale_price"]), 2) if s.get("actual_sale_price") is not None else None
            for s in share_list
        ]
        if collected_today is not None:
            target_total = round(max(0.0, total_dep + max(0.0, float(collected_today))), 2)
            if abs(target_total - cur_total) >= 0.01:
                weights = [max(0.01, w_lb) for w_lb in weights_lbs]
                w_sum = sum(weights) or 1.0
                allocated = 0.0
                for idx, w in enumerate(weights):
                    if idx == len(weights) - 1:
                        share = round(max(0.0, target_total - allocated), 2)
                    else:
                        share = round(target_total * (w / w_sum), 2)
                        allocated = round(allocated + share, 2)
                    new_actuals[idx] = share

        for s, act_p in zip(share_list, new_actuals):
            pay_stat = "paid" if mark_paid else (s.get("payment_status") or "unpaid")
            pick_stat = "picked_up" if mark_picked_up else (s.get("pickup_status") or "assigned")
            bnot_val = str(s.get("butcher_notified_at") or "").strip()
            if mark_butcher_notified and not bnot_val:
                bnot_val = now_utc
            if pm is not None:
                conn.execute(
                    """
                    UPDATE harvest_shares
                    SET payment_status = ?, pickup_status = ?, actual_sale_price = ?,
                        payment_method = ?,
                        paid_at = CASE WHEN ? = 'paid' AND COALESCE(paid_at, '') = '' THEN ? ELSE COALESCE(paid_at, '') END,
                        butcher_notified_at = ?
                    WHERE id = ? AND batch_id = ?
                    """,
                    (pay_stat, pick_stat, act_p, pm, pay_stat, now_utc, bnot_val, int(s["id"]), int(batch_id)),
                )
            else:
                conn.execute(
                    """
                    UPDATE harvest_shares
                    SET payment_status = ?, pickup_status = ?, actual_sale_price = ?,
                        paid_at = CASE WHEN ? = 'paid' AND COALESCE(paid_at, '') = '' THEN ? ELSE COALESCE(paid_at, '') END,
                        butcher_notified_at = ?
                    WHERE id = ? AND batch_id = ?
                    """,
                    (pay_stat, pick_stat, act_p, pay_stat, now_utc, bnot_val, int(s["id"]), int(batch_id)),
                )
        conn.commit()
        return len(share_list)


def update_customer_share_payment_method(
    user_id: int,
    batch_id: int,
    customer_id: int,
    payment_method: str,
) -> int:
    """Update payment_method across paid/picked-up shares for a customer in an owned batch."""
    pm = normalize_homestead_payment_method(payment_method)
    with get_db_connection() as conn:
        batch = conn.execute(
            "SELECT id FROM batches WHERE id = ? AND user_id = ?",
            (int(batch_id), int(user_id)),
        ).fetchone()
        if not batch:
            return 0
        cur = conn.execute(
            """
            UPDATE harvest_shares
            SET payment_method = ?
            WHERE batch_id = ? AND customer_id = ? AND COALESCE(for_sale, 1) = 1
              AND (
                LOWER(COALESCE(payment_status, '')) = 'paid'
                OR LOWER(COALESCE(pickup_status, '')) = 'picked_up'
                OR actual_sale_price IS NOT NULL
              )
            """,
            (pm, int(batch_id), int(customer_id)),
        )
        conn.commit()
        return int(cur.rowcount or 0)


def reopen_customer_shares(
    user_id: int,
    batch_id: int,
    customer_id: int,
) -> int:
    """Move a customer's shares back to assigned/pending status (undo paid/released/picked_up)."""
    with get_db_connection() as conn:
        batch = conn.execute(
            "SELECT id FROM batches WHERE id = ? AND user_id = ?",
            (int(batch_id), int(user_id)),
        ).fetchone()
        if not batch:
            return 0
        cur = conn.execute(
            """
            UPDATE harvest_shares
            SET pickup_status = 'assigned',
                payment_status = CASE WHEN COALESCE(deposit_paid, 0) > 0 THEN 'deposit' ELSE 'unpaid' END,
                paid_at = '',
                butcher_notified_at = ''
            WHERE batch_id = ? AND customer_id = ? AND COALESCE(for_sale, 1) = 1
            """,
            (int(batch_id), int(customer_id)),
        )
        conn.commit()
        return int(cur.rowcount or 0)


def claim_share_atomically(
    batch_id: int,
    share_id: int,
    customer_id: int,
    max_equiv: float | None = None,
    apply_preorder_deposit: bool = True,
) -> tuple[bool, str]:
    """Atomically claim an unassigned for-sale share on public /pick/<slug>."""
    now_iso = _now_utc_iso()
    with get_db_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        target = conn.execute(
            """
            SELECT id, share_fraction
            FROM harvest_shares
            WHERE id = ? AND batch_id = ? AND customer_id IS NULL AND COALESCE(for_sale, 1) = 1
            """,
            (int(share_id), int(batch_id)),
        ).fetchone()
        if not target:
            conn.rollback()
            return False, "already_claimed"

        share_frac = float(target["share_fraction"] or 1.0)
        already_row = conn.execute(
            """
            SELECT COALESCE(SUM(COALESCE(share_fraction, 1.0)), 0.0) AS equiv,
                   COALESCE(SUM(deposit_paid), 0.0) AS applied_dep
            FROM harvest_shares
            WHERE batch_id = ? AND customer_id = ? AND COALESCE(for_sale, 1) = 1
            """,
            (int(batch_id), int(customer_id)),
        ).fetchone()
        already_equiv = float(already_row["equiv"] or 0.0)
        applied_dep = float(already_row["applied_dep"] or 0.0)
        if max_equiv is not None and float(max_equiv) > 0:
            if (already_equiv + share_frac) > (float(max_equiv) + 1e-6):
                conn.rollback()
                return False, "limit_reached"

        credit = 0.0
        if apply_preorder_deposit:
            po_row = conn.execute(
                """
                SELECT COALESCE(SUM(quantity * COALESCE(share_fraction, 1.0)), 0.0) AS equiv,
                       COALESCE(SUM(deposit_paid), 0.0) AS paid_dep,
                       MAX(deposit_per_bird) AS per_whole
                FROM batch_preorders
                WHERE batch_id = ? AND customer_id = ? AND status = 'reserved'
                """,
                (int(batch_id), int(customer_id)),
            ).fetchone()
            po_equiv = float(po_row["equiv"] or 0.0)
            po_paid = float(po_row["paid_dep"] or 0.0)
            po_per_whole = float(po_row["per_whole"] or 0.0)
            unapplied = round(max(0.0, po_paid - applied_dep), 2)
            if unapplied > 0.0:
                if po_equiv > 0 and (already_equiv + share_frac) >= (po_equiv - 1e-6):
                    credit = unapplied
                elif po_per_whole > 0.0:
                    credit = round(min(unapplied, po_per_whole * share_frac), 2)
                elif po_equiv > 0:
                    credit = round(min(unapplied, (po_paid / po_equiv) * share_frac), 2)
                else:
                    credit = unapplied

        if credit > 0.0:
            cur = conn.execute(
                """
                UPDATE harvest_shares
                SET customer_id = ?, pickup_status = 'assigned', claimed_at = ?,
                    deposit_paid = ?,
                    payment_status = CASE WHEN payment_status = 'paid' THEN 'paid' ELSE 'deposit' END
                WHERE id = ? AND batch_id = ?
                  AND customer_id IS NULL
                  AND COALESCE(for_sale, 1) = 1
                """,
                (int(customer_id), now_iso, credit, int(share_id), int(batch_id)),
            )
        else:
            cur = conn.execute(
                """
                UPDATE harvest_shares
                SET customer_id = ?, pickup_status = 'assigned', claimed_at = ?
                WHERE id = ? AND batch_id = ?
                  AND customer_id IS NULL
                  AND COALESCE(for_sale, 1) = 1
                """,
                (int(customer_id), now_iso, int(share_id), int(batch_id)),
            )
        if cur.rowcount == 0:
            conn.rollback()
            return False, "already_claimed"
        conn.commit()
        return True, "ok"
