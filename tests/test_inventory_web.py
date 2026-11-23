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
        self.assertEqual(helper_client.get("/admin").status_code, 303)

    def test_super_admin_tenants_invites_and_workspace_switcher(self):
        # zach is the first user in seed(), so zach has is_admin=1
        admin = self.client("zach")
        admin_page = admin.get("/admin")
        self.assertEqual(admin_page.status_code, 200)
        self.assertIn("Tenants &amp; Onboarding", admin_page.text)

        # 1. Create a second tenant directly via /admin/merchants/create
        r = admin.post(
            "/admin/merchants/create",
            data={
                "merchant_name": "Zach's Bees",
                "owner_username": "zach2",
                "owner_display_name": "Zach Owner",
                "owner_password": "secret99",
            },
        )
        self.assertEqual(r.status_code, 303)
        self.assertIn("Zach&#39;s Bees", admin.get("/admin").text)

        # 2. Switch active workspace as Super Admin
        conn = get_connection()
        new_mid = conn.execute(
            "SELECT id FROM merchants WHERE name = ?", ("Zach's Bees",)
        ).fetchone()["id"]
        conn.close()
        sw = admin.post(
            "/switch-merchant",
            data={"merchant_id": new_mid, "next_url": "/inventory"},
        )
        self.assertEqual(sw.status_code, 303)
        self.assertIn("Zach&#39;s Bees", admin.get("/inventory").text)

        # 3. Generate a one-time invite link for a brand-new tenant
        inv_res = admin.post(
            "/admin/invites/create",
            data={
                "target_merchant_id": "",
                "role": "owner",
                "default_merchant_name": "Clover Hill Honey",
                "note": "For new vendor",
                "days_valid": 7,
            },
        )
        self.assertEqual(inv_res.status_code, 303)
        loc = inv_res.headers["location"]
        token = loc.split("invite_token=")[1]

        # Redeem the one-time invite link as a new user
        anon = TestClient(self.app, follow_redirects=False)
        self.assertEqual(anon.get(f"/invite/{token}").status_code, 200)
        redeem = anon.post(
            f"/invite/{token}",
            data={
                "merchant_name": "Clover Hill Honey",
                "display_name": "Clover Vendor",
                "username": "clover",
                "password": "clover123",
            },
        )
        self.assertEqual(redeem.status_code, 303)
        self.assertIn("Clover Hill Honey", anon.get("/inventory").text)

        # Reusing the same one-time invite link must fail with 404/400
        self.assertEqual(anon.get(f"/invite/{token}").status_code, 404)
        self.assertEqual(
            anon.post(
                f"/invite/{token}",
                data={
                    "merchant_name": "Again",
                    "username": "again",
                    "password": "again123",
                },
            ).status_code,
            400,
        )

        # 4. Generate a staff invite link for an existing tenant (Marty flow)
        staff_inv = admin.post(
            "/admin/invites/create",
            data={
                "target_merchant_id": str(new_mid),
                "role": "staff",
                "note": "For Marty",
                "days_valid": 7,
            },
        )
        staff_token = staff_inv.headers["location"].split("invite_token=")[1]
        marty2 = TestClient(self.app, follow_redirects=False)
        self.assertEqual(
            marty2.post(
                f"/invite/{staff_token}",
                data={
                    "display_name": "Marty Stall",
                    "username": "marty_stall",
                    "password": "stall123",
                },
            ).status_code,
            303,
        )
        self.assertEqual(marty2.get("/").headers["location"], "/quick-sale")

    def test_sales_ranges_accordion_and_csv(self):
        import csv
        import io
        from datetime import datetime, timedelta

        c = self.client("zach")
        # Ring a cash sale now so there is a sale with detail to expand.
        conn = get_connection()
        sku = conn.execute("SELECT id FROM skus WHERE sku_code='CL-250'").fetchone()["id"]
        conn.close()
        c.post("/api/cash-sale", json={"client_uuid": "csv-1", "cash_received_cents": 2000,
                                       "items": [{"sku_id": sku, "quantity": 2}]})
        page = c.get("/sales?period=today")
        self.assertEqual(page.status_code, 200)
        self.assertIn('<details class="sale">', page.text)
        self.assertIn("2 × Clover honey", page.text)
        self.assertIn("Change given", page.text)  # cash received $20 vs $18 total
        self.assertIn("Today", page.text)

        for url in ("/sales?period=yesterday", "/sales?period=month", "/sales?period=week&limit=50"):
            self.assertEqual(c.get(url).status_code, 200, url)

        # Custom range, including a reversed one and a garbage one (falls back, no crash)
        today = datetime.utcnow().date()
        a, b = (today - timedelta(days=45)).isoformat(), today.isoformat()
        long = c.get(f"/sales?period=custom&from={a}&to={b}")
        self.assertEqual(long.status_code, 200)
        self.assertIn("Sales by week", long.text)  # >31 days groups by week
        self.assertEqual(c.get(f"/sales?period=custom&from={b}&to={a}").status_code, 200)
        self.assertEqual(c.get("/sales?period=custom&from=nope&to=").status_code, 200)

        # CSV: one row per line item, same range semantics, formula-safe
        resp = c.get(f"/sales/export.csv?period=custom&from={a}&to={b}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp.headers["content-type"])
        self.assertIn("ironledger-sales-", resp.headers["content-disposition"])
        rows = list(csv.DictReader(io.StringIO(resp.text)))
        self.assertTrue(rows)
        mine = [r for r in rows if r["item"] == "Clover honey · 250 g jar" and r["quantity"] == "2"]
        self.assertEqual(len(mine), 1)
        self.assertEqual((mine[0]["line_subtotal"], mine[0]["sale_total"]), ("18.00", "18.00"))
        self.assertEqual((mine[0]["source"], mine[0]["payment_type"]), ("cash", "cash"))
        only_cash = c.get("/sales/export.csv?period=week&source=cash").text
        self.assertNotIn(",zettle,", only_cash)

        # Staff cannot export
        marty = self.client("marty")
        self.assertEqual(marty.get("/sales/export.csv").status_code, 303)

    def test_branding_and_inventory_cards(self):
        c = self.client("zach")
        inv = c.get("/inventory").text
        self.assertIn("iron ledger", inv)
        self.assertNotIn("hive", inv.lower())
        self.assertIn("desktop-table", inv)
        self.assertIn("mobile-cards", inv)
        self.assertIn("inv-card", inv)
        self.assertIn("Needs attention", inv)

        # Zero-stock item with empty sku_code should show Out of stock, no stray leading dot,
        # and sort ahead of In stock items.
        conn = get_connection()
        conn.execute(
            "INSERT INTO skus (merchant_id, zettle_product_uuid, zettle_variant_uuid,"
            " name, variant_name, sku_code, category, price_cents, updated_at)"
            " VALUES (?, 'z-empty', 'z-empty-v', 'Beeswax Candle', '6in', '', 'Candles', 500, '2026-11-14')",
            (self.mid,),
        )
        conn.commit()
        conn.close()
        inv2 = c.get("/inventory").text
        self.assertIn("Out of stock", inv2)
        self.assertIn("<small>6in</small>", inv2)
        self.assertNotIn("· 6in", inv2.split("6in · Candles")[0])
        self.assertLess(inv2.index("Beeswax Candle"), inv2.index("Wildflower honey"))

    def test_stock_history_endpoint_and_pwa_assets(self):
        c = self.client("zach")
        conn = get_connection()
        sku_id = conn.execute("SELECT id FROM skus LIMIT 1").fetchone()[0]
        conn.close()
        r_set = c.post("/inventory/stock", data={"sku_id": str(sku_id), "mode": "set", "quantity": "12", "note": "Shelf count"}, follow_redirects=False)
        self.assertEqual(r_set.status_code, 303)
        hist = c.get(f"/api/stock/{sku_id}/history")
        self.assertEqual(hist.status_code, 200)
        entries = hist.json()["entries"]
        self.assertTrue(any(e["note"] == "Shelf count" and "local_time" in e for e in entries))

        qs = c.get("/quick-sale").text
        self.assertIn('"on_hand":', qs)

        self.assertEqual(c.get("/manifest.webmanifest").status_code, 200)
        self.assertEqual(c.get("/static/icon-192.png").status_code, 200)
        self.assertEqual(c.get("/static/icon-512.png").status_code, 200)

        staff = self.client("marty")
        r_staff = staff.get(f"/api/stock/{sku_id}/history", follow_redirects=False)
        self.assertEqual(r_staff.status_code, 403)

    def test_hide_sku_from_quick_sale(self):
        c = self.client("zach")
        conn = get_connection()
        row = conn.execute("SELECT id, name FROM skus LIMIT 1").fetchone()
        conn.close()
        sku_id, sku_name = row["id"], row["name"]

        self.assertIn(f'"id": {sku_id},', c.get("/quick-sale").text)
        r_hide = c.post(
            "/inventory/stock",
            data={"sku_id": str(sku_id), "mode": "set", "quantity": "5", "quick_sale_present": "1"},
            follow_redirects=False,
        )
        self.assertEqual(r_hide.status_code, 303)
        self.assertNotIn(f'"id": {sku_id},', c.get("/quick-sale").text)
        self.assertIn("Hidden from Quick Sale", c.get("/inventory").text)

        c.post(
            "/inventory/stock",
            data={"sku_id": str(sku_id), "mode": "set", "quantity": "5", "quick_sale_present": "1", "show_in_quick_sale": "1"},
            follow_redirects=False,
        )
        self.assertIn(f'"id": {sku_id},', c.get("/quick-sale").text)

    def test_onboarding_tour_assets_and_button(self):
        c = self.client("zach")
        inv = c.get("/inventory").text
        self.assertIn("startIronLedgerTour", inv)
        self.assertIn("tour.js", inv)
        self.assertEqual(c.get("/static/tour.js").status_code, 200)

        staff = self.client("marty")
        self.assertNotIn("? Tour", staff.get("/quick-sale").text)

    def test_sell_by_lb_whole_bird_and_bulk(self):
        c = self.client("zach")
        conn = get_connection()
        conn.execute(
            "INSERT INTO skus (merchant_id, zettle_product_uuid, zettle_variant_uuid,"
            " name, variant_name, sku_code, category, price_cents, updated_at)"
            " VALUES (?, 'z-chk', 'z-chk-v', 'Whole Chicken', '', 'CHK', 'Poultry', 550, '2026-11-16')",
            (self.mid,),
        )
        chk_id = conn.execute("SELECT id FROM skus WHERE sku_code='CHK'").fetchone()[0]
        conn.execute(
            "INSERT INTO skus (merchant_id, zettle_product_uuid, zettle_variant_uuid,"
            " name, variant_name, sku_code, category, price_cents, updated_at)"
            " VALUES (?, 'z-beef', 'z-beef-v', 'Ground Beef', '', 'BEF', 'Beef', 800, '2026-11-16')",
            (self.mid,),
        )
        beef_id = conn.execute("SELECT id FROM skus WHERE sku_code='BEF'").fetchone()[0]
        conn.commit()
        conn.close()

        c.post("/inventory/stock", data={
            "sku_id": str(chk_id), "mode": "set", "quantity": "25",
            "low_stock_threshold": "5", "sell_mode": "lb_stock_unit",
            "quick_sale_present": "1", "show_in_quick_sale": "1",
        }, follow_redirects=False)
        c.post("/inventory/stock", data={
            "sku_id": str(beef_id), "mode": "set", "quantity": "10.5",
            "low_stock_threshold": "3", "sell_mode": "lb_stock_lb",
            "quick_sale_present": "1", "show_in_quick_sale": "1",
        }, follow_redirects=False)

        inv = c.get("/inventory").text
        self.assertIn("/lb", inv)
        self.assertIn("10.5", inv)
        qs = c.get("/quick-sale").text
        self.assertIn("weightm", qs)
        self.assertIn('"sell_mode": "lb_stock_unit"', qs)

        r = c.post("/api/cash-sale", json={
            "client_uuid": "lb-sale-1",
            "items": [
                {"sku_id": chk_id, "quantity": 4.85},
                {"sku_id": chk_id, "quantity": 5.15},
                {"sku_id": beef_id, "quantity": 2.25},
            ],
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["total_cents"], round(550 * 4.85) + round(550 * 5.15) + round(800 * 2.25))

        from inventory.stock import list_stock
        by_id = {s["id"]: s for s in list_stock(self.mid)}
        self.assertEqual(by_id[chk_id]["on_hand"], 23)
        self.assertEqual(by_id[beef_id]["on_hand"], 8.25)

        csv_text = c.get("/sales/export.csv?period=all").text
        self.assertIn(",4.85,", csv_text)
        self.assertIn(",2.25,", csv_text)

    def test_stripe_webhook_2025_plus_payload(self):
        from inventory.billing import get_subscription, process_webhook_event, upsert_subscription

        upsert_subscription(
            self.mid, status="trialing", plan_key="monthly",
            stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
        )
        res_inv = process_webhook_event({
            "type": "invoice.paid",
            "data": {"object": {
                "customer": None,
                "parent": {"subscription_details": {"subscription": "sub_1"}},
                "lines": {"data": [{"period": {"end": 1800000000}}]},
            }},
        })
        self.assertEqual(res_inv, {"handled": True, "action": "activated"})

        res_sub = process_webhook_event({
            "type": "customer.subscription.updated",
            "data": {"object": {
                "id": "sub_1",
                "customer": "cus_1",
                "status": "active",
                "items": {"data": [{"current_period_end": 1800000000}]},
            }},
        })
        self.assertEqual(res_sub, {"handled": True, "action": "updated_active"})
        self.assertTrue(get_subscription(self.mid)["current_period_end"])

        # Login page shows /signup link only when Stripe billing is configured
        from fastapi.testclient import TestClient
        from inventory.app import app
        anon = TestClient(app)
        self.assertNotIn('href="/signup"', anon.get("/login").text)
        os.environ["STRIPE_SECRET_KEY"] = "sk_test_123"
        os.environ["STRIPE_PRICE_MONTHLY"] = "price_123"
        try:
            self.assertIn('href="/signup"', anon.get("/login").text)
        finally:
            os.environ.pop("STRIPE_SECRET_KEY", None)
            os.environ.pop("STRIPE_PRICE_MONTHLY", None)

    def test_contact_api_honeypot_and_resend(self):
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from inventory.app import app

        anon = TestClient(app)
        # Honeypot silently succeeds without calling Resend
        with patch("inventory.app._send_resend_email") as mock_send:
            r_bot = anon.post("/api/contact", json={
                "name": "Bot", "email": "bot@example.com", "message": "spam", "website": "http://spam",
            })
            self.assertEqual(r_bot.status_code, 200)
            mock_send.assert_not_called()

        # Validation rejects missing fields
        r_bad = anon.post("/api/contact", json={"name": "", "email": "bad", "message": ""})
        self.assertEqual(r_bad.status_code, 400)

        # Valid submission calls Resend helper
        with patch("inventory.app._send_resend_email") as mock_send:
            r_ok = anon.post("/api/contact", json={
                "name": "Alice", "email": "alice@example.com",
                "engagement": "C2C / B2B Contracting", "message": "Need backend help.",
            })
            self.assertEqual(r_ok.status_code, 200)
            mock_send.assert_called_once()

    def test_fees_payment_split_insights_quicksale_and_settings(self):
        from datetime import datetime, timezone
        from inventory.db import get_connection
        from inventory.stock import set_stock_count

        c = self.client("zach")
        # Create a regular product (stock=1 -> will sell out) and a fee item ("Card charge")
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        conn = get_connection()
        try:
            honey_id = int(conn.execute(
                "INSERT INTO skus (merchant_id, zettle_product_uuid, zettle_variant_uuid, name, category, price_cents, updated_at)"
                " VALUES (1, 'prod-whiskey-honey', '', 'Whiskey Honey', 'Honey', 1500, ?)",
                (now_iso,),
            ).lastrowid)
            fee_id = int(conn.execute(
                "INSERT INTO skus (merchant_id, zettle_product_uuid, zettle_variant_uuid, name, category, price_cents, updated_at)"
                " VALUES (1, 'prod-card-charge', '', 'Card charge', 'Fees', 75, ?)",
                (now_iso,),
            ).lastrowid)
            conn.commit()
        finally:
            conn.close()
        set_stock_count(1, honey_id, 1)

        # Mark "Card charge" as fee/non-inventory in Settings
        r_type = c.post("/settings/item-type", data={"sku_id": str(fee_id), "item_type": "fee"}, follow_redirects=False)
        self.assertEqual(r_type.status_code, 303)
        inv_html = c.get("/inventory").text
        self.assertIn("Whiskey Honey", inv_html)
        self.assertNotIn("Card charge", inv_html)

        # Record a Zettle card sale with 1 x Whiskey Honey + 1 x Card charge, and a Zettle cash sale
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        from inventory.sync import import_purchase
        conn = get_connection()
        try:
            honey_uuid = conn.execute("SELECT zettle_product_uuid FROM skus WHERE id = ?", (honey_id,)).fetchone()[0]
            fee_uuid = conn.execute("SELECT zettle_product_uuid FROM skus WHERE id = ?", (fee_id,)).fetchone()[0]
            import_purchase(conn, 1, {
                "purchaseUUID": "p-card-1",
                "timestamp": now_iso + "+00:00",
                "amount": 1575,
                "payments": [{"type": "IZETTLE_CARD"}],
                "products": [
                    {"productUuid": honey_uuid, "variantUuid": "", "name": "Whiskey Honey", "quantity": "1", "unitPrice": 1500},
                    {"productUuid": fee_uuid, "variantUuid": "", "name": "Card charge", "quantity": "1", "unitPrice": 75},
                ],
            }, "2020-01-01T00:00:00")
            import_purchase(conn, 1, {
                "purchaseUUID": "p-cash-1",
                "timestamp": now_iso + "+00:00",
                "amount": 1500,
                "payments": [{"type": "CASH"}],
                "products": [
                    {"productUuid": honey_uuid, "variantUuid": "", "name": "Whiskey Honey", "quantity": "1", "unitPrice": 1500},
                ],
            }, "2020-01-01T00:00:00")
            conn.commit()
        finally:
            conn.close()

        # Sales page: Card vs Cash breakdown + transaction header shows "1 unit" (not 2) + Market day filter
        sales_html = c.get("/sales?period=today").text
        self.assertIn("PAYMENT BREAKDOWN", sales_html)
        self.assertIn("1 unit", sales_html)
        self.assertIn("Card charge", sales_html)
        self.assertIn("Market day", sales_html)
        today_str = datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(c.get(f"/sales?period=day&date={today_str}").status_code, 200)

        # Cash filter includes Zettle cash transactions (and excludes card); Card filter shows only card
        cash_only_html = c.get("/sales?period=today&source=cash").text
        self.assertIn("$15.00", cash_only_html)
        self.assertNotIn("Card charge", cash_only_html)
        card_only_html = c.get("/sales?period=today&source=card").text
        self.assertIn("$15.75", card_only_html)
        self.assertIn("Card charge", card_only_html)

        # Quick sale usability: sticky cart bar, hide out of stock, fast cash buttons
        qs_html = c.get("/quick-sale").text
        self.assertIn("sticky-cart", qs_html)
        self.assertIn("Hide out of stock", qs_html)
        self.assertIn("Exact", qs_html)

        # Insights page: Top products, Card fees collected, Early sell-out flags (Whiskey Honey <= 0 left)
        set_stock_count(1, honey_id, 0)
        ins_html = c.get("/insights?period=today").text
        self.assertIn("Card fees collected", ins_html)
        self.assertIn("$0.75", ins_html)
        self.assertIn("Early sell-out flags", ins_html)
        self.assertIn("Whiskey Honey", ins_html)

        # Settings polish: redirect_uri uses APP_BASE_URL / ironcladsystems.dev fallback + Non-Inventory section
        os.environ["APP_BASE_URL"] = "https://ledger.ironcladsystems.dev"
        try:
            set_html = c.get("/settings").text
            self.assertIn("https://ledger.ironcladsystems.dev/zettle/callback", set_html)
            self.assertIn("Non-Inventory &amp; Fee Items", set_html)
        finally:
            os.environ.pop("APP_BASE_URL", None)

    def test_marketplaces_and_market_days(self):
        from datetime import datetime
        from inventory.db import get_connection
        from inventory.markets import list_marketplaces

        c = self.client("zach")
        # 1. Create two marketplaces in Settings
        r1 = c.post("/settings/marketplaces", data={
            "name": "Sussex Farmers Market",
            "location": "Augusta, NJ",
            "booth_fee": "45.00",
            "travel_cost": "20.00",
        }, follow_redirects=False)
        self.assertEqual(r1.status_code, 303)
        c.post("/settings/marketplaces", data={
            "name": "Montclair Market",
            "location": "Montclair, NJ",
            "booth_fee": "60.00",
            "travel_cost": "15.00",
        }, follow_redirects=False)

        mps = list_marketplaces(1)
        self.assertEqual(len(mps), 2)
        sussex = next(m for m in mps if m["name"] == "Sussex Farmers Market")
        montclair = next(m for m in mps if m["name"] == "Montclair Market")

        # 2. Record a cash sale today -> should appear as an untagged sale day on Insights
        conn = get_connection()
        try:
            sku_id = conn.execute("SELECT id FROM skus WHERE merchant_id = 1 LIMIT 1").fetchone()["id"]
        finally:
            conn.close()
        c.post("/api/cash-sale", json={
            "client_uuid": "mp-sale-1",
            "items": [{"sku_id": sku_id, "quantity": 5}],
        })
        today_str = datetime.now().strftime("%Y-%m-%d")
        ins_before = c.get("/insights?period=all").text
        self.assertIn("Untagged sale days", ins_before)
        self.assertIn(today_str, ins_before)

        # 3. Tag today as Sussex Farmers Market
        r_tag = c.post("/market-days/save", data={
            "market_date": today_str,
            "marketplace_id": str(sussex["id"]),
            "booth_fee": "45.00",
            "travel_cost": "20.00",
            "weather": "Sunny",
            "notes": "Great turnout",
            "next_url": "/insights?period=quarter",
        }, follow_redirects=False)
        self.assertEqual(r_tag.status_code, 303)

        ins_after = c.get("/insights?period=quarter").text
        self.assertIn("Sussex Farmers Market", ins_after)
        self.assertIn("$65.00", ins_after)
        self.assertIn("Sunny", ins_after)
        self.assertIn("Great turnout", ins_after)
        self.assertIn("Recommended cash drawer", ins_after)
        self.assertIn("$1 bills", ins_after)
        self.assertIn("30% morning-rush buffer", ins_after)
        self.assertIn("Suggested pack list", ins_after)
        self.assertIn("Copy pack list", ins_after)

        ins_montclair = c.get(f"/insights?period=quarter&marketplace={montclair['id']}").text
        self.assertIn("Starter float estimate", ins_montclair)

        # 4. Marketplace filter on Sales & Insights scopes to assigned days
        sales_sussex = c.get(f"/sales?period=today&marketplace={sussex['id']}").text
        self.assertNotIn("0 transactions", sales_sussex)
        self.assertIn("$30.00", sales_sussex)
        sales_montclair = c.get(f"/sales?period=today&marketplace={montclair['id']}").text
        self.assertIn("0 transactions", sales_montclair)

        # 5. Archive Montclair marketplace
        r_arch = c.post(f"/settings/marketplaces/{montclair['id']}/archive", follow_redirects=False)
        self.assertEqual(r_arch.status_code, 303)
        self.assertEqual(len(list_marketplaces(1)), 1)

    def test_three_way_payment_categories_venmo_paypal_and_retag(self):
        from datetime import datetime, timezone
        from inventory.db import get_connection
        from inventory.sync import import_purchase

        c = self.client("zach")
        conn = get_connection()
        try:
            sku_row = conn.execute("SELECT id, zettle_product_uuid, name FROM skus WHERE merchant_id = 1 LIMIT 1").fetchone()
            sku_id, prod_uuid, prod_name = sku_row["id"], sku_row["zettle_product_uuid"], sku_row["name"]
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            # 1. Zettle PayPal QR sale ($12.00) + Zettle Cash sale ($18.00)
            import_purchase(conn, 1, {
                "purchaseUUID": "p-paypal-1",
                "timestamp": now_iso + "+00:00",
                "amount": 1200,
                "payments": [{"type": "PAYPAL_QRC"}],
                "products": [{"productUuid": prod_uuid, "variantUuid": "", "name": prod_name, "quantity": "1", "unitPrice": 1200}],
            }, "2020-01-01T00:00:00")
            import_purchase(conn, 1, {
                "purchaseUUID": "p-zcash-retag",
                "timestamp": now_iso + "+00:00",
                "amount": 1800,
                "payments": [{"type": "IZETTLE_CASH"}],
                "products": [{"productUuid": prod_uuid, "variantUuid": "", "name": prod_name, "quantity": "1", "unitPrice": 1800}],
            }, "2020-01-01T00:00:00")
            conn.commit()
            zcash_sale_id = conn.execute(
                "SELECT id FROM sales WHERE external_id = 'p-zcash-retag'"
            ).fetchone()["id"]
        finally:
            conn.close()

        # 2. Quick sale Venmo transaction
        r_v = c.post("/api/cash-sale", json={
            "client_uuid": "qs-venmo-1",
            "payment_type": "venmo",
            "items": [{"sku_id": sku_id, "quantity": 1}],
        })
        self.assertEqual(r_v.status_code, 200)

        # 3. Re-tag the Zettle Cash sale ($18.00) to Venmo via Sales page
        r_retag = c.post(
            f"/sales/{zcash_sale_id}/payment-type",
            data={"payment_type": "venmo", "next_url": "/sales?period=today"},
            follow_redirects=False,
        )
        self.assertEqual(r_retag.status_code, 303)

        # Verify Sales breakdown shows Digital cash, Venmo, PayPal, and filters work
        sales_html = c.get("/sales?period=today").text
        self.assertIn("Digital cash", sales_html)
        self.assertIn("Physical cash", sales_html)
        self.assertIn("PayPal ($12.00)", sales_html)

        dig_list = c.get("/sales?period=today&source=digital").text.split('id="sales-list"', 1)[1]
        self.assertIn("$12.00", dig_list)
        self.assertIn("$18.00", dig_list)
        self.assertIn("$6.00", dig_list)

        pp_list = c.get("/sales?period=today&source=paypal").text.split('id="sales-list"', 1)[1]
        self.assertIn("$12.00", pp_list)
        self.assertNotIn("$18.00", pp_list)

        venmo_list = c.get("/sales?period=today&source=venmo").text.split('id="sales-list"', 1)[1]
        self.assertIn("$18.00", venmo_list)
        self.assertIn("$6.00", venmo_list)
        self.assertNotIn("$12.00", venmo_list)
        self.assertNotIn("pay-badge paypal", venmo_list)

        # Physical cash filter excludes both PayPal ($12) and re-tagged Venmo ($18)
        phys_list = c.get("/sales?period=today&source=cash").text.split('id="sales-list"', 1)[1]
        self.assertNotIn("$12.00", phys_list)
        self.assertNotIn("$18.00", phys_list)


if __name__ == "__main__":
    unittest.main()
