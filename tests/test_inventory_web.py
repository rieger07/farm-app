import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from inventory.db import get_connection
from inventory.seed import seed


class WebTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        os.environ["FARM_DB_PATH"] = os.path.join(self._dir.name, "t.db")
        self.mid = seed()
        from inventory.app import app

        self.app = app

    def tearDown(self):
        self._dir.cleanup()

    def client(self, user):
        c = TestClient(self.app, follow_redirects=False)
        r = c.post("/login", data={"username": user, "password": "honey1234"})
        self.assertEqual(r.status_code, 303)
        return c

    def test_login_required(self):
        c = TestClient(self.app, follow_redirects=False)
        self.assertEqual(c.get("/inventory").headers["location"], "/login")
        self.assertEqual(c.post("/api/cash-sale", json={}).status_code, 401)
        bad = c.post("/login", data={"username": "zach", "password": "nope"})
        self.assertEqual(bad.status_code, 401)

    def test_owner_pages_render(self):
        c = self.client("zach")
        self.assertEqual(c.get("/").headers["location"], "/inventory")
        inv = c.get("/inventory")
        self.assertEqual(inv.status_code, 200)
        self.assertIn("Wildflower honey", inv.text)
        self.assertIn("Low stock", inv.text)
        for url in ("/sales", "/sales?period=week", "/sales?source=cash", "/quick-sale"):
            self.assertEqual(c.get(url).status_code, 200, url)

    def test_staff_lands_on_quick_sale_only(self):
        c = self.client("marty")
        self.assertEqual(c.get("/").headers["location"], "/quick-sale")
        self.assertEqual(c.get("/inventory").headers["location"], "/quick-sale")
        self.assertEqual(c.get("/sales").headers["location"], "/quick-sale")
        page = c.get("/quick-sale")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('href="/inventory"', page.text)

    def test_cash_sale_updates_stock_and_is_idempotent(self):
        c = self.client("marty")
        conn = get_connection()
        sku = conn.execute("SELECT id, price_cents FROM skus WHERE sku_code='CH-200'").fetchone()
        before = conn.execute("SELECT SUM(delta) FROM stock_ledger WHERE sku_id=?", (sku["id"],)).fetchone()[0]
        body = {"client_uuid": "abc-123", "cash_received_cents": 2000,
                "items": [{"sku_id": sku["id"], "quantity": 2}]}
        r1 = c.post("/api/cash-sale", json=body).json()
        r2 = c.post("/api/cash-sale", json=body).json()
        self.assertEqual(r1["total_cents"], 2 * sku["price_cents"])
        self.assertFalse(r1["duplicate"])
        self.assertTrue(r2["duplicate"])
        self.assertEqual(r1["sale_id"], r2["sale_id"])
        after = conn.execute("SELECT SUM(delta) FROM stock_ledger WHERE sku_id=?", (sku["id"],)).fetchone()[0]
        self.assertEqual(after, before - 2)
        conn.close()

    def test_sale_never_blocked_by_stock(self):
        c = self.client("zach")
        conn = get_connection()
        sku = conn.execute("SELECT id FROM skus WHERE sku_code='CH-200'").fetchone()["id"]
        conn.close()
        r = c.post("/api/cash-sale", json={"client_uuid": "big", "items": [{"sku_id": sku, "quantity": 500}]})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Count needed", c.get("/inventory").text)

    def test_invalid_sale_rejected(self):
        c = self.client("zach")
        r = c.post("/api/cash-sale", json={"client_uuid": "e", "items": []})
        self.assertEqual(r.status_code, 400)
        r = c.post("/api/cash-sale", json={"client_uuid": "e2", "items": [{"sku_id": 99999, "quantity": 1}]})
        self.assertEqual(r.status_code, 400)

    def test_first_run_onboarding(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["FARM_DB_PATH"] = os.path.join(d, "fresh.db")
            from inventory.db import init_inventory_db

            init_inventory_db()
            c = TestClient(self.app, follow_redirects=False)
            self.assertIn("Set up your workspace", c.get("/login").text)
            r = c.post(
                "/login",
                data={
                    "merchant_name": "Rieger Farms",
                    "display_name": "Steve",
                    "username": "steve",
                    "password": "password123",
                },
            )
            self.assertEqual(r.status_code, 303)
            inv = c.get("/inventory")
            self.assertEqual(inv.status_code, 200)
            self.assertIn("Rieger Farms", inv.text)

    def test_stock_update_and_member_management(self):
        c = self.client("zach")
        conn = get_connection()
        sku = conn.execute("SELECT id FROM skus WHERE sku_code='CH-200'").fetchone()["id"]
        conn.close()

        # Set exact count to 50 and threshold to 15
        r = c.post(
            "/inventory/stock",
            data={
                "sku_id": sku,
                "mode": "set",
                "quantity": 50,
                "low_stock_threshold": 15,
                "note": "Counted shelf",
            },
        )
        self.assertEqual(r.status_code, 303)
        conn = get_connection()
        on_hand = conn.execute("SELECT SUM(delta) FROM stock_ledger WHERE sku_id=?", (sku,)).fetchone()[0]
        thresh = conn.execute("SELECT low_stock_threshold FROM skus WHERE id=?", (sku,)).fetchone()[0]
        conn.close()
        self.assertEqual((on_hand, thresh), (50, 15))

        # Restock +10
        c.post(
            "/inventory/stock",
            data={"sku_id": sku, "mode": "restock", "quantity": 10, "low_stock_threshold": 15},
        )
        conn = get_connection()
        on_hand = conn.execute("SELECT SUM(delta) FROM stock_ledger WHERE sku_id=?", (sku,)).fetchone()[0]
        conn.close()
        self.assertEqual(on_hand, 60)

        # Add a new staff member from settings and verify they can log in
        r = c.post(
            "/settings/members",
            data={
                "username": "helper1",
                "display_name": "Market Helper",
                "password": "helperpass",
                "role": "staff",
            },
        )
        self.assertEqual(r.status_code, 303)
        helper_client = TestClient(self.app, follow_redirects=False)
        login_res = helper_client.post(
            "/login", data={"username": "helper1", "password": "helperpass"}
        )
        self.assertEqual(login_res.status_code, 303)
        self.assertEqual(helper_client.get("/").headers["location"], "/quick-sale")


if __name__ == "__main__":
    unittest.main()
