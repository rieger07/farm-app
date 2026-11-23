import json
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from app import app
from homestead.database import init_db


class FarmWebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FARM_DB_PATH"] = os.path.join(self.tmp.name, "farm.db")
        os.environ["FARM_COOKIE_SECURE"] = "0"
        init_db()
        self.c = TestClient(app)

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_farm_workflow(self):
        self.assertEqual(self.c.get("/healthz").json(), {"ok": True})

        # Unauthenticated public homepage, privacy policy & terms for Google OAuth verification
        home_resp = self.c.get("/", follow_redirects=False)
        self.assertEqual(home_resp.status_code, 200)
        self.assertIn("Ironclad Systems LLC", home_resp.text)
        self.assertIn("drive.file", home_resp.text)
        self.assertIn("/privacy", home_resp.text)
        self.assertEqual(self.c.get("/privacy", follow_redirects=False).status_code, 200)
        self.assertEqual(self.c.get("/terms", follow_redirects=False).status_code, 200)

        # First-time setup: register admin user
        login_html = self.c.get("/login").text
        self.assertIn("Create admin", login_html)
        r = self.c.post("/register", data={
            "display_name": "Steve",
            "username": "steve",
            "password": "secret",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)

        # Create a batch
        from datetime import date, timedelta
        r_b = self.c.post("/batches/save", data={
            "name": "Spring Broilers",
            "species": "cornish cross",
            "start_date": (date.today() - timedelta(days=20)).isoformat(),
            "initial_quantity": "30",
            "unit_cost": "3.50",
            "avg_dressed_weight": "4.5",
            "sale_price_per_lb": "5.50",
            "processing_cost": "5.00",
            "lifecycle_type": "live_arrival",
            "next_url": "/",
        }, follow_redirects=False)
        self.assertEqual(r_b.status_code, 303)

        radar = self.c.get("/").text
        self.assertIn("Spring Broilers", radar)
        self.assertIn("Batch lifecycle timeline", radar)
        self.assertIn("Hatch Window", radar)

        # Check off a milestone and verify it disappears from Radar
        r_ack = self.c.post("/milestones/ack", data={"batch_id": "1", "task": "Hatch Window"}, follow_redirects=False)
        self.assertEqual(r_ack.status_code, 303)
        self.assertNotIn('value="Hatch Window"', self.c.get("/").text)

        # Grab batch id from /batches
        batches_html = self.c.get("/batches").text
        self.assertIn("Spring Broilers", batches_html)
        self.assertIn("openDuplicateBatchModal", batches_html)

        # Log loss, harvest, sale, feed on batch 1
        self.c.post("/batches/log-loss", data={"batch_id": "1", "loss_date": "2026-11-05", "count": "2", "reason": "Runt", "next_url": "/batches/1"}, follow_redirects=False)
        self.c.post("/batches/log-harvest", data={"batch_id": "1", "harvest_date": "2026-11-16", "head_count": "10", "dressed_lbs": "46.5", "processing_cost_total": "50.0", "next_url": "/batches/1"}, follow_redirects=False)
        self.c.post("/batches/log-sale", data={"batch_id": "1", "entry_date": "2026-11-17", "allocation_type": "sale", "weight_lbs": "20", "revenue": "110", "next_url": "/batches/1"}, follow_redirects=False)
        self.c.post("/batches/log-feed", data={
            "batch_id": "1", "feed_date": "2026-11-17", "feed_type": "grower",
            "bags": "4", "feed_lbs": "200", "cost_total": "88", "notes": "Tractor Supply",
            "next_url": "/batches/1",
        }, follow_redirects=False)

        detail = self.c.get("/batches/1").text
        self.assertIn("28 / 30", detail)
        self.assertIn("46.5 lb", detail)
        self.assertIn("110.00", detail)
        self.assertIn("200.0 lb ($88.00)", detail)

        # Feed, Financials, ICS, Backup, Settings
        self.c.post("/feed/settings", data={"barrel_capacity": "300", "bag_weight": "50"}, follow_redirects=False)
        feed = self.c.get("/feed").text
        self.assertIn("Purchasing schedule", feed)

        fin = self.c.get("/financials").text
        self.assertIn("Spring Broilers", fin)

        ics = self.c.get("/calendar.ics")
        self.assertEqual(ics.status_code, 200)
        self.assertIn("BEGIN:VCALENDAR", ics.text)

        bak = self.c.get("/backup.json")
        self.assertEqual(bak.status_code, 200)
        self.assertTrue(json.loads(bak.text)["batches"])

        self.c.post("/settings/species", data={
            "species": "berkshire pigs",
            "lifecycle_type": "mammal_breeding",
            "days_to_birth": "114",
            "starter_weeks": "6",
            "grower_weeks": "20",
            "starter_feed_lbs": "45",
            "grower_feed_lbs": "520",
            "expected_yield_lbs": "165",
        }, follow_redirects=False)
        settings = self.c.get("/settings").text
        self.assertIn("Berkshire Pigs", settings)
        self.assertIn("Breeding", settings)
        self.assertIn("26w", settings)
        self.assertIn("165.0 lb", settings)
        self.assertIn("Google Drive Receipts", settings)

        # General farm expense + receipt URL + Schedule F CSV export
        r_exp = self.c.post("/expenses/add", data={
            "expense_date": "2026-11-18",
            "vendor": "Tractor Supply",
            "category": "Supplies & Equipment",
            "amount": "48.50",
            "notes": "Pine shavings",
            "receipt_url": "https://drive.google.com/file/d/abc123/view",
        }, follow_redirects=False)
        self.assertEqual(r_exp.status_code, 303)
        fin_html = self.c.get("/financials").text
        self.assertIn("Tractor Supply", fin_html)
        self.assertIn("https://drive.google.com/file/d/abc123/view", fin_html)

        csv_resp = self.c.get("/financials/expenses.csv")
        self.assertEqual(csv_resp.status_code, 200)
        self.assertIn("Tractor Supply", csv_resp.text)
        self.assertIn("48.50", csv_resp.text)
        self.assertIn("https://drive.google.com/file/d/abc123/view", csv_resp.text)

        # Multipart receipt upload -> mocked Google Drive uploader
        from unittest.mock import patch
        with patch("app.upload_receipt_to_drive", return_value=("drv999", "https://drive.google.com/file/d/drv999/view")) as mock_up:
            r_up = self.c.post(
                "/expenses/add",
                data={
                    "expense_date": "2026-11-18",
                    "vendor": "Runnings",
                    "category": "Feed",
                    "amount": "32.00",
                },
                files={"receipt_file": ("receipt.jpg", b"fake-jpeg-bytes", "image/jpeg")},
                follow_redirects=False,
            )
            self.assertEqual(r_up.status_code, 303)
            self.assertTrue(mock_up.called)
        self.assertIn("https://drive.google.com/file/d/drv999/view", self.c.get("/financials").text)

        # Harvest Tally page + live bird-by-bird API + Undo + CSV export
        from homestead.database import list_batches
        bid = list_batches()[0]["id"]
        ht_html = self.c.get(f"/batches/{bid}/harvest").text
        self.assertIn("Harvest Tally", ht_html)
        self.assertIn("Decimal lb", ht_html)
        self.assertIn("lb + oz", ht_html)

        r_add = self.c.post(f"/api/batches/{bid}/harvest", json={
            "harvest_date": "2026-11-22",
            "head_count": 1,
            "dressed_lbs": 19.75,
            "notes": "Big tom",
        })
        self.assertEqual(r_add.status_code, 200)
        t_data = r_add.json()
        self.assertGreaterEqual(t_data["harvested_head"], 1)
        self.assertEqual(t_data["entries"][0]["lb_oz"], "19 lb 12 oz")

        csv_h = self.c.get(f"/batches/{bid}/harvest.csv")
        self.assertEqual(csv_h.status_code, 200)
        self.assertIn("19.75", csv_h.text)
        self.assertIn("19 lb 12 oz", csv_h.text)

        # Undo (delete) that bird
        hid = t_data["entries"][0]["id"]
        r_del = self.c.delete(f"/api/batches/{bid}/harvest/{hid}")
        self.assertEqual(r_del.status_code, 200)
        self.assertEqual(r_del.json()["harvested_head"], t_data["harvested_head"] - 1)

        # Future batch -> Planned status, separated on Radar, toggleable on Financials, Start now works
        self.c.post("/batches/save", data={
            "name": "Thanksgiving 2028",
            "species": "turkey chicks",
            "start_date": "2028-06-01",
            "initial_quantity": "25",
            "unit_cost": "10.00",
            "avg_dressed_weight": "18",
            "sale_price_per_lb": "5.00",
            "processing_cost": "0",
        }, follow_redirects=False)
        radar_html = self.c.get("/").text
        self.assertIn("Planned batches", radar_html)
        self.assertIn("Starts 2028-06-01", radar_html)
        self.assertIn("+1 planned", radar_html)

        fin_all = self.c.get("/financials?include_planned=1").text
        self.assertIn("Thanksgiving 2028", fin_all)
        fin_active = self.c.get("/financials?include_planned=0").text
        self.assertNotIn("Thanksgiving 2028", fin_active)

        planned_bid = next(b["id"] for b in list_batches() if b["name"] == "Thanksgiving 2028")
        r_start = self.c.post(f"/batches/{planned_bid}/start-now", data={"next_url": "/"}, follow_redirects=False)
        self.assertEqual(r_start.status_code, 303)
        self.assertNotIn("Planned batches", self.c.get("/").text)

    def test_customers_bird_assignment_and_public_picking(self):
        from homestead.database import list_batches

        # Register admin & create a turkey batch at $5.00/lb
        self.c.post("/register", data={"display_name": "Steve", "username": "steve", "password": "secret"})
        self.c.post("/batches/save", data={
            "name": "Thanksgiving Turkeys",
            "species": "turkey chicks",
            "start_date": "2026-06-01",
            "initial_quantity": "10",
            "unit_cost": "10.00",
            "avg_dressed_weight": "18",
            "sale_price_per_lb": "5.00",
            "processing_cost": "0",
        }, follow_redirects=False)
        batch = list_batches()[0]
        bid = batch["id"]
        slug = batch["picking_slug"]
        self.assertTrue(slug)

        # Customer directory CRUD
        r_cust = self.c.post("/customers/save", data={
            "name": "Sarah Miller", "phone": "555-123-4567", "email": "sarah@example.com",
            "preferred_contact": "sms", "notes": "Prefers 18-20 lb",
        }, follow_redirects=False)
        self.assertEqual(r_cust.status_code, 303)
        self.assertIn("Sarah Miller", self.c.get("/customers?q=sarah").text)

        # Inline quick-create customer API
        c2 = self.c.post("/api/customers", json={"name": "Bob Jones", "phone": "555-999-8888"}).json()
        self.assertEqual(c2["name"], "Bob Jones")

        # Weigh 3 birds: 2 for_sale (20 lb, 16 lb) and 1 not_for_sale (18 lb)
        b1 = self.c.post(f"/api/batches/{bid}/harvest", json={"dressed_lbs": 20.0, "for_sale": True}).json()
        b2 = self.c.post(f"/api/batches/{bid}/harvest", json={"dressed_lbs": 16.0, "for_sale": True}).json()
        b3 = self.c.post(f"/api/batches/{bid}/harvest", json={"dressed_lbs": 18.0, "for_sale": False}).json()

        # KPI check: Harvested & Total lbs include all 3 (54.0 lb), but Est. value & Available exclude 18 lb NFS bird ($180, 2 avail)
        self.assertEqual(b3["harvested_head"], 3)
        self.assertAlmostEqual(b3["total_lbs"], 54.0)
        self.assertEqual(b3["available_count"], 2)
        self.assertEqual(b3["not_for_sale_count"], 1)
        self.assertAlmostEqual(b3["est_value"], 180.0)

        bird1_id = b1["entries"][0]["id"]
        bird2_id = b2["entries"][0]["id"]

        # Assign Bird #1 to Bob Jones with $115 actual sale price ($100 calc + $15 tip) & $25 deposit -> balance due = $90
        assigned = self.c.post(f"/api/batches/{bid}/harvest/{bird1_id}/assign", json={
            "for_sale": True,
            "customer_id": c2["id"],
            "actual_sale_price": 115.0,
            "deposit_paid": 25.0,
            "payment_status": "paid",
            "pickup_status": "picked_up",
        }).json()
        a_entry = next(e for e in assigned["entries"] if e["id"] == bird1_id)
        self.assertEqual(a_entry["customer_name"], "Bob Jones")
        self.assertAlmostEqual(a_entry["actual_sale_price"], 115.0)
        self.assertAlmostEqual(a_entry["tip_diff"], 15.0)
        self.assertAlmostEqual(a_entry["balance_due"], 90.0)
        self.assertEqual(assigned["available_count"], 1)

        # Auto-applied batch economics: Bird #1 (paid, 20 lb, $115) -> sold_lbs & actual_sales_revenue; Bird #3 (not_for_sale, 18 lb) -> freezer_lbs!
        from homestead.scheduling import compute_batch_schedule
        sched = compute_batch_schedule(list_batches()[0])
        self.assertAlmostEqual(sched["sold_lbs"], 20.0)
        self.assertAlmostEqual(sched["actual_sales_revenue"], 115.0)
        self.assertAlmostEqual(sched["freezer_lbs"], 18.0)

        # Customer Insights page shows species breakdown, Paid vs Calc, and +$15.00 tip
        c_detail = self.c.get(f"/customers?selected={c2['id']}").text
        self.assertIn("Thanksgiving Turkeys", c_detail)
        self.assertIn("20.00 lb", c_detail)
        self.assertIn("+$15.00", c_detail)
        self.assertIn("1 × Turkey Chicks", c_detail)

        # Configure ntfy push notifications in Settings
        r_ntfy = self.c.post("/settings/ntfy", data={
            "ntfy_url": "https://ntfy.sh",
            "ntfy_topic": "farm-test-topic",
            "ntfy_token": "tk_secret",
        }, follow_redirects=False)
        self.assertEqual(r_ntfy.status_code, 303)
        self.assertIn("farm-test-topic", self.c.get("/settings").text)

        # Public picking page: closed before enabled
        anon = TestClient(app)
        self.assertIn("Picking is currently closed", anon.get(f"/pick/{slug}").text)

        # Enable public picking with 1-bird-per-customer limit
        self.assertEqual(
            self.c.post(f"/api/batches/{bid}/picking", json={"picking_enabled": True, "max_birds_per_customer": 1}).status_code,
            200,
        )
        pick_html = anon.get(f"/pick/{slug}").text
        self.assertIn("Thanksgiving Turkeys", pick_html)
        self.assertIn("16.0", pick_html)       # for-sale bird visible
        self.assertNotIn("18.0", pick_html)    # not-for-sale bird completely hidden
        self.assertNotIn("Bob Jones", pick_html)  # buyer privacy preserved!

        # Identify-first flow matches existing customer Sarah even with +1 country code & different formatting (fires ntfy!)
        from unittest.mock import patch
        ident_sarah = anon.post(f"/api/pick/{slug}/identify", json={"name": "Sarah Miller", "contact": "+1 (555) 123-4567"}).json()
        self.assertEqual(ident_sarah["name"], "Sarah Miller")
        with patch("app.send_ntfy_async") as mock_ntfy:
            r_claim = anon.post(f"/api/pick/{slug}/claim/{bird2_id}", json={"customer_id": ident_sarah["customer_id"]})
            self.assertEqual(r_claim.status_code, 200)
            self.assertAlmostEqual(r_claim.json()["bird"]["est_price"], 80.0)
            mock_ntfy.assert_called_once()
            self.assertIn("Sarah Miller", mock_ntfy.call_args[0][2])

        # Double-booking protection: another customer trying to claim Bird #2 gets 409 Conflict
        ident_other = anon.post(f"/api/pick/{slug}/identify", json={"name": "Late Buyer", "contact": "late@example.com"}).json()
        r_dup = anon.post(f"/api/pick/{slug}/claim/{bird2_id}", json={"customer_id": ident_other["customer_id"]})
        self.assertEqual(r_dup.status_code, 409)
        self.assertIn("Someone just grabbed that bird", r_dup.json()["detail"])

        # Limit check: Sarah trying to claim a 2nd bird (even if another available bird is added) gets 409
        b4 = self.c.post(f"/api/batches/{bid}/harvest", json={"dressed_lbs": 17.5, "for_sale": True}).json()
        bird4_id = b4["entries"][0]["id"]
        r_lim = anon.post(f"/api/pick/{slug}/claim/{bird4_id}", json={"customer_id": ident_sarah["customer_id"]})
        self.assertEqual(r_lim.status_code, 409)
        self.assertIn("Limit of 1", r_lim.json()["detail"])

        # Merge duplicate customer ("Late Buyer") into "Bob Jones" -> backfills email & removes duplicate
        r_merge = self.c.post(
            f"/customers/{ident_other['customer_id']}/merge",
            data={"target_customer_id": str(c2["id"])},
            follow_redirects=False,
        )
        self.assertEqual(r_merge.status_code, 303)
        merged_html = self.c.get(f"/customers?selected={c2['id']}").text
        self.assertIn("late@example.com", merged_html)
        self.assertNotIn("Late Buyer", merged_html)

        # Verify Batch Detail & Batches List bring forward Harvest Tally fulfillment, sales, assigned, and home freezer
        bd_html = self.c.get(f"/batches/{bid}").text
        self.assertIn("Harvest Tally &amp; Fulfillment", bd_html)
        self.assertIn("Public picking live", bd_html)
        self.assertIn("Bob Jones", bd_html)
        self.assertIn("Sarah Miller", bd_html)
        self.assertIn("Home Freezer", bd_html)
        self.assertIn("18.00 lb", bd_html)
        self.assertIn("Total Sold / Freezer", bd_html)
        self.assertNotIn("No sales or freezer entries yet.", bd_html)

        blist_html = self.c.get("/batches").text
        self.assertIn("1 avail", blist_html)
        self.assertIn("1 asgn", blist_html)
        self.assertIn("1 picked", blist_html)
        self.assertIn("1 frz", blist_html)

    def test_preorders_waitlist_priority_picking_and_pickup_day(self):
        from unittest.mock import patch
        from homestead.database import list_batches
        from homestead.scheduling import compute_batch_schedule

        # Register admin & save Farm Pickup + Venmo/PayPal handles
        self.c.post("/register", data={"display_name": "Steve", "username": "steve", "password": "secret"}, follow_redirects=False)
        r_set = self.c.post("/settings/pickup-payments", data={
            "farm_name": "Cassberry Lane Farm",
            "pickup_address": "123 Farm Rd",
            "pickup_instructions": "Bring a cooler!",
            "venmo_handle": "https://venmo.com/u/@CassberryFarm/",
            "paypal_handle": "https://paypal.me/CassberryFarm/",
        }, follow_redirects=False)
        self.assertEqual(r_set.status_code, 303)
        set_html = self.c.get("/settings").text
        self.assertIn("venmo.com/CassberryFarm", set_html)
        self.assertIn("paypal.me/CassberryFarm", set_html)

        # Create a turkey batch ($7.50/lb)
        self.c.post("/batches/save", data={
            "name": "Fall Turkeys",
            "species": "turkey chicks",
            "start_date": "2026-07-01",
            "initial_quantity": "10",
            "unit_cost": "12.00",
            "avg_dressed_weight": "16.0",
            "sale_price_per_lb": "7.50",
            "processing_cost": "10.00",
            "lifecycle_type": "live_arrival",
            "next_url": "/batches",
        }, follow_redirects=False)
        batch = list_batches()[0]
        bid = batch["id"]
        slug = batch["picking_slug"]

        # Enable pre-orders (cap=2, $25/bird deposit)
        r_po_cfg = self.c.post(f"/batches/{bid}/preorders/settings", data={
            "preorder_enabled": "1",
            "preorder_cap": "2",
            "deposit_per_bird": "25.00",
            "pickup_date": "2026-11-24",
            "pickup_window": "Tue 3pm-6pm",
            "preorder_notes": "Pasture-raised heritage turkeys.",
        }, follow_redirects=False)
        self.assertEqual(r_po_cfg.status_code, 303)

        anon = TestClient(app)
        res_html = anon.get(f"/reserve/{slug}").text
        self.assertIn("2 spots left", res_html)
        self.assertIn("$25.00/bird", res_html)

        # Customer A reserves 2 birds -> reserved, $50 deposit, fires ntfy
        with patch("app.send_ntfy_async") as mock_ntfy:
            r_a = anon.post(f"/api/reserve/{slug}", json={
                "name": "Alice Green",
                "contact": "555-222-3333",
                "quantity": 2,
                "notes": "Two medium birds",
            })
            self.assertEqual(r_a.status_code, 200)
            da = r_a.json()
            self.assertEqual(da["status"], "reserved")
            self.assertAlmostEqual(da["deposit_total"], 50.0)
            self.assertEqual(da["venmo_handle"], "CassberryFarm")
            mock_ntfy.assert_called_once()

        # Cap is now full -> page switches to Waitlist Open & Customer B goes to waitlist
        res_full_html = anon.get(f"/reserve/{slug}").text
        self.assertIn("Waitlist Open", res_full_html)
        with patch("app.send_ntfy_async") as mock_ntfy:
            r_b = anon.post(f"/api/reserve/{slug}", json={
                "name": "Ben Waiter",
                "contact": "ben@example.com",
                "quantity": 1,
            })
            self.assertEqual(r_b.status_code, 200)
            db_res = r_b.json()
            self.assertEqual(db_res["status"], "waitlist")
            mock_ntfy.assert_called_once()

        # Farmer marks Alice's $50 deposit paid
        po_id_a = da["preorder_id"]
        po_id_b = db_res["preorder_id"]
        r_paid = self.c.post(f"/batches/{bid}/preorders/{po_id_a}/mark-paid", data={}, follow_redirects=False)
        self.assertEqual(r_paid.status_code, 303)
        po_page = self.c.get(f"/batches/{bid}/preorders").text
        self.assertIn("Paid $50.00", po_page)
        self.assertIn("Ben Waiter", po_page)

        # Weigh 3 birds and enable Pre-Order Priority Picking
        b1 = self.c.post(f"/api/batches/{bid}/harvest", json={"dressed_lbs": 16.0, "for_sale": True}).json()["entries"][0]["id"]
        b2 = self.c.post(f"/api/batches/{bid}/harvest", json={"dressed_lbs": 18.0, "for_sale": True}).json()["entries"][0]["id"]
        b3 = self.c.post(f"/api/batches/{bid}/harvest", json={"dressed_lbs": 15.0, "for_sale": True}).json()["entries"][0]["id"]

        self.assertEqual(
            self.c.post(f"/api/batches/{bid}/picking", json={
                "picking_enabled": True,
                "max_birds_per_customer": 1,
                "picking_preorder_only": True,
            }).status_code,
            200,
        )

        # Waitlisted Ben is blocked while picking_preorder_only is ON
        r_ben_id = anon.post(f"/api/pick/{slug}/identify", json={"name": "Ben Waiter", "contact": "ben@example.com"})
        self.assertEqual(r_ben_id.status_code, 403)

        # Reserved Alice can identify with effective_max_birds = 2 and unapplied deposit $50
        r_al_id = anon.post(f"/api/pick/{slug}/identify", json={"name": "Alice Green", "contact": "+1 (555) 222-3333"})
        self.assertEqual(r_al_id.status_code, 200)
        al_ident = r_al_id.json()
        self.assertEqual(al_ident["effective_max_birds"], 2)
        self.assertAlmostEqual(al_ident["unapplied_deposit"], 50.0)

        # Alice claims Bird #1 ($120) & Bird #2 ($135) -> $25 deposit auto-credited onto EACH bird!
        with patch("app.send_ntfy_async"):
            c1 = anon.post(f"/api/pick/{slug}/claim/{b1}", json={"customer_id": al_ident["customer_id"]})
            self.assertEqual(c1.status_code, 200)
            self.assertAlmostEqual(c1.json()["bird"]["deposit_paid"], 25.0)
            c2 = anon.post(f"/api/pick/{slug}/claim/{b2}", json={"customer_id": al_ident["customer_id"]})
            self.assertEqual(c2.status_code, 200)
            self.assertAlmostEqual(c2.json()["bird"]["deposit_paid"], 25.0)
            # 3rd bird blocked by her reserved qty of 2
            c3 = anon.post(f"/api/pick/{slug}/claim/{b3}", json={"customer_id": al_ident["customer_id"]})
            self.assertEqual(c3.status_code, 409)

        # Promote Ben from waitlist -> Ben can now claim Bird #3
        self.assertEqual(
            self.c.post(f"/batches/{bid}/preorders/{po_id_b}/promote", follow_redirects=False).status_code,
            303,
        )
        r_ben_ok = anon.post(f"/api/pick/{slug}/identify", json={"name": "Ben Waiter", "contact": "ben@example.com"})
        self.assertEqual(r_ben_ok.status_code, 200)
        with patch("app.send_ntfy_async"):
            self.assertEqual(
                anon.post(f"/api/pick/{slug}/claim/{b3}", json={"customer_id": r_ben_ok.json()["customer_id"]}).status_code,
                200,
            )

        # Pickup Day board groups Alice's 2 birds ($255 total - $50 deposit = $205 due)
        pu_html = self.c.get(f"/batches/{bid}/pickup").text
        self.assertIn("Alice Green", pu_html)
        self.assertIn("-$50.00", pu_html)
        self.assertIn("$205.00", pu_html)
        self.assertIn("Venmo QR", pu_html)
        self.assertIn("123 Farm Rd", pu_html)

        # Settle Alice with $220 collected today ($15 tip -> $270 total realized revenue!)
        r_settle = self.c.post(
            f"/batches/{bid}/pickup/{al_ident['customer_id']}/settle",
            data={"collected_today": "220.00"},
            follow_redirects=False,
        )
        self.assertEqual(r_settle.status_code, 303)
        sched = compute_batch_schedule(list_batches()[0])
        self.assertAlmostEqual(sched["actual_sales_revenue"], 270.0)
        self.assertEqual(sched["harvest_picked_up_head"], 2)

        pu_after = self.c.get(f"/batches/{bid}/pickup").text
        self.assertIn("Completed Pickups (1)", pu_after)
        self.assertIn("$270.00", pu_after)

    def test_egg_tracker_laying_flock_graduation_and_iron_ledger_bridge(self):
        from datetime import date
        from homestead.database import compute_egg_dashboard, list_batches
        from inventory.db import get_connection, init_inventory_db
        from inventory.sales import record_cash_sale

        # Initialize Iron Ledger tables in the shared DB and create a merchant + Chicken Eggs (dz) SKU
        init_inventory_db()
        self.c.post("/register", data={"display_name": "Steve", "username": "steve", "password": "secret"}, follow_redirects=False)

        conn = get_connection()
        try:
            m_cur = conn.execute(
                "INSERT INTO merchants (name, created_at) VALUES ('Cassberry Lane Farm', '2026-01-01T00:00:00')"
            )
            mid = int(m_cur.lastrowid)
            s_cur = conn.execute(
                """
                INSERT INTO skus (merchant_id, zettle_product_uuid, zettle_variant_uuid,
                                  name, variant_name, sku_code, category, price_cents,
                                  currency, active, sell_mode, updated_at)
                VALUES (?, 'p-eggs', '', 'Chicken Eggs (dz)', '', '', 'Eggs', 500,
                        'USD', 1, 'unit', '2026-01-01T00:00:00')
                """,
                (mid,),
            )
            egg_sku_id = int(s_cur.lastrowid)
            conn.commit()
        finally:
            conn.close()

        # 1. Create a chicken chicks batch (15 head) and graduate it into the laying flock
        self.c.post("/batches/save", data={
            "name": "Spring Pullets",
            "species": "chicken chicks",
            "start_date": "2026-05-01",
            "initial_quantity": "15",
            "unit_cost": "4.00",
            "avg_dressed_weight": "4.5",
            "sale_price_per_lb": "6.50",
            "processing_cost": "5.00",
            "lifecycle_type": "live_arrival",
            "next_url": "/batches",
        }, follow_redirects=False)
        bid = list_batches()[0]["id"]

        r_grad = self.c.post(f"/batches/{bid}/graduate-layers", data={"head_count": "15"}, follow_redirects=False)
        self.assertEqual(r_grad.status_code, 303)

        # 2. Add 5 more hens via /eggs/flock -> 20 active laying hens
        r_fl = self.c.post("/eggs/flock", data={
            "event_date": date.today().isoformat(),
            "reason": "purchase",
            "count": "5",
            "notes": "ISA Browns",
        }, follow_redirects=False)
        self.assertEqual(r_fl.status_code, 303)

        # 3. Link Iron Ledger Chicken Eggs (dz) SKU and set layer + scratch feed settings (0.25 lb/hen layer + 2.0 lb/day scratch)
        r_cfg = self.c.post("/eggs/settings", data={
            "feed_lbs_per_hen_day": "0.25",
            "scratch_lbs_per_day": "2.0",
            "bag_price": "20.00",
            "scratch_bag_price": "15.00",
            "ledger_sku_id": str(egg_sku_id),
        }, follow_redirects=False)
        self.assertEqual(r_cfg.status_code, 303)

        # 4. Log 30 eggs collected today (2 cracked, 1 home used -> 27 packable loose eggs = 2 full dz + 3 loose)
        r_log = self.c.post("/eggs/log", data={
            "log_date": date.today().isoformat(),
            "total_collected": "30",
            "cracked_unusable": "2",
            "home_used": "1",
            "notes": "Coop check",
        }, follow_redirects=False)
        self.assertEqual(r_log.status_code, 303)

        # Log 1 bag of layer feed (50 lb @ $15.00 = $0.30/lb) + 1 bag of scratch (50 lb @ $12.50 = $0.25/lb)
        r_fd1 = self.c.post("/eggs/feed", data={
            "feed_date": date.today().isoformat(),
            "feed_type": "layer",
            "bags": "1",
            "feed_lbs": "50",
            "cost_total": "15.00",
            "notes": "Layer pellets",
        }, follow_redirects=False)
        self.assertEqual(r_fd1.status_code, 303)
        r_fd2 = self.c.post("/eggs/feed", data={
            "feed_date": date.today().isoformat(),
            "feed_type": "scratch",
            "bags": "1",
            "feed_lbs": "50",
            "cost_total": "12.50",
            "notes": "Scratch grains",
        }, follow_redirects=False)
        self.assertEqual(r_fd2.status_code, 303)

        dash1 = compute_egg_dashboard(1)
        self.assertEqual(dash1["active_hens"], 20)
        self.assertEqual(dash1["loose_eggs"], 27)
        self.assertEqual(dash1["ready_dozens"], 2)
        self.assertEqual(dash1["remainder_loose"], 3)
        self.assertAlmostEqual(dash1["layer_remaining_lbs"], 45.0)
        self.assertAlmostEqual(dash1["layer_days_left"], 9.0)
        self.assertAlmostEqual(dash1["scratch_remaining_lbs"], 48.0)
        self.assertAlmostEqual(dash1["scratch_days_left"], 24.0)
        # 5 lb layer ($1.50) + 2 lb scratch ($0.50) = $2.00 eaten today / 2.5 dz = $0.80/dz (while $27.50 total spent on bags)
        self.assertAlmostEqual(dash1["reconciled_feed_cost"], 2.0)
        self.assertAlmostEqual(dash1["logged_feed_cost"], 27.5)
        self.assertAlmostEqual(dash1["cost_per_dozen"], 0.80)

        # 5. Pack 2 dozen cartons & sync to Iron Ledger -> loose_eggs drops to 3, Iron Ledger stock becomes +2 dz!
        r_pk = self.c.post("/eggs/pack", data={
            "pack_date": date.today().isoformat(),
            "dozens_packed": "2",
            "sync_to_ledger": "1",
            "notes": "Market cooler",
        }, follow_redirects=False)
        self.assertEqual(r_pk.status_code, 303)

        dash2 = compute_egg_dashboard(1)
        self.assertEqual(dash2["loose_eggs"], 3)
        self.assertEqual(dash2["linked_sku"]["on_hand"], 2.0)

        # 6. Sell 2 dozen in Iron Ledger ($5.00/dz = $10.00) -> /eggs pulls 2.0 dz sold & $10.00 revenue automatically!
        record_cash_sale(mid, "egg-sale-1", [{"sku_id": egg_sku_id, "quantity": 2}], payment_type="cash")
        dash3 = compute_egg_dashboard(1)
        self.assertEqual(dash3["linked_sku"]["on_hand"], 0.0)
        self.assertEqual(dash3["ledger_dozens_sold"], 2.0)
        self.assertAlmostEqual(dash3["ledger_revenue"], 10.0)

        eggs_html = self.c.get("/eggs").text
        self.assertIn("Eggs &amp; Laying Flock", eggs_html)
        self.assertIn("Chicken Eggs (dz)", eggs_html)
        self.assertIn("+2 synced", eggs_html)

        radar_html = self.c.get("/").text
        self.assertIn("Today's Eggs", radar_html)
        self.assertIn("20 active laying hens", radar_html)

        feed_html = self.c.get("/feed").text
        self.assertIn("Laying Flock Feed Demand (20 active hens)", feed_html)


if __name__ == "__main__":
    unittest.main()
