"""Marketplaces, Market Days, and location-based market trend analytics."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone

from inventory.db import get_connection

WEATHER_OPTIONS = (
    "Sunny",
    "Partly cloudy",
    "Cloudy",
    "Rainy",
    "Windy",
    "Hot",
    "Cold",
)


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def list_marketplaces(merchant_id: int, include_inactive: bool = False) -> list[dict]:
    sql = "SELECT * FROM marketplaces WHERE merchant_id = ?"
    if not include_inactive:
        sql += " AND active = 1"
    sql += " ORDER BY name ASC, id ASC"
    conn = get_connection()
    try:
        rows = conn.execute(sql, (int(merchant_id),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_marketplace(merchant_id: int, marketplace_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM marketplaces WHERE merchant_id = ? AND id = ?",
            (int(merchant_id), int(marketplace_id)),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_marketplace(
    merchant_id: int,
    name: str,
    location: str = "",
    booth_fee_cents: int = 0,
    travel_cost_cents: int = 0,
    marketplace_id: int | None = None,
) -> int:
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("Marketplace name is required.")
    clean_loc = (location or "").strip()
    booth = max(0, int(booth_fee_cents or 0))
    travel = max(0, int(travel_cost_cents or 0))
    conn = get_connection()
    try:
        if marketplace_id:
            conn.execute(
                "UPDATE marketplaces SET name = ?, location = ?, booth_fee_cents = ?,"
                " travel_cost_cents = ?, active = 1"
                " WHERE id = ? AND merchant_id = ?",
                (clean_name, clean_loc, booth, travel, int(marketplace_id), int(merchant_id)),
            )
            conn.commit()
            return int(marketplace_id)
        cur = conn.execute(
            "INSERT INTO marketplaces (merchant_id, name, location, booth_fee_cents,"
            " travel_cost_cents, active, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
            (int(merchant_id), clean_name, clean_loc, booth, travel, _now()),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def archive_marketplace(merchant_id: int, marketplace_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE marketplaces SET active = 0 WHERE id = ? AND merchant_id = ?",
            (int(marketplace_id), int(merchant_id)),
        )
        conn.commit()
    finally:
        conn.close()


def get_market_day(merchant_id: int, market_date: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT md.*, m.name AS marketplace_name, m.location AS marketplace_location
            FROM market_days md
            JOIN marketplaces m ON m.id = md.marketplace_id
            WHERE md.merchant_id = ? AND md.market_date = ?
            """,
            (int(merchant_id), str(market_date).strip()),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_market_day(
    merchant_id: int,
    market_date: str,
    marketplace_id: int,
    booth_fee_cents: int | None = None,
    travel_cost_cents: int | None = None,
    weather: str = "",
    notes: str = "",
) -> None:
    mp = get_marketplace(merchant_id, marketplace_id)
    if not mp:
        raise ValueError("Selected marketplace not found.")
    mdate = str(market_date).strip()
    date.fromisoformat(mdate)  # validate YYYY-MM-DD
    booth = mp["booth_fee_cents"] if booth_fee_cents is None else max(0, int(booth_fee_cents))
    travel = mp["travel_cost_cents"] if travel_cost_cents is None else max(0, int(travel_cost_cents))
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO market_days (
                merchant_id, market_date, marketplace_id, booth_fee_cents,
                travel_cost_cents, weather, notes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(merchant_id, market_date) DO UPDATE SET
                marketplace_id = excluded.marketplace_id,
                booth_fee_cents = excluded.booth_fee_cents,
                travel_cost_cents = excluded.travel_cost_cents,
                weather = excluded.weather,
                notes = excluded.notes
            """,
            (
                int(merchant_id),
                mdate,
                int(marketplace_id),
                booth,
                travel,
                str(weather or "").strip(),
                str(notes or "").strip(),
                _now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def delete_market_day(merchant_id: int, market_date: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM market_days WHERE merchant_id = ? AND market_date = ?",
            (int(merchant_id), str(market_date).strip()),
        )
        conn.commit()
    finally:
        conn.close()


def _day_utc_bounds(day_str: str, tz) -> tuple[str, str]:
    d = date.fromisoformat(day_str)
    s0 = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    d1 = d + timedelta(days=1)
    s1 = datetime(d1.year, d1.month, d1.day, tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    return s0.isoformat(timespec="seconds"), s1.isoformat(timespec="seconds")


def marketplace_utc_windows(
    merchant_id: int,
    marketplace_id: int,
    date_from: str,
    date_to: str,
    tz,
) -> list[tuple[str, str]]:
    """Return UTC [start, end) windows for each date assigned to marketplace_id in [date_from, date_to]."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT market_date FROM market_days"
            " WHERE merchant_id = ? AND marketplace_id = ?"
            " AND market_date >= ? AND market_date <= ?"
            " ORDER BY market_date ASC",
            (int(merchant_id), int(marketplace_id), date_from, date_to),
        ).fetchall()
    finally:
        conn.close()
    return [_day_utc_bounds(r["market_date"], tz) for r in rows]


def marketplace_analytics(
    merchant_id: int,
    date_from: str,
    date_to: str,
    since_iso: str,
    until_iso: str,
    tz,
) -> dict:
    """Compare marketplaces, list tagged market days, and surface untagged sale days."""
    conn = get_connection()
    try:
        md_rows = conn.execute(
            """
            SELECT md.*, m.name AS marketplace_name, m.location AS marketplace_location
            FROM market_days md
            JOIN marketplaces m ON m.id = md.marketplace_id
            WHERE md.merchant_id = ?
              AND md.market_date >= ? AND md.market_date <= ?
            ORDER BY md.market_date DESC
            """,
            (int(merchant_id), date_from, date_to),
        ).fetchall()

        sales_rows = conn.execute(
            """
            SELECT s.id, s.occurred_at, s.total_cents
            FROM sales s
            WHERE s.merchant_id = ? AND s.status = 'completed'
              AND s.occurred_at >= ? AND s.occurred_at < ?
            """,
            (int(merchant_id), since_iso, until_iso),
        ).fetchall()

        item_rows = conn.execute(
            """
            SELECT s.occurred_at, si.name_snapshot, si.quantity
            FROM sale_items si
            JOIN sales s ON s.id = si.sale_id
            LEFT JOIN skus k ON k.id = si.sku_id
            WHERE s.merchant_id = ? AND s.status = 'completed'
              AND s.occurred_at >= ? AND s.occurred_at < ?
              AND COALESCE(k.item_type, 'product') != 'fee'
            """,
            (int(merchant_id), since_iso, until_iso),
        ).fetchall()
    finally:
        conn.close()

    def _to_local_date(ts: str) -> str:
        dt = datetime.fromisoformat(str(ts).replace("Z", "")[:19]).replace(tzinfo=timezone.utc)
        return dt.astimezone(tz).date().isoformat()

    day_rev: dict[str, int] = {}
    day_txns: dict[str, int] = {}
    for s in sales_rows:
        ld = _to_local_date(s["occurred_at"])
        day_rev[ld] = day_rev.get(ld, 0) + int(s["total_cents"] or 0)
        day_txns[ld] = day_txns.get(ld, 0) + 1

    day_items: dict[str, Counter[str]] = {}
    for it in item_rows:
        ld = _to_local_date(it["occurred_at"])
        day_items.setdefault(ld, Counter())[str(it["name_snapshot"] or "Item")] += float(it["quantity"] or 0)

    tagged_dates: set[str] = set()
    market_day_rows: list[dict] = []
    by_mp: dict[int, dict] = {}

    for r in md_rows:
        md = dict(r)
        d_str = md["market_date"]
        tagged_dates.add(d_str)
        rev = day_rev.get(d_str, 0)
        txns = day_txns.get(d_str, 0)
        cost = int(md["booth_fee_cents"] or 0) + int(md["travel_cost_cents"] or 0)
        net = rev - cost
        md["revenue_cents"] = rev
        md["txns"] = txns
        md["cost_cents"] = cost
        md["net_cents"] = net
        market_day_rows.append(md)

        mp_id = int(md["marketplace_id"])
        agg = by_mp.setdefault(
            mp_id,
            {
                "marketplace_id": mp_id,
                "name": md["marketplace_name"],
                "location": md["marketplace_location"],
                "days": 0,
                "revenue_cents": 0,
                "cost_cents": 0,
                "net_cents": 0,
                "items": Counter(),
            },
        )
        agg["days"] += 1
        agg["revenue_cents"] += rev
        agg["cost_cents"] += cost
        agg["net_cents"] += net
        if d_str in day_items:
            agg["items"].update(day_items[d_str])

    comparison = []
    for agg in by_mp.values():
        days = max(1, agg["days"])
        top = agg["items"].most_common(1)[0][0] if agg["items"] else "—"
        comparison.append(
            {
                "marketplace_id": agg["marketplace_id"],
                "name": agg["name"],
                "location": agg["location"],
                "days": agg["days"],
                "revenue_cents": agg["revenue_cents"],
                "cost_cents": agg["cost_cents"],
                "net_cents": agg["net_cents"],
                "avg_revenue_cents": round(agg["revenue_cents"] / days),
                "avg_net_cents": round(agg["net_cents"] / days),
                "top_product": top,
            }
        )
    comparison.sort(key=lambda x: (-x["avg_net_cents"], -x["revenue_cents"]))

    untagged_days = [
        {"date": d_str, "revenue_cents": rev, "txns": day_txns.get(d_str, 0)}
        for d_str, rev in sorted(day_rev.items(), reverse=True)
        if d_str not in tagged_dates
    ][:10]

    return {
        "comparison": comparison,
        "market_day_rows": market_day_rows,
        "untagged_days": untagged_days,
    }


def _estimate_tender_cents(total_cents: int) -> int:
    if total_cents <= 0:
        return 0
    if total_cents < 1000:
        return 1000
    if total_cents == 1000:
        return 2000
    if total_cents <= 2000:
        return 2000
    if total_cents <= 4000:
        return 4000
    if total_cents <= 5000:
        return 5000
    return ((total_cents + 1999) // 2000) * 2000


def _round_up_mult(val: float, step: int, minimum: int = 0) -> int:
    import math
    n = max(minimum, math.ceil(val))
    if step <= 1:
        return n
    return ((n + step - 1) // step) * step


def _fmt_price_short(cents: int) -> str:
    return f"${cents // 100}" if cents % 100 == 0 else f"${cents / 100:.2f}"


def recommend_cash_drawer(
    merchant_id: int,
    since_iso: str,
    until_iso: str,
    tz,
    utc_windows: list[tuple[str, str]] | None = None,
) -> dict:
    """Estimate a recommended cash drawer float & bill breakdown from cash history + product prices."""
    from inventory.sales import _window_filter

    win_sql, win_args = _window_filter(utc_windows, alias="s")
    conn = get_connection()
    try:
        cash_sales = conn.execute(
            f"""
            SELECT s.occurred_at, s.total_cents, s.cash_received_cents
            FROM sales s
            WHERE s.merchant_id = ? AND s.status = 'completed'
              AND (s.source = 'cash' OR LOWER(COALESCE(s.payment_type, '')) = 'cash')
              AND s.total_cents > 0
              AND s.occurred_at >= ? AND s.occurred_at < ?{win_sql}
            """,
            [int(merchant_id), since_iso, until_iso, *win_args],
        ).fetchall()

        sold_prices = conn.execute(
            f"""
            SELECT si.unit_price_cents, SUM(si.quantity) AS q
            FROM sale_items si
            JOIN sales s ON s.id = si.sale_id
            LEFT JOIN skus k ON k.id = si.sku_id
            WHERE s.merchant_id = ? AND s.status = 'completed'
              AND COALESCE(k.item_type, 'product') != 'fee'
              AND si.unit_price_cents > 0
              AND s.occurred_at >= ? AND s.occurred_at < ?{win_sql}
            GROUP BY si.unit_price_cents
            ORDER BY q DESC
            LIMIT 6
            """,
            [int(merchant_id), since_iso, until_iso, *win_args],
        ).fetchall()

        if not sold_prices:
            sold_prices = conn.execute(
                """
                SELECT price_cents AS unit_price_cents, 1 AS q
                FROM skus
                WHERE merchant_id = ? AND active = 1
                  AND COALESCE(item_type, 'product') != 'fee'
                  AND price_cents > 0
                ORDER BY price_cents ASC
                LIMIT 6
                """,
                (int(merchant_id),),
            ).fetchall()
    finally:
        conn.close()

    price_list = [int(r["unit_price_cents"]) for r in sold_prices if int(r["unit_price_cents"]) > 0]
    sample_prices = [_fmt_price_short(p) for p in sorted(set(price_list))[:5]]
    has_cents = any(p % 100 != 0 for p in price_list)
    has_small_coins = any(p % 25 != 0 for p in price_list)

    if not cash_sales:
        # Starter recommendation derived purely from catalog pricing
        ones = 20
        fives = 8
        tens = 2
        quarters = 20 if has_cents else 0
        small_cents = 200 if has_small_coins else 0
        total_cents = tens * 1000 + fives * 500 + ones * 100 + quarters * 25 + small_cents
        denoms = [
            {"label": "$1 bills", "count": ones, "cents": ones * 100},
            {"label": "$5 bills", "count": fives, "cents": fives * 500},
            {"label": "$10 bills", "count": tens, "cents": tens * 1000},
        ]
        if quarters:
            denoms.append({"label": "Quarters", "count": quarters, "cents": quarters * 25})
        if small_cents:
            denoms.append({"label": "Dimes / nickels / pennies", "count": None, "cents": small_cents})
        return {
            "has_history": False,
            "cash_days": 0,
            "avg_cash_txns": 0.0,
            "avg_cash_cents": 0,
            "recommended_total_cents": total_cents,
            "denominations": denoms,
            "needs_coins": bool(quarters or small_cents),
            "sample_prices": sample_prices,
        }

    days_seen: set[str] = set()
    total_cash_cents = 0
    sum_tens = 0
    sum_fives = 0
    sum_ones = 0
    sum_quarters = 0
    sum_small = 0

    for row in cash_sales:
        dt = datetime.fromisoformat(str(row["occurred_at"]).replace("Z", "")[:19]).replace(tzinfo=timezone.utc)
        days_seen.add(dt.astimezone(tz).date().isoformat())
        tot = int(row["total_cents"] or 0)
        total_cash_cents += tot
        if tot % 100 != 0:
            has_cents = True
        if tot % 25 != 0:
            has_small_coins = True
        rec = row["cash_received_cents"]
        if rec is not None and int(rec) >= tot:
            change = int(rec) - tot
        else:
            change = max(0, _estimate_tender_cents(tot) - tot)
        rem = change
        sum_tens += rem // 1000
        rem %= 1000
        sum_fives += rem // 500
        rem %= 500
        sum_ones += rem // 100
        rem %= 100
        sum_quarters += rem // 25
        rem %= 25
        sum_small += rem

    n_days = max(1, len(days_seen))
    buf = 1.3  # 30% morning-rush safety buffer
    ones = _round_up_mult((sum_ones / n_days) * buf, step=5, minimum=15)
    fives = _round_up_mult((sum_fives / n_days) * buf, step=2, minimum=6)
    tens = _round_up_mult((sum_tens / n_days) * buf, step=1, minimum=2)
    quarters = _round_up_mult((sum_quarters / n_days) * buf, step=4, minimum=20) if has_cents else 0
    small_cents = _round_up_mult((sum_small / n_days) * buf, step=100, minimum=200) if has_small_coins else 0

    total_cents = tens * 1000 + fives * 500 + ones * 100 + quarters * 25 + small_cents
    denoms = [
        {"label": "$1 bills", "count": ones, "cents": ones * 100},
        {"label": "$5 bills", "count": fives, "cents": fives * 500},
        {"label": "$10 bills", "count": tens, "cents": tens * 1000},
    ]
    if quarters:
        denoms.append({"label": "Quarters", "count": quarters, "cents": quarters * 25})
    if small_cents:
        denoms.append({"label": "Dimes / nickels / pennies", "count": None, "cents": small_cents})

    return {
        "has_history": True,
        "cash_days": n_days,
        "avg_cash_txns": round(len(cash_sales) / n_days, 1),
        "avg_cash_cents": round(total_cash_cents / n_days),
        "recommended_total_cents": total_cents,
        "denominations": denoms,
        "needs_coins": bool(quarters or small_cents),
        "sample_prices": sample_prices,
    }


def suggest_pack_list(
    merchant_id: int,
    since_iso: str,
    until_iso: str,
    tz,
    utc_windows: list[tuple[str, str]] | None = None,
) -> dict:
    """Build a suggested pack list per market day with 1.25x (or 1.5x if sold out) buffer & stock shortage flags."""
    import math
    from inventory.sales import _window_filter
    from inventory.stock import fmt_qty

    win_sql, win_args = _window_filter(utc_windows, alias="s")
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT s.occurred_at, si.sku_id, si.name_snapshot, si.quantity,
                   COALESCE(k.sell_mode, 'unit') AS sell_mode
            FROM sale_items si
            JOIN sales s ON s.id = si.sale_id
            LEFT JOIN skus k ON k.id = si.sku_id
            WHERE s.merchant_id = ? AND s.status = 'completed'
              AND COALESCE(k.item_type, 'product') != 'fee'
              AND s.occurred_at >= ? AND s.occurred_at < ?{win_sql}
            """,
            [int(merchant_id), since_iso, until_iso, *win_args],
        ).fetchall()

        stock_rows = conn.execute(
            """
            SELECT k.id AS sku_id,
                   COALESCE(SUM(l.delta), 0) AS on_hand
            FROM skus k
            LEFT JOIN stock_ledger l ON l.sku_id = k.id
            WHERE k.merchant_id = ? AND k.active = 1
              AND COALESCE(k.item_type, 'product') != 'fee'
            GROUP BY k.id
            """,
            (int(merchant_id),),
        ).fetchall()
    finally:
        conn.close()

    on_hand_by_sku = {int(r["sku_id"]): float(r["on_hand"] or 0) for r in stock_rows}
    days_seen: set[str] = set()
    by_key: dict[tuple, dict] = {}

    for r in rows:
        dt = datetime.fromisoformat(str(r["occurred_at"]).replace("Z", "")[:19]).replace(tzinfo=timezone.utc)
        days_seen.add(dt.astimezone(tz).date().isoformat())
        sid = int(r["sku_id"]) if r["sku_id"] is not None else None
        name = str(r["name_snapshot"] or "Item")
        key = (sid, name)
        entry = by_key.setdefault(
            key,
            {"sku_id": sid, "name": name, "sell_mode": r["sell_mode"] or "unit", "total_qty": 0.0},
        )
        entry["total_qty"] += float(r["quantity"] or 0)

    n_days = len(days_seen)
    if not n_days:
        return {"days": 0, "items": [], "short_count": 0}

    items = []
    short_count = 0
    for entry in by_key.values():
        sid = entry["sku_id"]
        avg_day = entry["total_qty"] / n_days
        on_hand = on_hand_by_sku.get(sid) if sid is not None else None
        sold_out = on_hand is not None and on_hand <= 1e-6
        mult = 1.5 if sold_out else 1.25
        if entry["sell_mode"] == "lb_stock_lb":
            pack_qty = fmt_qty(math.ceil(avg_day * mult * 2) / 2.0)
            unit = "lb"
        else:
            pack_qty = max(1, math.ceil(avg_day * mult))
            unit = "units"
        short_by = 0
        if on_hand is not None and on_hand < pack_qty:
            short_by = fmt_qty(pack_qty - max(0.0, on_hand))
            if short_by > 0:
                short_count += 1
        items.append(
            {
                "key": f"{sid or 'x'}:{entry['name']}",
                "name": entry["name"],
                "avg_per_day": round(avg_day, 1),
                "pack_qty": pack_qty,
                "unit": unit,
                "on_hand": fmt_qty(on_hand) if on_hand is not None else None,
                "sold_out": sold_out,
                "short_by": short_by,
            }
        )
    items.sort(key=lambda x: (-x["pack_qty"], x["name"]))
    return {"days": n_days, "items": items, "short_count": short_count}
