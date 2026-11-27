"""Customer CRM, bird assignments, atomic /pick claims, pre-orders, and pickup checkout."""

from datetime import datetime
import secrets

from homestead.db.core import (
    get_db_connection,
    get_text_setting,
    set_text_setting,
)
from homestead.db.shares import (
    backfill_batch_harvest_shares,
    effective_share_weight,
    normalize_sale_unit,
    share_slot_label,
)
from shared.bridge import HOMESTEAD_PAYMENT_METHODS, normalize_homestead_payment_method
from shared.merchants import clean_payment_handle as _clean_payment_handle


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
                   COALESCE((
                       SELECT COUNT(*) FROM batch_harvests bh
                       WHERE bh.customer_id = c.id
                         AND NOT EXISTS (SELECT 1 FROM harvest_shares hs0 WHERE hs0.harvest_id = bh.id)
                   ), 0) + COALESCE((
                       SELECT COUNT(*) FROM harvest_shares hs
                       WHERE hs.customer_id = c.id
                   ), 0) AS bird_count,
                   COALESCE((
                       SELECT SUM(bh.dressed_lbs) FROM batch_harvests bh
                       WHERE bh.customer_id = c.id
                         AND NOT EXISTS (SELECT 1 FROM harvest_shares hs0 WHERE hs0.harvest_id = bh.id)
                   ), 0) + COALESCE((
                       SELECT SUM(COALESCE(hs.share_weight_lbs, bh2.dressed_lbs * COALESCE(hs.share_fraction, 1.0)))
                       FROM harvest_shares hs
                       JOIN batch_harvests bh2 ON bh2.id = hs.harvest_id
                       WHERE hs.customer_id = c.id
                   ), 0) AS total_lbs
            FROM customers c
            WHERE c.user_id = ?
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
            "UPDATE harvest_shares SET customer_id = NULL, pickup_status = 'unassigned'"
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
            "UPDATE harvest_shares SET customer_id = ?"
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


def _query_assigned_harvest_items(
    conn, user_id: int, customer_id: int | None = None
) -> list[dict]:
    """Fetch customer-assigned harvests and individual harvest shares cleanly without duplication."""
    cust_clause_bh = "AND bh.customer_id = ?" if customer_id is not None else "AND bh.customer_id IS NOT NULL"
    cust_clause_hs = "AND hs.customer_id = ?" if customer_id is not None else "AND hs.customer_id IS NOT NULL"
    params_bh: list[object] = [int(user_id)] + ([int(customer_id)] if customer_id is not None else [])
    params_hs: list[object] = [int(user_id)] + ([int(customer_id)] if customer_id is not None else [])

    bird_rows = conn.execute(
        f"""
        SELECT bh.*, b.name AS batch_name, b.species, b.sale_price_per_lb
        FROM batch_harvests bh
        JOIN batches b ON b.id = bh.batch_id
        WHERE b.user_id = ? {cust_clause_bh}
          AND NOT EXISTS (SELECT 1 FROM harvest_shares hs0 WHERE hs0.harvest_id = bh.id)
        ORDER BY bh.harvest_date DESC, bh.id DESC
        """,
        tuple(params_bh),
    ).fetchall()

    share_rows = conn.execute(
        f"""
        SELECT hs.*,
               bh.harvest_date, bh.dressed_lbs AS carcass_lbs,
               COALESCE(bh.animal_label, '') AS animal_label,
               COALESCE(bh.weight_basis, 'dressed') AS weight_basis,
               b.name AS batch_name, b.species, b.sale_price_per_lb
        FROM harvest_shares hs
        JOIN batch_harvests bh ON bh.id = hs.harvest_id
        JOIN batches b ON b.id = hs.batch_id
        WHERE b.user_id = ? {cust_clause_hs}
        ORDER BY bh.harvest_date DESC, hs.id DESC
        """,
        tuple(params_hs),
    ).fetchall()

    items: list[dict] = []
    for r in bird_rows:
        d = dict(r)
        d["is_share"] = False
        d["share_fraction"] = 1.0
        d["share_slot"] = "whole"
        d["share_label"] = ""
        d["butcher_notified_at"] = ""
        d["weight_basis"] = str(d.get("weight_basis") or "dressed")
        items.append(d)

    for r in share_rows:
        d = dict(r)
        carcass_lbs = float(d.get("carcass_lbs") or 0.0)
        eff_lbs = effective_share_weight(d, carcass_lbs)
        carcass_name = (d.get("animal_label") or "").strip() or f"Carcass #{d['harvest_id']}"
        slot_lbl = share_slot_label(d.get("share_slot"))
        items.append(
            {
                "id": int(d["id"]),
                "batch_id": int(d["batch_id"]),
                "harvest_id": int(d["harvest_id"]),
                "customer_id": int(d["customer_id"]),
                "harvest_date": d["harvest_date"],
                "head_count": 1,
                "dressed_lbs": eff_lbs,
                "actual_sale_price": d.get("actual_sale_price"),
                "deposit_paid": float(d.get("deposit_paid") or 0.0),
                "payment_status": d.get("payment_status") or "unpaid",
                "payment_method": d.get("payment_method") or "",
                "pickup_status": d.get("pickup_status") or "assigned",
                "customer_butcher_fee": float(d.get("customer_butcher_fee") or 0.0),
                "butcher_notified_at": d.get("butcher_notified_at") or "",
                "notes": d.get("cut_sheet_notes") or "",
                "batch_name": d["batch_name"],
                "species": d["species"],
                "sale_price_per_lb": d["sale_price_per_lb"],
                "is_share": True,
                "share_fraction": float(d.get("share_fraction") or 1.0),
                "share_slot": d.get("share_slot") or "whole",
                "share_label": f"{carcass_name} · {slot_lbl}",
                "weight_basis": str(d.get("weight_basis") or "hanging"),
            }
        )

    items.sort(key=lambda x: (str(x.get("harvest_date") or ""), int(x.get("id") or 0)), reverse=True)
    return items


def list_customer_birds(user_id: int, customer_id: int) -> list[dict]:
    with get_db_connection() as conn:
        return _query_assigned_harvest_items(conn, user_id, customer_id=customer_id)


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
    payment_method: str | None = None,
) -> bool:
    pay = payment_status if payment_status in PAYMENT_STATUSES else "unpaid"
    pstat = pickup_status if pickup_status in PICKUP_STATUSES else "unassigned"
    actual_val = round(max(0.0, float(actual_sale_price)), 2) if actual_sale_price is not None else None
    pm_val = (
        normalize_homestead_payment_method(payment_method)
        if payment_method is not None
        else None
    )
    if not for_sale:
        customer_id = None
        pstat = "unassigned"
        actual_val = None
        pm_val = ""
    elif customer_id is not None and pstat == "unassigned":
        pstat = "assigned"
    elif customer_id is None and pstat != "unassigned":
        pstat = "unassigned"
    now_utc = datetime.utcnow().isoformat(timespec="seconds")
    claimed_at = now_utc if customer_id else ""
    with get_db_connection() as conn:
        pm_sql = ", payment_method = ?" if pm_val is not None else ""
        pm_args: list[object] = [pm_val] if pm_val is not None else []
        if notes is not None:
            cur = conn.execute(
                f"""
                UPDATE batch_harvests
                SET customer_id = ?, for_sale = ?, deposit_paid = ?, actual_sale_price = ?,
                    payment_status = ?, pickup_status = ?,
                    claimed_at = CASE WHEN ? != '' AND COALESCE(claimed_at, '') = '' THEN ? WHEN ? = '' THEN '' ELSE claimed_at END,
                    paid_at = CASE
                        WHEN ? = 'paid' AND COALESCE(paid_at, '') = '' THEN ?
                        WHEN ? != 'paid' AND ? != 'picked_up' THEN ''
                        ELSE COALESCE(paid_at, '')
                    END,
                    notes = ?{pm_sql}
                WHERE id = ? AND batch_id = ?
                """,
                [
                    int(customer_id) if customer_id else None,
                    1 if for_sale else 0,
                    max(0.0, float(deposit_paid or 0.0)),
                    actual_val,
                    pay,
                    pstat,
                    claimed_at,
                    claimed_at,
                    claimed_at,
                    pay,
                    now_utc,
                    pay,
                    pstat,
                    str(notes).strip(),
                    *pm_args,
                    int(harvest_id),
                    int(batch_id),
                ],
            )
        else:
            cur = conn.execute(
                f"""
                UPDATE batch_harvests
                SET customer_id = ?, for_sale = ?, deposit_paid = ?, actual_sale_price = ?,
                    payment_status = ?, pickup_status = ?,
                    claimed_at = CASE WHEN ? != '' AND COALESCE(claimed_at, '') = '' THEN ? WHEN ? = '' THEN '' ELSE claimed_at END,
                    paid_at = CASE
                        WHEN ? = 'paid' AND COALESCE(paid_at, '') = '' THEN ?
                        WHEN ? != 'paid' AND ? != 'picked_up' THEN ''
                        ELSE COALESCE(paid_at, '')
                    END{pm_sql}
                WHERE id = ? AND batch_id = ?
                """,
                [
                    int(customer_id) if customer_id else None,
                    1 if for_sale else 0,
                    max(0.0, float(deposit_paid or 0.0)),
                    actual_val,
                    pay,
                    pstat,
                    claimed_at,
                    claimed_at,
                    claimed_at,
                    pay,
                    now_utc,
                    pay,
                    pstat,
                    *pm_args,
                    int(harvest_id),
                    int(batch_id),
                ],
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
        bird_rows = _query_assigned_harvest_items(conn, user_id, customer_id=None)

    by_cust: dict[int, list[dict]] = {}
    for r in bird_rows:
        by_cust.setdefault(int(r["customer_id"]), []).append(r)

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
    sale_unit: str | None = None,
    customer_pays_butcher: bool | None = None,
    butcher_name: str | None = None,
    butcher_phone: str | None = None,
    butcher_email: str | None = None,
) -> dict | None:
    cap_val = int(preorder_cap) if preorder_cap is not None and int(preorder_cap) > 0 else None
    dep_val = round(max(0.0, float(deposit_per_bird or 0.0)), 2)
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT picking_slug, COALESCE(sale_unit, 'whole') AS sale_unit,
                   COALESCE(customer_pays_butcher, 0) AS customer_pays_butcher,
                   COALESCE(butcher_name, '') AS butcher_name,
                   COALESCE(butcher_phone, '') AS butcher_phone,
                   COALESCE(butcher_email, '') AS butcher_email
            FROM batches WHERE id = ? AND user_id = ?
            """,
            (int(batch_id), int(user_id)),
        ).fetchone()
        if not row:
            return None
        slug = row["picking_slug"] or secrets.token_urlsafe(10)
        new_unit = normalize_sale_unit(sale_unit) if sale_unit is not None else str(row["sale_unit"] or "whole")
        new_cpb = (
            (1 if customer_pays_butcher else 0)
            if customer_pays_butcher is not None
            else int(row["customer_pays_butcher"] or 0)
        )
        new_bn = str(butcher_name).strip()[:120] if butcher_name is not None else str(row["butcher_name"] or "")
        new_bp = str(butcher_phone).strip()[:60] if butcher_phone is not None else str(row["butcher_phone"] or "")
        new_be = str(butcher_email).strip()[:120] if butcher_email is not None else str(row["butcher_email"] or "")
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
                picking_slug = ?,
                sale_unit = ?,
                customer_pays_butcher = ?,
                butcher_name = ?,
                butcher_phone = ?,
                butcher_email = ?
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
                new_unit,
                new_cpb,
                new_bn,
                new_bp,
                new_be,
                int(batch_id),
                int(user_id),
            ),
        )
        if new_unit == "shares":
            backfill_batch_harvest_shares(int(batch_id), conn=conn)
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
            SELECT status, quantity, COALESCE(share_fraction, 1.0) AS share_fraction,
                   deposit_per_bird, deposit_paid
            FROM batch_preorders
            WHERE batch_id = ? AND customer_id = ? AND status IN ('reserved', 'waitlist')
            """,
            (int(batch_id), int(customer_id)),
        ).fetchall()
        applied_bh = conn.execute(
            """
            SELECT COUNT(*) AS claimed_cnt, COALESCE(SUM(deposit_paid), 0) AS applied_dep
            FROM batch_harvests bh
            WHERE bh.batch_id = ? AND bh.customer_id = ? AND COALESCE(bh.for_sale, 1) = 1
              AND NOT EXISTS (SELECT 1 FROM harvest_shares hs0 WHERE hs0.harvest_id = bh.id)
            """,
            (int(batch_id), int(customer_id)),
        ).fetchone()
        applied_hs = conn.execute(
            """
            SELECT COUNT(*) AS claimed_cnt,
                   COALESCE(SUM(COALESCE(share_fraction, 1.0)), 0.0) AS claimed_equiv,
                   COALESCE(SUM(deposit_paid), 0) AS applied_dep
            FROM harvest_shares
            WHERE batch_id = ? AND customer_id = ? AND COALESCE(for_sale, 1) = 1
            """,
            (int(batch_id), int(customer_id)),
        ).fetchone()
    reserved_qty = sum(int(r["quantity"] or 0) for r in rows if r["status"] == "reserved")
    waitlist_qty = sum(int(r["quantity"] or 0) for r in rows if r["status"] == "waitlist")
    reserved_equiv = round(
        sum(int(r["quantity"] or 0) * float(r["share_fraction"] or 1.0) for r in rows if r["status"] == "reserved"),
        2,
    )
    waitlist_equiv = round(
        sum(int(r["quantity"] or 0) * float(r["share_fraction"] or 1.0) for r in rows if r["status"] == "waitlist"),
        2,
    )
    total_paid = round(sum(float(r["deposit_paid"] or 0.0) for r in rows if r["status"] == "reserved"), 2)
    per_bird = max((float(r["deposit_per_bird"] or 0.0) for r in rows if r["status"] == "reserved"), default=0.0)
    applied_dep = round(
        float(applied_bh["applied_dep"] or 0.0) + float(applied_hs["applied_dep"] or 0.0), 2
    )
    claimed_cnt = int(applied_bh["claimed_cnt"] or 0) + int(applied_hs["claimed_cnt"] or 0)
    claimed_equiv = round(
        float(applied_bh["claimed_cnt"] or 0) + float(applied_hs["claimed_equiv"] or 0.0), 2
    )
    return {
        "has_reservation": reserved_qty > 0,
        "reserved_qty": reserved_qty,
        "waitlist_qty": waitlist_qty,
        "reserved_equiv": reserved_equiv,
        "waitlist_equiv": waitlist_equiv,
        "deposit_per_bird": round(per_bird, 2),
        "total_deposit_paid": total_paid,
        "already_applied_deposit": applied_dep,
        "remaining_unapplied_deposit": round(max(0.0, total_paid - applied_dep), 2),
        "claimed_count": claimed_cnt,
        "claimed_equiv": claimed_equiv,
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
    share_fraction: float = 1.0,
    share_label: str = "",
) -> dict:
    qty = max(1, int(quantity or 1))
    st = status if status in ("reserved", "waitlist", "cancelled") else "reserved"
    frac = round(max(0.25, float(share_fraction or 1.0)), 2)
    s_lbl = str(share_label or "").strip().lower()
    dpb = round(max(0.0, float(deposit_per_bird or 0.0)), 2)
    dp = round(max(0.0, float(deposit_paid or 0.0)), 2)
    expected_dep = round(dpb * qty * frac, 2)
    dst = "paid" if (deposit_status == "paid" or (expected_dep > 0 and dp >= expected_dep and dp > 0)) else "unpaid"
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO batch_preorders (
                batch_id, user_id, customer_id, quantity, status,
                deposit_per_bird, deposit_paid, deposit_status, notes,
                share_fraction, share_label, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(batch_id), int(user_id), int(customer_id), qty, st,
                dpb, dp, dst, str(notes or "").strip(), frac, s_lbl, now_iso,
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
    share_fraction: float = 1.0,
    share_label: str = "",
) -> dict:
    """Atomically reserve quantity if under cap (in animal equivalents), else place on waitlist."""
    qty = max(1, min(50, int(quantity or 1)))
    frac = round(max(0.25, float(share_fraction or 1.0)), 2)
    s_lbl = str(share_label or "").strip().lower()
    dpb = round(max(0.0, float(deposit_per_bird or 0.0)), 2)
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        reserved_equiv = float(
            conn.execute(
                "SELECT COALESCE(SUM(quantity * COALESCE(share_fraction, 1.0)), 0.0)"
                " FROM batch_preorders WHERE batch_id = ? AND status = 'reserved'",
                (int(batch_id),),
            ).fetchone()[0]
        )
        if cap is not None and int(cap) > 0 and (reserved_equiv + (qty * frac)) > (float(cap) + 1e-6):
            status = "waitlist"
        else:
            status = "reserved"
        cur = conn.execute(
            """
            INSERT INTO batch_preorders (
                batch_id, user_id, customer_id, quantity, status,
                deposit_per_bird, deposit_paid, deposit_status, notes,
                share_fraction, share_label, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0.0, 'unpaid', ?, ?, ?, ?)
            """,
            (
                int(batch_id), int(user_id), int(customer_id), qty, status,
                dpb, str(notes or "").strip(), frac, s_lbl, now_iso,
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
    share_fraction: float | None = None,
    share_label: str | None = None,
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
        new_frac = (
            round(max(0.25, float(share_fraction)), 2)
            if share_fraction is not None
            else float(existing["share_fraction"] or 1.0)
        )
        new_slabel = (
            str(share_label).strip().lower()
            if share_label is not None
            else str(existing["share_label"] or "")
        )
        conn.execute(
            """
            UPDATE batch_preorders
            SET status = ?, quantity = ?, deposit_paid = ?, deposit_status = ?, notes = ?,
                share_fraction = ?, share_label = ?
            WHERE id = ? AND batch_id = ? AND user_id = ?
            """,
            (
                new_status, new_qty, new_dp, new_dst, new_notes,
                new_frac, new_slabel,
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
                   SUM(CASE WHEN status = 'reserved' THEN quantity * COALESCE(share_fraction, 1.0) ELSE 0 END) AS reserved_equiv,
                   SUM(CASE WHEN status = 'waitlist' THEN quantity * COALESCE(share_fraction, 1.0) ELSE 0 END) AS waitlist_equiv,
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
            "reserved_equiv": round(float(r["reserved_equiv"] or 0.0), 2),
            "waitlist_equiv": round(float(r["waitlist_equiv"] or 0.0), 2),
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
    bird_ids: list[int] | None = None,
    payment_method: str | None = None,
) -> int:
    """Settle selected (or all) for-sale birds assigned to customer_id in batch_id on Pickup Day.

    If ``bird_ids`` is provided, only those specific bird IDs are settled (supporting partial
    pickups while keeping remaining birds assigned to the customer).
    If ``collected_today`` is provided, computes ``new_total = total_deposits + collected_today``
    for the settled birds and distributes any difference proportionally into ``actual_sale_price``.
    """
    clean_ids = [int(x) for x in (bird_ids or []) if int(x) > 0]
    pm = (
        normalize_homestead_payment_method(payment_method)
        if payment_method is not None
        else None
    )
    now_utc = datetime.utcnow().isoformat(timespec="seconds")
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
            birds = conn.execute(
                f"""
                SELECT id, dressed_lbs, deposit_paid, actual_sale_price, payment_status, pickup_status
                FROM batch_harvests
                WHERE batch_id = ? AND customer_id = ? AND COALESCE(for_sale, 1) = 1
                  AND id IN ({marks})
                ORDER BY id ASC
                """,
                [int(batch_id), int(customer_id), *clean_ids],
            ).fetchall()
        else:
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
            if pm is not None:
                conn.execute(
                    """
                    UPDATE batch_harvests
                    SET payment_status = ?, pickup_status = ?, actual_sale_price = ?,
                        payment_method = ?,
                        paid_at = CASE WHEN ? = 'paid' AND COALESCE(paid_at, '') = '' THEN ? ELSE COALESCE(paid_at, '') END
                    WHERE id = ? AND batch_id = ?
                    """,
                    (pay_stat, pick_stat, act_p, pm, pay_stat, now_utc, int(b["id"]), int(batch_id)),
                )
            else:
                conn.execute(
                    """
                    UPDATE batch_harvests
                    SET payment_status = ?, pickup_status = ?, actual_sale_price = ?,
                        paid_at = CASE WHEN ? = 'paid' AND COALESCE(paid_at, '') = '' THEN ? ELSE COALESCE(paid_at, '') END
                    WHERE id = ? AND batch_id = ?
                    """,
                    (pay_stat, pick_stat, act_p, pay_stat, now_utc, int(b["id"]), int(batch_id)),
                )
        conn.commit()
        return len(bird_list)


def update_customer_batch_payment_method(
    user_id: int,
    batch_id: int,
    customer_id: int,
    payment_method: str,
) -> int:
    """Update payment_method across paid/picked-up birds for a customer in an owned batch."""
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
            UPDATE batch_harvests
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
