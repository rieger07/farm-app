"""Stock ledger queries. On hand is always derived from the ledger."""

from inventory.db import get_connection


def stock_status(on_hand: int, threshold: int) -> str:
    if on_hand < 0:
        return "count_needed"
    if on_hand <= threshold:
        return "low"
    return "in_stock"


def add_ledger_entry(
    merchant_id: int,
    sku_id: int,
    delta: int,
    reason: str,
    note: str = "",
    created_by: int | None = None,
) -> None:
    from datetime import datetime

    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO stock_ledger"
            " (merchant_id, sku_id, delta, reason, note, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                int(merchant_id),
                int(sku_id),
                int(delta),
                reason,
                note,
                created_by,
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def set_stock_count(
    merchant_id: int,
    sku_id: int,
    new_on_hand: int,
    note: str = "",
    created_by: int | None = None,
) -> int:
    """Set exact on-hand count for a SKU by appending a delta entry. Returns delta."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(l.delta), 0) AS on_hand,"
            " COUNT(l.id) AS entry_count"
            " FROM skus s LEFT JOIN stock_ledger l ON l.sku_id = s.id"
            " WHERE s.id = ? AND s.merchant_id = ?"
            " GROUP BY s.id",
            (int(sku_id), int(merchant_id)),
        ).fetchone()
        if not row:
            raise ValueError(f"Unknown SKU {sku_id}.")
        current = int(row["on_hand"])
        reason = "initial" if int(row["entry_count"]) == 0 else "adjustment"
    finally:
        conn.close()
    delta = int(new_on_hand) - current
    if delta != 0 or reason == "initial":
        add_ledger_entry(
            merchant_id, sku_id, delta, reason, note or "Stock count", created_by
        )
    return delta


def set_low_stock_threshold(
    merchant_id: int, sku_id: int, threshold: int
) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE skus SET low_stock_threshold = ?"
            " WHERE id = ? AND merchant_id = ?",
            (max(0, int(threshold)), int(sku_id), int(merchant_id)),
        )
        conn.commit()
    finally:
        conn.close()


def list_stock(merchant_id: int) -> list[dict]:
    """Active SKUs with derived on-hand quantity and status."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT s.*, COALESCE(SUM(l.delta), 0) AS on_hand"
            " FROM skus s LEFT JOIN stock_ledger l ON l.sku_id = s.id"
            " WHERE s.merchant_id = ? AND s.active = 1"
            " GROUP BY s.id ORDER BY s.category, s.name, s.variant_name",
            (int(merchant_id),),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["status"] = stock_status(d["on_hand"], d["low_stock_threshold"])
        out.append(d)
    return out
