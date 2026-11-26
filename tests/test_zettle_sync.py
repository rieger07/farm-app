import os
import tempfile
import unittest
from datetime import datetime, timedelta

import httpx

from inventory import sync, zettle
from inventory.db import get_connection, init_inventory_db
from inventory.merchants import create_merchant
from inventory.stock import list_stock
from shared.auth import create_user
from shared.crypto import PREFIX, decrypt_token, encrypt_token


def _ts(delta_minutes=0):
    t = datetime.utcnow() + timedelta(minutes=delta_minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%S.123+0000")


class FakeZettle:
    """httpx MockTransport handler emulating the three Zettle endpoints."""

    def __init__(self):
        self.purchases = []
        self.token_calls = 0
        self.catalog = [
            {"uuid": "p1", "name": "Clover honey", "category": {"name": "Honey"},
             "variants": [{"uuid": "v1", "name": "250 g jar", "sku": "CL-250",
                           "price": {"amount": 900, "currencyId": "USD"}}]},
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(zettle.TOKEN_URL):
            self.token_calls += 1
            return httpx.Response(200, json={
                "access_token": f"tok{self.token_calls}", "refresh_token": "r2",
                "expires_in": 7200})
        if url.startswith(zettle.PRODUCTS_URL):
            return httpx.Response(200, json=self.catalog)
        if url.startswith(zettle.PURCHASES_URL):
            return httpx.Response(200, json={"purchases": self.purchases,
                                             "lastPurchaseHash": "h"})
        return httpx.Response(404)


def purchase(uuid, minutes_ago, amount=900, qty="2", ptype="IZETTLE_CARD", refund=False):
    return {"purchaseUUID": uuid, "timestamp": _ts(-minutes_ago), "amount": amount,
            "refund": refund, "payments": [{"type": ptype, "amount": amount}],
            "products": [{"productUuid": "p1", "variantUuid": "v1", "name": "Clover honey",
                          "variantName": "250 g jar", "quantity": qty, "unitPrice": 450}]}


class SyncTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        os.environ["FARM_DB_PATH"] = os.path.join(self._dir.name, "t.db")
        os.environ["ZETTLE_CLIENT_ID"] = "cid"
        os.environ["ZETTLE_CLIENT_SECRET"] = "secret"
        init_inventory_db()
        uid = create_user("zach", "password1", "Zach")["id"]
        self.mid = create_merchant("Apiary", uid)
        self.fake = FakeZettle()
        self.http = httpx.Client(transport=httpx.MockTransport(self.fake))
        sync.save_connection(self.mid, {"access_token": "t0", "refresh_token": "r1",
                                        "expires_in": 7200})

    def tearDown(self):
        self.http.close()
        self._dir.cleanup()

    def test_timestamp_parsing(self):
        self.assertEqual(sync.parse_ts("2026-11-14T17:00:00.123+0000"), "2026-11-14T17:00:00")
        self.assertEqual(sync.parse_ts("2026-11-14T12:00:00-05:00"), "2026-11-14T17:00:00")
        self.assertEqual(sync.parse_ts("2026-11-14T17:00:00Z"), "2026-11-14T17:00:00")

    def test_authorize_url(self):
        url = zettle.build_authorize_url("https://x.test/cb", "st")
        self.assertIn("client_id=cid", url)
        self.assertIn("READ%3APURCHASE+READ%3APRODUCT", url)
        self.assertNotIn("secret", url)

    def test_catalog_mirror(self):
        res = sync.sync_merchant(self.mid, self.http)
        self.assertEqual(res["error"], "")
        row = list_stock(self.mid)[0]
        self.assertEqual((row["name"], row["variant_name"], row["category"], row["price_cents"]),
                         ("Clover honey", "250 g jar", "Honey", 900))
        self.fake.catalog = []  # an empty response must not wipe the catalog
        sync.sync_merchant(self.mid, self.http)
        self.assertEqual(len(list_stock(self.mid)), 1)

    def test_purchases_decrement_stock_once(self):
        sync.sync_merchant(self.mid, self.http)
        sku = list_stock(self.mid)[0]["id"]
        conn = get_connection()
        conn.execute("INSERT INTO stock_ledger (merchant_id, sku_id, delta, reason, created_at)"
                     " VALUES (?, ?, 20, 'initial', '2026-01-01')", (self.mid, sku))
        conn.commit(); conn.close()
        # Tracking starts at connect time, so a sale just after it counts.
        self.fake.purchases = [purchase("a", -1, qty="2", ptype="IZETTLE_CASH")]
        self.assertEqual(sync.sync_merchant(self.mid, self.http)["purchases"], 1)
        self.assertEqual(sync.sync_merchant(self.mid, self.http)["purchases"], 0)  # idempotent
        self.assertEqual(list_stock(self.mid)[0]["on_hand"], 18)
        conn = get_connection()
        self.assertEqual(conn.execute("SELECT payment_type FROM sales").fetchone()[0], "cash")
        conn.close()

    def test_history_before_connect_does_not_touch_stock(self):
        sync.sync_merchant(self.mid, self.http)
        self.fake.purchases = [purchase("old", 60 * 24 * 3)]
        sync.sync_merchant(self.mid, self.http)
        conn = get_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0], 1)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM stock_ledger WHERE reason='sale'").fetchone()[0], 0)
        conn.close()

    def test_refund_is_negative_and_reported_only(self):
        sync.sync_merchant(self.mid, self.http)
        self.fake.purchases = [purchase("r", -1, refund=True)]
        sync.sync_merchant(self.mid, self.http)
        conn = get_connection()
        row = conn.execute("SELECT total_cents, status FROM sales").fetchone()
        self.assertEqual((row[0], row[1]), (-900, "refunded"))
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM stock_ledger WHERE reason='sale'").fetchone()[0], 0)
        conn.close()

    def test_unknown_product_still_recorded(self):
        sync.sync_merchant(self.mid, self.http)
        p = purchase("u", -1)
        p["products"][0]["productUuid"] = "gone"
        self.fake.purchases = [p]
        sync.sync_merchant(self.mid, self.http)
        conn = get_connection()
        self.assertEqual(conn.execute("SELECT sku_id FROM sale_items").fetchone()[0], None)
        conn.close()

    def test_expired_token_is_refreshed(self):
        conn = get_connection()
        conn.execute("UPDATE zettle_connections SET expires_at='2000-01-01T00:00:00'")
        conn.commit(); conn.close()
        sync.sync_merchant(self.mid, self.http)
        self.assertEqual(self.fake.token_calls, 1)
        conn = get_connection()
        stored = conn.execute("SELECT access_token FROM zettle_connections").fetchone()[0]
        conn.close()
        self.assertTrue(stored.startswith(PREFIX))
        self.assertEqual(decrypt_token(stored), "tok1")

    def test_token_encryption_roundtrip_and_legacy_plaintext(self):
        enc = encrypt_token("secret-oauth-token")
        self.assertIsNotNone(enc)
        self.assertTrue(enc.startswith(PREFIX))
        self.assertNotIn("secret-oauth-token", enc)
        self.assertEqual(decrypt_token(enc), "secret-oauth-token")
        self.assertEqual(decrypt_token("legacy-plaintext-tok"), "legacy-plaintext-tok")
        self.assertIsNone(encrypt_token(None))
        self.assertIsNone(decrypt_token(None))

    def test_failure_is_recorded_not_raised(self):
        bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        res = sync.sync_merchant(self.mid, bad)
        self.assertTrue(res["error"])
        self.assertTrue(sync.connection_status(self.mid)["last_error"])

    def test_weight_based_catalog_and_purchase_sync(self):
        from inventory.stock import set_sell_mode, set_stock_count

        self.fake.catalog.append({
            "uuid": "p-lb",
            "name": "Whole Chicken",
            "unitName": "lb",
            "category": {"name": "Poultry"},
            "variants": [{"uuid": "v-lb", "name": "", "sku": "CHK-1",
                          "price": {"amount": 550, "currencyId": "USD"}}],
        })
        sync.sync_merchant(self.mid, self.http)
        chicken = [r for r in list_stock(self.mid) if r["name"] == "Whole Chicken"][0]
        self.assertEqual(chicken["sell_mode"], "lb_stock_lb")
        set_stock_count(self.mid, chicken["id"], 25)

        # Bulk lb mode: 1.75 lb purchase decrements 1.75
        self.fake.purchases.append({
            "purchaseUUID": "pur-lb-1",
            "timestamp": _ts(),
            "amount": 963,
            "products": [{"productUuid": "p-lb", "variantUuid": "v-lb",
                          "name": "Whole Chicken", "quantity": "1.75", "unitPrice": 550}],
        })
        sync.sync_merchant(self.mid, self.http)
        chicken = [r for r in list_stock(self.mid) if r["name"] == "Whole Chicken"][0]
        self.assertEqual(chicken["on_hand"], 23.25)

        # Whole-bird mode (sell by lb, stock by unit): 4.85 lb purchase decrements 1 unit
        set_sell_mode(self.mid, chicken["id"], "lb_stock_unit")
        set_stock_count(self.mid, chicken["id"], 25)
        self.fake.purchases.append({
            "purchaseUUID": "pur-lb-2",
            "timestamp": _ts(),
            "amount": 2668,
            "products": [{"productUuid": "p-lb", "variantUuid": "v-lb",
                          "name": "Whole Chicken", "quantity": "4.85", "unitPrice": 550}],
        })
        sync.sync_merchant(self.mid, self.http)
        chicken = [r for r in list_stock(self.mid) if r["name"] == "Whole Chicken"][0]
        self.assertEqual(chicken["on_hand"], 24)


if __name__ == "__main__":
    unittest.main()
