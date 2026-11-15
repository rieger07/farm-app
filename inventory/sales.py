"""Sales recording and reporting queries."""

from datetime import datetime, timedelta

from inventory.db import get_connection


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def record_cash_sale(
    merchant_id: int,
    client_uuid: str,
    items: list[dict],
    cash_received_cents: int | None = None,
    created_by: int | None = None,
    occurred_at: str | None = None,
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
        {"sku_id": int(i["sku_id"]), "quantity": int(i["quantity"])}
        for i in items
        if int(i.get("quantity", 0)) > 0
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
                "SELECT id, name, variant_name, price_cents FROM skus"
                " WHERE id = ? AND merchant_id = ?",
                (it["sku_id"], int(merchant_id)),
            ).fetchone()
            if not row:
                raise ValueError(f"Unknown SKU {it['sku_id']}.")
            skus[it["sku_id"]] = row

        total = sum(skus[i["sku_id"]]["price_cents"] * i["quantity"] for i in clean)
        now = _now()
        cur = conn.execute(
            "INSERT INTO sales (merchant_id, source, external_id, occurred_at,"
            " total_cents, cash_received_cents, created_by, created_at)"
            " VALUES (?, 'cash', ?, ?, ?, ?, ?, ?)",
            (
                int(merchant_id),
                client_uuid,
                occurred_at or now,
                total,
                cash_received_cents,
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
            conn.execute(
                "INSERT INTO stock_ledger (merchant_id, sku_id, delta, reason,"
                " sale_item_id, created_by, created_at)"
                " VALUES (?, ?, ?, 'sale', ?, ?, ?)",
                (
                    int(merchant_id),
                    it["sku_id"],
                    -it["quantity"],
                    int(item_cur.lastrowid),
                    created_by,
                    now,
                ),
            )
        conn.commit()
        return {"sale_id": sale_id, "total_cents": total, "duplicate": False}
    finally:
        conn.close()


def sales_summary(merchant_id: int, since_iso: str, until_iso: str) -> dict:
    """Totals and source split for sales in [since, until)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT s.source, COUNT(*) AS n, SUM(s.total_cents) AS cents,"
            " COALESCE(SUM((SELECT SUM(quantity) FROM sale_items WHERE sale_id = s.id)), 0)"
            "   AS units"
            " FROM sales s WHERE s.merchant_id = ? AND s.status = 'completed'"
            " AND s.occurred_at >= ? AND s.occurred_at < ? GROUP BY s.source",
            (int(merchant_id), since_iso, until_iso),
        ).fetchall()
    finally:
        conn.close()
    by = {r["source"]: r for r in rows}

    def pick(src, col):
        return int(by[src][col] or 0) if src in by else 0

    count = pick("zettle", "n") + pick("cash", "n")
    total = pick("zettle", "cents") + pick("cash", "cents")
    return {
        "total_cents": total,
        "count": count,
        "avg_cents": round(total / count) if count else 0,
        "units": pick("zettle", "units") + pick("cash", "units"),
        "zettle_count": pick("zettle", "n"),
        "zettle_cents": pick("zettle", "cents"),
        "cash_count": pick("cash", "n"),
        "cash_cents": pick("cash", "cents"),
    }


def daily_totals(merchant_id: int, start_day: datetime, days: int = 7) -> list[dict]:
    """Per-day Zettle/cash totals, one entry per day starting at start_day."""
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
                "zettle_cents": s["zettle_cents"],
                "cash_cents": s["cash_cents"],
            }
        )
    return out


def count_sales(
    merchant_id: int, since_iso: str, until_iso: str, source: str | None = None
) -> int:
    sql = (
        "SELECT COUNT(*) FROM sales WHERE merchant_id = ?"
        " AND occurred_at >= ? AND occurred_at < ?"
    )
    args: list = [int(merchant_id), since_iso, until_iso]
    if source in ("zettle", "cash"):
        sql += " AND source = ?"
        args.append(source)
    conn = get_connection()
    try:
        return int(conn.execute(sql, args).fetchone()[0])
    finally:
        conn.close()


def recent_sales(
    merchant_id: int, since_iso: str, until_iso: str,
    source: str | None = None, limit: int = 25, offset: int = 0,
) -> list[dict]:
    """Newest-first sales in [since, until), each with its line items attached."""
    sql = (
        "SELECT s.id, s.source, s.occurred_at, s.total_cents, s.status,"
        " s.payment_type, s.cash_received_cents,"
        " COALESCE((SELECT SUM(quantity) FROM sale_items WHERE sale_id = s.id), 0) AS units"
        " FROM sales s WHERE s.merchant_id = ? AND s.occurred_at >= ? AND s.occurred_at < ?"
    )
    args: list = [int(merchant_id), since_iso, until_iso]
    if source in ("zettle", "cash"):
        sql += " AND s.source = ?"
        args.append(source)
    sql += " ORDER BY s.occurred_at DESC, s.id DESC LIMIT ? OFFSET ?"
    args += [int(limit), int(offset)]
    conn = get_connection()
    try:
        sales = [dict(r) for r in conn.execute(sql, args).fetchall()]
        if sales:
            ids = [s["id"] for s in sales]
            marks = ",".join("?" * len(ids))
            items = conn.execute(
                "SELECT sale_id, name_snapshot, quantity, unit_price_cents"
                f" FROM sale_items WHERE sale_id IN ({marks}) ORDER BY id",
                ids,
            ).fetchall()
        else:
            items = []
    finally:
        conn.close()
    by_sale: dict[int, list[dict]] = {}
    for it in items:
        d = dict(it)
        d["subtotal_cents"] = d["quantity"] * d["unit_price_cents"]
        by_sale.setdefault(d["sale_id"], []).append(d)
    for s in sales:
        s["items"] = by_sale.get(s["id"], [])
        rec = s["cash_received_cents"]
        s["change_cents"] = (rec - s["total_cents"]) if rec is not None else None
    return sales


def export_rows(
    merchant_id: int, since_iso: str, until_iso: str, source: str | None = None
) -> list[dict]:
    """One row per sale line item (sales with no items still produce one row)."""
    sql = (
        "SELECT s.id AS sale_id, s.occurred_at, s.source, s.payment_type, s.status,"
        " u.username AS cashier, i.name_snapshot AS item, i.quantity,"
        " i.unit_price_cents, s.total_cents"
        " FROM sales s"
        " LEFT JOIN sale_items i ON i.sale_id = s.id"
        " LEFT JOIN users u ON u.id = s.created_by"
        " WHERE s.merchant_id = ? AND s.occurred_at >= ? AND s.occurred_at < ?"
    )
    args: list = [int(merchant_id), since_iso, until_iso]
    if source in ("zettle", "cash"):
        sql += " AND s.source = ?"
        args.append(source)
    sql += " ORDER BY s.occurred_at ASC, s.id ASC, i.id ASC"
    conn = get_connection()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"
