"""Seed demo data: python -m inventory.seed

Creates users zach (owner), marty (staff) and a demo merchant with the six
products from the design mocks plus a week of Zettle and cash sales.
Password for both users: ``honey1234`` (demo only).
"""

import random
import uuid
from datetime import datetime, timedelta

from homestead.database import create_user, get_user_by_username
from inventory.db import get_connection, init_inventory_db
from inventory.merchants import STAFF, add_member, create_merchant
from inventory.sales import record_cash_sale
from inventory.stock import add_ledger_entry

PRODUCTS = [
    ("Wildflower honey", "250 g jar", "WF-250", "Honey", 800, 64),
    ("Wildflower honey", "500 g jar", "WF-500", "Honey", 1400, 38),
    ("Clover honey", "250 g jar", "CL-250", "Honey", 900, 42),
    ("Clover honey", "500 g jar", "CL-500", "Honey", 1600, 12),
    ("Beeswax candle", "Single candle", "BW-001", "Beeswax", 600, 24),
    ("Comb honey", "200 g box", "CH-200", "Honey", 1800, 6),
]


def _user(username, display):
    return get_user_by_username(username) or create_user(
        username, "honey1234", display, claim_unowned_data=False
    )


def seed(days: int = 7, rng_seed: int = 7) -> int:
    init_inventory_db()
    rng = random.Random(rng_seed)
    zach, marty = _user("zach", "Zach"), _user("marty", "Marty")
    merchant_id = create_merchant("Willow Creek Apiary", zach["id"])
    add_member(merchant_id, marty["id"], STAFF)

    conn = get_connection()
    now = datetime.utcnow().isoformat(timespec="seconds")
    sku_ids = []
    for name, variant, code, cat, price, qty in PRODUCTS:
        cur = conn.execute(
            "INSERT INTO skus (merchant_id, zettle_product_uuid, zettle_variant_uuid,"
            " name, variant_name, sku_code, category, price_cents, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (merchant_id, f"demo-{code}", f"demo-{code}-v", name, variant, code,
             cat, price, now),
        )
        sku_ids.append((int(cur.lastrowid), price, qty))
    conn.commit()

    # Opening counts are larger so seeded sales leave the mock's on-hand numbers.
    start = datetime.utcnow().replace(hour=9, minute=0, second=0, microsecond=0)
    start -= timedelta(days=days - 1)
    plan = []
    for d in range(days):
        for _ in range(rng.randint(2, 6)):
            plan.append((d, rng.randint(9, 16), rng.randrange(len(sku_ids))))
    sold = {i: 0 for i in range(len(sku_ids))}
    for _, _, i in plan:
        sold[i] += 1
    for i, (sku_id, _, qty) in enumerate(sku_ids):
        add_ledger_entry(merchant_id, sku_id, qty + sold[i], "initial", "seed")

    for d, hour, i in plan:
        sku_id, price, _ = sku_ids[i]
        when = (start + timedelta(days=d)).replace(hour=hour, minute=rng.randint(0, 59))
        if rng.random() < 0.6:  # simulated Zettle purchase
            c = conn.execute(
                "INSERT INTO sales (merchant_id, source, external_id, occurred_at,"
                " total_cents, created_at) VALUES (?, 'zettle', ?, ?, ?, ?)",
                (merchant_id, str(uuid.uuid4()), when.isoformat(timespec="seconds"),
                 price, now),
            )
            item = conn.execute(
                "INSERT INTO sale_items (sale_id, sku_id, name_snapshot, quantity,"
                " unit_price_cents) VALUES (?, ?, 'demo', 1, ?)",
                (c.lastrowid, sku_id, price),
            )
            conn.execute(
                "INSERT INTO stock_ledger (merchant_id, sku_id, delta, reason,"
                " sale_item_id, created_at) VALUES (?, ?, -1, 'sale', ?, ?)",
                (merchant_id, sku_id, item.lastrowid, now),
            )
            conn.commit()
        else:
            record_cash_sale(
                merchant_id, str(uuid.uuid4()), [{"sku_id": sku_id, "quantity": 1}],
                cash_received_cents=price, created_by=zach["id"],
                occurred_at=when.isoformat(timespec="seconds"),
            )
    conn.close()
    return merchant_id


if __name__ == "__main__":
    print(f"Seeded merchant {seed()}. Login: zach / honey1234 or marty / honey1234")
