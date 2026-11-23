"""Sales recording and reporting queries."""

from datetime import datetime, timedelta

from inventory.db import get_connection
from inventory.stock import fmt_qty


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


PAYMENT_METHODS = ("cash", "venmo", "paypal", "card")


def _normalize_payment_method(source: str, payment_type: str | None) -> str:
    pt = (payment_type or "").strip().lower()
    if "venmo" in pt:
        return "venmo"
    if "paypal" in pt:
        return "paypal"
    if "cash" in pt or (source == "cash" and not pt):
        return "cash"
    return "card"


def record_cash_sale(
    merchant_id: int,
    client_uuid: str,
    items: list[dict],
    cash_received_cents: int | None = None,
    created_by: int | None = None,
    occurred_at: str | None = None,
    payment_type: str = "cash",
) -> dict:
    """Record a cash sale idempotently.

    ``items`` is a list of ``{"sku_id": int, "quantity": int}``. Prices are
    snapshotted from the SKU catalog server-side. Sales are never blocked by
    stock level. Replaying the same ``client_uuid`` returns the original sale
    with ``duplicate=True`` and changes nothing.
    """
    if not client_uuid:
        raise ValueError("client_uuid is required.")
    clean = [
        {"sku_id": int(i["sku_id"]), "quantity": fmt_qty(i["quantity"])}
        for i in items
        if float(i.get("quantity", 0) or 0) > 0
    ]
    if not clean:
        raise ValueError("A sale needs at least one item.")

    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT id, total_cents FROM sales"
            " WHERE merchant_id = ? AND source = 'cash' AND external_id = ?",
            (int(merchant_id), client_uuid),
        ).fetchone()
        if existing:
            return {
                "sale_id": existing["id"],
                "total_cents": existing["total_cents"],
                "duplicate": True,
            }

        skus = {}
        for it in clean:
            row = conn.execute(
                "SELECT id, name, variant_name, price_cents, sell_mode, item_type FROM skus"
                " WHERE id = ? AND merchant_id = ?",
                (it["sku_id"], int(merchant_id)),
            ).fetchone()
            if not row:
                raise ValueError(f"Unknown SKU {it['sku_id']}.")
            skus[it["sku_id"]] = row

        total = sum(round(skus[i["sku_id"]]["price_cents"] * i["quantity"]) for i in clean)
        now = _now()
        clean_pt = _normalize_payment_method("cash", payment_type)
        if clean_pt not in ("cash", "venmo", "paypal"):
            clean_pt = "cash"
        cash_rec = cash_received_cents if clean_pt == "cash" else None
        cur = conn.execute(
            "INSERT INTO sales (merchant_id, source, external_id, occurred_at,"
            " total_cents, cash_received_cents, payment_type, created_by, created_at)"
            " VALUES (?, 'cash', ?, ?, ?, ?, ?, ?, ?)",
            (
                int(merchant_id),
                client_uuid,
                occurred_at or now,
                total,
                cash_rec,
                clean_pt,
                created_by,
                now,
            ),
        )
        sale_id = int(cur.lastrowid)
        for it in clean:
            sku = skus[it["sku_id"]]
            label = sku["name"] + (f" · {sku['variant_name']}" if sku["variant_name"] else "")
            item_cur = conn.execute(
                "INSERT INTO sale_items (sale_id, sku_id, name_snapshot, quantity,"
                " unit_price_cents) VALUES (?, ?, ?, ?, ?)",
                (sale_id, it["sku_id"], label, it["quantity"], sku["price_cents"]),
            )
            if (sku["item_type"] or "product") != "fee":
                stock_delta = -1 if sku["sell_mode"] == "lb_stock_unit" else -it["quantity"]
                conn.execute(
                    "INSERT INTO stock_ledger (merchant_id, sku_id, delta, reason,"
                    " sale_item_id, created_by, created_at)"
                    " VALUES (?, ?, ?, 'sale', ?, ?, ?)",
                    (
                        int(merchant_id),
                        it["sku_id"],
                        stock_delta,
                        int(item_cur.lastrowid),
                        created_by,
                        now,
                    ),
                )
        conn.commit()
        return {"sale_id": sale_id, "total_cents": total, "duplicate": False}
    finally:
        conn.close()


def _window_filter(
    utc_windows: list[tuple[str, str]] | None, alias: str = "s"
) -> tuple[str, list]:
    if utc_windows is None:
        return "", []
    if not utc_windows:
        return " AND 1=0", []
    p = f"{alias}." if alias else ""
    clause = " OR ".join(
        f"({p}occurred_at >= ? AND {p}occurred_at < ?)" for _ in utc_windows
    )
    params = [v for pair in utc_windows for v in pair]
    return f" AND ({clause})", params


def update_sale_payment_type(merchant_id: int, sale_id: int, payment_type: str) -> bool:
    clean = (payment_type or "").strip().lower()
    if clean not in PAYMENT_METHODS:
        raise ValueError(f"Unsupported payment type: {payment_type}")
    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE sales SET payment_type = ? WHERE id = ? AND merchant_id = ?",
            (clean, int(sale_id), int(merchant_id)),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def void_quick_sale(
    merchant_id: int,
    sale_id: int,
    created_by: int | None = None,
    restore_stock: bool = True,
) -> dict:
    """Void a Quick sale transaction, optionally restoring its deducted stock.

    Zettle sales cannot be voided here because they are mirrored from Zettle's
    API; only ``source = 'cash'`` (Quick sale) transactions are eligible.
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        sale = conn.execute(
            "SELECT id, source, status FROM sales WHERE id = ? AND merchant_id = ?",
            (int(sale_id), int(merchant_id)),
        ).fetchone()
        if not sale:
            conn.rollback()
            raise ValueError("Sale not found.")
        if sale["source"] != "cash":
            conn.rollback()
            raise ValueError("Only Quick sale transactions can be voided (Zettle sales are synced read-only).")
        if sale["status"] == "voided":
            conn.rollback()
            raise ValueError(f"Sale #{sale_id} is already voided.")

        conn.execute(
            "UPDATE sales SET status = 'voided' WHERE id = ? AND merchant_id = ?",
            (int(sale_id), int(merchant_id)),
        )

        restored_items = 0
        if restore_stock:
            ledger_rows = conn.execute(
                """
                SELECT l.sku_id, l.delta
                FROM stock_ledger l
                JOIN sale_items si ON si.id = l.sale_item_id
                WHERE si.sale_id = ? AND l.merchant_id = ?
                """,
                (int(sale_id), int(merchant_id)),
            ).fetchall()
            now = _now()
            for lr in ledger_rows:
                comp_delta = -float(lr["delta"] or 0)
                if abs(comp_delta) < 1e-9:
                    continue
                conn.execute(
                    "INSERT INTO stock_ledger (merchant_id, sku_id, delta, reason,"
                    " note, created_by, created_at)"
                    " VALUES (?, ?, ?, 'adjustment', ?, ?, ?)",
                    (
                        int(merchant_id),
                        int(lr["sku_id"]),
                        comp_delta,
                        f"Voided Quick sale #{sale_id}",
                        created_by,
                        now,
                    ),
                )
                restored_items += 1
        conn.commit()
        return {"sale_id": int(sale_id), "restored_items": restored_items}
    finally:
        conn.close()


def sales_summary(
    merchant_id: int,
    since_iso: str,
    until_iso: str,
    utc_windows: list[tuple[str, str]] | None = None,
) -> dict:
    """Totals, 3-way payment categories (card / digital cash / physical cash), and source split."""
    win_sql, win_args = _window_filter(utc_windows, alias="s")
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT s.source,
                   CASE
                     WHEN LOWER(COALESCE(s.payment_type, '')) LIKE '%venmo%' THEN 'venmo'
                     WHEN LOWER(COALESCE(s.payment_type, '')) LIKE '%paypal%' THEN 'paypal'
                     WHEN LOWER(COALESCE(s.payment_type, '')) LIKE '%cash%'
                          OR (s.source = 'cash' AND COALESCE(s.payment_type, '') = '') THEN 'cash'
                     ELSE 'card'
                   END AS pay_method,
                   COUNT(*) AS n,
                   SUM(s.total_cents) AS cents,
                   COALESCE(SUM((
                     SELECT SUM(CASE WHEN COALESCE(k.sell_mode, 'unit') = 'lb_stock_unit' THEN 1 ELSE si.quantity END)
                     FROM sale_items si
                     LEFT JOIN skus k ON k.id = si.sku_id
                     WHERE si.sale_id = s.id AND COALESCE(k.item_type, 'product') != 'fee'
                   )), 0) AS units
            FROM sales s
            WHERE s.merchant_id = ? AND s.status = 'completed'
              AND s.occurred_at >= ? AND s.occurred_at < ?{win_sql}
            GROUP BY s.source, pay_method
            """,
            [int(merchant_id), since_iso, until_iso, *win_args],
        ).fetchall()
    finally:
        conn.close()

    def bucket(src: str | None = None, pay: str | None = None, col: str = "cents") -> int:
        return int(sum(
            (r[col] or 0) for r in rows
            if (src is None or r["source"] == src) and (pay is None or r["pay_method"] == pay)
        ))

    count = bucket(col="n")
    total = bucket(col="cents")
    units = fmt_qty(sum(float(r["units"] or 0) for r in rows))

    card_cents = bucket(pay="card", col="cents")
    card_count = bucket(pay="card", col="n")

    venmo_cents = bucket(pay="venmo", col="cents")
    venmo_count = bucket(pay="venmo", col="n")
    paypal_cents = bucket(pay="paypal", col="cents")
    paypal_count = bucket(pay="paypal", col="n")
    digital_cents = venmo_cents + paypal_cents
    digital_count = venmo_count + paypal_count

    zettle_cash_cents = bucket("zettle", "cash", "cents")
    zettle_cash_count = bucket("zettle", "cash", "n")
    quick_cash_cents = bucket("cash", "cash", "cents")
    quick_cash_count = bucket("cash", "cash", "n")
    cash_cents = zettle_cash_cents + quick_cash_cents
    cash_count = zettle_cash_count + quick_cash_count

    return {
        "total_cents": total,
        "count": count,
        "avg_cents": round(total / count) if count else 0,
        "units": units,
        "card_count": card_count,
        "card_cents": card_cents,
        "digital_count": digital_count,
        "digital_cents": digital_cents,
        "venmo_count": venmo_count,
        "venmo_cents": venmo_cents,
        "paypal_count": paypal_count,
        "paypal_cents": paypal_cents,
        "cash_count": cash_count,
        "cash_cents": cash_cents,
        "zettle_card_count": bucket("zettle", "card", "n"),
        "zettle_card_cents": bucket("zettle", "card", "cents"),
        "zettle_cash_count": zettle_cash_count,
        "zettle_cash_cents": zettle_cash_cents,
        "quick_cash_count": quick_cash_count,
        "quick_cash_cents": quick_cash_cents,
        "zettle_count": bucket("zettle", col="n"),
        "zettle_cents": bucket("zettle", col="cents"),
    }


def daily_totals(merchant_id: int, start_day: datetime, days: int = 7) -> list[dict]:
    """Per-day payment/source totals, one entry per day starting at start_day."""
    out = []
    for i in range(days):
        day = start_day + timedelta(days=i)
        s = sales_summary(
            merchant_id,
            day.isoformat(timespec="seconds"),
            (day + timedelta(days=1)).isoformat(timespec="seconds"),
        )
        out.append(
            {
                "label": day.strftime("%a"),
                "date": day.date().isoformat(),
                "card_cents": s["card_cents"],
                "digital_cents": s["digital_cents"],
                "cash_cents": s["cash_cents"],
                "zettle_card_cents": s["zettle_card_cents"],
                "zettle_cash_cents": s["zettle_cash_cents"],
                "quick_cash_cents": s["quick_cash_cents"],
                "zettle_cents": s["zettle_cents"],
            }
        )
    return out


def _source_filter_clause(source: str | None, alias: str = "") -> str:
    p = f"{alias}." if alias else ""
    if source == "voided":
        return f" AND {p}status = 'voided'"
    not_void = f" AND {p}status != 'voided'"
    if source == "cash":
        return not_void + (
            f" AND (LOWER(COALESCE({p}payment_type, '')) LIKE '%cash%'"
            f" OR ({p}source = 'cash' AND COALESCE({p}payment_type, '') = ''))"
        )
    if source == "digital":
        return not_void + (
            f" AND (LOWER(COALESCE({p}payment_type, '')) LIKE '%venmo%'"
            f" OR LOWER(COALESCE({p}payment_type, '')) LIKE '%paypal%')"
        )
    if source == "venmo":
        return not_void + f" AND LOWER(COALESCE({p}payment_type, '')) LIKE '%venmo%'"
    if source == "paypal":
        return not_void + f" AND LOWER(COALESCE({p}payment_type, '')) LIKE '%paypal%'"
    if source == "card":
        return not_void + (
            f" AND NOT (LOWER(COALESCE({p}payment_type, '')) LIKE '%cash%'"
            f" OR ({p}source = 'cash' AND COALESCE({p}payment_type, '') = ''))"
            f" AND NOT (LOWER(COALESCE({p}payment_type, '')) LIKE '%venmo%'"
            f" OR LOWER(COALESCE({p}payment_type, '')) LIKE '%paypal%')"
        )
    if source == "zettle":
        return not_void + f" AND {p}source = 'zettle'"
    if source == "quick_sale":
        return not_void + f" AND {p}source = 'cash'"
    return not_void


def count_sales(
    merchant_id: int,
    since_iso: str,
    until_iso: str,
    source: str | None = None,
    utc_windows: list[tuple[str, str]] | None = None,
) -> int:
    win_sql, win_args = _window_filter(utc_windows, alias="")
    sql = (
        "SELECT COUNT(*) FROM sales WHERE merchant_id = ?"
        " AND occurred_at >= ? AND occurred_at < ?"
        + _source_filter_clause(source)
        + win_sql
    )
    args: list = [int(merchant_id), since_iso, until_iso, *win_args]
    conn = get_connection()
    try:
        return int(conn.execute(sql, args).fetchone()[0])
    finally:
        conn.close()


def recent_sales(
    merchant_id: int, since_iso: str, until_iso: str,
    source: str | None = None, limit: int = 25, offset: int = 0,
    utc_windows: list[tuple[str, str]] | None = None,
) -> list[dict]:
    """Newest-first sales in [since, until), each with its line items attached."""
    win_sql, win_args = _window_filter(utc_windows, alias="s")
    sql = (
        "SELECT s.id, s.source, s.occurred_at, s.total_cents, s.status,"
        " s.payment_type, s.cash_received_cents,"
        " COALESCE(("
        "   SELECT SUM(CASE WHEN COALESCE(k.sell_mode, 'unit') = 'lb_stock_unit' THEN 1 ELSE si.quantity END) FROM sale_items si"
        "   LEFT JOIN skus k ON k.id = si.sku_id"
        "   WHERE si.sale_id = s.id AND COALESCE(k.item_type, 'product') != 'fee'"
        " ), 0) AS units"
        " FROM sales s WHERE s.merchant_id = ? AND s.occurred_at >= ? AND s.occurred_at < ?"
        + _source_filter_clause(source, alias="s")
        + win_sql
    )
    args: list = [int(merchant_id), since_iso, until_iso, *win_args]
    sql += " ORDER BY s.occurred_at DESC, s.id DESC LIMIT ? OFFSET ?"
    args += [int(limit), int(offset)]
    conn = get_connection()
    try:
        sales = [dict(r) for r in conn.execute(sql, args).fetchall()]
        if sales:
            ids = [s["id"] for s in sales]
            marks = ",".join("?" * len(ids))
            items = conn.execute(
                "SELECT si.sale_id, si.name_snapshot, si.quantity, si.unit_price_cents,"
                " COALESCE(k.item_type, 'product') AS item_type"
                " FROM sale_items si"
                " LEFT JOIN skus k ON k.id = si.sku_id"
                f" WHERE si.sale_id IN ({marks}) ORDER BY si.id",
                ids,
            ).fetchall()
        else:
            items = []
    finally:
        conn.close()
    by_sale: dict[int, list[dict]] = {}
    for it in items:
        d = dict(it)
        d["quantity"] = fmt_qty(d["quantity"])
        d["subtotal_cents"] = round(d["quantity"] * d["unit_price_cents"])
        d["is_fee"] = d["item_type"] == "fee"
        by_sale.setdefault(d["sale_id"], []).append(d)
    for s in sales:
        s["units"] = fmt_qty(s["units"])
        s["items"] = by_sale.get(s["id"], [])
        pm = _normalize_payment_method(s["source"], s.get("payment_type"))
        s["pay_method"] = pm
        s["pay_category"] = "digital" if pm in ("venmo", "paypal") else pm
        rec = s["cash_received_cents"]
        s["change_cents"] = (rec - s["total_cents"]) if rec is not None else None
    return sales


def export_rows(
    merchant_id: int,
    since_iso: str,
    until_iso: str,
    source: str | None = None,
    utc_windows: list[tuple[str, str]] | None = None,
) -> list[dict]:
    """One row per sale line item (sales with no items still produce one row)."""
    win_sql, win_args = _window_filter(utc_windows, alias="s")
    sql = (
        "SELECT s.id AS sale_id, s.occurred_at, s.source, s.payment_type, s.status,"
        " u.username AS cashier, i.name_snapshot AS item, i.quantity,"
        " i.unit_price_cents, s.total_cents"
        " FROM sales s"
        " LEFT JOIN sale_items i ON i.sale_id = s.id"
        " LEFT JOIN users u ON u.id = s.created_by"
        " WHERE s.merchant_id = ? AND s.occurred_at >= ? AND s.occurred_at < ?"
        + _source_filter_clause(source, alias="s")
        + win_sql
    )
    args: list = [int(merchant_id), since_iso, until_iso, *win_args]
    sql += " ORDER BY s.occurred_at ASC, s.id ASC, i.id ASC"
    conn = get_connection()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def market_insights(
    merchant_id: int,
    since_iso: str,
    until_iso: str,
    utc_windows: list[tuple[str, str]] | None = None,
) -> dict:
    """Event-driven market analytics: top products, fees, sell-outs, and baskets."""
    from collections import Counter
    from itertools import combinations

    win_sql, win_args = _window_filter(utc_windows, alias="s")
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT si.sale_id,
                   si.sku_id,
                   si.name_snapshot,
                   si.quantity,
                   si.unit_price_cents,
                   s.occurred_at,
                   COALESCE(k.item_type, 'product') AS item_type,
                   COALESCE(k.sell_mode, 'unit') AS sell_mode
            FROM sale_items si
            JOIN sales s ON s.id = si.sale_id
            LEFT JOIN skus k ON k.id = si.sku_id
            WHERE s.merchant_id = ? AND s.status = 'completed'
              AND s.occurred_at >= ? AND s.occurred_at < ?{win_sql}
            ORDER BY s.occurred_at ASC, si.id ASC
            """,
            [int(merchant_id), since_iso, until_iso, *win_args],
        ).fetchall()

        sellout_rows = conn.execute(
            """
            SELECT k.id AS sku_id,
                   k.name,
                   k.variant_name,
                   COALESCE(SUM(l.delta), 0) AS on_hand
            FROM skus k
            LEFT JOIN stock_ledger l ON l.sku_id = k.id
            WHERE k.merchant_id = ? AND k.active = 1
              AND COALESCE(k.item_type, 'product') != 'fee'
            GROUP BY k.id
            HAVING COALESCE(SUM(l.delta), 0) <= 1e-6
            """,
            (int(merchant_id),),
        ).fetchall()
    finally:
        conn.close()

    by_prod: dict[str, dict] = {}
    baskets: dict[int, set[str]] = {}
    basket_units: dict[int, float] = {}
    last_sold_by_sku: dict[int, tuple[str, float]] = {}
    fee_cents = 0
    fee_count = 0

    for r in rows:
        qty = float(r["quantity"] or 0)
        unit_qty = 1.0 if (r["sell_mode"] or "unit") == "lb_stock_unit" else qty
        rev = round(qty * int(r["unit_price_cents"] or 0))
        if r["item_type"] == "fee":
            fee_cents += rev
            fee_count += 1
            continue
        name = str(r["name_snapshot"] or "Item")
        entry = by_prod.setdefault(
            name,
            {"name": name, "units": 0.0, "revenue_cents": 0, "sale_ids": set()},
        )
        entry["units"] += unit_qty
        entry["revenue_cents"] += rev
        entry["sale_ids"].add(r["sale_id"])

        baskets.setdefault(r["sale_id"], set()).add(name)
        basket_units[r["sale_id"]] = basket_units.get(r["sale_id"], 0.0) + unit_qty

        if r["sku_id"] is not None:
            sid = int(r["sku_id"])
            prev_ts, prev_q = last_sold_by_sku.get(sid, ("", 0.0))
            occ = str(r["occurred_at"])
            last_sold_by_sku[sid] = (occ if occ > prev_ts else prev_ts, prev_q + unit_qty)

    products = [
        {
            "name": v["name"],
            "units": fmt_qty(v["units"]),
            "revenue_cents": v["revenue_cents"],
            "sales_count": len(v["sale_ids"]),
        }
        for v in by_prod.values()
    ]
    top_by_units = sorted(products, key=lambda x: (-x["units"], -x["revenue_cents"]))[:10]
    top_by_revenue = sorted(products, key=lambda x: (-x["revenue_cents"], -x["units"]))[:10]

    sellouts = []
    for sr in sellout_rows:
        sid = int(sr["sku_id"])
        if sid not in last_sold_by_sku:
            continue
        last_ts, sold_q = last_sold_by_sku[sid]
        label = sr["name"] + (f" · {sr['variant_name']}" if sr["variant_name"] else "")
        sellouts.append(
            {
                "sku_id": sid,
                "name": label,
                "units_sold": fmt_qty(sold_q),
                "last_sold_at": last_ts,
            }
        )
    sellouts.sort(key=lambda x: x["last_sold_at"])

    pair_counts: Counter[tuple[str, str]] = Counter()
    for items in baskets.values():
        if len(items) >= 2:
            for a, b in combinations(sorted(items), 2):
                pair_counts[(a, b)] += 1
    top_pairs = [
        {"a": a, "b": b, "count": c}
        for (a, b), c in pair_counts.most_common(8)
    ]

    basket_count = len(baskets)
    avg_basket_items = round(sum(basket_units.values()) / basket_count, 2) if basket_count else 0.0

    return {
        "top_by_units": top_by_units,
        "top_by_revenue": top_by_revenue,
        "fee_cents": fee_cents,
        "fee_count": fee_count,
        "sellouts": sellouts,
        "basket_count": basket_count,
        "avg_basket_items": avg_basket_items,
        "top_pairs": top_pairs,
    }
