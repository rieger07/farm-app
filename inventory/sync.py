"""Zettle sync: connection storage, catalog mirror and purchase import."""

import logging
from datetime import datetime, timedelta, timezone

import httpx

from inventory import zettle
from inventory.crypto import decrypt_token, encrypt_token
from inventory.db import get_connection
from inventory.stock import fmt_qty

log = logging.getLogger(__name__)
INITIAL_LOOKBACK_DAYS = 30
OVERLAP_DAYS = 1  # re-read a day of purchases each poll; inserts are idempotent


def _now_dt() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def parse_ts(value: str) -> str:
    """Normalise a Zettle timestamp to naive-UTC ISO seconds."""
    v = str(value).strip().replace("Z", "+00:00")
    if len(v) > 5 and v[-5] in "+-" and v[-3] != ":":
        v = v[:-2] + ":" + v[-2:]  # +0000 -> +00:00
    if "." in v:
        head, rest = v.split(".", 1)
        tz = ""
        for sep in ("+", "-"):
            if sep in rest:
                rest, tz = rest.split(sep, 1)
                tz = sep + tz
                break
        v = head + tz
    dt = datetime.fromisoformat(v)
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return _iso(dt)


# ---- state helpers -------------------------------------------------------

def get_state(conn, merchant_id: int, key: str, default: str | None = None):
    row = conn.execute(
        "SELECT value FROM sync_state WHERE merchant_id = ? AND key = ?",
        (merchant_id, key),
    ).fetchone()
    return row["value"] if row else default


def set_state(conn, merchant_id: int, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_state (merchant_id, key, value) VALUES (?, ?, ?)"
        " ON CONFLICT(merchant_id, key) DO UPDATE SET value = excluded.value",
        (merchant_id, key, value),
    )


# ---- connection ----------------------------------------------------------

def save_connection(merchant_id: int, tokens: dict) -> None:
    expires = _now_dt() + timedelta(seconds=int(tokens.get("expires_in", 7200)))
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO zettle_connections (merchant_id, access_token, refresh_token,"
            " expires_at, scopes, connected_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(merchant_id) DO UPDATE SET access_token = excluded.access_token,"
            " refresh_token = COALESCE(excluded.refresh_token, refresh_token),"
            " expires_at = excluded.expires_at, scopes = excluded.scopes",
            (
                merchant_id,
                encrypt_token(tokens.get("access_token")),
                encrypt_token(tokens.get("refresh_token")),
                _iso(expires),
                zettle.SCOPES,
                _iso(_now_dt()),
            ),
        )
        # Sales before this moment are reported but never touch stock counts.
        if get_state(conn, merchant_id, "stock_tracking_start") is None:
            set_state(conn, merchant_id, "stock_tracking_start", _iso(_now_dt()))
        conn.commit()
    finally:
        conn.close()


def is_connected(merchant_id: int) -> bool:
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT 1 FROM zettle_connections WHERE merchant_id = ?", (merchant_id,)
        ).fetchone() is not None
    finally:
        conn.close()


def connection_status(merchant_id: int) -> dict:
    conn = get_connection()
    try:
        return {
            "connected": conn.execute(
                "SELECT 1 FROM zettle_connections WHERE merchant_id = ?", (merchant_id,)
            ).fetchone() is not None,
            "last_sync": get_state(conn, merchant_id, "last_sync_at"),
            "last_error": get_state(conn, merchant_id, "last_error") or "",
        }
    finally:
        conn.close()


def _valid_token(merchant_id: int, http: httpx.Client | None) -> str:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT access_token, refresh_token, expires_at FROM zettle_connections"
            " WHERE merchant_id = ?",
            (merchant_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise zettle.ZettleError("Zettle is not connected.")
    if row["expires_at"] and row["expires_at"] > _iso(_now_dt() + timedelta(seconds=60)):
        return decrypt_token(row["access_token"])
    tokens = zettle.refresh_access_token(decrypt_token(row["refresh_token"]), http)
    save_connection(merchant_id, tokens)
    return tokens["access_token"]


# ---- catalog -------------------------------------------------------------

def sync_catalog(merchant_id: int, api: zettle.ZettleClient) -> int:
    products = api.products()
    now = _iso(_now_dt())
    seen = set()
    conn = get_connection()
    try:
        for p in products:
            category = (p.get("category") or {}).get("name", "") if isinstance(
                p.get("category"), dict) else ""
            for v in p.get("variants") or [{}]:
                puid, vuid = p.get("uuid", ""), v.get("uuid", "") or ""
                if not puid:
                    continue
                price = v.get("price") or {}
                vname = v.get("name") or ""
                if vname == p.get("name"):
                    vname = ""
                unit_name = str(p.get("unitName") or v.get("unitName") or "").strip().lower()
                default_mode = "lb_stock_lb" if unit_name in ("lb", "lbs", "pound", "pounds") else "unit"
                conn.execute(
                    "INSERT INTO skus (merchant_id, zettle_product_uuid, zettle_variant_uuid,"
                    " name, variant_name, sku_code, category, price_cents, currency, active,"
                    " sell_mode, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)"
                    " ON CONFLICT(merchant_id, zettle_product_uuid, zettle_variant_uuid)"
                    " DO UPDATE SET name = excluded.name, variant_name = excluded.variant_name,"
                    " sku_code = excluded.sku_code, category = excluded.category,"
                    " price_cents = excluded.price_cents, currency = excluded.currency,"
                    " active = 1, updated_at = excluded.updated_at",
                    (
                        merchant_id, puid, vuid, p.get("name", "Unnamed"), vname,
                        v.get("sku") or "", category, int(price.get("amount") or 0),
                        price.get("currencyId") or "USD", default_mode, now,
                    ),
                )
                seen.add((puid, vuid))
        if seen:  # never wipe the catalog on an empty response
            for r in conn.execute(
                "SELECT id, zettle_product_uuid, zettle_variant_uuid FROM skus"
                " WHERE merchant_id = ? AND active = 1", (merchant_id,)
            ).fetchall():
                if (r["zettle_product_uuid"], r["zettle_variant_uuid"]) not in seen:
                    conn.execute("UPDATE skus SET active = 0 WHERE id = ?", (r["id"],))
        conn.commit()
    finally:
        conn.close()
    return len(seen)


# ---- purchases -----------------------------------------------------------

def _payment_type(purchase: dict) -> str:
    types = [str(p.get("type", "")) for p in purchase.get("payments") or []]
    if any("CASH" in t for t in types):
        return "cash"
    if any("CARD" in t for t in types):
        return "card"
    return types[0].lower() if types else ""


def import_purchase(conn, merchant_id: int, purchase: dict, tracking_start: str) -> bool:
    """Insert one purchase. Returns False if it was already imported."""
    uuid = purchase.get("purchaseUUID") or purchase.get("purchaseUuid")
    if not uuid:
        return False
    if conn.execute(
        "SELECT 1 FROM sales WHERE merchant_id = ? AND source = 'zettle' AND external_id = ?",
        (merchant_id, uuid),
    ).fetchone():
        return False

    occurred = parse_ts(purchase.get("timestamp"))
    refund = bool(purchase.get("refund"))
    total = int(purchase.get("amount") or 0)
    if refund and total > 0:
        total = -total
    now = _iso(_now_dt())
    cur = conn.execute(
        "INSERT INTO sales (merchant_id, source, external_id, occurred_at, total_cents,"
        " status, payment_type, created_at) VALUES (?, 'zettle', ?, ?, ?, ?, ?, ?)",
        (merchant_id, uuid, occurred, total, "refunded" if refund else "completed",
         _payment_type(purchase), now),
    )
    sale_id = int(cur.lastrowid)
    for line in purchase.get("products") or []:
        qty = fmt_qty(float(line.get("quantity") or 1))
        sku = conn.execute(
            "SELECT id, sell_mode FROM skus WHERE merchant_id = ? AND zettle_product_uuid = ?"
            " AND zettle_variant_uuid = ?",
            (merchant_id, line.get("productUuid", ""), line.get("variantUuid") or ""),
        ).fetchone()
        label = line.get("name") or "Item"
        if line.get("variantName"):
            label += f" · {line['variantName']}"
        item = conn.execute(
            "INSERT INTO sale_items (sale_id, sku_id, name_snapshot, quantity,"
            " unit_price_cents) VALUES (?, ?, ?, ?, ?)",
            (sale_id, sku["id"] if sku else None, label, qty,
             int(line.get("unitPrice") or 0)),
        )
        # Refunds are reported only. Whether a returned jar goes back on the
        # shelf is a human call, so Zach makes it with a manual adjustment.
        if sku and not refund and occurred >= tracking_start:
            stock_delta = -1 if sku["sell_mode"] == "lb_stock_unit" else -qty
            conn.execute(
                "INSERT INTO stock_ledger (merchant_id, sku_id, delta, reason,"
                " sale_item_id, created_at) VALUES (?, ?, ?, 'sale', ?, ?)",
                (merchant_id, sku["id"], stock_delta, int(item.lastrowid), now),
            )
    return True


def sync_purchases(merchant_id: int, api: zettle.ZettleClient) -> int:
    conn = get_connection()
    try:
        cursor = get_state(conn, merchant_id, "purchases_cursor")
        if cursor:
            start = _iso(datetime.fromisoformat(cursor) - timedelta(days=OVERLAP_DAYS))
        else:
            start = _iso(_now_dt() - timedelta(days=INITIAL_LOOKBACK_DAYS))
        tracking_start = get_state(conn, merchant_id, "stock_tracking_start") or _iso(_now_dt())
        added, newest = 0, cursor or ""
        for purchase in api.purchases(start):
            if import_purchase(conn, merchant_id, purchase, tracking_start):
                added += 1
            ts = parse_ts(purchase.get("timestamp"))
            newest = max(newest, ts)
        if newest:
            set_state(conn, merchant_id, "purchases_cursor", newest)
        conn.commit()
        return added
    finally:
        conn.close()


def sync_merchant(merchant_id: int, http: httpx.Client | None = None) -> dict:
    """Run a full sync for one merchant, recording success or the error."""
    result = {"products": 0, "purchases": 0, "error": ""}
    try:
        api = zettle.ZettleClient(_valid_token(merchant_id, http), http)
        result["products"] = sync_catalog(merchant_id, api)
        result["purchases"] = sync_purchases(merchant_id, api)
    except Exception as exc:  # noqa: BLE001 - surfaced to the settings page
        log.exception("Zettle sync failed for merchant %s", merchant_id)
        result["error"] = str(exc) or exc.__class__.__name__
    conn = get_connection()
    try:
        set_state(conn, merchant_id, "last_error", result["error"])
        if not result["error"]:
            set_state(conn, merchant_id, "last_sync_at", _iso(_now_dt()))
        conn.commit()
    finally:
        conn.close()
    return result


def sync_all() -> None:
    conn = get_connection()
    try:
        ids = [r["merchant_id"] for r in conn.execute(
            "SELECT merchant_id FROM zettle_connections").fetchall()]
    finally:
        conn.close()
    for mid in ids:
        sync_merchant(mid)
