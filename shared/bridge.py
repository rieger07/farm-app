"""Cross-app database helpers bridging Homestead Planner and Iron Ledger tables."""

from __future__ import annotations

from datetime import datetime
import sqlite3

from shared.db.connection import get_connection


def list_active_ledger_skus() -> list[dict]:
    """List active non-fee Iron Ledger SKUs ordered with egg products first."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT k.id, k.merchant_id, k.name, k.variant_name, k.price_cents,
                   COALESCE(m.name, 'Merchant #' || k.merchant_id) AS merchant_name,
                   COALESCE((SELECT SUM(delta) FROM stock_ledger l WHERE l.sku_id = k.id), 0) AS on_hand
            FROM skus k
            LEFT JOIN merchants m ON m.id = k.merchant_id
            WHERE k.active = 1 AND COALESCE(k.item_type, 'product') != 'fee'
            ORDER BY CASE WHEN LOWER(k.name) LIKE '%egg%' THEN 0 ELSE 1 END, k.name ASC, k.id ASC
            """
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
