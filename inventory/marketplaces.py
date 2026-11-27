"""Marketplaces CRUD, Market Day metadata records, and local-day UTC windows."""

from __future__ import annotations

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


def _market_day_window_bounds(
    day_str: str,
    tz,
    status: str = "open",
    closed_at: str | None = None,
) -> tuple[str, str]:
    """Return [start_utc, end_utc) for a Market Day: 12:00 AM local through closed_at (capped to day end)."""
    s0, s1 = _day_utc_bounds(day_str, tz)
    if status == "closed" and closed_at:
        ca = str(closed_at).replace("Z", "")[:26]
        if ca < s0:
            return s0, s0
        if ca < s1:
            return s0, ca
    return s0, s1


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
            "SELECT market_date, COALESCE(status, 'open') AS status, closed_at"
            " FROM market_days"
            " WHERE merchant_id = ? AND marketplace_id = ?"
            " AND market_date >= ? AND market_date <= ?"
            " ORDER BY market_date ASC",
            (int(merchant_id), int(marketplace_id), date_from, date_to),
        ).fetchall()
    finally:
        conn.close()
    return [_market_day_window_bounds(r["market_date"], tz, r["status"], r["closed_at"]) for r in rows]
