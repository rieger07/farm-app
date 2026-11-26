"""Active Market Day Mode: pack-out allocation, live booth state, closeout & reopen."""

from __future__ import annotations

from datetime import date, datetime, timezone

from inventory.db import get_connection
from inventory.marketplaces import (
    _market_day_window_bounds,
    _now,
    get_marketplace,
    save_marketplace,
)


def list_recent_and_upcoming_market_days(
    merchant_id: int,
    today_iso: str,
    limit: int = 8,
) -> list[dict]:
    """Return upcoming planned pack-ahead days + recent market days for quick switching."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT md.id, md.market_date, COALESCE(md.status, 'open') AS status,
                   m.name AS marketplace_name,
                   COUNT(CASE WHEN mdi.brought_qty > 0 THEN 1 END) AS packed_skus
            FROM market_days md
            JOIN marketplaces m ON m.id = md.marketplace_id
            LEFT JOIN market_day_items mdi ON mdi.market_day_id = md.id
            WHERE md.merchant_id = ?
            GROUP BY md.id
            ORDER BY md.market_date DESC
            LIMIT ?
            """,
            (int(merchant_id), int(limit)),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d_str = r["market_date"]
        status = r["status"] or "open"
        if status == "closed":
            state = "closed"
        elif d_str > today_iso:
            state = "planned"
        else:
            state = "live"
        out.append(
            {
                "id": int(r["id"]),
                "market_date": d_str,
                "marketplace_name": r["marketplace_name"],
                "status": status,
                "state": state,
                "packed_skus": int(r["packed_skus"] or 0),
            }
        )
    return out


def save_market_day_pack(
    merchant_id: int,
    market_date: str,
    marketplace_id: int | None,
    brought_by_sku: dict[int, float],
    new_marketplace_name: str = "",
    booth_fee_cents: int | None = None,
    travel_cost_cents: int | None = None,
    starting_cash_cents: int = 0,
    weather: str = "",
    notes: str = "",
) -> int:
    """Create or update a Market Day session and its virtual booth pack list."""
    from inventory.stock import fmt_qty

    mdate = str(market_date or "").strip()
    date.fromisoformat(mdate)

    clean_new_mp = (new_marketplace_name or "").strip()
    if clean_new_mp:
        mp_id = save_marketplace(
            merchant_id,
            clean_new_mp,
            booth_fee_cents=max(0, int(booth_fee_cents or 0)),
            travel_cost_cents=max(0, int(travel_cost_cents or 0)),
        )
    elif marketplace_id:
        mp_id = int(marketplace_id)
    else:
        raise ValueError("Choose a marketplace or enter a new market name.")

    mp = get_marketplace(merchant_id, mp_id)
    if not mp:
        raise ValueError("Selected marketplace not found.")

    booth = mp["booth_fee_cents"] if booth_fee_cents is None else max(0, int(booth_fee_cents))
    travel = mp["travel_cost_cents"] if travel_cost_cents is None else max(0, int(travel_cost_cents))
    start_cash = max(0, int(starting_cash_cents or 0))
    now = _now()

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO market_days (
                merchant_id, market_date, marketplace_id, booth_fee_cents,
                travel_cost_cents, starting_cash_cents, status, started_at,
                weather, notes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
            ON CONFLICT(merchant_id, market_date) DO UPDATE SET
                marketplace_id = excluded.marketplace_id,
                booth_fee_cents = excluded.booth_fee_cents,
                travel_cost_cents = excluded.travel_cost_cents,
                starting_cash_cents = excluded.starting_cash_cents,
                started_at = COALESCE(market_days.started_at, excluded.started_at),
                weather = CASE WHEN excluded.weather != '' THEN excluded.weather ELSE market_days.weather END,
                notes = CASE WHEN excluded.notes != '' THEN excluded.notes ELSE market_days.notes END
            """,
            (
                int(merchant_id),
                mdate,
                int(mp_id),
                booth,
                travel,
                start_cash,
                now,
                str(weather or "").strip(),
                str(notes or "").strip(),
                now,
            ),
        )
        mday = conn.execute(
            "SELECT id FROM market_days WHERE merchant_id = ? AND market_date = ?",
            (int(merchant_id), mdate),
        ).fetchone()
        mday_id = int(mday["id"])

        valid_skus = {
            int(r["id"])
            for r in conn.execute(
                "SELECT id FROM skus WHERE merchant_id = ?",
                (int(merchant_id),),
            ).fetchall()
        }

        for raw_sid, raw_qty in (brought_by_sku or {}).items():
            sid = int(raw_sid)
            if sid not in valid_skus:
                continue
            qty = fmt_qty(max(0.0, float(raw_qty or 0)))
            existing = conn.execute(
                "SELECT write_off_qty, counted_left_qty, closeout_ledger_id"
                " FROM market_day_items WHERE market_day_id = ? AND sku_id = ?",
                (mday_id, sid),
            ).fetchone()
            if qty <= 1e-9 and (
                not existing
                or (
                    float(existing["write_off_qty"] or 0) <= 1e-9
                    and existing["counted_left_qty"] is None
                    and existing["closeout_ledger_id"] is None
                )
            ):
                conn.execute(
                    "DELETE FROM market_day_items WHERE market_day_id = ? AND sku_id = ?",
                    (mday_id, sid),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO market_day_items (market_day_id, sku_id, brought_qty, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(market_day_id, sku_id) DO UPDATE SET
                        brought_qty = excluded.brought_qty,
                        updated_at = excluded.updated_at
                    """,
                    (mday_id, sid, qty, now),
                )
        conn.commit()
        return mday_id
    finally:
        conn.close()


def restock_market_day_item(
    merchant_id: int,
    market_date: str,
    sku_id: int,
    delta_qty: float,
) -> float:
    """Increment or decrement brought_qty for a single SKU on an existing Market Day."""
    from inventory.stock import fmt_qty

    mdate = str(market_date or "").strip()
    date.fromisoformat(mdate)
    now = _now()
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        mday = conn.execute(
            "SELECT id FROM market_days WHERE merchant_id = ? AND market_date = ?",
            (int(merchant_id), mdate),
        ).fetchone()
        if not mday:
            conn.rollback()
            raise ValueError("Start a Market Day first before restocking booth items.")
        sku = conn.execute(
            "SELECT id FROM skus WHERE id = ? AND merchant_id = ?",
            (int(sku_id), int(merchant_id)),
        ).fetchone()
        if not sku:
            conn.rollback()
            raise ValueError("Unknown product.")
        mday_id = int(mday["id"])
        cur_row = conn.execute(
            "SELECT brought_qty FROM market_day_items WHERE market_day_id = ? AND sku_id = ?",
            (mday_id, int(sku_id)),
        ).fetchone()
        cur_qty = float(cur_row["brought_qty"] or 0) if cur_row else 0.0
        new_qty = fmt_qty(max(0.0, cur_qty + float(delta_qty or 0)))
        conn.execute(
            """
            INSERT INTO market_day_items (market_day_id, sku_id, brought_qty, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(market_day_id, sku_id) DO UPDATE SET
                brought_qty = excluded.brought_qty,
                updated_at = excluded.updated_at
            """,
            (mday_id, int(sku_id), new_qty, now),
        )
        conn.commit()
        return new_qty
    finally:
        conn.close()


def compute_market_day_status(
    merchant_id: int,
    market_date: str,
    tz,
) -> dict:
    """Compute complete Pack / Live Booth / Closeout state for a given date."""
    from inventory.sales import money
    from inventory.stock import fmt_qty

    mdate = str(market_date or "").strip()
    date.fromisoformat(mdate)
    today_iso = datetime.now(tz).date().isoformat()

    conn = get_connection()
    try:
        md_row = conn.execute(
            """
            SELECT md.*, m.name AS marketplace_name, m.location AS marketplace_location
            FROM market_days md
            JOIN marketplaces m ON m.id = md.marketplace_id
            WHERE md.merchant_id = ? AND md.market_date = ?
            """,
            (int(merchant_id), mdate),
        ).fetchone()

        sku_rows = conn.execute(
            """
            SELECT k.id, k.name, k.variant_name, k.category, k.price_cents,
                   COALESCE(k.sell_mode, 'unit') AS sell_mode,
                   COALESCE(k.show_in_quick_sale, 1) AS show_in_quick_sale,
                   COALESCE(SUM(l.delta), 0) AS on_hand
            FROM skus k
            LEFT JOIN stock_ledger l ON l.sku_id = k.id
            WHERE k.merchant_id = ? AND k.active = 1
              AND COALESCE(k.item_type, 'product') != 'fee'
            GROUP BY k.id
            ORDER BY k.name ASC, k.variant_name ASC, k.id ASC
            """,
            (int(merchant_id),),
        ).fetchall()

        mday = dict(md_row) if md_row else None
        pack_by_sku: dict[int, dict] = {}
        if mday:
            for r in conn.execute(
                "SELECT * FROM market_day_items WHERE market_day_id = ?",
                (int(mday["id"]),),
            ).fetchall():
                pack_by_sku[int(r["sku_id"])] = dict(r)

        status = (mday.get("status") or "open") if mday else "open"
        closed_at = mday.get("closed_at") if mday else None
        win_start, win_end = _market_day_window_bounds(mdate, tz, status=status, closed_at=closed_at)

        sale_rows = conn.execute(
            """
            SELECT s.id, s.source, s.total_cents, s.payment_type, s.occurred_at
            FROM sales s
            WHERE s.merchant_id = ? AND s.status = 'completed'
              AND s.occurred_at >= ? AND s.occurred_at < ?
            """,
            (int(merchant_id), win_start, win_end),
        ).fetchall()

        item_rows = conn.execute(
            """
            SELECT si.sku_id, si.name_snapshot, si.quantity, si.unit_price_cents,
                   s.occurred_at,
                   COALESCE(k.sell_mode, 'unit') AS sell_mode,
                   COALESCE(k.item_type, 'product') AS item_type
            FROM sale_items si
            JOIN sales s ON s.id = si.sale_id
            LEFT JOIN skus k ON k.id = si.sku_id
            WHERE s.merchant_id = ? AND s.status = 'completed'
              AND s.occurred_at >= ? AND s.occurred_at < ?
              AND COALESCE(k.item_type, 'product') != 'fee'
            ORDER BY s.occurred_at ASC, si.id ASC
            """,
            (int(merchant_id), win_start, win_end),
        ).fetchall()
    finally:
        conn.close()

    gross_sales_cents = 0
    txns_count = len(sale_rows)
    card_cents = 0
    venmo_cents = 0
    paypal_cents = 0
    physical_cash_cents = 0
    for sr in sale_rows:
        cents = int(sr["total_cents"] or 0)
        gross_sales_cents += cents
        pt = (sr["payment_type"] or "").strip().lower()
        if "venmo" in pt:
            venmo_cents += cents
        elif "paypal" in pt:
            paypal_cents += cents
        elif "cash" in pt or (sr["source"] == "cash" and not pt):
            physical_cash_cents += cents
        else:
            card_cents += cents
    digital_cash_cents = venmo_cents + paypal_cents

    sold_by_sku: dict[int, dict] = {}
    for ir in item_rows:
        if ir["sku_id"] is None:
            continue
        sid = int(ir["sku_id"])
        qty = float(ir["quantity"] or 0)
        units = 1.0 if (ir["sell_mode"] or "unit") == "lb_stock_unit" else qty
        rev = round(qty * int(ir["unit_price_cents"] or 0))
        cur = sold_by_sku.setdefault(sid, {"qty": 0.0, "revenue_cents": 0, "last_sold_at": ""})
        cur["qty"] += units
        cur["revenue_cents"] += rev
        occ = str(ir["occurred_at"] or "")
        if occ > cur["last_sold_at"]:
            cur["last_sold_at"] = occ

    skus_out = []
    packed_items = []
    booth_sellouts = []
    total_brought_qty = 0.0
    total_sold_packed_qty = 0.0

    for kr in sku_rows:
        sid = int(kr["id"])
        label = kr["name"] + (f" · {kr['variant_name']}" if kr["variant_name"] else "")
        sell_mode = kr["sell_mode"] or "unit"
        unit_label = "lb" if sell_mode == "lb_stock_lb" else "units"
        on_hand = fmt_qty(float(kr["on_hand"] or 0))

        pk = pack_by_sku.get(sid)
        brought = fmt_qty(float(pk["brought_qty"] or 0)) if pk else 0
        write_off = fmt_qty(float(pk["write_off_qty"] or 0)) if pk else 0
        counted_left = (
            fmt_qty(float(pk["counted_left_qty"]))
            if (pk and pk["counted_left_qty"] is not None)
            else None
        )

        sold_info = sold_by_sku.get(sid, {"qty": 0.0, "revenue_cents": 0, "last_sold_at": ""})
        sold_qty = fmt_qty(sold_info["qty"])
        expected_left_raw = float(brought) - float(sold_info["qty"])
        expected_left = fmt_qty(max(0.0, expected_left_raw))
        expected_after_wo = fmt_qty(max(0.0, expected_left_raw - float(write_off)))
        left_at_booth = counted_left if counted_left is not None else expected_after_wo

        is_packed = float(brought) > 1e-9 or (pk is not None and (float(write_off) > 1e-9 or counted_left is not None))
        booth_sold_out = float(brought) > 1e-9 and float(left_at_booth) <= 1e-6
        sell_through_pct = (
            min(100.0, round((float(sold_info["qty"]) / float(brought)) * 100.0, 1))
            if float(brought) > 1e-9
            else 0.0
        )

        item = {
            "sku_id": sid,
            "name": kr["name"],
            "variant_name": kr["variant_name"],
            "label": label,
            "category": kr["category"] or "",
            "price_cents": int(kr["price_cents"] or 0),
            "sell_mode": sell_mode,
            "unit_label": unit_label,
            "on_hand": on_hand,
            "brought_qty": brought,
            "sold_qty": sold_qty,
            "sold_revenue_cents": int(sold_info["revenue_cents"]),
            "write_off_qty": write_off,
            "counted_left_qty": counted_left,
            "expected_left": expected_left,
            "expected_after_write_off": expected_after_wo,
            "left_at_booth": left_at_booth,
            "is_packed": is_packed,
            "booth_sold_out": booth_sold_out,
            "sell_through_pct": sell_through_pct,
            "last_sold_at": sold_info["last_sold_at"],
        }
        skus_out.append(item)
        if is_packed:
            packed_items.append(item)
            total_brought_qty += float(brought)
            total_sold_packed_qty += min(float(brought), float(sold_info["qty"]))
            if booth_sold_out:
                last_lbl = ""
                if sold_info["last_sold_at"]:
                    dt = datetime.fromisoformat(sold_info["last_sold_at"].replace("Z", "")[:19]).replace(tzinfo=timezone.utc)
                    last_lbl = dt.astimezone(tz).strftime("%-I:%M %p").replace("AM", "am").replace("PM", "pm")
                booth_sellouts.append(
                    {
                        "sku_id": sid,
                        "label": label,
                        "brought_qty": brought,
                        "sold_qty": sold_qty,
                        "last_sold_label": last_lbl,
                    }
                )

    if not mday:
        state = "unstarted"
    elif status == "closed":
        state = "closed"
    elif mdate > today_iso:
        state = "planned"
    else:
        state = "live"

    booth_fee_cents = int(mday["booth_fee_cents"] or 0) if mday else 0
    travel_cost_cents = int(mday["travel_cost_cents"] or 0) if mday else 0
    booth_cost_cents = booth_fee_cents + travel_cost_cents
    net_after_booth_cents = gross_sales_cents - booth_cost_cents
    breakeven_pct = (
        min(100.0, round((gross_sales_cents / booth_cost_cents) * 100.0, 1))
        if booth_cost_cents > 0
        else (100.0 if gross_sales_cents > 0 else 0.0)
    )
    overall_sell_through_pct = (
        min(100.0, round((total_sold_packed_qty / total_brought_qty) * 100.0, 1))
        if total_brought_qty > 1e-9
        else 0.0
    )

    starting_cash_cents = int(mday.get("starting_cash_cents") or 0) if mday else 0
    expected_drawer_cents = starting_cash_cents + physical_cash_cents
    counted_cash_cents = (
        int(mday["counted_cash_cents"])
        if (mday and mday.get("counted_cash_cents") is not None)
        else None
    )
    drawer_diff_cents = (
        counted_cash_cents - expected_drawer_cents
        if counted_cash_cents is not None
        else None
    )

    mp_title = mday["marketplace_name"] if mday else "Market Day"
    lines_txt = [
        f"📊 {mp_title} ({mdate})",
        f"Gross: {money(gross_sales_cents)} ({txns_count} sales)",
        f"  • Card: {money(card_cents)} | Digital: {money(digital_cash_cents)} | Cash: {money(physical_cash_cents)}",
    ]
    if booth_cost_cents > 0:
        lines_txt.append(f"Net after {money(booth_cost_cents)} booth/travel: {money(net_after_booth_cents)}")
    if packed_items:
        lines_txt.append(f"Booth sell-through: {overall_sell_through_pct:.0f}% ({len(booth_sellouts)} sold out)")
    if drawer_diff_cents is not None:
        sign = "+" if drawer_diff_cents >= 0 else "-"
        lines_txt.append(f"Cash drawer: {money(counted_cash_cents)} counted ({sign}{money(abs(drawer_diff_cents))})")

    return {
        "market_date": mdate,
        "today_iso": today_iso,
        "state": state,
        "market_day": mday,
        "window_start_utc": win_start,
        "window_end_utc": win_end,
        "skus": skus_out,
        "packed_items": packed_items,
        "packed_sku_count": len(packed_items),
        "total_brought_qty": fmt_qty(total_brought_qty),
        "total_sold_packed_qty": fmt_qty(total_sold_packed_qty),
        "overall_sell_through_pct": overall_sell_through_pct,
        "booth_sellouts": booth_sellouts,
        "gross_sales_cents": gross_sales_cents,
        "txns_count": txns_count,
        "card_cents": card_cents,
        "digital_cash_cents": digital_cash_cents,
        "venmo_cents": venmo_cents,
        "paypal_cents": paypal_cents,
        "physical_cash_cents": physical_cash_cents,
        "booth_fee_cents": booth_fee_cents,
        "travel_cost_cents": travel_cost_cents,
        "booth_cost_cents": booth_cost_cents,
        "net_after_booth_cents": net_after_booth_cents,
        "breakeven_pct": breakeven_pct,
        "starting_cash_cents": starting_cash_cents,
        "expected_drawer_cents": expected_drawer_cents,
        "counted_cash_cents": counted_cash_cents,
        "drawer_diff_cents": drawer_diff_cents,
        "scorecard_text": "\n".join(lines_txt),
    }


def close_market_day(
    merchant_id: int,
    market_date: str,
    tz,
    counted_cash_cents: int | None = None,
    write_offs: dict[int, float] | None = None,
    counted_lefts: dict[int, float | None] | None = None,
    weather: str = "",
    notes: str = "",
    created_by: int | None = None,
    closed_at_override: str | None = None,
) -> dict:
    """Finalize a Market Day, recording cash count and idempotent stock adjustments for write-offs / discrepancies."""
    from inventory.stock import fmt_qty

    mdate = str(market_date or "").strip()
    date.fromisoformat(mdate)
    close_ts = closed_at_override or datetime.utcnow().isoformat(timespec="microseconds")
    now_sec = _now()

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        md_row = conn.execute(
            """
            SELECT md.*, m.name AS marketplace_name
            FROM market_days md
            JOIN marketplaces m ON m.id = md.marketplace_id
            WHERE md.merchant_id = ? AND md.market_date = ?
            """,
            (int(merchant_id), mdate),
        ).fetchone()
        if not md_row:
            conn.rollback()
            raise ValueError("Market Day not found.")
        mday_id = int(md_row["id"])
        mp_name = md_row["marketplace_name"]

        win_start, win_end = _market_day_window_bounds(mdate, tz, status="closed", closed_at=close_ts)
        sold_rows = conn.execute(
            """
            SELECT si.sku_id, si.quantity, COALESCE(k.sell_mode, 'unit') AS sell_mode
            FROM sale_items si
            JOIN sales s ON s.id = si.sale_id
            LEFT JOIN skus k ON k.id = si.sku_id
            WHERE s.merchant_id = ? AND s.status = 'completed'
              AND s.occurred_at >= ? AND s.occurred_at < ?
              AND si.sku_id IS NOT NULL
              AND COALESCE(k.item_type, 'product') != 'fee'
            """,
            (int(merchant_id), win_start, win_end),
        ).fetchall()
        sold_by_sku: dict[int, float] = {}
        for sr in sold_rows:
            sid = int(sr["sku_id"])
            units = 1.0 if (sr["sell_mode"] or "unit") == "lb_stock_unit" else float(sr["quantity"] or 0)
            sold_by_sku[sid] = sold_by_sku.get(sid, 0.0) + units

        pack_rows = conn.execute(
            "SELECT * FROM market_day_items WHERE market_day_id = ?",
            (mday_id,),
        ).fetchall()

        adjustments_written = 0
        for pr in pack_rows:
            sid = int(pr["sku_id"])
            brought = float(pr["brought_qty"] or 0)
            prev_ledger_id = pr["closeout_ledger_id"]

            # Reverse any prior closeout adjustment so re-closing is 100% idempotent
            if prev_ledger_id is not None:
                old_l = conn.execute(
                    "SELECT delta FROM stock_ledger WHERE id = ? AND merchant_id = ?",
                    (int(prev_ledger_id), int(merchant_id)),
                ).fetchone()
                if old_l and abs(float(old_l["delta"] or 0)) > 1e-9:
                    conn.execute(
                        "INSERT INTO stock_ledger (merchant_id, sku_id, delta, reason, note, created_by, created_at)"
                        " VALUES (?, ?, ?, 'adjustment', ?, ?, ?)",
                        (
                            int(merchant_id),
                            sid,
                            fmt_qty(-float(old_l["delta"])),
                            f"Reset prior closeout ({mp_name} {mdate})",
                            created_by,
                            now_sec,
                        ),
                    )

            wo = fmt_qty(max(0.0, float((write_offs or {}).get(sid, pr["write_off_qty"] or 0) or 0)))
            if counted_lefts is not None and sid in counted_lefts:
                cl_raw = counted_lefts[sid]
                cl = fmt_qty(max(0.0, float(cl_raw))) if cl_raw is not None else None
            else:
                cl = fmt_qty(float(pr["counted_left_qty"])) if pr["counted_left_qty"] is not None else None

            expected_left = max(0.0, brought - sold_by_sku.get(sid, 0.0))
            if cl is not None:
                shrink_delta = fmt_qty(float(cl) - expected_left)
            else:
                shrink_delta = fmt_qty(-float(wo))

            new_ledger_id = None
            if abs(float(shrink_delta)) > 1e-6:
                reason_bits = []
                if float(wo) > 1e-6:
                    reason_bits.append(f"write-off {wo}")
                if cl is not None:
                    reason_bits.append(f"counted {cl} vs {fmt_qty(expected_left)} expected")
                detail = ", ".join(reason_bits) or "closeout count"
                cur = conn.execute(
                    "INSERT INTO stock_ledger (merchant_id, sku_id, delta, reason, note, created_by, created_at)"
                    " VALUES (?, ?, ?, 'adjustment', ?, ?, ?)",
                    (
                        int(merchant_id),
                        sid,
                        shrink_delta,
                        f"Market closeout ({mp_name} {mdate}: {detail})",
                        created_by,
                        now_sec,
                    ),
                )
                new_ledger_id = int(cur.lastrowid)
                adjustments_written += 1

            conn.execute(
                """
                UPDATE market_day_items
                SET write_off_qty = ?, counted_left_qty = ?, closeout_ledger_id = ?, updated_at = ?
                WHERE market_day_id = ? AND sku_id = ?
                """,
                (wo, cl, new_ledger_id, now_sec, mday_id, sid),
            )

        clean_weather = str(weather or "").strip() or (md_row["weather"] or "")
        clean_notes = str(notes or "").strip() or (md_row["notes"] or "")
        conn.execute(
            """
            UPDATE market_days
            SET status = 'closed',
                closed_at = ?,
                counted_cash_cents = ?,
                weather = ?,
                notes = ?
            WHERE id = ? AND merchant_id = ?
            """,
            (
                close_ts,
                int(counted_cash_cents) if counted_cash_cents is not None else None,
                clean_weather,
                clean_notes,
                mday_id,
                int(merchant_id),
            ),
        )
        conn.commit()
        return {"market_day_id": mday_id, "adjustments_written": adjustments_written, "closed_at": close_ts}
    finally:
        conn.close()


def reopen_market_day(
    merchant_id: int,
    market_date: str,
    created_by: int | None = None,
) -> dict:
    """Reopen a closed Market Day and reverse any closeout stock adjustments."""
    from inventory.stock import fmt_qty

    mdate = str(market_date or "").strip()
    date.fromisoformat(mdate)
    now_sec = _now()

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        md_row = conn.execute(
            """
            SELECT md.*, m.name AS marketplace_name
            FROM market_days md
            JOIN marketplaces m ON m.id = md.marketplace_id
            WHERE md.merchant_id = ? AND md.market_date = ?
            """,
            (int(merchant_id), mdate),
        ).fetchone()
        if not md_row:
            conn.rollback()
            raise ValueError("Market Day not found.")
        mday_id = int(md_row["id"])
        mp_name = md_row["marketplace_name"]

        pack_rows = conn.execute(
            "SELECT sku_id, closeout_ledger_id FROM market_day_items WHERE market_day_id = ?",
            (mday_id,),
        ).fetchall()
        reversed_count = 0
        for pr in pack_rows:
            lid = pr["closeout_ledger_id"]
            if lid is not None:
                old_l = conn.execute(
                    "SELECT delta FROM stock_ledger WHERE id = ? AND merchant_id = ?",
                    (int(lid), int(merchant_id)),
                ).fetchone()
                if old_l and abs(float(old_l["delta"] or 0)) > 1e-9:
                    conn.execute(
                        "INSERT INTO stock_ledger (merchant_id, sku_id, delta, reason, note, created_by, created_at)"
                        " VALUES (?, ?, ?, 'adjustment', ?, ?, ?)",
                        (
                            int(merchant_id),
                            int(pr["sku_id"]),
                            fmt_qty(-float(old_l["delta"])),
                            f"Reopened market day ({mp_name} {mdate})",
                            created_by,
                            now_sec,
                        ),
                    )
                    reversed_count += 1
                conn.execute(
                    "UPDATE market_day_items SET closeout_ledger_id = NULL, updated_at = ?"
                    " WHERE market_day_id = ? AND sku_id = ?",
                    (now_sec, mday_id, int(pr["sku_id"])),
                )

        conn.execute(
            "UPDATE market_days SET status = 'open', closed_at = NULL WHERE id = ? AND merchant_id = ?",
            (mday_id, int(merchant_id)),
        )
        conn.commit()
        return {"market_day_id": mday_id, "reversed_adjustments": reversed_count}
    finally:
        conn.close()
