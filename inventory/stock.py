"""Stock ledger queries. On hand is always derived from the ledger."""

from inventory.db import get_connection


SELL_MODES = ("unit", "lb_stock_unit", "lb_stock_lb")
ITEM_TYPES = ("product", "fee")


def fmt_qty(val: float | int) -> int | float:
    r = round(float(val or 0), 3)
    return int(r) if r == int(r) else r


def stock_status(on_hand: float, threshold: float) -> str:
    if on_hand < -1e-6:
        return "count_needed"
    if abs(on_hand) <= 1e-6:
        return "out_of_stock"
    if on_hand <= threshold + 1e-6:
        return "low"
    return "in_stock"


def add_ledger_entry(
    merchant_id: int,
    sku_id: int,
    delta: float,
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
                fmt_qty(delta),
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
    new_on_hand: float,
    note: str = "",
    created_by: int | None = None,
) -> int | float:
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
        current = float(row["on_hand"])
        reason = "initial" if int(row["entry_count"]) == 0 else "adjustment"
    finally:
        conn.close()
    delta = fmt_qty(float(new_on_hand) - current)
    if abs(delta) > 1e-6 or reason == "initial":
        add_ledger_entry(
            merchant_id, sku_id, delta, reason, note or "Stock count", created_by
        )
    return delta


def set_low_stock_threshold(
    merchant_id: int, sku_id: int, threshold: float
) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE skus SET low_stock_threshold = ?"
            " WHERE id = ? AND merchant_id = ?",
            (max(0, fmt_qty(threshold)), int(sku_id), int(merchant_id)),
        )
        conn.commit()
    finally:
        conn.close()


def set_sell_mode(merchant_id: int, sku_id: int, sell_mode: str) -> None:
    if sell_mode not in SELL_MODES:
        raise ValueError(f"Invalid sell mode: {sell_mode}")
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE skus SET sell_mode = ? WHERE id = ? AND merchant_id = ?",
            (sell_mode, int(sku_id), int(merchant_id)),
        )
        conn.commit()
    finally:
        conn.close()


def set_quick_sale_visibility(
    merchant_id: int, sku_id: int, show: bool
) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE skus SET show_in_quick_sale = ?"
            " WHERE id = ? AND merchant_id = ?",
            (1 if show else 0, int(sku_id), int(merchant_id)),
        )
        conn.commit()
    finally:
        conn.close()


def set_item_type(merchant_id: int, sku_id: int, item_type: str) -> None:
    if item_type not in ITEM_TYPES:
        raise ValueError(f"Invalid item type: {item_type}")
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE skus SET item_type = ? WHERE id = ? AND merchant_id = ?",
            (item_type, int(sku_id), int(merchant_id)),
        )
        conn.commit()
    finally:
        conn.close()


def list_stock(merchant_id: int, include_fees: bool = False) -> list[dict]:
    """Active SKUs with derived on-hand quantity and status."""
    fee_filter = "" if include_fees else " AND COALESCE(s.item_type, 'product') != 'fee'"
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT s.*, COALESCE(SUM(l.delta), 0) AS on_hand"
            " FROM skus s LEFT JOIN stock_ledger l ON l.sku_id = s.id"
            f" WHERE s.merchant_id = ? AND s.active = 1{fee_filter}"
            " GROUP BY s.id ORDER BY s.category, s.name, s.variant_name",
            (int(merchant_id),),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["on_hand"] = fmt_qty(d["on_hand"])
        d["low_stock_threshold"] = fmt_qty(d["low_stock_threshold"])
        d["sell_mode"] = d.get("sell_mode") or "unit"
        d["item_type"] = d.get("item_type") or "product"
        d["stock_unit"] = "lb" if d["sell_mode"] == "lb_stock_lb" else "units"
        d["status"] = stock_status(d["on_hand"], d["low_stock_threshold"])
        out.append(d)
    return out


def list_sku_history(merchant_id: int, sku_id: int, limit: int = 8) -> list[dict]:
    """Recent ledger entries for a single SKU, newest first."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT l.id, l.delta, l.reason, l.note, l.created_at,
                   COALESCE(u.display_name, u.username, 'Zettle sync') AS actor
            FROM stock_ledger l
            LEFT JOIN users u ON u.id = l.created_by
            WHERE l.merchant_id = ? AND l.sku_id = ?
            ORDER BY l.id DESC
            LIMIT ?
            """,
            (int(merchant_id), int(sku_id), int(limit)),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["delta"] = fmt_qty(d["delta"])
            out.append(d)
        return out
    finally:
        conn.close()
