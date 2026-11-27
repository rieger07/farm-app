"""Cross-app database helpers bridging Homestead Planner and Iron Ledger tables."""

from __future__ import annotations

from datetime import date, datetime, timezone
import sqlite3

from shared.db.connection import get_connection


HOMESTEAD_PAYMENT_METHODS = ("cash", "venmo", "paypal", "card", "check")


def normalize_homestead_payment_method(value: str | None) -> str:
    """Validate and normalize a Homestead payment method (empty string clears tag)."""
    clean = str(value or "").strip().lower()
    if not clean:
        return ""
    if clean not in HOMESTEAD_PAYMENT_METHODS:
        raise ValueError(f"Unsupported payment method: {value}")
    return clean


def _get_merchant_owner_user_id(
    conn: sqlite3.Connection, merchant_id: int
) -> int | None:
    """Resolve the primary Homestead owner user_id for ``merchant_id``."""
    row = conn.execute(
        """
        SELECT user_id
        FROM merchant_members
        WHERE merchant_id = ?
        ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'manager' THEN 1 ELSE 2 END,
                 created_at ASC, user_id ASC
        LIMIT 1
        """,
        (int(merchant_id),),
    ).fetchone()
    return int(row["user_id"]) if row else None


def _safe_iso_date(raw: str | None) -> str | None:
    txt = str(raw or "").strip()[:10]
    if len(txt) != 10:
        return None
    try:
        date.fromisoformat(txt)
        return txt
    except ValueError:
        return None


def _safe_pm(raw: str | None) -> str:
    clean = str(raw or "").strip().lower()
    return clean if clean in HOMESTEAD_PAYMENT_METHODS else ""


def _resolve_harvest_timestamp(
    paid_at: str | None,
    pickup_date: str | None,
    harvest_date: str | None,
    tz,
) -> tuple[str, str, str, str]:
    """Return (local_date, sort_ts, occurred_at, time_label) for a paid bird."""
    target_tz = tz if tz is not None else timezone.utc
    raw_paid = str(paid_at or "").strip()
    if raw_paid:
        try:
            dt = datetime.fromisoformat(raw_paid.replace("Z", ""))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            local_dt = dt.astimezone(target_tz)
            return (
                local_dt.date().isoformat(),
                local_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"),
                local_dt.strftime("%-I:%M %p").lower(),
            )
        except ValueError:
            pass
    d_str = _safe_iso_date(pickup_date) or _safe_iso_date(harvest_date) or "1970-01-01"
    return (
        d_str,
        f"{d_str}T12:00:00",
        f"{d_str}T12:00:00",
        "Farm pickup",
    )


def list_active_ledger_skus(
    merchant_id: int | None = None,
    prefer_keywords: tuple[str, ...] = ("egg",),
) -> list[dict]:
    """List active non-fee Iron Ledger SKUs ordered with keyword-matching products first."""
    m_clause = " AND k.merchant_id = ?" if merchant_id is not None else ""
    params: list[object] = [int(merchant_id)] if merchant_id is not None else []
    kws = tuple(str(kw).strip().lower() for kw in (prefer_keywords or ()) if str(kw).strip())
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT k.id, k.merchant_id, k.name, k.variant_name, k.price_cents,
                   COALESCE(m.name, 'Merchant #' || k.merchant_id) AS merchant_name,
                   COALESCE((SELECT SUM(delta) FROM stock_ledger l WHERE l.sku_id = k.id), 0) AS on_hand
            FROM skus k
            LEFT JOIN merchants m ON m.id = k.merchant_id
            WHERE k.active = 1 AND COALESCE(k.item_type, 'product') != 'fee'{m_clause}
            ORDER BY k.name ASC, k.id ASC
            """,
            tuple(params),
        ).fetchall()
        out = []
        for r in rows:
            label = r["name"] + (f" · {r['variant_name']}" if r["variant_name"] else "")
            out.append(
                {
                    "id": int(r["id"]),
                    "merchant_id": int(r["merchant_id"]),
                    "merchant_name": r["merchant_name"],
                    "name": r["name"],
                    "variant_name": r["variant_name"] or "",
                    "label": label,
                    "price_cents": int(r["price_cents"] or 0),
                    "on_hand": float(r["on_hand"] or 0.0),
                }
            )
        out.sort(
            key=lambda item: (
                0
                if any(kw in f"{item['name']} {item['variant_name']}".lower() for kw in kws)
                else 1,
                item["name"].lower(),
                item["id"],
            )
        )
        return out


def restock_ledger_sku(
    sku_id: int,
    delta: float,
    note: str = "",
    created_by: int | None = None,
    created_at: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[int | None, int | None]:
    """Append a 'restock' stock_ledger entry for an existing SKU. Returns (sku_id, ledger_entry_id)."""
    owns_conn = conn is None
    c = conn if conn is not None else get_connection()
    ts = created_at or datetime.utcnow().isoformat(timespec="seconds")
    try:
        sku_row = c.execute(
            "SELECT id, merchant_id FROM skus WHERE id = ?",
            (int(sku_id),),
        ).fetchone()
        if not sku_row:
            return None, None
        cur = c.execute(
            """
            INSERT INTO stock_ledger (
                merchant_id, sku_id, delta, reason, note, created_by, created_at
            ) VALUES (?, ?, ?, 'restock', ?, ?, ?)
            """,
            (
                int(sku_row["merchant_id"]),
                int(sku_id),
                delta,
                str(note or "").strip(),
                int(created_by) if created_by is not None else None,
                ts,
            ),
        )
        if owns_conn:
            c.commit()
        return int(sku_id), int(cur.lastrowid)
    finally:
        if owns_conn:
            c.close()


def delete_ledger_entry(
    ledger_entry_id: int,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Remove a specific stock_ledger row (used when undoing a carton pack)."""
    owns_conn = conn is None
    c = conn if conn is not None else get_connection()
    try:
        c.execute("DELETE FROM stock_ledger WHERE id = ?", (int(ledger_entry_id),))
        if owns_conn:
            c.commit()
    finally:
        if owns_conn:
            c.close()


def get_sku_completed_sales_summary(
    sku_id: int,
    conn: sqlite3.Connection | None = None,
) -> tuple[float, float]:
    """Return (units_sold, revenue_dollars) across completed sales for a SKU."""
    owns_conn = conn is None
    c = conn if conn is not None else get_connection()
    try:
        row = c.execute(
            """
            SELECT COALESCE(SUM(si.quantity), 0) AS qty,
                   COALESCE(SUM(si.quantity * si.unit_price_cents), 0) AS rev_cents
            FROM sale_items si
            JOIN sales s ON s.id = si.sale_id
            WHERE si.sku_id = ? AND s.status = 'completed'
            """,
            (int(sku_id),),
        ).fetchone()
        qty = round(float(row["qty"] or 0.0), 1)
        rev = round(float(row["rev_cents"] or 0.0) / 100.0, 2)
        return qty, rev
    finally:
        if owns_conn:
            c.close()


def delete_user_account(
    user_id: int, acting_user_id: int | None = None
) -> None:
    """Delete a user account and clean up Homestead & Core child rows atomically."""
    if acting_user_id is not None and int(user_id) == int(acting_user_id):
        raise ValueError("You cannot delete your own account.")
    with get_connection() as conn:
        target = conn.execute(
            "SELECT id, is_admin FROM users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
        if not target:
            raise ValueError("User not found.")
        if target["is_admin"]:
            admins = conn.execute(
                "SELECT COUNT(*) FROM users WHERE is_admin = 1"
            ).fetchone()[0]
            if admins <= 1:
                raise ValueError("Cannot delete the last Super Admin.")
        # Legacy batches.user_id lacks an FK constraint; child batch tables cascade on batches(id).
        conn.execute("DELETE FROM batches WHERE user_id = ?", (int(user_id),))
        conn.execute("DELETE FROM users WHERE id = ?", (int(user_id),))
        conn.commit()


def list_ledger_market_day_costs(
    merchant_id: int | None, tax_year: int | None = None
) -> list[dict]:
    """Read-only Iron Ledger market booth/travel fees for Schedule F Tax Books."""
    if not merchant_id:
        return []
    params: list[object] = [int(merchant_id)]
    year_clause = ""
    if tax_year is not None:
        year_clause = " AND substr(md.market_date, 1, 4) = ?"
        params.append(str(int(tax_year)))
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT md.id, md.market_date, mp.name AS marketplace_name,
                   mp.location, md.booth_fee_cents, md.travel_cost_cents, md.notes
            FROM market_days md
            JOIN marketplaces mp ON mp.id = md.marketplace_id
            WHERE md.merchant_id = ?
              AND (md.booth_fee_cents > 0 OR md.travel_cost_cents > 0)
              {year_clause}
            ORDER BY md.market_date DESC, md.id DESC
            """,
            tuple(params),
        ).fetchall()
        out = []
        for r in rows:
            booth = round(float(r["booth_fee_cents"] or 0) / 100.0, 2)
            travel = round(float(r["travel_cost_cents"] or 0) / 100.0, 2)
            out.append(
                {
                    "id": int(r["id"]),
                    "market_date": str(r["market_date"] or "")[:10],
                    "marketplace_name": str(r["marketplace_name"] or "Market"),
                    "location": str(r["location"] or ""),
                    "booth_fee_dollars": booth,
                    "travel_cost_dollars": travel,
                    "total_dollars": round(booth + travel, 2),
                    "notes": str(r["notes"] or ""),
                }
            )
        return out


def get_ledger_sales_summary_by_year(
    merchant_id: int | None, tax_year: int | None = None
) -> dict:
    """Read-only completed Iron Ledger POS sales summary (and active years)."""
    if not merchant_id:
        return {"total_dollars": 0.0, "sale_count": 0, "years": []}
    with get_connection() as conn:
        year_rows = conn.execute(
            """
            SELECT DISTINCT substr(occurred_at, 1, 4) AS yr
            FROM sales
            WHERE merchant_id = ? AND status = 'completed'
            """,
            (int(merchant_id),),
        ).fetchall()
        years = sorted(
            {
                int(str(r["yr"]))
                for r in year_rows
                if r["yr"] and str(r["yr"]).isdigit() and len(str(r["yr"])) == 4
            }
        )
        if tax_year is not None:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(total_cents), 0) AS total_cents, COUNT(*) AS cnt
                FROM sales
                WHERE merchant_id = ? AND status = 'completed'
                  AND substr(occurred_at, 1, 4) = ?
                """,
                (int(merchant_id), str(int(tax_year))),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(total_cents), 0) AS total_cents, COUNT(*) AS cnt
                FROM sales
                WHERE merchant_id = ? AND status = 'completed'
                """,
                (int(merchant_id),),
            ).fetchone()
        total_cents = float(row["total_cents"] or 0.0) if row else 0.0
        cnt = int(row["cnt"] or 0) if row else 0
        return {
            "total_dollars": round(total_cents / 100.0, 2),
            "sale_count": cnt,
            "years": years,
        }


def list_homestead_paid_transactions(
    merchant_id: int | None,
    date_from: str | None = None,
    date_to: str | None = None,
    tz=None,
) -> list[dict]:
    """Read paid Homestead harvest & bulk sales live for Iron Ledger display (no DB duplication)."""
    if not merchant_id:
        return []
    with get_connection() as conn:
        owner_uid = _get_merchant_owner_user_id(conn, int(merchant_id))
        if owner_uid is None:
            return []

        harvest_rows = conn.execute(
            """
            SELECT bh.id, bh.batch_id, bh.customer_id, bh.harvest_date, bh.head_count,
                   bh.dressed_lbs, bh.actual_sale_price, bh.payment_status, bh.pickup_status,
                   COALESCE(bh.payment_method, '') AS payment_method,
                   COALESCE(bh.paid_at, '') AS paid_at,
                   COALESCE(bh.notes, '') AS notes,
                   b.name AS batch_name, b.sale_price_per_lb,
                   COALESCE(b.pickup_date, '') AS pickup_date,
                   COALESCE(c.name, '') AS customer_name
            FROM batch_harvests bh
            JOIN batches b ON b.id = bh.batch_id
            LEFT JOIN customers c ON c.id = bh.customer_id
            WHERE b.user_id = ?
              AND COALESCE(bh.for_sale, 1) = 1
              AND NOT EXISTS (SELECT 1 FROM harvest_shares hs0 WHERE hs0.harvest_id = bh.id)
              AND (
                LOWER(COALESCE(bh.payment_status, '')) = 'paid'
                OR LOWER(COALESCE(bh.pickup_status, '')) = 'picked_up'
                OR bh.actual_sale_price IS NOT NULL
              )
            ORDER BY bh.id ASC
            """,
            (owner_uid,),
        ).fetchall()

        share_rows = conn.execute(
            """
            SELECT hs.id, hs.harvest_id, hs.batch_id, hs.customer_id,
                   hs.share_slot, hs.share_fraction, hs.share_weight_lbs,
                   hs.actual_sale_price, hs.payment_status, hs.pickup_status,
                   COALESCE(hs.payment_method, '') AS payment_method,
                   COALESCE(hs.paid_at, '') AS paid_at,
                   COALESCE(hs.cut_sheet_notes, '') AS notes,
                   bh.harvest_date, bh.dressed_lbs AS carcass_lbs,
                   COALESCE(bh.animal_label, '') AS animal_label,
                   COALESCE(bh.weight_basis, 'hanging') AS weight_basis,
                   b.name AS batch_name, b.sale_price_per_lb,
                   COALESCE(b.pickup_date, '') AS pickup_date,
                   COALESCE(c.name, '') AS customer_name
            FROM harvest_shares hs
            JOIN batch_harvests bh ON bh.id = hs.harvest_id
            JOIN batches b ON b.id = hs.batch_id
            LEFT JOIN customers c ON c.id = hs.customer_id
            WHERE b.user_id = ?
              AND COALESCE(hs.for_sale, 1) = 1
              AND (
                LOWER(COALESCE(hs.payment_status, '')) = 'paid'
                OR LOWER(COALESCE(hs.pickup_status, '')) = 'picked_up'
              )
            ORDER BY hs.id ASC
            """,
            (owner_uid,),
        ).fetchall()

        batch_sale_rows = conn.execute(
            """
            SELECT bs.id, bs.batch_id, bs.entry_date, bs.weight_lbs, bs.revenue,
                   COALESCE(bs.payment_method, '') AS payment_method,
                   COALESCE(bs.notes, '') AS notes,
                   b.name AS batch_name
            FROM batch_sales bs
            JOIN batches b ON b.id = bs.batch_id
            WHERE b.user_id = ? AND bs.allocation_type = 'Customer Sale'
            ORDER BY bs.entry_date DESC, bs.id DESC
            """,
            (owner_uid,),
        ).fetchall()

    def _accumulate_group(
        buckets: dict[tuple, dict],
        key: tuple,
        *,
        record_type: str,
        rec_id: int,
        batch_id: int,
        bname: str,
        customer_name: str,
        local_date: str,
        occurred_at: str,
        sort_ts: str,
        time_label: str,
        pm: str,
        price_cents: int,
        qty: int,
        lbs: float,
        item_name: str,
        source_id: str | None = None,
    ) -> None:
        grp = buckets.get(key)
        if grp is None:
            grp = {
                "source": "homestead",
                "record_type": record_type,
                "record_ids": [],
                "batch_id": batch_id,
                "batch_name": bname,
                "customer_name": customer_name or "Walk-in / Unassigned",
                "date_str": local_date,
                "occurred_at": occurred_at,
                "sort_ts": sort_ts,
                "time_label": time_label,
                "total_cents": 0,
                "units": 0,
                "total_lbs": 0.0,
                "pay_method": pm,
                "pay_label": pm.capitalize() if pm else "Untagged",
                "payment_type": pm,
                "status": "completed",
                "cash_received_cents": None,
                "change_cents": None,
                "items": [],
            }
            if source_id:
                grp["source_id"] = source_id
            buckets[key] = grp

        grp["record_ids"].append(rec_id)
        grp["total_cents"] += price_cents
        grp["units"] += qty
        grp["total_lbs"] = round(grp["total_lbs"] + lbs, 2)
        if sort_ts > grp["sort_ts"]:
            grp["sort_ts"] = sort_ts
            grp["occurred_at"] = occurred_at
            grp["time_label"] = time_label
        grp["items"].append(
            {
                "id": rec_id,
                "name_snapshot": item_name,
                "quantity": qty,
                "unit_price_cents": price_cents,
                "subtotal_cents": price_cents,
                "is_fee": False,
            }
        )

    grouped_harvests: dict[tuple, dict] = {}
    for r in harvest_rows:
        local_date, sort_ts, occurred_at, time_label = _resolve_harvest_timestamp(
            r["paid_at"], r["pickup_date"], r["harvest_date"], tz
        )
        if date_from and local_date < date_from:
            continue
        if date_to and local_date > date_to:
            continue

        pm = _safe_pm(r["payment_method"])
        lbs = round(float(r["dressed_lbs"] or 0.0), 2)
        if r["actual_sale_price"] is not None:
            amt_dollars = float(r["actual_sale_price"] or 0.0)
        else:
            amt_dollars = round(lbs * float(r["sale_price_per_lb"] or 0.0), 2)
        price_cents = int(round(max(0.0, amt_dollars) * 100))
        qty = max(1, int(r["head_count"] or 1))
        note = str(r["notes"] or "").strip()
        bname = str(r["batch_name"] or "Batch")
        item_name = f"{bname} · {lbs:.2f} lb" + (f" ({note})" if note else "")

        cid = int(r["customer_id"]) if r["customer_id"] else None
        key = ("harvest", int(r["batch_id"]), cid, local_date, pm) if cid is not None else ("harvest_single", int(r["id"]))
        _accumulate_group(
            grouped_harvests,
            key,
            record_type="harvest",
            rec_id=int(r["id"]),
            batch_id=int(r["batch_id"]),
            bname=bname,
            customer_name=str(r["customer_name"] or "").strip(),
            local_date=local_date,
            occurred_at=occurred_at,
            sort_ts=sort_ts,
            time_label=time_label,
            pm=pm,
            price_cents=price_cents,
            qty=qty,
            lbs=lbs,
            item_name=item_name,
        )

    grouped_shares: dict[tuple, dict] = {}
    for r in share_rows:
        local_date, sort_ts, occurred_at, time_label = _resolve_harvest_timestamp(
            r["paid_at"], r["pickup_date"], r["harvest_date"], tz
        )
        if date_from and local_date < date_from:
            continue
        if date_to and local_date > date_to:
            continue

        pm = _safe_pm(r["payment_method"])
        if r["share_weight_lbs"] is not None:
            lbs = round(max(0.0, float(r["share_weight_lbs"])), 2)
        else:
            lbs = round(max(0.0, float(r["carcass_lbs"] or 0.0)) * float(r["share_fraction"] or 1.0), 2)
        if r["actual_sale_price"] is not None:
            amt_dollars = float(r["actual_sale_price"] or 0.0)
        else:
            amt_dollars = round(lbs * float(r["sale_price_per_lb"] or 0.0), 2)
        price_cents = int(round(max(0.0, amt_dollars) * 100))

        slot_raw = str(r["share_slot"] or "").strip().lower()
        slot_lbl = "Half A" if slot_raw == "half_a" else ("Half B" if slot_raw == "half_b" else "Whole")
        carcass_lbl = str(r["animal_label"] or "").strip() or f"Carcass #{int(r['harvest_id'])}"
        basis = str(r["weight_basis"] or "hanging").strip().lower()
        note = str(r["notes"] or "").strip()
        bname = str(r["batch_name"] or "Batch")
        item_name = f"{bname} · {carcass_lbl} {slot_lbl} · {lbs:.1f} lb {basis}" + (f" ({note})" if note else "")

        cid = int(r["customer_id"]) if r["customer_id"] else None
        key = ("share", int(r["batch_id"]), cid, local_date, pm) if cid is not None else ("share_single", int(r["id"]))
        cid_key = cid if cid is not None else f"unassigned_{int(r['id'])}"
        _accumulate_group(
            grouped_shares,
            key,
            record_type="share",
            rec_id=int(r["id"]),
            batch_id=int(r["batch_id"]),
            bname=bname,
            customer_name=str(r["customer_name"] or "").strip(),
            local_date=local_date,
            occurred_at=occurred_at,
            sort_ts=sort_ts,
            time_label=time_label,
            pm=pm,
            price_cents=price_cents,
            qty=1,
            lbs=lbs,
            item_name=item_name,
            source_id=f"homestead:share:{int(r['batch_id'])}:{cid_key}:{local_date}:{pm}",
        )

    out: list[dict] = []
    for grp in grouped_harvests.values():
        grp["id"] = f"H-{grp['record_ids'][0]}"
        grp["record_ids_csv"] = ",".join(str(x) for x in grp["record_ids"])
        out.append(grp)
    for grp in grouped_shares.values():
        grp["id"] = f"HS-{grp['record_ids'][0]}"
        grp["record_ids_csv"] = ",".join(str(x) for x in grp["record_ids"])
        out.append(grp)

    for r in batch_sale_rows:
        d_str = _safe_iso_date(r["entry_date"]) or "1970-01-01"
        if date_from and d_str < date_from:
            continue
        if date_to and d_str > date_to:
            continue
        pm = _safe_pm(r["payment_method"])
        lbs = round(float(r["weight_lbs"] or 0.0), 2)
        price_cents = int(round(max(0.0, float(r["revenue"] or 0.0)) * 100))
        note = str(r["notes"] or "").strip()
        bname = str(r["batch_name"] or "Batch")
        item_name = f"{bname} · {lbs:.2f} lb bulk sale" + (f" ({note})" if note else "")
        out.append(
            {
                "id": f"BS-{int(r['id'])}",
                "source": "homestead",
                "record_type": "batch_sale",
                "record_ids": [int(r["id"])],
                "record_ids_csv": str(int(r["id"])),
                "batch_id": int(r["batch_id"]),
                "batch_name": bname,
                "customer_name": "Bulk Customer Sale",
                "date_str": d_str,
                "occurred_at": f"{d_str}T12:00:00",
                "sort_ts": f"{d_str}T12:00:00",
                "time_label": "Batch sale",
                "total_cents": price_cents,
                "units": 1,
                "total_lbs": lbs,
                "pay_method": pm,
                "pay_label": pm.capitalize() if pm else "Untagged",
                "payment_type": pm,
                "status": "completed",
                "cash_received_cents": None,
                "change_cents": None,
                "items": [
                    {
                        "id": int(r["id"]),
                        "name_snapshot": item_name,
                        "quantity": 1,
                        "unit_price_cents": price_cents,
                        "subtotal_cents": price_cents,
                        "is_fee": False,
                    }
                ],
            }
        )

    out.sort(key=lambda t: (t["sort_ts"], t["record_ids"][0]), reverse=True)
    return out


def update_homestead_transaction_payment_method(
    merchant_id: int,
    record_type: str,
    record_ids: list[int],
    payment_method: str | None,
) -> int:
    """Update payment_method on workspace-owned Homestead harvest or bulk sale rows."""
    pm = normalize_homestead_payment_method(payment_method)
    ids = [int(x) for x in record_ids if int(x) > 0]
    if not ids:
        raise ValueError("No transaction records selected.")

    with get_connection() as conn:
        owner_uid = _get_merchant_owner_user_id(conn, int(merchant_id))
        if owner_uid is None:
            raise ValueError("Workspace owner not found.")
        marks = ",".join("?" * len(ids))
        if record_type == "harvest":
            cur = conn.execute(
                f"""
                UPDATE batch_harvests
                SET payment_method = ?
                WHERE id IN ({marks})
                  AND batch_id IN (SELECT id FROM batches WHERE user_id = ?)
                """,
                [pm, *ids, owner_uid],
            )
        elif record_type == "batch_sale":
            cur = conn.execute(
                f"""
                UPDATE batch_sales
                SET payment_method = ?
                WHERE id IN ({marks})
                  AND batch_id IN (SELECT id FROM batches WHERE user_id = ?)
                """,
                [pm, *ids, owner_uid],
            )
        elif record_type == "share":
            cur = conn.execute(
                f"""
                UPDATE harvest_shares
                SET payment_method = ?
                WHERE id IN ({marks})
                  AND batch_id IN (SELECT id FROM batches WHERE user_id = ?)
                """,
                [pm, *ids, owner_uid],
            )
        else:
            raise ValueError("Invalid Homestead record type.")
        conn.commit()
        if not cur.rowcount:
            raise ValueError("Homestead transaction was not found in this workspace.")
        return int(cur.rowcount)
