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
