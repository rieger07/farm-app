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
        with patch("homestead.gdrive.upload_receipt_to_drive", return_value=("drv999", "https://drive.google.com/file/d/drv999/view")) as mock_up:
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
        batch_detail_html = self.c.get(f"/batches/{bid}").text
        self.assertIn(f"${sched['effective_freezer_cost_per_lb']:.2f}/lb freezer", batch_detail_html)

        # Customer Insights page shows species breakdown, Paid vs Calc, and +$15.00 tip
        c_detail = self.c.get(f"/customers?selected={c2['id']}").text
        self.assertIn("Thanksgiving Turkeys", c_detail)
        self.assertIn("20.00 lb", c_detail)
        self.assertIn("+$15.00", c_detail)
        self.assertIn("1 × Turkey", c_detail)
        self.assertNotIn("Turkey Chicks", c_detail)

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
        with patch("homestead.ntfy.send_ntfy_async") as mock_ntfy:
            r_claim = anon.post(f"/api/pick/{slug}/claim/{bird2_id}", json={"customer_id": ident_sarah["customer_id"]})
            self.assertEqual(r_claim.status_code, 200)
            self.assertAlmostEqual(r_claim.json()["bird"]["est_price"], 80.0)
            mock_ntfy.assert_called_once()
            self.assertIn("Sarah Miller", mock_ntfy.call_args[0][2])

        # Double-booking protection: another customer trying to claim Bird #2 gets 409 Conflict
        ident_other = anon.post(f"/api/pick/{slug}/identify", json={"name": "Late Buyer", "contact": "late@example.com"}).json()
        r_dup = anon.post(f"/api/pick/{slug}/claim/{bird2_id}", json={"customer_id": ident_other["customer_id"]})
        self.assertEqual(r_dup.status_code, 409)
        self.assertIn("Someone just grabbed that turkey", r_dup.json()["detail"])

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

        # Verify Batch Detail (1. Grow & Feed) stays clean with KPIs & 4-step Stage Bar (no duplicate bird tables)
        bd_html = self.c.get(f"/batches/{bid}").text
        self.assertIn("1. Grow &amp; Feed", bd_html)
        self.assertIn("3. Harvest &amp; Pick", bd_html)
        self.assertIn("Picking live", bd_html)
        self.assertIn("20.0 / 18.0 lb", bd_html)
        self.assertIn("$115.00 sales", bd_html)
        self.assertNotIn("Harvest Tally &amp; Fulfillment", bd_html)

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
        self.assertIn("$25.00/turkey", res_html)

        # Customer A reserves 2 birds -> reserved, $50 deposit, fires ntfy
        with patch("homestead.ntfy.send_ntfy_async") as mock_ntfy:
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
        with patch("homestead.ntfy.send_ntfy_async") as mock_ntfy:
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
        with patch("homestead.ntfy.send_ntfy_async"):
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
        with patch("homestead.ntfy.send_ntfy_async"):
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

        # Partial pickup: Alice picks up ONLY Bird #1 ($120 - $25 dep = $95) today, leaving Bird #2 assigned!
        r_part1 = self.c.post(
            f"/batches/{bid}/pickup/{al_ident['customer_id']}/settle",
            data={"collected_today": "95.00", "bird_ids": [str(b1)]},
            follow_redirects=False,
        )
        self.assertEqual(r_part1.status_code, 303)
        pu_mid = self.c.get(f"/batches/{bid}/pickup").text
        self.assertIn("Already picked up: Turkey #1", pu_mid)
        self.assertIn("$110.00", pu_mid)  # Bird #2 ($135 - $25 dep = $110) still waiting!
        sched_mid = compute_batch_schedule(list_batches()[0])
        self.assertEqual(sched_mid["harvest_picked_up_head"], 1)

        # Later Alice picks up Bird #2 with $125 collected ($15 tip -> $270 total realized revenue across both birds!)
        r_settle = self.c.post(
            f"/batches/{bid}/pickup/{al_ident['customer_id']}/settle",
            data={"collected_today": "125.00", "bird_ids": [str(b2)]},
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

    def test_shared_farm_merchant_workspace_roles_and_logged_by(self):
        from datetime import date
        from homestead.database import list_batches

        # 1. Owner registers, creates a turkey batch + 1 unassigned bird, renames farm, adds Sarah (manager) & Mom (staff)
        self.c.post("/register", data={"display_name": "Steve", "username": "steve", "password": "secret"}, follow_redirects=False)
        self.c.post("/batches/save", data={
            "name": "Family Turkeys",
            "species": "turkey chicks",
            "start_date": "2026-07-01",
            "initial_quantity": "12",
            "unit_cost": "12.00",
            "avg_dressed_weight": "16.0",
            "sale_price_per_lb": "7.50",
            "processing_cost": "10.00",
            "lifecycle_type": "live_arrival",
            "next_url": "/batches",
        }, follow_redirects=False)
        bid = list_batches()[0]["id"]
        self.c.post(f"/api/batches/{bid}/harvest", json={"dressed_lbs": 17.25, "for_sale": True})

        self.assertEqual(
            self.c.post("/settings/merchant/rename", data={"name": "Cassberry Lane Farm"}, follow_redirects=False).status_code,
            303,
        )
        self.assertEqual(
            self.c.post("/settings/members", data={
                "username": "sarah", "display_name": "Sarah", "password": "secret", "role": "manager",
            }, follow_redirects=False).status_code,
            303,
        )
        self.assertEqual(
            self.c.post("/settings/members", data={
                "username": "mom", "display_name": "Mom", "password": "secret", "role": "staff",
            }, follow_redirects=False).status_code,
            303,
        )

        # 2. Sarah (manager) logs in -> sees shared Family Turkeys & unassigned bird, logs eggs attributed to "Sarah"
        c_sarah = TestClient(app)
        c_sarah.post("/login", data={"username": "sarah", "password": "secret"}, follow_redirects=False)
        sarah_tally = c_sarah.get(f"/batches/{bid}/harvest").text
        self.assertIn("Family Turkeys", sarah_tally)
        self.assertIn("17.25", sarah_tally)
        self.assertIn("Cassberry Lane Farm", sarah_tally)
        self.assertIn("1. Grow &amp; Feed", sarah_tally)
        self.assertIn("2. Pre-Orders", sarah_tally)
        self.assertIn("3. Harvest &amp; Pick", sarah_tally)
        self.assertIn("4. Pickup Day", sarah_tally)

        c_sarah.post("/eggs/log", data={
            "log_date": date.today().isoformat(),
            "total_collected": "18",
            "notes": "Morning coop run",
        }, follow_redirects=False)

        radar_html = self.c.get("/").text
        self.assertIn("Last logged today by <b>Sarah</b>", radar_html)
        eggs_html = self.c.get("/eggs").text
        self.assertIn("Sarah", eggs_html)

        # Manager can view Financials & Customers (with Farm subnav bar, without Settings & Team pill), but cannot access Settings or delete a batch
        fin_sarah = c_sarah.get("/financials")
        self.assertEqual(fin_sarah.status_code, 200)
        self.assertIn("Feed Schedule", fin_sarah.text)
        self.assertIn("Financials &amp; Taxes", fin_sarah.text)
        self.assertNotIn("Settings &amp; Team", fin_sarah.text)
        self.assertEqual(c_sarah.get("/customers").status_code, 200)
        self.assertEqual(c_sarah.get("/settings").status_code, 403)
        self.assertEqual(c_sarah.post(f"/batches/{bid}/delete").status_code, 403)

        # 3. Mom (staff) logs in -> can view Radar, Harvest Tally & Eggs (logs afternoon eggs), blocked from Financials/Customers/Settings
        c_mom = TestClient(app)
        c_mom.post("/login", data={"username": "mom", "password": "secret"}, follow_redirects=False)
        self.assertEqual(c_mom.get("/").status_code, 200)
        self.assertEqual(c_mom.get(f"/batches/{bid}/harvest").status_code, 200)
        self.assertEqual(c_mom.get("/eggs").status_code, 200)
        c_mom.post("/eggs/log", data={
            "log_date": date.today().isoformat(),
            "total_collected": "6",
            "notes": "Afternoon check",
        }, follow_redirects=False)
        self.assertIn("Last logged today by <b>Mom</b>", self.c.get("/").text)
        self.assertEqual(c_mom.get("/financials").status_code, 403)
        self.assertEqual(c_mom.get("/customers").status_code, 403)
        self.assertEqual(c_mom.get("/settings").status_code, 403)

    def test_multi_species_eggs_duck_goose_feed_allocation_and_mobile_debloat(self):
        from datetime import date, datetime
        from homestead.database import compute_egg_dashboard
        from shared.db import get_connection, migrate
        from shared.merchants import create_merchant

        self.c.post("/register", data={"display_name": "Steve", "username": "steve", "password": "secret"}, follow_redirects=False)

        # 1. Default state: only Chicken enabled; Duck & Goose tabs not rendered in sticky switcher
        eggs_default = self.c.get("/eggs").text
        self.assertIn("sticky-species-bar", eggs_default)
        self.assertNotIn('href="/eggs?species=duck', eggs_default)
        self.assertNotIn('href="/eggs?species=goose', eggs_default)
        self.assertIn("egg-subtabs", eggs_default)
        self.assertNotIn("<table", eggs_default)  # replaced wide clipping tables with stacked hist-row items

        # 2. Enable Chicken + Duck + Goose in Farm Settings with Goose pack_size = 1
        r_sp = self.c.post(
            "/settings/egg-species",
            data={
                "enabled_species": ["chicken", "duck", "goose"],
                "goose_pack_size": "1",
            },
            follow_redirects=False,
        )
        self.assertEqual(r_sp.status_code, 303)

        duck_page = self.c.get("/eggs?species=duck").text
        self.assertIn("bumpEggs(1)", duck_page)
        self.assertIn("bumpEggs(6)", duck_page)
        self.assertIn("Half-Dozens (6-pk) to Pack (6 eggs each)", duck_page)

        goose_page_1 = self.c.get("/eggs?species=goose").text
        self.assertIn("bumpEggs(4)", goose_page_1)
        self.assertIn("Single Eggs to Pack (1 egg each)", goose_page_1)

        # Switch Goose pack size to 4 via /eggs/settings
        self.c.post("/eggs/settings", data={
            "species": "goose",
            "goose_pack_size": "4",
            "feed_lbs_per_hen_day": "0.50",
            "bag_price": "22.00",
            "scratch_lbs_per_day": "0",
            "scratch_bag_price": "16.00",
        }, follow_redirects=False)
        goose_page_4 = self.c.get("/eggs?species=goose").text
        self.assertIn("4-Packs to Pack (4 eggs each)", goose_page_4)

        # 3. Create Iron Ledger SKUs for Chicken Eggs (12) and Duck Eggs (6)
        migrate()
        mid = create_merchant("Waterfowl & Coops", owner_user_id=1)
        now = datetime.utcnow().isoformat(timespec="seconds")
        conn = get_connection()
        try:
            chicken_sku = conn.execute(
                "INSERT INTO skus (merchant_id, zettle_product_uuid, zettle_variant_uuid, name, price_cents, updated_at)"
                " VALUES (?, 'p-chk', 'v-chk', 'Chicken Eggs (Dozen)', 500, ?)",
                (mid, now),
            ).lastrowid
            duck_sku = conn.execute(
                "INSERT INTO skus (merchant_id, zettle_product_uuid, zettle_variant_uuid, name, price_cents, updated_at)"
                " VALUES (?, 'p-dck', 'v-dck', 'Duck Eggs (Half-Dozen)', 600, ?)",
                (mid, now),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()

        self.c.post("/eggs/settings", data={
            "species": "chicken",
            "feed_lbs_per_hen_day": "0.25",
            "bag_price": "20.00",
            "scratch_lbs_per_day": "0.0",
            "scratch_bag_price": "15.00",
            "ledger_sku_id": str(chicken_sku),
        }, follow_redirects=False)
        self.c.post("/eggs/settings", data={
            "species": "duck",
            "feed_lbs_per_hen_day": "0.40",
            "bag_price": "28.00",
            "scratch_lbs_per_day": "0.0",
            "scratch_bag_price": "15.00",
            "ledger_sku_id": str(duck_sku),
        }, follow_redirects=False)

        # 4. Log separate headcounts & egg collections for Chickens and Ducks
        today_iso = date.today().isoformat()
        self.c.post("/eggs/flock", data={"species": "chicken", "event_date": today_iso, "reason": "initial", "count": "10"})
        self.c.post("/eggs/flock", data={"species": "duck", "event_date": today_iso, "reason": "initial", "count": "5"})

        self.c.post("/eggs/log", data={"species": "chicken", "log_date": today_iso, "total_collected": "12"})
        self.c.post("/eggs/log", data={"species": "duck", "log_date": today_iso, "total_collected": "8", "next_url": "/"})

        # Radar shows 3-col compact KPI strip, top-right Eggs Hub link, inline species pills & combined summary!
        radar_html = self.c.get("/").text
        self.assertIn("kpi-strip-3", radar_html)
        self.assertIn("radar-species-pills", radar_html)
        self.assertIn("radar-eggs-hub-link", radar_html)
        self.assertIn("12 chicken · 8 duck", radar_html)

        # 5. Pack 1 Duck half-dozen (6 eggs) -> restocks Duck SKU in Iron Ledger, leaves 2 loose duck eggs & 12 loose chicken eggs
        r_pk_duck = self.c.post("/eggs/pack", data={
            "species": "duck",
            "pack_date": today_iso,
            "dozens_packed": "1",
            "sync_to_ledger": "1",
        }, follow_redirects=False)
        self.assertEqual(r_pk_duck.status_code, 303)

        duck_dash = compute_egg_dashboard(1, species="duck")
        chk_dash = compute_egg_dashboard(1, species="chicken")
        self.assertEqual(duck_dash["loose_eggs"], 2)
        self.assertEqual(duck_dash["linked_sku"]["on_hand"], 1.0)
        self.assertEqual(chk_dash["loose_eggs"], 12)

        # 6. Shared Feed Log species_tags allocation: $20/50lb ($0.40/lb) Chickens vs $30/50lb ($0.60/lb) Waterfowl
        self.c.post("/eggs/feed", data={
            "species": "chicken",
            "feed_date": today_iso,
            "feed_type": "layer",
            "species_tags": ["chicken"],
            "bags": "1",
            "feed_lbs": "50",
            "cost_total": "20.00",
        }, follow_redirects=False)
        self.c.post("/eggs/feed", data={
            "species": "duck",
            "feed_date": today_iso,
            "feed_type": "layer",
            "species_tags": ["waterfowl"],
            "bags": "1",
            "feed_lbs": "50",
            "cost_total": "30.00",
        }, follow_redirects=False)

        chk_alloc = compute_egg_dashboard(1, species="chicken")
        duck_alloc = compute_egg_dashboard(1, species="duck")
        # Chickens: 10 hens * 0.25 lb = 2.5 lb * $0.40/lb = $1.00 / 1.0 dz = $1.00/dz (0% of waterfowl feed)
        self.assertAlmostEqual(chk_alloc["species_logged_feed_cost"], 20.0)
        self.assertAlmostEqual(chk_alloc["reconciled_feed_cost"], 1.0)
        self.assertAlmostEqual(chk_alloc["cost_per_dozen"], 1.0)
        # Ducks: 5 ducks * 0.40 lb = 2.0 lb * $0.60/lb = $1.20 / (8/6 = 1.33 half-dz) = $0.90/half-dz (0% of chicken feed)
        self.assertAlmostEqual(duck_alloc["species_logged_feed_cost"], 30.0)
        self.assertAlmostEqual(duck_alloc["reconciled_feed_cost"], 1.20)
        self.assertAlmostEqual(duck_alloc["cost_per_pack"], round(1.20 / 1.33, 2))

        # Undo duck carton pack reverses Iron Ledger restock cleanly
        pack_id = duck_alloc["pack_logs"][0]["id"]
        self.c.post(f"/eggs/pack/{pack_id}/delete", data={"species": "duck"}, follow_redirects=False)
        duck_after_undo = compute_egg_dashboard(1, species="duck")
        self.assertEqual(duck_after_undo["loose_eggs"], 8)
        self.assertEqual(duck_after_undo["linked_sku"]["on_hand"], 0.0)

    def test_workspace_member_roles_and_admin_user_crud(self):
        from homestead.database import get_user_by_username, list_batches
        from homestead.db.workspaces import list_homestead_merchant_members

        # Register first admin owner ("steve") and verify workspace/settings render
        self.c.post("/register", data={"display_name": "Steve", "username": "steve", "password": "secret"}, follow_redirects=False)
        steve = get_user_by_username("steve")
        self.c.post("/settings/members", data={
            "username": "sarah", "display_name": "Sarah", "password": "secret", "role": "manager",
        }, follow_redirects=False)
        sarah = get_user_by_username("sarah")

        # 1. Inline role change: promote sarah to owner, then back to staff
        r_up = self.c.post("/settings/members/role", data={"user_id": str(sarah["id"]), "role": "owner"}, follow_redirects=False)
        self.assertEqual(r_up.status_code, 303)
        self.assertEqual(
            next(m["role"] for m in list_homestead_merchant_members(1) if m["user_id"] == sarah["id"]),
            "owner",
        )
        self.c.post("/settings/members/role", data={"user_id": str(sarah["id"]), "role": "staff"}, follow_redirects=False)
        self.assertEqual(
            next(m["role"] for m in list_homestead_merchant_members(1) if m["user_id"] == sarah["id"]),
            "staff",
        )

        # 2. Cannot demote the last owner via /settings/members/role or /settings/members
        r_guard = self.c.post("/settings/members/role", data={"user_id": str(steve["id"]), "role": "manager"}, follow_redirects=False)
        self.assertIn("Cannot+demote+the+last+owner", r_guard.headers["location"])
        r_guard2 = self.c.post("/settings/members", data={"username": "steve", "role": "manager"}, follow_redirects=False)
        self.assertIn("Cannot+demote+the+last+owner", r_guard2.headers["location"])
        self.assertEqual(
            next(m["role"] for m in list_homestead_merchant_members(1) if m["user_id"] == steve["id"]),
            "owner",
        )

        # 3. Admin user CRUD: create -> edit display name + reset password -> self-delete guard -> delete with batch cleanup
        self.c.post("/settings/admin/users", data={
            "username": "helper", "display_name": "Helpr", "password": "pass1",
        }, follow_redirects=False)
        helper = get_user_by_username("helper")
        self.assertIsNotNone(helper)

        r_edit = self.c.post(f"/settings/admin/users/{helper['id']}/update", data={
            "display_name": "Farm Helper", "new_password": "pass22",
        }, follow_redirects=False)
        self.assertEqual(r_edit.status_code, 303)
        self.assertEqual(get_user_by_username("helper")["display_name"], "Farm Helper")

        c_helper = TestClient(app)
        self.assertEqual(
            c_helper.post("/login", data={"username": "helper", "password": "pass22"}, follow_redirects=False).status_code,
            303,
        )
        # Create a batch owned by helper's own workspace so batch cleanup during delete is exercised
        c_helper.post("/batches/save", data={
            "name": "Helper Ducklings",
            "species": "cornish cross",
            "start_date": "2026-07-01",
            "initial_quantity": "10",
            "unit_cost": "4.00",
            "avg_dressed_weight": "5.0",
            "sale_price_per_lb": "6.00",
            "processing_cost": "4.00",
            "lifecycle_type": "live_arrival",
            "next_url": "/batches",
        }, follow_redirects=False)
        self.assertTrue(any(b["name"] == "Helper Ducklings" for b in list_batches(user_id=helper["id"])))

        # Admin cannot delete self
        r_self_del = self.c.post(f"/settings/admin/users/{steve['id']}/delete", follow_redirects=False)
        self.assertIn("You+cannot+delete+your+own+account", r_self_del.headers["location"])
        self.assertIsNotNone(get_user_by_username("steve"))

        # Delete helper cleanly along with helper's batches
        r_del = self.c.post(f"/settings/admin/users/{helper['id']}/delete", follow_redirects=False)
        self.assertEqual(r_del.status_code, 303)
        self.assertIsNone(get_user_by_username("helper"))
        self.assertEqual(list_batches(user_id=helper["id"]), [])

    def test_unified_split_receipt_tax_books_and_csv(self):
        import csv
        import io
        from unittest.mock import patch
        from homestead.database import (
            compute_egg_dashboard,
            list_batch_feed_logs,
            list_batches,
        )
        from homestead.db.eggs import list_layer_feed_logs
        from homestead.db.expenses import list_farm_expenses
        from inventory.market_day import save_market_day_pack
        from shared.db import get_connection

        self.c.post(
            "/register",
            data={"display_name": "Steve", "username": "steve", "password": "secret"},
            follow_redirects=False,
        )

        # Radar shows unified + Log receipt CTA for owner/manager
        radar_owner = self.c.get("/").text
        self.assertIn("/financials?tab=taxes&amp;new=1", radar_owner)

        # Create Thanksgiving 2026 batch with 2 paid/sold birds ($240 + $15 proc) and 1 Home Freezer bird (40 lb)
        self.c.post("/batches/save", data={
            "name": "Thanksgiving 2026",
            "species": "turkey chicks",
            "start_date": "2026-06-01",
            "initial_quantity": "10",
            "unit_cost": "10.00",
            "avg_dressed_weight": "20.0",
            "sale_price_per_lb": "6.00",
            "processing_cost": "5.00",
            "grower_bag_price": "25.00",
            "lifecycle_type": "live_arrival",
            "next_url": "/batches",
        }, follow_redirects=False)
        bid = list_batches()[0]["id"]
        self.c.post("/batches/log-harvest", data={
            "batch_id": str(bid),
            "harvest_date": "2026-11-15",
            "head_count": "1",
            "dressed_lbs": "20.0",
            "processing_cost_total": "15.00",
            "notes": "Butcher invoice #42",
        }, follow_redirects=False)
        # Assign harvest #1 to customer and mark paid ($120), plus manual Customer Sale ($120, 20 lb) and Home Freezer (40 lb)
        self.c.post(f"/batches/{bid}/harvests/1/delete", follow_redirects=False)
        conn = get_connection()
        conn.execute(
            """
            INSERT INTO batch_harvests (
                batch_id, harvest_date, head_count, dressed_lbs, processing_cost_total,
                for_sale, actual_sale_price, payment_status, pickup_status, notes, created_at
            ) VALUES (?, '2026-11-15', 2, 40.0, 15.00, 1, 240.00, 'paid', 'picked_up', 'Butcher invoice #42', '2026-11-15T12:00:00')
            """,
            (bid,),
        )
        conn.execute(
            """
            INSERT INTO batch_harvests (
                batch_id, harvest_date, head_count, dressed_lbs, processing_cost_total,
                for_sale, notes, created_at
            ) VALUES (?, '2026-11-15', 1, 40.0, 0.0, 0, 'Family freezer bird', '2026-11-15T12:05:00')
            """,
            (bid,),
        )
        conn.execute(
            """
            INSERT INTO sales (
                merchant_id, source, external_id, occurred_at, total_cents, status, created_at
            ) VALUES (1, 'cash', 'pos-tax-1', '2026-09-12T16:00:00', 18000, 'completed', '2026-09-12T16:00:00')
            """,
        )
        conn.commit()
        conn.close()

        # Seed an Iron Ledger market day ($50 booth + $15 travel = $65)
        save_market_day_pack(
            merchant_id=1,
            market_date="2026-09-12",
            marketplace_id=None,
            brought_by_sku={},
            new_marketplace_name="Sussex Farmers Market",
            booth_fee_cents=5000,
            travel_cost_cents=1500,
        )

        # 1. Submit 3-way split Tractor Supply receipt ($110 batch feed + $52 layer feed + $93.68 supplies = $255.68)
        split_lines = [
            {"kind": "batch_feed", "batch_id": str(bid), "feed_type": "grower", "bags": 4, "feed_lbs": 200, "amount": 110.00, "notes": "Turkey grower"},
            {"kind": "layer_feed", "feed_type": "layer", "species_tags": ["chicken"], "bags": 2, "feed_lbs": 100, "amount": 52.00, "notes": "Layer pellets"},
            {"kind": "general", "category": "Supplies & Equipment", "amount": 93.68, "notes": "Pine shavings & heat lamp"},
        ]
        with patch("homestead.gdrive.upload_receipt_to_drive", return_value=("drv777", "https://drive.google.com/file/d/drv777/view")) as mock_up:
            r_split = self.c.post(
                "/expenses/receipt",
                data={
                    "receipt_date": "2026-11-04",
                    "vendor": "Tractor Supply",
                    "lines_json": json.dumps(split_lines),
                },
                files={"receipt_file": ("tsc.jpg", b"jpeg-bytes", "image/jpeg")},
                follow_redirects=False,
            )
            self.assertEqual(r_split.status_code, 303)
            self.assertEqual(mock_up.call_count, 1)

        bf = list_batch_feed_logs(bid)
        lf = list_layer_feed_logs(1)
        fe = list_farm_expenses(1)
        self.assertEqual(len(bf), 1)
        self.assertEqual(len(lf), 1)
        self.assertEqual(len(fe), 1)
        gid = bf[0]["receipt_group_id"]
        self.assertTrue(gid)
        self.assertEqual(lf[0]["receipt_group_id"], gid)
        self.assertEqual(fe[0]["receipt_group_id"], gid)
        self.assertEqual(bf[0]["receipt_url"], "https://drive.google.com/file/d/drv777/view")
        self.assertEqual(lf[0]["receipt_url"], "https://drive.google.com/file/d/drv777/view")
        self.assertEqual(fe[0]["receipt_url"], "https://drive.google.com/file/d/drv777/view")

        # Verify batch detail & egg hub updated from unified receipt
        self.assertIn("200.0 lb ($110.00)", self.c.get(f"/batches/{bid}").text)
        self.assertAlmostEqual(compute_egg_dashboard(1)["logged_feed_cost"], 52.0)

        # 2. Log a second layer feed entry WITHOUT a receipt, then attach one cleanly
        self.c.post(
            "/expenses/receipt",
            data={
                "receipt_date": "2026-10-01",
                "vendor": "Runnings",
                "lines_json": json.dumps([
                    {"kind": "layer_feed", "feed_type": "scratch", "species_tags": ["all"], "bags": 1, "feed_lbs": 50, "amount": 24.00, "notes": "Scratch bag"}
                ]),
            },
            follow_redirects=False,
        )
        runnings_lf = next(x for x in list_layer_feed_logs(1) if x["vendor"] == "Runnings")
        self.assertEqual(runnings_lf["receipt_url"], "")
        with patch("homestead.gdrive.upload_receipt_to_drive", return_value=("drv888", "https://drive.google.com/file/d/drv888/view")):
            r_att = self.c.post(
                "/expenses/attach-receipt",
                data={
                    "source_key": "layer_feed",
                    "entry_id": str(runnings_lf["id"]),
                    "entry_date": "2026-10-01",
                    "label": "Runnings",
                    "year": "2026",
                },
                files={"receipt_file": ("runnings.jpg", b"jpeg-2", "image/jpeg")},
                follow_redirects=False,
            )
            self.assertEqual(r_att.status_code, 303)
        self.assertEqual(
            next(x["receipt_url"] for x in list_layer_feed_logs(1) if x["id"] == runnings_lf["id"]),
            "https://drive.google.com/file/d/drv888/view",
        )

        # 3. Tax Books HTML shows combined income ($420), non-duplicated expenses ($359.68), Schedule F categories, and freezer note
        tax_html = self.c.get("/financials?tab=taxes&year=2026").text
        self.assertIn("$420.00", tax_html)  # $240 Homestead + $180 POS
        self.assertIn("$359.68", tax_html)  # $186 feed + $15 proc + $93.68 supplies + $65 market
        self.assertIn("$186.00", tax_html)  # Line 16 Feed (110 + 52 + 24)
        self.assertIn("$93.68", tax_html)   # Line 28 Supplies
        self.assertIn("$65.00", tax_html)   # Line 32 Market Booth & Travel Fees
        self.assertIn("$15.00", tax_html)   # Line 15 Custom Hire / Processing
        self.assertIn("Thanksgiving 2026 (40.0 of 80.0 lb · 50.0%)", tax_html)
        self.assertIn("Split (3)", tax_html)

        # 4. Schedule F CSV export includes all 5 sources and sums cleanly to exact $359.68 (zero double-counting)
        csv_resp = self.c.get("/financials/expenses.csv?year=2026")
        self.assertEqual(csv_resp.status_code, 200)
        rows = list(csv.DictReader(io.StringIO(csv_resp.text)))
        sources = {r["Source"] for r in rows}
        self.assertEqual(sources, {"General", "Batch Feed", "Layer Feed", "Processing", "Market Day"})
        self.assertAlmostEqual(sum(float(r["Amount ($)"]) for r in rows), 359.68, places=2)

        # 5. Deleting split receipt group removes all 3 splits cleanly at once
        r_gdel = self.c.post(
            "/expenses/group/delete",
            data={"receipt_group_id": gid, "year": "2026"},
            follow_redirects=False,
        )
        self.assertEqual(r_gdel.status_code, 303)
        self.assertEqual(list_batch_feed_logs(bid), [])
        self.assertEqual(list_farm_expenses(1), [])
        remaining_lf = list_layer_feed_logs(1)
        self.assertEqual(len(remaining_lf), 1)

        # Single entry delete via /expenses/entry/delete also works cleanly
        r_ed = self.c.post(
            "/expenses/entry/delete",
            data={"source_key": "layer_feed", "entry_id": str(remaining_lf[0]["id"]), "year": "2026"},
            follow_redirects=False,
        )
        self.assertEqual(r_ed.status_code, 303)
        self.assertEqual(list_layer_feed_logs(1), [])

        # 6. Staff user cannot log receipts or view Financials
        self.c.post("/settings/members", data={"username": "mom", "display_name": "Mom", "password": "secret", "role": "staff"}, follow_redirects=False)
        c_mom = TestClient(app)
        c_mom.post("/login", data={"username": "mom", "password": "secret"}, follow_redirects=False)
        self.assertNotIn("/financials?tab=taxes&amp;new=1", c_mom.get("/").text)
        self.assertEqual(
            c_mom.post("/expenses/receipt", data={"lines_json": "[]"}, follow_redirects=False).status_code,
            403,
        )

    def test_homestead_payment_method_crud_pickup_harvest_and_batch_sales(self):
        from homestead.db.batches import (
            add_batch_harvest,
            list_batch_harvests,
            list_batch_sales,
            save_batch,
        )
        from homestead.db.customers import save_customer, update_harvest_assignment
        from homestead.db.backup import export_backup_json, import_backup_json

        r_reg = self.c.post(
            "/register",
            data={"display_name": "Steve", "username": "steve", "password": "secret"},
            follow_redirects=False,
        )
        self.assertEqual(r_reg.status_code, 303)

        bid = save_batch(
            {
                "name": "Pay Method Batch",
                "species": "cornish cross",
                "quantity": 10,
                "start_date": "2026-09-01",
                "days_to_birth": 0,
                "weeks_to_proc": 8,
                "feed_total": 10.0,
                "sale_price_per_lb": 5.0,
            },
            user_id=1,
        )
        alice_id = save_customer(1, "Alice Pay", phone="5550001111")
        bob_id = save_customer(1, "Bob Legacy", phone="5550002222")
        h1 = add_batch_harvest(bid, "2026-10-25", 1, 5.0, 0.0, for_sale=True)
        h2 = add_batch_harvest(bid, "2026-10-25", 1, 6.0, 0.0, for_sale=True)
        update_harvest_assignment(bid, h1, customer_id=alice_id, for_sale=True)
        # Legacy paid bird without payment method
        update_harvest_assignment(
            bid, h2, customer_id=bob_id, for_sale=True, actual_sale_price=32.0,
            payment_status="paid", pickup_status="picked_up",
        )

        # 1. Pickup Day settle records payment_method & paid_at
        r_settle = self.c.post(
            f"/batches/{bid}/pickup/{alice_id}/settle",
            data={"collected_today": "25.00", "payment_method": "venmo", "bird_ids": [str(h1)]},
            follow_redirects=False,
        )
        self.assertEqual(r_settle.status_code, 303)
        hv_map = {int(h["id"]): h for h in list_batch_harvests(bid)}
        self.assertEqual(hv_map[h1]["payment_method"], "venmo")
        self.assertTrue(hv_map[h1]["paid_at"])
        self.assertEqual(hv_map[h2]["payment_method"], "")

        # 2. Pickup Day Completed Pickups inline CRUD re-tags payment_method (including legacy birds)
        r_retag_bob = self.c.post(
            f"/batches/{bid}/pickup/{bob_id}/payment-method",
            data={"payment_method": "check"},
            follow_redirects=False,
        )
        self.assertEqual(r_retag_bob.status_code, 303)
        hv_map = {int(h["id"]): h for h in list_batch_harvests(bid)}
        self.assertEqual(hv_map[h2]["payment_method"], "check")

        # 3. Harvest Tally assign API persists payment_method edits
        r_api = self.c.post(
            f"/api/batches/{bid}/harvest/{h2}/assign",
            json={
                "for_sale": True,
                "customer_id": bob_id,
                "actual_sale_price": 32.0,
                "deposit_paid": 0,
                "payment_status": "paid",
                "pickup_status": "picked_up",
                "payment_method": "paypal",
                "notes": "Paid via PayPal later",
            },
        )
        self.assertEqual(r_api.status_code, 200)
        hv_map = {int(h["id"]): h for h in list_batch_harvests(bid)}
        self.assertEqual(hv_map[h2]["payment_method"], "paypal")

        # 4. Manual batch_sales supports payment_method on creation & CRUD re-tagging, ignoring Home Freezer
        self.c.post(
            "/batches/log-sale",
            data={
                "batch_id": str(bid),
                "entry_date": "2026-10-28",
                "allocation_type": "sale",
                "weight_lbs": "12.0",
                "revenue": "60.00",
                "payment_method": "check",
                "next_url": f"/batches/{bid}",
            },
            follow_redirects=False,
        )
        self.c.post(
            "/batches/log-sale",
            data={
                "batch_id": str(bid),
                "entry_date": "2026-10-28",
                "allocation_type": "freezer",
                "weight_lbs": "8.0",
                "revenue": "0",
                "payment_method": "cash",
                "next_url": f"/batches/{bid}",
            },
            follow_redirects=False,
        )
        bs_rows = list_batch_sales(bid)
        cust_sale = next(s for s in bs_rows if s["allocation_type"] == "Customer Sale")
        freezer_sale = next(s for s in bs_rows if s["allocation_type"] == "Home Freezer")
        self.assertEqual(cust_sale["payment_method"], "check")
        self.assertEqual(freezer_sale["payment_method"], "")

        r_bs_edit = self.c.post(
            f"/batches/{bid}/sales/{cust_sale['id']}/payment-method",
            data={"payment_method": "card"},
            follow_redirects=False,
        )
        self.assertEqual(r_bs_edit.status_code, 303)
        self.assertEqual(
            next(s["payment_method"] for s in list_batch_sales(bid) if s["id"] == cust_sale["id"]),
            "card",
        )

        # 5. Pickup Day & Batch Detail pages render payment controls cleanly
        pu_html = self.c.get(f"/batches/{bid}/pickup").text
        self.assertIn(f"/batches/{bid}/pickup/{alice_id}/payment-method", pu_html)
        bd_html = self.c.get(f"/batches/{bid}").text
        self.assertIn(f"/batches/{bid}/sales/{cust_sale['id']}/payment-method", bd_html)

        # 6. JSON backup export + import preserves payment_method and paid_at
        raw_json = export_backup_json(user_id=1)
        imported = import_backup_json(raw_json, replace_existing=True, user_id=1)
        self.assertEqual(imported, 1)
        from homestead.db.batches import list_batches
        new_bid = list_batches(user_id=1)[0]["id"]
        restored_cs = next(s for s in list_batch_sales(new_bid) if s["allocation_type"] == "Customer Sale")
        self.assertEqual(restored_cs["payment_method"], "card")

    def test_capital_equipment_line14_and_gemini_receipt_ocr(self):
        import csv
        import io
        from unittest.mock import patch
        import httpx

        self.c.post(
            "/register",
            data={"display_name": "Steve", "username": "steve", "password": "secret"},
            follow_redirects=False,
        )

        # 1. Log both Line 14 Capital Equipment ($3,200) and Line 28 Supplies ($45)
        lines = [
            {
                "kind": "general",
                "category": "Capital Equipment / Depreciation",
                "amount": 3200.00,
                "notes": "Zero-turn mower",
            },
            {
                "kind": "general",
                "category": "Supplies & Equipment",
                "amount": 45.00,
                "notes": "Buckets and twine",
            },
        ]
        r_save = self.c.post(
            "/expenses/receipt",
            data={
                "receipt_date": "2026-11-26",
                "vendor": "John Deere",
                "lines_json": json.dumps(lines),
            },
            follow_redirects=False,
        )
        self.assertEqual(r_save.status_code, 303)

        # Verify Financials Schedule F Summary, Capital (L14) filter, and CSV export
        fin_html = self.c.get("/financials?tab=taxes&year=2026").text
        self.assertIn("Line 14 · Capital Equipment / Depreciation (CPA check)", fin_html)
        self.assertIn("Line 28 · Supplies", fin_html)
        self.assertIn('data-src="capital"', fin_html)
        self.assertIn('data-line="Line 14"', fin_html)

        csv_resp = self.c.get("/financials/expenses.csv?year=2026")
        self.assertEqual(csv_resp.status_code, 200)
        csv_rows = list(csv.DictReader(io.StringIO(csv_resp.text)))
        cap_row = next(r for r in csv_rows if r["Category"] == "Capital Equipment / Depreciation")
        self.assertEqual(cap_row["Schedule F Line"], "Line 14")
        self.assertEqual(cap_row["Amount ($)"], "3200.00")

        # 2. When GEMINI_API_KEY is unset, scan trigger is hidden and OCR endpoint returns 503
        prev_key = os.environ.pop("GEMINI_API_KEY", None)
        prev_model = os.environ.pop("GEMINI_OCR_MODEL", None)
        prev_thinking = os.environ.pop("GEMINI_OCR_THINKING_LEVEL", None)
        try:
            fin_no_key = self.c.get("/financials?tab=taxes").text
            self.assertNotIn('id="rcpt-scan-btn"', fin_no_key)
            r_no_key = self.c.post(
                "/api/receipts/parse",
                files={"file": ("receipt.jpg", b"\xff\xd8\xff\xe0test", "image/jpeg")},
            )
            self.assertEqual(r_no_key.status_code, 503)

            # 3. With GEMINI_API_KEY set (including accidental quotes), scan trigger renders & structured outputs work
            os.environ["GEMINI_API_KEY"] = '"secret-gemini-test-key"'
            fin_with_key = self.c.get("/financials?tab=taxes").text
            self.assertIn('id="rcpt-scan-btn"', fin_with_key)
            self.assertIn("compressReceiptImage", fin_with_key)

            mock_ocr_payload = {
                "date": "2026-11-26",
                "vendor": "Tractor Supply",
                "receipt_total": 2912.50,
                "tax_charged": 3.12,
                "mixed_use_warning": True,
                "splits": [
                    {
                        "split_type": "laying_flock_feed",
                        "amount": 44.00,
                        "notes": "2x 50lb Layer Pellets",
                        "schedule_f_category": None,
                        "feed_type": "layer",
                        "bags": 2,
                        "total_weight_lb": 100,
                    },
                    {
                        "split_type": "meat_batch_feed",
                        "amount": 68.50,
                        "notes": "3x 50lb Meatbird Crumble",
                        "schedule_f_category": None,
                        "feed_type": "grower",
                        "bags": 3,
                        "total_weight_lb": 150,
                    },
                    {
                        "split_type": "general_farm_expense",
                        "amount": 2800.00,
                        "notes": "Utility trailer (incl. tax)",
                        "schedule_f_category": "capital_equipment",
                        "feed_type": None,
                        "bags": None,
                        "total_weight_lb": None,
                    },
                ],
            }
            captured_calls: list[dict] = []

            class _FakeResp:
                def __init__(self, status_code=200, body=None):
                    self.status_code = status_code
                    self._body = body or {}

                def json(self):
                    return self._body

            async def _fake_post(self_client, url, **kwargs):
                captured_calls.append({"url": url, "kwargs": kwargs})
                return _FakeResp(
                    200,
                    {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {"thought": True, "text": "internal reasoning"},
                                        {"text": json.dumps(mock_ocr_payload)},
                                    ]
                                }
                            }
                        ]
                    },
                )

            with patch.object(httpx.AsyncClient, "post", _fake_post):
                r_img = self.c.post(
                    "/api/receipts/parse",
                    files={"file": ("tsc.jpg", b"\xff\xd8\xff\xe0imgbytes", "image/jpeg")},
                )
                self.assertEqual(r_img.status_code, 200)
                parsed = r_img.json()
                self.assertEqual(parsed["vendor"], "Tractor Supply")
                self.assertEqual(parsed["date"], "2026-11-26")
                self.assertAlmostEqual(parsed["tax_charged"], 3.12, places=2)
                self.assertTrue(parsed["mixed_use_warning"])
                self.assertEqual(len(parsed["splits"]), 3)
                self.assertEqual(parsed["splits"][0]["bags"], 2)
                self.assertEqual(parsed["splits"][0]["total_weight_lb"], 100)
                self.assertEqual(parsed["splits"][2]["schedule_f_category"], "capital_equipment")

                # Native PDF upload also works without resampling
                r_pdf = self.c.post(
                    "/api/receipts/parse",
                    files={"file": ("order.pdf", b"%PDF-1.4\n%test", "application/pdf")},
                )
                self.assertEqual(r_pdf.status_code, 200)

            self.assertEqual(len(captured_calls), 2)
            req0 = captured_calls[0]
            self.assertIn("gemini-3.8-flash:generateContent", req0["url"])
            self.assertNotIn("secret-gemini-test-key", req0["url"])
            self.assertEqual(req0["kwargs"]["headers"]["x-goog-api-key"], "secret-gemini-test-key")
            self.assertEqual(req0["kwargs"]["timeout"], 25.0)
            req_json = req0["kwargs"]["json"]
            self.assertEqual(req_json["generationConfig"]["responseMimeType"], "application/json")
            self.assertIn("responseSchema", req_json["generationConfig"])
            self.assertEqual(
                req_json["generationConfig"]["thinkingConfig"],
                {"thinkingLevel": "LOW"},
            )
            self.assertIn("NJ Farm Exemption", req_json["systemInstruction"]["parts"][0]["text"])
            self.assertEqual(
                captured_calls[1]["kwargs"]["json"]["contents"][0]["parts"][1]["inlineData"]["mimeType"],
                "application/pdf",
            )

            # GEMINI_OCR_THINKING_LEVEL overrides default LOW and supports off/none
            captured_calls.clear()
            os.environ["GEMINI_OCR_THINKING_LEVEL"] = "minimal"
            with patch.object(httpx.AsyncClient, "post", _fake_post):
                r_min = self.c.post(
                    "/api/receipts/parse",
                    files={"file": ("tsc.jpg", b"\xff\xd8\xff\xe0imgbytes", "image/jpeg")},
                )
                self.assertEqual(r_min.status_code, 200)
            self.assertEqual(
                captured_calls[0]["kwargs"]["json"]["generationConfig"]["thinkingConfig"],
                {"thinkingLevel": "MINIMAL"},
            )

            captured_calls.clear()
            os.environ["GEMINI_OCR_THINKING_LEVEL"] = "off"
            with patch.object(httpx.AsyncClient, "post", _fake_post):
                r_off = self.c.post(
                    "/api/receipts/parse",
                    files={"file": ("tsc.jpg", b"\xff\xd8\xff\xe0imgbytes", "image/jpeg")},
                )
                self.assertEqual(r_off.status_code, 200)
            self.assertNotIn(
                "thinkingConfig",
                captured_calls[0]["kwargs"]["json"]["generationConfig"],
            )
            os.environ.pop("GEMINI_OCR_THINKING_LEVEL", None)

            # 4. File validation, timeout & upstream error handling (Cloudflare-safe 4xx, no API key leak)
            r_bad = self.c.post(
                "/api/receipts/parse",
                files={"file": ("notes.txt", b"hello", "text/plain")},
            )
            self.assertEqual(r_bad.status_code, 400)
            r_empty = self.c.post(
                "/api/receipts/parse",
                files={"file": ("empty.jpg", b"", "image/jpeg")},
            )
            self.assertEqual(r_empty.status_code, 400)

            async def _timeout_post(self_client, url, **kwargs):
                raise httpx.ReadTimeout("timed out")

            with patch.object(httpx.AsyncClient, "post", _timeout_post):
                r_to = self.c.post(
                    "/api/receipts/parse",
                    files={"file": ("tsc.jpg", b"\xff\xd8\xff\xe0imgbytes", "image/jpeg")},
                )
                self.assertEqual(r_to.status_code, 422)
                self.assertNotIn("secret-gemini-test-key", r_to.text)

            async def _err_404_post(self_client, url, **kwargs):
                return _FakeResp(
                    404,
                    {"error": {"message": "models/gemini-2.5-flash is not found (key secret-gemini-test-key)"}},
                )

            with patch.object(httpx.AsyncClient, "post", _err_404_post):
                r_err = self.c.post(
                    "/api/receipts/parse",
                    files={"file": ("tsc.jpg", b"\xff\xd8\xff\xe0imgbytes", "image/jpeg")},
                )
                self.assertEqual(r_err.status_code, 422)
                self.assertIn("Gemini HTTP 404", r_err.text)
                self.assertIn("is not found", r_err.text)
                self.assertNotIn("secret-gemini-test-key", r_err.text)

            async def _err_429_post(self_client, url, **kwargs):
                return _FakeResp(429, {"error": {"message": "Quota exceeded"}})

            with patch.object(httpx.AsyncClient, "post", _err_429_post):
                r_429 = self.c.post(
                    "/api/receipts/parse",
                    files={"file": ("tsc.jpg", b"\xff\xd8\xff\xe0imgbytes", "image/jpeg")},
                )
                self.assertEqual(r_429.status_code, 429)
                self.assertIn("Quota exceeded", r_429.text)

            # 5. Staff user is blocked with 403
            self.c.post(
                "/settings/members",
                data={"username": "helper", "display_name": "Helper", "password": "secret", "role": "staff"},
                follow_redirects=False,
            )
            c_staff = TestClient(app)
            c_staff.post("/login", data={"username": "helper", "password": "secret"}, follow_redirects=False)
            r_staff = c_staff.post(
                "/api/receipts/parse",
                files={"file": ("tsc.jpg", b"\xff\xd8\xff\xe0imgbytes", "image/jpeg")},
            )
            self.assertEqual(r_staff.status_code, 403)
        finally:
            if prev_key is None:
                os.environ.pop("GEMINI_API_KEY", None)
            else:
                os.environ["GEMINI_API_KEY"] = prev_key
            if prev_model is None:
                os.environ.pop("GEMINI_OCR_MODEL", None)
            else:
                os.environ["GEMINI_OCR_MODEL"] = prev_model
            if prev_thinking is None:
                os.environ.pop("GEMINI_OCR_THINKING_LEVEL", None)
            else:
                os.environ["GEMINI_OCR_THINKING_LEVEL"] = prev_thinking

    def test_receipt_refund_lines_all_split_types_and_ocr(self):
        import csv
        import io
        from unittest.mock import patch
        import httpx
        from homestead.db.batches import list_batch_feed_logs, list_batches
        from homestead.db.eggs import list_layer_feed_logs
        from homestead.db.expenses import list_farm_expenses

        self.c.post(
            "/register",
            data={"display_name": "Steve", "username": "steve", "password": "secret"},
            follow_redirects=False,
        )
        r_b = self.c.post(
            "/batches/save",
            data={
                "name": "Broilers 2026",
                "species": "cornish cross",
                "start_date": "2026-10-01",
                "initial_quantity": "25",
                "unit_cost": "3.50",
                "avg_dressed_weight": "4.5",
                "sale_price_per_lb": "5.50",
                "processing_cost": "5.00",
                "lifecycle_type": "live_arrival",
                "next_url": "/batches",
            },
            follow_redirects=False,
        )
        self.assertEqual(r_b.status_code, 303)
        batch_id = list_batches(user_id=1)[0]["id"]

        # 1. Verify Gemini OCR handles mixed purchase+refund receipts, sign normalization, and standalone returns
        prev_key = os.environ.pop("GEMINI_API_KEY", None)
        try:
            os.environ["GEMINI_API_KEY"] = "secret-gemini-test-key"

            mixed_ocr = {
                "date": "2026-11-27",
                "vendor": "Tractor Supply",
                "receipt_total": 47.00,
                "tax_charged": 0.0,
                "mixed_use_warning": False,
                "splits": [
                    {
                        "split_type": "general_farm_expense",
                        "amount": 100.00,
                        "notes": "Fencing wire & clips",
                        "schedule_f_category": "supplies",
                        "feed_type": None,
                        "bags": None,
                        "total_weight_lb": None,
                    },
                    {
                        "split_type": "general_farm_expense",
                        "amount": -25.00,
                        "notes": "Refund: Damaged bucket",
                        "schedule_f_category": "supplies",
                        "feed_type": None,
                        "bags": None,
                        "total_weight_lb": None,
                    },
                    {
                        "split_type": "meat_batch_feed",
                        "amount": -22.00,
                        "notes": "Refund: Returned 1x 50lb grower",
                        "schedule_f_category": None,
                        "feed_type": "grower",
                        "bags": 1,  # intentionally positive in raw OCR to verify sign normalization
                        "total_weight_lb": 50,
                    },
                    {
                        "split_type": "laying_flock_feed",
                        "amount": -6.00,
                        "notes": "Refund: Layer feed price adjustment",
                        "schedule_f_category": None,
                        "feed_type": "layer",
                        "bags": None,
                        "total_weight_lb": None,
                    },
                ],
            }
            standalone_return_ocr = {
                "date": "2026-11-27",
                "vendor": "Runnings",
                "receipt_total": -45.00,
                "tax_charged": 0.0,
                "mixed_use_warning": False,
                "splits": [],
            }
            captured_prompts: list[str] = []
            responses = iter([mixed_ocr, standalone_return_ocr])

            class _FakeResp:
                def __init__(self, status_code=200, body=None):
                    self.status_code = status_code
                    self._body = body or {}

                def json(self):
                    return self._body

            async def _fake_post(self_client, url, **kwargs):
                captured_prompts.append(kwargs["json"]["systemInstruction"]["parts"][0]["text"])
                payload = next(responses)
                return _FakeResp(
                    200,
                    {"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]},
                )

            with patch.object(httpx.AsyncClient, "post", _fake_post):
                r_mix = self.c.post(
                    "/api/receipts/parse",
                    files={"file": ("mixed.jpg", b"\xff\xd8\xff\xe0imgbytes", "image/jpeg")},
                )
                self.assertEqual(r_mix.status_code, 200)
                parsed_mix = r_mix.json()
                self.assertAlmostEqual(parsed_mix["receipt_total"], 47.00, places=2)
                self.assertEqual(len(parsed_mix["splits"]), 4)
                self.assertAlmostEqual(parsed_mix["splits"][1]["amount"], -25.00, places=2)
                self.assertAlmostEqual(parsed_mix["splits"][2]["amount"], -22.00, places=2)
                self.assertAlmostEqual(parsed_mix["splits"][2]["bags"], -1.0, places=2)
                self.assertAlmostEqual(parsed_mix["splits"][2]["total_weight_lb"], -50.0, places=2)
                self.assertAlmostEqual(parsed_mix["splits"][3]["amount"], -6.00, places=2)
                self.assertIsNone(parsed_mix["splits"][3]["bags"])

                r_ret = self.c.post(
                    "/api/receipts/parse",
                    files={"file": ("return.jpg", b"\xff\xd8\xff\xe0imgbytes", "image/jpeg")},
                )
                self.assertEqual(r_ret.status_code, 200)
                parsed_ret = r_ret.json()
                self.assertAlmostEqual(parsed_ret["receipt_total"], -45.00, places=2)
                self.assertEqual(len(parsed_ret["splits"]), 1)
                self.assertAlmostEqual(parsed_ret["splits"][0]["amount"], -45.00, places=2)
                self.assertTrue(parsed_ret["splits"][0]["notes"].startswith("Refund: Runnings"))

            self.assertIn("Neighbor's Club Rewards", captured_prompts[0])
            self.assertIn("Refund: ", captured_prompts[0])
        finally:
            if prev_key is None:
                os.environ.pop("GEMINI_API_KEY", None)
            else:
                os.environ["GEMINI_API_KEY"] = prev_key

        # 2. Submit baseline purchases + mixed refund splits across all 3 split types + standalone return
        r_base = self.c.post(
            "/expenses/receipt",
            data={
                "receipt_date": "2026-11-10",
                "vendor": "Tractor Supply",
                "lines_json": json.dumps(
                    [
                        {
                            "kind": "batch_feed",
                            "batch_id": str(batch_id),
                            "feed_type": "grower",
                            "bags": 4,
                            "feed_lbs": 200,
                            "amount": 88.00,
                            "notes": "4x 50lb grower",
                        },
                        {
                            "kind": "layer_feed",
                            "feed_type": "layer",
                            "species_tags": ["all"],
                            "bags": 2,
                            "feed_lbs": 100,
                            "amount": 44.00,
                            "notes": "2x 50lb layer",
                        },
                    ]
                ),
            },
            follow_redirects=False,
        )
        self.assertEqual(r_base.status_code, 303)

        r_mixed_save = self.c.post(
            "/expenses/receipt",
            data={
                "receipt_date": "2026-11-27",
                "vendor": "Tractor Supply",
                "lines_json": json.dumps(
                    [
                        {
                            "kind": "general",
                            "category": "Supplies & Equipment",
                            "amount": 100.00,
                            "notes": "Fencing wire & clips",
                        },
                        {
                            "kind": "general",
                            "category": "Supplies & Equipment",
                            "amount": -25.00,
                            "notes": "Refund: Damaged bucket",
                        },
                        {
                            "kind": "batch_feed",
                            "batch_id": str(batch_id),
                            "feed_type": "grower",
                            "bags": 1,  # route enforces negative sign when amount < 0
                            "feed_lbs": 50,
                            "amount": -22.00,
                            "notes": "Refund: Returned 1x 50lb grower",
                        },
                        {
                            "kind": "layer_feed",
                            "feed_type": "layer",
                            "species_tags": ["all"],
                            "bags": 0,
                            "feed_lbs": 0,
                            "amount": -6.00,
                            "notes": "Refund: Layer price adjustment",
                        },
                    ]
                ),
            },
            follow_redirects=False,
        )
        self.assertEqual(r_mixed_save.status_code, 303)

        r_ret_save = self.c.post(
            "/expenses/receipt",
            data={
                "receipt_date": "2026-11-28",
                "vendor": "Runnings",
                "lines_json": json.dumps(
                    [
                        {
                            "kind": "general",
                            "category": "Supplies & Equipment",
                            "amount": -45.00,
                            "notes": "Refund: Extra waterer returned",
                        }
                    ]
                ),
            },
            follow_redirects=False,
        )
        self.assertEqual(r_ret_save.status_code, 303)
        self.assertIn("Saved+receipt+%28-%2445.00%29.", r_ret_save.headers["location"])

        # 3. Verify DB rows store signed negative amounts & feed bags/lbs without clamping
        gen_rows = list_farm_expenses(user_id=1)
        self.assertEqual(len(gen_rows), 3)
        self.assertIn(-45.00, [round(r["amount"], 2) for r in gen_rows])
        self.assertIn(-25.00, [round(r["amount"], 2) for r in gen_rows])

        batch_rows = list_batch_feed_logs(batch_id)
        ret_batch_row = next(r for r in batch_rows if r["cost_total"] < 0)
        self.assertAlmostEqual(ret_batch_row["cost_total"], -22.00, places=2)
        self.assertAlmostEqual(ret_batch_row["bags"], -1.0, places=2)
        self.assertAlmostEqual(ret_batch_row["feed_lbs"], -50.0, places=2)

        layer_rows = list_layer_feed_logs(user_id=1, limit=50)
        ret_layer_row = next(r for r in layer_rows if r["cost_total"] < 0)
        self.assertAlmostEqual(ret_layer_row["cost_total"], -6.00, places=2)
        self.assertAlmostEqual(ret_layer_row["bags"], 0.0, places=2)
        self.assertAlmostEqual(ret_layer_row["feed_lbs"], 0.0, places=2)

        # 4. Verify Schedule F Tax Books, negative formatting (-$ vs $-), Refund pill, Batch/Eggs pages & CSV
        fin_html = self.c.get("/financials?tab=taxes&year=2026").text
        self.assertNotIn("$-", fin_html)
        self.assertIn("-$45.00", fin_html)
        self.assertIn("-$25.00", fin_html)
        self.assertIn("-$22.00", fin_html)
        self.assertIn("-$6.00", fin_html)
        self.assertIn('<span class="pill blue">Refund</span>', fin_html)
        # Net Line 28 Supplies = 100 - 25 - 45 = $30.00; Net Line 16 Feed = 88 - 22 + 44 - 6 = $104.00; Net total = $134.00
        self.assertIn("$30.00", fin_html)
        self.assertIn("$104.00", fin_html)
        self.assertIn("$134.00", fin_html)

        batch_html = self.c.get(f"/batches/{batch_id}").text
        self.assertNotIn("$-", batch_html)
        self.assertIn("-$22.00", batch_html)
        self.assertIn('<span class="pill blue">Refund</span>', batch_html)
        self.assertIn("150.0 lb ($66.00)", batch_html)

        eggs_html = self.c.get("/eggs?tab=flock").text
        self.assertNotIn("$-", eggs_html)
        self.assertIn("-$6.00", eggs_html)
        self.assertIn('<span class="pill blue">Refund</span>', eggs_html)
        self.assertIn("$38.00 (100.0 lb)", eggs_html)

        csv_resp = self.c.get("/financials/expenses.csv?year=2026")
        self.assertEqual(csv_resp.status_code, 200)
        csv_rows = list(csv.DictReader(io.StringIO(csv_resp.text)))
        csv_amounts = sorted(float(r["Amount ($)"]) for r in csv_rows)
        self.assertEqual(csv_amounts, [-45.0, -25.0, -22.0, -6.0, 44.0, 88.0, 100.0])

    def test_multibatch_allocation_timeframe_filter_and_edit_receipt(self):
        from homestead.db.batches import list_batch_feed_logs, list_batches
        from homestead.db.expenses import list_farm_expenses
        from homestead.receipt_allocation import allocate_signed_total

        # 1. Exact largest-remainder math for positive charges & negative refunds
        pos_shares = allocate_signed_total(100.00, [1, 1, 1], decimals=2)
        self.assertEqual(pos_shares, [33.34, 33.33, 33.33])
        self.assertAlmostEqual(sum(pos_shares), 100.00, places=2)

        neg_shares = allocate_signed_total(-100.00, [1, 1, 1], decimals=2)
        self.assertEqual(neg_shares, [-33.34, -33.33, -33.33])
        self.assertAlmostEqual(sum(neg_shares), -100.00, places=2)

        self.assertEqual(allocate_signed_total(132.00, [60, 40], decimals=2), [79.20, 52.80])

        self.c.post(
            "/register",
            data={"display_name": "Steve", "username": "steve", "password": "secret"},
            follow_redirects=False,
        )

        # Create overlapping + future batches
        for b_payload in (
            {
                "name": "Turkeys 2026",
                "species": "turkey chicks",
                "start_date": "2026-06-01",
                "initial_quantity": "15",
                "unit_cost": "12.00",
                "avg_dressed_weight": "16.0",
                "sale_price_per_lb": "7.50",
                "processing_cost": "15.00",
                "lifecycle_type": "direct_arrival",
                "next_url": "/batches",
            },
            {
                "name": "Cornish Cross Aug",
                "species": "cornish cross",
                "start_date": "2026-07-20",
                "initial_quantity": "50",
                "unit_cost": "4.00",
                "avg_dressed_weight": "4.5",
                "sale_price_per_lb": "6.50",
                "processing_cost": "5.00",
                "lifecycle_type": "direct_arrival",
                "next_url": "/batches",
            },
            {
                "name": "Future 2027",
                "species": "cornish cross",
                "start_date": "2027-04-01",
                "initial_quantity": "40",
                "unit_cost": "4.00",
                "avg_dressed_weight": "4.5",
                "sale_price_per_lb": "6.50",
                "processing_cost": "5.00",
                "lifecycle_type": "direct_arrival",
                "next_url": "/batches",
            },
        ):
            r_b = self.c.post("/batches/save", data=b_payload, follow_redirects=False)
            self.assertEqual(r_b.status_code, 303)

        batches_by_name = {b["name"]: b["id"] for b in list_batches(user_id=1)}
        turkey_id = batches_by_name["Turkeys 2026"]
        cx_id = batches_by_name["Cornish Cross Aug"]
        future_id = batches_by_name["Future 2027"]

        # 2. Verify UI embeds timeframe-aware batch options, show-all toggle & edit triggers
        fin_html = self.c.get("/financials?tab=taxes&year=2026").text
        self.assertIn("FIN_RECEIPT_BATCHES", fin_html)
        self.assertIn('id="rcpt-show-all-batches"', fin_html)
        self.assertIn("openEditReceiptModal", fin_html)
        self.assertIn("grower_weekly_feed", fin_html)
        self.assertIn("2026-07-20", fin_html)
        self.assertIn("2027-04-01", fin_html)

        # 3. Log multi-batch receipt on 2026-08-15 (6 bags grower feed split 60/40 + $50 bedding split 50/50)
        r_multi = self.c.post(
            "/expenses/receipt",
            data={
                "receipt_date": "2026-08-15",
                "vendor": "Tractor Supply",
                "lines_json": json.dumps(
                    [
                        {
                            "kind": "batch_feed",
                            "feed_type": "grower",
                            "bags": 6,
                            "feed_lbs": 300,
                            "amount": 132.00,
                            "notes": "Grower shared run",
                            "alloc_mode": "custom",
                            "allocations": [
                                {"batch_id": turkey_id, "weight": 60},
                                {"batch_id": cx_id, "weight": 40},
                            ],
                        },
                        {
                            "kind": "general",
                            "category": "Supplies & Equipment",
                            "amount": 50.00,
                            "notes": "Pine shavings bales",
                            "alloc_mode": "even",
                            "allocations": [
                                {"batch_id": turkey_id, "weight": 1},
                                {"batch_id": cx_id, "weight": 1},
                            ],
                        },
                    ]
                ),
            },
            follow_redirects=False,
        )
        self.assertEqual(r_multi.status_code, 303)
        self.assertIn("Saved+split+receipt+%284+items%2C+%24182.00%29.", r_multi.headers["location"])

        t_feeds = list_batch_feed_logs(turkey_id)
        cx_feeds = list_batch_feed_logs(cx_id)
        self.assertEqual(len(t_feeds), 1)
        self.assertEqual(len(cx_feeds), 1)
        self.assertAlmostEqual(t_feeds[0]["bags"], 3.6, places=2)
        self.assertAlmostEqual(t_feeds[0]["feed_lbs"], 180.0, places=1)
        self.assertAlmostEqual(t_feeds[0]["cost_total"], 79.20, places=2)
        self.assertAlmostEqual(cx_feeds[0]["bags"], 2.4, places=2)
        self.assertAlmostEqual(cx_feeds[0]["feed_lbs"], 120.0, places=1)
        self.assertAlmostEqual(cx_feeds[0]["cost_total"], 52.80, places=2)
        shared_gid = t_feeds[0]["receipt_group_id"]
        self.assertTrue(shared_gid)
        self.assertEqual(cx_feeds[0]["receipt_group_id"], shared_gid)

        gen_rows = list_farm_expenses(user_id=1)
        self.assertEqual(len(gen_rows), 2)
        self.assertEqual({r["receipt_group_id"] for r in gen_rows}, {shared_gid})
        self.assertEqual(sorted(r["amount"] for r in gen_rows), [25.0, 25.0])

        fin_after = self.c.get("/financials?tab=taxes&year=2026").text
        self.assertIn("$182.00", fin_after)
        self.assertIn("Edit receipt (4)", fin_after)

        # 4. Create a single entry with an existing Drive receipt URL, then edit/re-allocate into 2 batches
        r_single = self.c.post(
            "/expenses/receipt",
            data={
                "receipt_date": "2026-08-20",
                "vendor": "Runnings",
                "receipt_url": "https://drive.google.com/file/d/orig123/view",
                "lines_json": json.dumps(
                    [
                        {
                            "kind": "general",
                            "category": "Feed",
                            "amount": 90.00,
                            "notes": "Mislogged feed",
                        }
                    ]
                ),
            },
            follow_redirects=False,
        )
        self.assertEqual(r_single.status_code, 303)
        single_exp = next(r for r in list_farm_expenses(user_id=1) if r["vendor"] == "Runnings")

        api_single = self.c.get(
            f"/api/receipts/entry?source_key=general&entry_id={single_exp['id']}"
        )
        self.assertEqual(api_single.status_code, 200)
        single_data = api_single.json()
        self.assertEqual(single_data["edit_mode"], "single")
        self.assertEqual(single_data["receipt_url"], "https://drive.google.com/file/d/orig123/view")

        # Re-allocate that old single general expense into 2 batch feed rows (including future batch), preserving URL
        r_edit_single = self.c.post(
            "/expenses/receipt",
            data={
                "edit_mode": "single",
                "orig_source_key": "general",
                "orig_entry_id": str(single_exp["id"]),
                "receipt_date": "2026-08-20",
                "vendor": "Runnings",
                "lines_json": json.dumps(
                    [
                        {
                            "kind": "batch_feed",
                            "feed_type": "grower",
                            "bags": 4,
                            "feed_lbs": 200,
                            "amount": 90.00,
                            "notes": "Reallocated feed",
                            "alloc_mode": "custom",
                            "allocations": [
                                {"batch_id": cx_id, "weight": 3},
                                {"batch_id": future_id, "weight": 1},
                            ],
                        }
                    ]
                ),
            },
            follow_redirects=False,
        )
        self.assertEqual(r_edit_single.status_code, 303)
        self.assertIn("Updated+split+receipt+%282+items%2C+%2490.00%29.", r_edit_single.headers["location"])

        # Old general row gone; 2 new batch feed rows preserve existing receipt URL cleanly
        self.assertFalse(any(r["id"] == single_exp["id"] for r in list_farm_expenses(user_id=1)))
        fut_feeds = list_batch_feed_logs(future_id)
        cx_realloc = next(r for r in list_batch_feed_logs(cx_id) if r["vendor"] == "Runnings")
        self.assertEqual(len(fut_feeds), 1)
        self.assertAlmostEqual(cx_realloc["cost_total"], 67.50, places=2)
        self.assertAlmostEqual(fut_feeds[0]["cost_total"], 22.50, places=2)
        self.assertEqual(cx_realloc["receipt_url"], "https://drive.google.com/file/d/orig123/view")
        self.assertEqual(fut_feeds[0]["receipt_url"], "https://drive.google.com/file/d/orig123/view")
        self.assertTrue(cx_realloc["receipt_group_id"])
        self.assertEqual(cx_realloc["receipt_group_id"], fut_feeds[0]["receipt_group_id"])

        # 5. Edit existing 4-line split group cleanly (adjust amount & include a refund line)
        api_group = self.c.get(f"/api/receipts/entry?receipt_group_id={shared_gid}")
        self.assertEqual(api_group.status_code, 200)
        grp_data = api_group.json()
        self.assertEqual(grp_data["edit_mode"], "group")
        self.assertEqual(len(grp_data["lines"]), 4)

        r_edit_grp = self.c.post(
            "/expenses/receipt",
            data={
                "edit_mode": "group",
                "orig_group_id": shared_gid,
                "receipt_date": "2026-08-15",
                "vendor": "Tractor Supply",
                "lines_json": json.dumps(
                    [
                        {
                            "kind": "batch_feed",
                            "batch_id": str(turkey_id),
                            "feed_type": "grower",
                            "bags": 4,
                            "feed_lbs": 200,
                            "amount": 88.00,
                            "notes": "Updated Turkey share",
                        },
                        {
                            "kind": "general",
                            "batch_id": str(cx_id),
                            "category": "Supplies & Equipment",
                            "amount": -10.00,
                            "notes": "Refund: Damaged feeder",
                        },
                    ]
                ),
            },
            follow_redirects=False,
        )
        self.assertEqual(r_edit_grp.status_code, 303)
        self.assertIn("Updated+split+receipt+%282+items%2C+%2478.00%29.", r_edit_grp.headers["location"])
        t_after = list_batch_feed_logs(turkey_id)
        self.assertEqual(len(t_after), 1)
        self.assertAlmostEqual(t_after[0]["cost_total"], 88.00, places=2)
        gen_after = list_farm_expenses(user_id=1)
        self.assertEqual(len(gen_after), 1)
        self.assertAlmostEqual(gen_after[0]["amount"], -10.00, places=2)

        # 6. Staff (403) and other user's batch/entry (404) are rejected
        self.c.post(
            "/settings/members",
            data={"username": "helper", "display_name": "Helper", "password": "secret", "role": "staff"},
            follow_redirects=False,
        )
        c_staff = TestClient(app)
        c_staff.post("/login", data={"username": "helper", "password": "secret"}, follow_redirects=False)
        self.assertEqual(
            c_staff.get(f"/api/receipts/entry?receipt_group_id={shared_gid}").status_code,
            403,
        )
        self.c.post(
            "/settings/admin/users",
            data={"username": "other", "display_name": "Other Farm", "password": "secret"},
            follow_redirects=False,
        )
        c_other = TestClient(app)
        c_other.post(
            "/login",
            data={"username": "other", "password": "secret"},
            follow_redirects=False,
        )
        self.assertEqual(
            c_other.get(f"/api/receipts/entry?receipt_group_id={shared_gid}").status_code,
            404,
        )
        self.assertEqual(
            c_other.post(
                "/expenses/receipt",
                data={
                    "receipt_date": "2026-08-15",
                    "vendor": "Intruder",
                    "lines_json": json.dumps(
                        [
                            {
                                "kind": "general",
                                "category": "Supplies & Equipment",
                                "amount": 10.0,
                                "batch_id": str(turkey_id),
                            }
                        ]
                    ),
                },
                follow_redirects=False,
            ).status_code,
            404,
        )

    def test_perennial_herds_breeding_pigs_alpacas_feed_hay_ocr_and_litter(self):
        from datetime import date
        from types import SimpleNamespace
        from unittest.mock import patch

        import httpx

        from homestead.db.herds import get_herd, list_herd_animals, list_herd_feed_logs, list_herds

        self.c.post(
            "/register",
            data={"display_name": "Steve", "username": "steve", "password": "secret"},
            follow_redirects=False,
        )

        # 1. Create Breeding Pigs & Alpaca Herd
        r_pig = self.c.post(
            "/herds/save",
            data={
                "name": "Breeding Pigs",
                "species": "pig",
                "herd_type": "breeding",
                "head_count": "2",
                "notes": "Kunekune breeding pair",
            },
            follow_redirects=False,
        )
        self.assertEqual(r_pig.status_code, 303)

        r_alp = self.c.post(
            "/herds/save",
            data={
                "name": "Alpaca Herd",
                "species": "alpaca",
                "herd_type": "fiber",
                "head_count": "2",
                "notes": "Annual shearing fiber flock",
            },
            follow_redirects=False,
        )
        self.assertEqual(r_alp.status_code, 303)

        herds = list_herds(1)
        self.assertEqual(len(herds), 2)
        by_name = {h["name"]: h for h in herds}
        pig_id = by_name["Breeding Pigs"]["id"]
        alpaca_id = by_name["Alpaca Herd"]["id"]

        # 2. Manage named animal roster & retire Cocoa on Alpaca Herd
        self.c.post(
            f"/herds/{pig_id}/animals/save",
            data={
                "name": "Penelope",
                "sex_role": "sow",
                "color": "Black & White",
                "birth_year": "2024",
                "notes": "Breeding sow",
            },
            follow_redirects=False,
        )
        self.c.post(
            f"/herds/{pig_id}/animals/save",
            data={
                "name": "Wilbur",
                "sex_role": "boar",
                "color": "Ginger",
                "birth_year": "2024",
                "notes": "Breeding boar",
            },
            follow_redirects=False,
        )
        self.c.post(
            f"/herds/{alpaca_id}/animals/save",
            data={
                "name": "Luna",
                "sex_role": "female",
                "color": "White",
                "birth_year": "2022",
                "notes": "Fine white fleece",
            },
            follow_redirects=False,
        )
        self.c.post(
            f"/herds/{alpaca_id}/animals/save",
            data={
                "name": "Cocoa",
                "sex_role": "gelding",
                "color": "Dark Chocolate",
                "birth_year": "2021",
                "notes": "Retired",
            },
            follow_redirects=False,
        )

        alp_animals = list_herd_animals(1, herd_id=alpaca_id, include_inactive=True)
        self.assertEqual(len(alp_animals), 2)
        cocoa = next(a for a in alp_animals if a["name"] == "Cocoa")
        r_tog = self.c.post(
            f"/herds/{alpaca_id}/animals/{cocoa['id']}/toggle",
            data={"active": "0"},
            follow_redirects=False,
        )
        self.assertEqual(r_tog.status_code, 303)
        alp_after_retire = get_herd(1, alpaca_id)
        self.assertEqual(alp_after_retire["active_animal_count"], 1)
        self.assertEqual(alp_after_retire["effective_head_count"], 1)

        # 3. Log split receipt: 50/50 Hay Bales across both herds + signed Grain Bag refund for Pigs
        today = date.today()
        today_iso = today.isoformat()
        yr = today.year
        r_rec = self.c.post(
            "/expenses/receipt",
            data={
                "receipt_date": today_iso,
                "vendor": "Local Feed Mill",
                "receipt_url": "https://drive.google.com/file/d/herd-receipt-1/view",
                "lines_json": json.dumps(
                    [
                        {
                            "kind": "herd_feed",
                            "feed_category": "hay",
                            "unit": "bales",
                            "quantity": 10,
                            "weight_lbs": 500,
                            "amount": 95.00,
                            "notes": "First cutting square bales",
                            "allocations": [
                                {"herd_id": pig_id, "weight": 1},
                                {"herd_id": alpaca_id, "weight": 1},
                            ],
                        },
                        {
                            "kind": "herd_feed",
                            "feed_category": "grain",
                            "unit": "bags",
                            "quantity": -1,
                            "weight_lbs": -50,
                            "amount": -18.00,
                            "notes": "Refund: torn pig grower bag",
                            "herd_id": str(pig_id),
                        },
                    ]
                ),
            },
            follow_redirects=False,
        )
        self.assertEqual(r_rec.status_code, 303)

        pig_logs = list_herd_feed_logs(1, herd_id=pig_id)
        alp_logs = list_herd_feed_logs(1, herd_id=alpaca_id)
        self.assertEqual(len(pig_logs), 2)
        self.assertEqual(len(alp_logs), 1)
        self.assertAlmostEqual(sum(x["cost_total"] for x in pig_logs), 29.50, places=2)
        self.assertAlmostEqual(alp_logs[0]["cost_total"], 47.50, places=2)
        self.assertAlmostEqual(alp_logs[0]["quantity"], 5.0, places=2)
        self.assertAlmostEqual(alp_logs[0]["weight_lbs"], 250.0, places=2)
        self.assertEqual(alp_logs[0]["feed_category"], "hay")
        self.assertEqual(alp_logs[0]["unit"], "bales")
        self.assertEqual(alp_logs[0]["receipt_url"], "https://drive.google.com/file/d/herd-receipt-1/view")
        self.assertTrue(pig_logs[0]["receipt_group_id"])
        self.assertEqual(pig_logs[0]["receipt_group_id"], alp_logs[0]["receipt_group_id"])

        # 4. Verify /herds UI KPIs, Flocks & Herds subnav on /herds & /eggs, Tax Books & CSV
        herds_html = self.c.get(f"/herds?herd_id={pig_id}").text
        self.assertIn("Flocks &amp; Herds", herds_html)
        self.assertIn("Eggs &amp; Laying Flock", herds_html)
        self.assertIn("Perennial Herds", herds_html)
        self.assertIn("Penelope", herds_html)
        self.assertIn("Wilbur", herds_html)
        self.assertIn("$29.50", herds_html)
        self.assertIn(f"/batches?from_herd={pig_id}", herds_html)

        eggs_html = self.c.get("/eggs").text
        self.assertIn("Eggs &amp; Laying Flock", eggs_html)
        self.assertIn("Perennial Herds", eggs_html)

        fin_html = self.c.get(f"/financials?tab=taxes&year={yr}&source=feed").text
        self.assertIn("Herd Feed / Hay", fin_html)
        self.assertIn("FIN_HERDS", fin_html)
        self.assertIn("$77.00", fin_html)

        csv_resp = self.c.get(f"/financials/expenses.csv?year={yr}")
        self.assertEqual(csv_resp.status_code, 200)
        self.assertIn("Herd Feed", csv_resp.text)

        # 5. Edit single herd_feed entry & preserve existing Drive receipt URL
        alp_entry_id = alp_logs[0]["id"]
        api_single = self.c.get(f"/api/receipts/entry?source_key=herd_feed&entry_id={alp_entry_id}")
        self.assertEqual(api_single.status_code, 200)
        single_data = api_single.json()
        self.assertEqual(single_data["edit_mode"], "single")
        self.assertEqual(single_data["receipt_url"], "https://drive.google.com/file/d/herd-receipt-1/view")
        self.assertEqual(single_data["lines"][0]["kind"], "herd_feed")
        self.assertEqual(single_data["lines"][0]["herd_id"], alpaca_id)

        r_edit_single = self.c.post(
            "/expenses/receipt",
            data={
                "edit_mode": "single",
                "orig_source_key": "herd_feed",
                "orig_entry_id": str(alp_entry_id),
                "receipt_date": today_iso,
                "vendor": "Local Feed Mill",
                "lines_json": json.dumps(
                    [
                        {
                            "kind": "herd_feed",
                            "herd_id": str(alpaca_id),
                            "feed_category": "hay",
                            "unit": "bales",
                            "quantity": 6,
                            "weight_lbs": 300,
                            "amount": 55.00,
                            "notes": "Updated alpaca hay share",
                        }
                    ]
                ),
            },
            follow_redirects=False,
        )
        self.assertEqual(r_edit_single.status_code, 303)
        alp_after_edit = list_herd_feed_logs(1, herd_id=alpaca_id)
        self.assertEqual(len(alp_after_edit), 1)
        self.assertAlmostEqual(alp_after_edit[0]["cost_total"], 55.00, places=2)
        self.assertAlmostEqual(alp_after_edit[0]["quantity"], 6.0, places=2)
        self.assertEqual(
            alp_after_edit[0]["receipt_url"],
            "https://drive.google.com/file/d/herd-receipt-1/view",
        )

        # 6. Mock Gemini Vision OCR returning herd_feed hay + grain refund
        class _FakeResp:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload
                self.text = json.dumps(payload)

            def json(self):
                return self._payload

        mock_herd_ocr = {
            "date": today_iso,
            "vendor": "Local Feed Mill",
            "receipt_total": 77.00,
            "tax_charged": 0.0,
            "mixed_use_warning": False,
            "splits": [
                {
                    "split_type": "herd_feed",
                    "amount": 95.00,
                    "notes": "10 square hay bales",
                    "schedule_f_category": None,
                    "feed_type": "Timothy Grass Hay",
                    "feed_category": "hay",
                    "unit": "bales",
                    "quantity": 10,
                    "bags": None,
                    "total_weight_lb": 500,
                    "herd_hint": "Breeding Pigs, Alpaca Herd",
                },
                {
                    "split_type": "herd_feed",
                    "amount": -18.00,
                    "notes": "Refund damaged pig grower bag",
                    "schedule_f_category": None,
                    "feed_type": "Pig Grower",
                    "feed_category": "grain",
                    "unit": "bags",
                    "quantity": 1,
                    "bags": 1,
                    "total_weight_lb": 50,
                    "herd_hint": "Breeding Pigs",
                },
            ],
        }

        async def _fake_post(self_client, url, **kwargs):
            return _FakeResp(
                200,
                {
                    "candidates": [
                        {"content": {"parts": [{"text": json.dumps(mock_herd_ocr)}]}}
                    ]
                },
            )

        with patch.dict(os.environ, {"GEMINI_API_KEY": "secret-gemini-test-key"}):
            with patch.object(httpx.AsyncClient, "post", _fake_post):
                r_ocr = self.c.post(
                    "/api/receipts/parse",
                    files={"file": ("hay.jpg", b"\xff\xd8\xff\xe0imgbytes", "image/jpeg")},
                )
        self.assertEqual(r_ocr.status_code, 200)
        ocr_data = r_ocr.json()
        self.assertEqual(ocr_data["splits"][0]["split_type"], "herd_feed")
        self.assertEqual(ocr_data["splits"][0]["feed_category"], "hay")
        self.assertEqual(ocr_data["splits"][0]["unit"], "bales")
        self.assertEqual(ocr_data["splits"][0]["quantity"], 10)
        self.assertEqual(ocr_data["splits"][0]["total_weight_lb"], 500)
        self.assertEqual(ocr_data["splits"][0]["herd_hint"], "Breeding Pigs, Alpaca Herd")
        self.assertAlmostEqual(ocr_data["splits"][1]["amount"], -18.00, places=2)
        self.assertLess(ocr_data["splits"][1]["quantity"], 0)
        self.assertLess(ocr_data["splits"][1]["total_weight_lb"], 0)

        # 7. Pig Litter Batch helper pre-fills /batches?from_herd=<pig_id>
        batches_litter = self.c.get(f"/batches?from_herd={pig_id}")
        self.assertEqual(batches_litter.status_code, 200)
        self.assertIn("openNewBatchModal", batches_litter.text)
        self.assertIn("From Breeding Pigs", batches_litter.text)

        # 8. Role & workspace ownership guards (staff 200 GET / 403 POST, other 404)
        self.c.post(
            "/settings/members",
            data={"username": "helper", "display_name": "Helper", "password": "secret", "role": "staff"},
            follow_redirects=False,
        )
        c_staff = TestClient(app)
        c_staff.post("/login", data={"username": "helper", "password": "secret"}, follow_redirects=False)
        self.assertEqual(c_staff.get("/herds").status_code, 200)
        self.assertEqual(
            c_staff.post("/herds/save", data={"name": "Nope", "species": "pig"}, follow_redirects=False).status_code,
            403,
        )
        self.assertEqual(
            c_staff.post(
                f"/herds/{pig_id}/animals/save",
                data={"name": "Nope", "sex_role": "sow"},
                follow_redirects=False,
            ).status_code,
            403,
        )

        self.c.post(
            "/settings/admin/users",
            data={"username": "other", "display_name": "Other Farm", "password": "secret"},
            follow_redirects=False,
        )
        c_other = TestClient(app)
        c_other.post("/login", data={"username": "other", "password": "secret"}, follow_redirects=False)
        self.assertEqual(
            c_other.post(
                f"/herds/{pig_id}/animals/save",
                data={"name": "Intruder", "sex_role": "sow"},
                follow_redirects=False,
            ).status_code,
            404,
        )
        self.assertEqual(
            c_other.post(
                "/expenses/receipt",
                data={
                    "receipt_date": today_iso,
                    "vendor": "Intruder",
                    "lines_json": json.dumps(
                        [
                            {
                                "kind": "herd_feed",
                                "herd_id": str(pig_id),
                                "feed_category": "hay",
                                "unit": "bales",
                                "quantity": 1,
                                "amount": 10.0,
                            }
                        ]
                    ),
                },
                follow_redirects=False,
            ).status_code,
            404,
        )

    def test_pig_shares_preorders_hanging_weight_butcher_release_and_iron_ledger(self):
        from datetime import date

        from homestead.db.batches import list_batches
        from homestead.db.customers import list_batch_preorders, list_customers
        from homestead.db.shares import list_batch_harvest_shares
        from homestead.tax_books import build_unified_tax_books
        from shared.bridge import list_homestead_paid_transactions

        today_iso = date.today().isoformat()
        yr = date.today().year

        self.c.post(
            "/register",
            data={"display_name": "Steve", "username": "steve", "password": "secret"},
            follow_redirects=False,
        )

        # 1. Create a 3-pig batch in Share Mode with Customer Pays Butcher
        r_b = self.c.post(
            "/batches/save",
            data={
                "name": "Fall Kunekune Pigs",
                "species": "kunekune pigs",
                "start_date": f"{yr}-03-01",
                "initial_quantity": "3",
                "unit_cost": "150.00",
                "avg_dressed_weight": "140.0",
                "sale_price_per_lb": "6.50",
                "processing_cost": "175.00",
                "lifecycle_type": "mammal_breeding",
                "sale_unit": "shares",
                "customer_pays_butcher": "1",
                "next_url": "/batches",
            },
            follow_redirects=False,
        )
        self.assertEqual(r_b.status_code, 303)
        pig_batch = next(b for b in list_batches(user_id=1) if b["name"] == "Fall Kunekune Pigs")
        bid = int(pig_batch["id"])
        self.assertEqual(pig_batch["sale_unit"], "shares")
        self.assertEqual(int(pig_batch["customer_pays_butcher"]), 1)

        # 2. Configure Pre-Orders & Butcher Contact Info
        r_po_cfg = self.c.post(
            f"/batches/{bid}/preorders/settings",
            data={
                "preorder_enabled": "1",
                "preorder_cap": "3",
                "deposit_per_bird": "200.00",
                "sale_unit": "shares",
                "customer_pays_butcher": "1",
                "butcher_name": "Country Butcher",
                "butcher_phone": "555-999-0000",
                "butcher_email": "butcher@example.com",
                "pickup_date": today_iso,
            },
            follow_redirects=False,
        )
        self.assertEqual(r_po_cfg.status_code, 303)
        pig_batch = next(b for b in list_batches(user_id=1) if int(b["id"]) == bid)
        slug = pig_batch["picking_slug"]

        res_html = self.c.get(f"/reserve/{slug}")
        self.assertEqual(res_html.status_code, 200)
        self.assertIn("hanging weight", res_html.text)
        self.assertIn("3 pigs (6 halves) left", res_html.text)

        # 3. Public reservations fill 3.0 cap (Half + Half + Whole + Whole) and overflow to waitlist
        c_pub = TestClient(app)
        r_alice = c_pub.post(
            f"/api/reserve/{slug}",
            json={"name": "Alice", "contact": "555-100-0001", "quantity": 1, "share_size": "half"},
        )
        self.assertEqual(r_alice.status_code, 200)
        d_alice = r_alice.json()
        self.assertEqual(d_alice["status"], "reserved")
        self.assertAlmostEqual(d_alice["deposit_total"], 100.00, places=2)
        self.assertAlmostEqual(d_alice["share_fraction"], 0.5, places=2)

        r_bob = c_pub.post(
            f"/api/reserve/{slug}",
            json={"name": "Bob", "contact": "555-100-0002", "quantity": 1, "share_size": "half"},
        )
        self.assertEqual(r_bob.json()["status"], "reserved")

        r_charlie = c_pub.post(
            f"/api/reserve/{slug}",
            json={"name": "Charlie", "contact": "555-100-0003", "quantity": 1, "share_size": "whole"},
        )
        self.assertEqual(r_charlie.json()["status"], "reserved")
        self.assertAlmostEqual(r_charlie.json()["deposit_total"], 200.00, places=2)

        r_dana = c_pub.post(
            f"/api/reserve/{slug}",
            json={"name": "Dana", "contact": "555-100-0004", "quantity": 1, "share_size": "whole"},
        )
        self.assertEqual(r_dana.json()["status"], "reserved")

        r_eve = c_pub.post(
            f"/api/reserve/{slug}",
            json={"name": "Eve", "contact": "555-100-0005", "quantity": 1, "share_size": "half"},
        )
        self.assertEqual(r_eve.status_code, 200)
        self.assertEqual(r_eve.json()["status"], "waitlist")

        preorders = {p["customer_name"]: p for p in list_batch_preorders(bid)}
        for buyer in ("Alice", "Bob", "Charlie"):
            po_id = preorders[buyer]["id"]
            self.assertEqual(
                self.c.post(f"/batches/{bid}/preorders/{po_id}/mark-paid", data={}, follow_redirects=False).status_code,
                303,
            )

        custs = {c["name"]: int(c["id"]) for c in list_customers(1)}
        alice_id = custs["Alice"]
        bob_id = custs["Bob"]
        charlie_id = custs["Charlie"]

        # 4. Drop-off day logging with dressed_lbs=0 allowed in Share Mode
        r_h1 = self.c.post(
            f"/api/batches/{bid}/harvest",
            json={
                "harvest_date": today_iso,
                "animal_label": "Pig #1 (Spot)",
                "dressed_lbs": 0,
                "live_weight_lbs": 235,
                "split_mode": "halves",
                "weight_basis": "hanging",
                "for_sale": True,
            },
        )
        self.assertEqual(r_h1.status_code, 200)
        t1 = r_h1.json()
        self.assertEqual(t1["harvested_head"], 1)
        pig1 = t1["entries"][0]
        pig1_id = pig1["id"]
        half_a_id = pig1["shares"][0]["id"]
        half_b_id = pig1["shares"][1]["id"]

        # Assigning Alice & Bob auto-credits $100 pre-order deposits proportionally
        self.assertEqual(
            self.c.post(
                f"/api/batches/{bid}/shares/{half_a_id}/assign",
                json={"customer_id": alice_id, "for_sale": True},
            ).status_code,
            200,
        )
        r_bob_assign = self.c.post(
            f"/api/batches/{bid}/shares/{half_b_id}/assign",
            json={"customer_id": bob_id, "for_sale": True},
        )
        self.assertEqual(r_bob_assign.status_code, 200)

        shares_after_p1 = list_batch_harvest_shares(bid)
        sh_a = next(s for s in shares_after_p1 if int(s["id"]) == half_a_id)
        sh_b = next(s for s in shares_after_p1 if int(s["id"]) == half_b_id)
        self.assertAlmostEqual(sh_a["deposit_paid"], 100.00, places=2)
        self.assertAlmostEqual(sh_b["deposit_paid"], 100.00, places=2)
        self.assertEqual(sh_a["payment_status"], "deposit")

        # Merging 2 halves with two distinct customers is rejected (400)
        r_merge_err = self.c.post(
            f"/api/batches/{bid}/harvest/{pig1_id}/split-mode",
            json={"split_mode": "whole"},
        )
        self.assertEqual(r_merge_err.status_code, 400)

        # Pig #2 Whole carcass (0 lb pending) -> Charlie ($200 auto-deposit)
        r_h2 = self.c.post(
            f"/api/batches/{bid}/harvest",
            json={
                "harvest_date": today_iso,
                "animal_label": "Pig #2 (Ginger)",
                "dressed_lbs": 0,
                "split_mode": "whole",
                "weight_basis": "hanging",
            },
        )
        self.assertEqual(r_h2.status_code, 200)
        pig2 = next(e for e in r_h2.json()["entries"] if e["animal_label"] == "Pig #2 (Ginger)")
        pig2_id = pig2["id"]
        pig2_sh_id = pig2["shares"][0]["id"]
        self.c.post(
            f"/api/batches/{bid}/shares/{pig2_sh_id}/assign",
            json={"customer_id": charlie_id, "for_sale": True},
        )

        # Pig #3 Halves (130 lb hanging, farm-paid processing $175): Half A = Home Freezer, Half B = Available
        r_h3 = self.c.post(
            f"/api/batches/{bid}/harvest",
            json={
                "harvest_date": today_iso,
                "animal_label": "Pig #3 (Home)",
                "dressed_lbs": 130.0,
                "processing_cost_total": 175.00,
                "split_mode": "halves",
                "weight_basis": "hanging",
            },
        )
        self.assertEqual(r_h3.status_code, 200)
        pig3 = next(e for e in r_h3.json()["entries"] if e["animal_label"] == "Pig #3 (Home)")
        pig3_ha_id = pig3["shares"][0]["id"]
        r_freezer = self.c.post(
            f"/api/batches/{bid}/shares/{pig3_ha_id}/assign",
            json={"for_sale": False},
        )
        self.assertEqual(r_freezer.status_code, 200)
        tally_now = r_freezer.json()
        self.assertEqual(tally_now["harvested_head"], 3)
        self.assertEqual(len(tally_now["shares"]), 5)

        # 5. Butcher hanging weight callback updates prices & balances dynamically
        self.assertEqual(
            self.c.post(f"/api/batches/{bid}/harvest/{pig1_id}/update", json={"dressed_lbs": 144.0}).status_code,
            200,
        )
        r_upd2 = self.c.post(f"/api/batches/{bid}/harvest/{pig2_id}/update", json={"dressed_lbs": 150.0})
        self.assertEqual(r_upd2.status_code, 200)
        upd_shares = {int(s["id"]): s for s in r_upd2.json()["shares"]}
        self.assertAlmostEqual(upd_shares[half_a_id]["effective_lbs"], 72.0, places=2)
        self.assertAlmostEqual(upd_shares[half_a_id]["est_price"], 468.00, places=2)
        self.assertAlmostEqual(upd_shares[half_a_id]["balance_due"], 368.00, places=2)
        self.assertAlmostEqual(upd_shares[pig2_sh_id]["est_price"], 975.00, places=2)
        self.assertAlmostEqual(upd_shares[pig2_sh_id]["balance_due"], 775.00, places=2)

        # 6. Informational customer butcher fee ($185) excluded from Schedule F Processing; freezer half included
        r_bfee = self.c.post(
            f"/api/batches/{bid}/shares/{half_a_id}/assign",
            json={
                "customer_id": alice_id,
                "for_sale": True,
                "deposit_paid": 100.00,
                "payment_status": "deposit",
                "customer_butcher_fee": 185.00,
                "cut_sheet_notes": "No jowl, double smoked bacon",
            },
        )
        self.assertEqual(r_bfee.status_code, 200)
        books_before = build_unified_tax_books(1, merchant_id=1, tax_year=yr)
        proc_entries = [e for e in books_before["entries"] if e["source_key"] == "processing"]
        self.assertAlmostEqual(sum(e["amount"] for e in proc_entries), 175.00, places=2)
        self.assertAlmostEqual(books_before["totals"]["homestead_income"], 0.0, places=2)
        frz = next(f for f in books_before["freezer_notes"] if f["batch_name"] == "Fall Kunekune Pigs")
        self.assertAlmostEqual(frz["freezer_lbs"], 65.0, places=2)

        # 7. Pickup Day & Butcher Release + Iron Ledger verification (zero double-booking)
        pu_html = self.c.get(f"/batches/{bid}/pickup").text
        self.assertIn("Pickup Day &amp; Butcher Release", pu_html)
        self.assertIn("Country Butcher", pu_html)
        self.assertIn("$368.00", pu_html)

        r_settle = self.c.post(
            f"/batches/{bid}/pickup/{alice_id}/settle-shares",
            data={
                "collected_today": "368.00",
                "payment_method": "venmo",
                "action": "pay_only",
            },
            follow_redirects=False,
        )
        self.assertEqual(r_settle.status_code, 303)
        pu_after_pay = self.c.get(f"/batches/{bid}/pickup").text
        self.assertIn("Paid · Notify Butcher", pu_after_pay)
        self.assertIn("Copy All Paid Releases", pu_after_pay)

        ledger_txns = list_homestead_paid_transactions(merchant_id=1)
        self.assertEqual(len(ledger_txns), 1)
        self.assertEqual(ledger_txns[0]["record_type"], "share")
        self.assertEqual(ledger_txns[0]["total_cents"], 46800)
        self.assertEqual(ledger_txns[0]["pay_method"], "venmo")
        self.assertAlmostEqual(
            build_unified_tax_books(1, merchant_id=1, tax_year=yr)["totals"]["homestead_income"],
            468.00,
            places=2,
        )

        # Release All Paid releases ONLY Alice; Bob & Charlie remain un-released
        self.assertEqual(
            self.c.post(f"/batches/{bid}/pickup/butcher-release-all", follow_redirects=False).status_code,
            303,
        )
        shares_rel = {int(s["id"]): s for s in list_batch_harvest_shares(bid)}
        self.assertTrue(shares_rel[half_a_id]["butcher_notified_at"])
        self.assertFalse(shares_rel[half_b_id]["butcher_notified_at"])
        self.assertFalse(shares_rel[pig2_sh_id]["butcher_notified_at"])

        # Mark picked up, edit payment method -> check, check CRM & CSV
        self.assertEqual(
            self.c.post(
                f"/batches/{bid}/pickup/{alice_id}/settle-shares",
                data={"action": "mark_picked_up"},
                follow_redirects=False,
            ).status_code,
            303,
        )
        self.assertEqual(
            self.c.post(
                f"/batches/{bid}/pickup/{alice_id}/payment-method",
                data={"payment_method": "check"},
                follow_redirects=False,
            ).status_code,
            303,
        )
        ledger_after_pm = list_homestead_paid_transactions(merchant_id=1)
        self.assertEqual(len(ledger_after_pm), 1)
        self.assertEqual(ledger_after_pm[0]["pay_method"], "check")

        crm_html = self.c.get(f"/customers?selected={alice_id}")
        self.assertEqual(crm_html.status_code, 200)
        self.assertIn("Pig #1 (Spot) · Half A", crm_html.text)
        self.assertIn("Butcher Released", crm_html.text)

        csv_resp = self.c.get(f"/batches/{bid}/harvest.csv")
        self.assertEqual(csv_resp.status_code, 200)
        self.assertIn("Pig #1 (Spot)", csv_resp.text)
        self.assertIn("Freezer / Keep", csv_resp.text)

        # Public /pick/<slug> shows Share Mode safely when enabled (zero buyer PII) & supports atomic claim
        self.c.post(f"/api/batches/{bid}/picking", json={"picking_enabled": True})
        pick_pub = c_pub.get(f"/pick/{slug}")
        self.assertEqual(pick_pub.status_code, 200)
        self.assertIn("Pig #3 (Home)", pick_pub.text)
        self.assertIn("Half B", pick_pub.text)
        self.assertNotIn("Alice", pick_pub.text)

        pig3_hb_id = pig3["shares"][1]["id"]
        id_resp = c_pub.post(
            f"/api/pick/{slug}/identify",
            json={"name": "Dana", "contact": "555-100-0004"},
        )
        self.assertEqual(id_resp.status_code, 200)
        claim_resp = c_pub.post(
            f"/api/pick/{slug}/claim/{pig3_hb_id}",
            json={"customer_id": id_resp.json()["customer_id"]},
        )
        self.assertEqual(claim_resp.status_code, 200)
        self.assertEqual(claim_resp.json()["bird"]["label"], "Pig #3 (Home) · Half B")

        # 8. Backward compatibility: Whole-bird poultry batch still rejects 0 lb
        self.c.post(
            "/batches/save",
            data={
                "name": "Spring Broilers",
                "species": "chicken chicks",
                "start_date": f"{yr}-04-01",
                "initial_quantity": "10",
                "sale_price_per_lb": "6.50",
            },
            follow_redirects=False,
        )
        poultry_id = next(int(b["id"]) for b in list_batches(user_id=1) if b["name"] == "Spring Broilers")
        self.assertEqual(
            self.c.post(
                f"/api/batches/{poultry_id}/harvest",
                json={"harvest_date": today_iso, "head_count": 1, "dressed_lbs": 0},
            ).status_code,
            400,
        )
        self.assertEqual(
            self.c.post(
                f"/api/batches/{poultry_id}/harvest",
                json={"harvest_date": today_iso, "head_count": 1, "dressed_lbs": 4.8},
            ).status_code,
            200,
        )

    def test_alpaca_shearing_mill_lots_yarn_conversion_tax_books_and_iron_ledger(self):
        from datetime import date, datetime

        from homestead.db.fiber import (
            FIBER_SKU_KEYWORDS,
            compute_fiber_dashboard,
            list_fiber_mill_lots,
            list_fiber_shearings,
        )
        from homestead.db.herds import list_herd_animals, list_herds
        from homestead.tax_books import build_unified_tax_books
        from shared.bridge import list_active_ledger_skus
        from shared.db.connection import get_connection

        today_iso = date.today().isoformat()
        yr = date.today().year

        self.c.post(
            "/register",
            data={"display_name": "Steve", "username": "steve", "password": "secret"},
            follow_redirects=False,
        )

        # 1. Create Alpaca Herd with two differently colored named alpacas
        self.c.post(
            "/herds/save",
            data={
                "name": "Alpaca Herd",
                "species": "alpaca",
                "herd_type": "fiber",
                "head_count": "2",
            },
            follow_redirects=False,
        )
        alp_id = list_herds(1)[0]["id"]
        self.c.post(
            f"/herds/{alp_id}/animals/save",
            data={"name": "Luna", "sex_role": "female", "color": "White", "birth_year": "2022"},
            follow_redirects=False,
        )
        self.c.post(
            f"/herds/{alp_id}/animals/save",
            data={"name": "Cocoa", "sex_role": "female", "color": "Dark Chocolate", "birth_year": "2021"},
            follow_redirects=False,
        )
        animals = {a["name"]: a for a in list_herd_animals(1, herd_id=alp_id)}
        luna_id = animals["Luna"]["id"]
        cocoa_id = animals["Cocoa"]["id"]

        # Seed Iron Ledger SKUs (Goat Milk Soap first alphabetically, Alpaca Yarn second)
        now_iso = datetime.utcnow().isoformat(timespec="seconds")
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO skus (merchant_id, zettle_product_uuid, zettle_variant_uuid,
                                  name, variant_name, sku_code, category, price_cents,
                                  currency, active, sell_mode, updated_at)
                VALUES (1, 'p-soap', '', 'Goat Milk Soap', '', '', 'Bath', 800,
                        'USD', 1, 'unit', ?)
                """,
                (now_iso,),
            )
            yarn_sku_id = int(
                conn.execute(
                    """
                    INSERT INTO skus (merchant_id, zettle_product_uuid, zettle_variant_uuid,
                                      name, variant_name, sku_code, category, price_cents,
                                      currency, active, sell_mode, updated_at)
                    VALUES (1, 'p-yarn', 'v-luna-white', 'Alpaca Yarn', 'Luna White DK', '', 'Fiber', 2800,
                            'USD', 1, 'unit', ?)
                    """,
                    (now_iso,),
                ).lastrowid
            )
            conn.commit()

        ordered_skus = list_active_ledger_skus(merchant_id=1, prefer_keywords=FIBER_SKU_KEYWORDS)
        self.assertEqual(ordered_skus[0]["id"], yarn_sku_id)

        # 2. Log annual shearings (colors auto-fill from named alpacas)
        self.c.post(
            f"/herds/{alp_id}/shearings/add",
            data={
                "animal_id": str(luna_id),
                "shear_date": today_iso,
                "fleece_color": "",
                "prime_lbs": "5.0",
                "seconds_lbs": "1.0",
                "shearing_fee": "35.00",
                "notes": "Clean first clip",
            },
            follow_redirects=False,
        )
        self.c.post(
            f"/herds/{alp_id}/shearings/add",
            data={
                "animal_id": str(cocoa_id),
                "shear_date": today_iso,
                "fleece_color": "",
                "prime_lbs": "4.5",
                "seconds_lbs": "0",
                "shearing_fee": "35.00",
            },
            follow_redirects=False,
        )

        shearings = {s["animal_name"]: s for s in list_fiber_shearings(1, herd_id=alp_id)}
        self.assertEqual(shearings["Luna"]["fleece_color"], "White")
        self.assertAlmostEqual(shearings["Luna"]["total_lbs"], 6.0, places=2)
        self.assertEqual(shearings["Cocoa"]["fleece_color"], "Dark Chocolate")

        sh_html = self.c.get(f"/herds?herd_id={alp_id}&tab=shearing")
        self.assertEqual(sh_html.status_code, 200)
        self.assertIn("10.5 lb", sh_html.text)
        self.assertIn("White", sh_html.text)
        self.assertIn("Dark Chocolate", sh_html.text)

        luna_sh_id = shearings["Luna"]["id"]
        cocoa_sh_id = shearings["Cocoa"]["id"]

        # 3. Over-allocation (> 6.0 lb) is rejected gracefully
        r_over = self.c.post(
            f"/herds/{alp_id}/mill-lots/save",
            data={
                "lot_name": "Too Heavy",
                "inputs_submitted": "1",
                "shearing_ids": [str(luna_sh_id)],
                f"raw_lbs_{luna_sh_id}": "7.0",
            },
            follow_redirects=False,
        )
        self.assertEqual(r_over.status_code, 303)
        self.assertIn("Error", r_over.headers["location"])

        # 4. Create color-separated White mill lot (5.0 lb prime out of 6.0 lb shorn; 1.0 lb remains)
        r_lot1 = self.c.post(
            f"/herds/{alp_id}/mill-lots/save",
            data={
                "lot_name": "2026 Luna White DK Yarn",
                "mill_name": "Battenkill Fibers",
                "status": "sent_to_mill",
                "sent_date": today_iso,
                "inputs_submitted": "1",
                "shearing_ids": [str(luna_sh_id)],
                f"raw_lbs_{luna_sh_id}": "5.0",
                "shipping_out_cost": "22.00",
                "milling_fee": "180.00",
                "shipping_back_cost": "23.00",
                "other_handling_fee": "15.00",
                "include_in_tax_books": "1",
            },
            follow_redirects=False,
        )
        self.assertEqual(r_lot1.status_code, 303)

        lots = list_fiber_mill_lots(1, herd_id=alp_id)
        self.assertEqual(len(lots), 1)
        lot1 = lots[0]
        lot1_id = lot1["id"]
        self.assertEqual(lot1["color_group"], "White")
        self.assertFalse(lot1["mixed_color_warning"])
        self.assertAlmostEqual(lot1["total_mill_cost"], 240.00, places=2)
        self.assertAlmostEqual(lot1["allocated_shearing_cost"], 29.17, places=2)
        self.assertAlmostEqual(lot1["total_direct_cogs"], 269.17, places=2)

        sh_after = {s["animal_name"]: s for s in list_fiber_shearings(1, herd_id=alp_id)}
        self.assertAlmostEqual(sh_after["Luna"]["remaining_lbs"], 1.0, places=2)
        self.assertAlmostEqual(sh_after["Cocoa"]["remaining_lbs"], 4.5, places=2)

        # 5. Log 20 returned skeins (70.0 oz) & auto-restock Iron Ledger
        r_out1 = self.c.post(
            f"/herds/{alp_id}/mill-lots/{lot1_id}/outputs/add",
            data={
                "product_type": "yarn_skein",
                "product_name": "Luna White DK (200 yd)",
                "unit": "skeins",
                "quantity": "20",
                "finished_weight_oz": "70.0",
                "ledger_sku_id": str(yarn_sku_id),
                "sync_to_ledger": "1",
            },
            follow_redirects=False,
        )
        self.assertEqual(r_out1.status_code, 303)

        dash1 = compute_fiber_dashboard(1, alp_id, merchant_id=1)
        lot1_after = dash1["mill_lots"][0]
        self.assertEqual(lot1_after["status"], "received")
        self.assertAlmostEqual(lot1_after["yield_pct"], 87.5, places=1)
        out1 = lot1_after["outputs"][0]
        self.assertTrue(out1["synced"])
        self.assertAlmostEqual(out1["cost_per_unit"], 13.46, places=2)
        self.assertAlmostEqual(out1["margin_per_unit"], 14.54, places=2)

        yarn_sku = next(s for s in list_active_ledger_skus(merchant_id=1) if s["id"] == yarn_sku_id)
        self.assertAlmostEqual(yarn_sku["on_hand"], 20.0, places=1)

        # Log unlinked output #2, sync later, guard double-sync, then remove (reverses restock)
        self.c.post(
            f"/herds/{alp_id}/mill-lots/{lot1_id}/outputs/add",
            data={
                "product_type": "roving_bump",
                "product_name": "White Roving Bump",
                "unit": "bumps",
                "quantity": "2",
                "finished_weight_oz": "0",
            },
            follow_redirects=False,
        )
        lot1_two = list_fiber_mill_lots(1, herd_id=alp_id)[0]
        out2_id = next(o["id"] for o in lot1_two["outputs"] if o["product_name"] == "White Roving Bump")

        self.c.post(
            f"/herds/{alp_id}/mill-lots/{lot1_id}/outputs/{out2_id}/sync-ledger",
            data={"ledger_sku_id": str(yarn_sku_id)},
            follow_redirects=False,
        )
        self.assertAlmostEqual(
            next(s for s in list_active_ledger_skus(merchant_id=1) if s["id"] == yarn_sku_id)["on_hand"],
            22.0,
            places=1,
        )
        r_dup_sync = self.c.post(
            f"/herds/{alp_id}/mill-lots/{lot1_id}/outputs/{out2_id}/sync-ledger",
            data={"ledger_sku_id": str(yarn_sku_id)},
            follow_redirects=False,
        )
        self.assertIn("Error", r_dup_sync.headers["location"])
        self.assertAlmostEqual(
            next(s for s in list_active_ledger_skus(merchant_id=1) if s["id"] == yarn_sku_id)["on_hand"],
            22.0,
            places=1,
        )

        self.c.post(
            f"/herds/{alp_id}/mill-lots/{lot1_id}/outputs/{out2_id}/delete",
            follow_redirects=False,
        )
        self.assertAlmostEqual(
            next(s for s in list_active_ledger_skus(merchant_id=1) if s["id"] == yarn_sku_id)["on_hand"],
            20.0,
            places=1,
        )

        # 6. Schedule F Tax Books includes shearing + mill costs (and respects include_in_tax_books=0)
        self.c.post(
            f"/herds/{alp_id}/mill-lots/save",
            data={
                "lot_name": "2026 Cocoa Chocolate Lot",
                "inputs_submitted": "1",
                "shearing_ids": [str(cocoa_sh_id)],
                f"raw_lbs_{cocoa_sh_id}": "4.5",
                "milling_fee": "120.00",
                "include_in_tax_books": "",
            },
            follow_redirects=False,
        )
        books = build_unified_tax_books(1, merchant_id=1, tax_year=yr)
        fiber_entries = [e for e in books["entries"] if e["source_key"] in ("fiber_shearing", "fiber_mill")]
        self.assertAlmostEqual(sum(e["amount"] for e in fiber_entries), 310.00, places=2)

        mill_page = self.c.get(f"/herds?herd_id={alp_id}&tab=mill")
        self.assertEqual(mill_page.status_code, 200)
        self.assertIn("2026 Luna White DK Yarn", mill_page.text)
        self.assertIn("$13.46", mill_page.text)

        # 7. Staff (200 GET / 403 POST) and cross-farm user (404 POST)
        self.c.post(
            "/settings/members",
            data={"username": "helper", "display_name": "Helper", "password": "secret", "role": "staff"},
            follow_redirects=False,
        )
        c_staff = TestClient(app)
        c_staff.post("/login", data={"username": "helper", "password": "secret"}, follow_redirects=False)
        self.assertEqual(c_staff.get(f"/herds?herd_id={alp_id}&tab=mill").status_code, 200)
        self.assertEqual(
            c_staff.post(
                f"/herds/{alp_id}/shearings/add",
                data={"prime_lbs": "3.0"},
                follow_redirects=False,
            ).status_code,
            403,
        )
        self.assertEqual(
            c_staff.post(
                f"/herds/{alp_id}/mill-lots/save",
                data={"lot_name": "Blocked"},
                follow_redirects=False,
            ).status_code,
            403,
        )

        self.c.post(
            "/settings/admin/users",
            data={"username": "other", "display_name": "Other Farm", "password": "secret"},
            follow_redirects=False,
        )
        c_other = TestClient(app)
        c_other.post("/login", data={"username": "other", "password": "secret"}, follow_redirects=False)
        self.assertEqual(
            c_other.post(
                f"/herds/{alp_id}/shearings/add",
                data={"prime_lbs": "3.0"},
                follow_redirects=False,
            ).status_code,
            404,
        )
        self.assertEqual(
            c_other.post(
                f"/herds/{alp_id}/mill-lots/save",
                data={"lot_name": "Intruder"},
                follow_redirects=False,
            ).status_code,
            404,
        )

    def test_species_aware_terminology_and_clean_species_display(self):
        from unittest.mock import patch
        from homestead.config import (
            format_quantity_fraction,
            format_species_display,
            format_species_preset_label,
            get_species_terminology,
        )
        from homestead.database import list_batches

        # 1. Pure species display, preset label, quantity fraction & terminology helpers
        self.assertEqual(format_quantity_fraction(4), "4")
        self.assertEqual(format_quantity_fraction(0.5), "½")
        self.assertEqual(format_quantity_fraction(1.5), "1½")
        self.assertEqual(format_quantity_fraction(0.25), "¼")
        self.assertEqual(format_quantity_fraction(0.75), "¾")

        self.assertEqual(format_species_display("chicken eggs"), "Chicken")
        self.assertEqual(format_species_display("chicken eggs", plural=True), "Chickens")
        self.assertEqual(format_species_display("chicken chicks"), "Chicken")
        self.assertEqual(format_species_display("turkey chicks"), "Turkey")
        self.assertEqual(format_species_display("turkey chicks", plural=True), "Turkeys")
        self.assertEqual(format_species_display("kunekune pigs"), "Kunekune Pig")
        self.assertEqual(format_species_display("kunekune pigs", plural=True), "Kunekune Pigs")
        self.assertEqual(format_species_display("cornish cross eggs"), "Cornish Cross")
        self.assertEqual(format_species_display("cornish cross eggs", plural=True), "Cornish Cross")
        self.assertEqual(format_species_display("pekin ducklings"), "Pekin Duck")
        self.assertEqual(format_species_display("pekin ducklings", plural=True), "Pekin Ducks")
        self.assertEqual(format_species_display("toulouse goslings"), "Toulouse Goose")
        self.assertEqual(format_species_display("toulouse goslings", plural=True), "Toulouse Geese")
        self.assertEqual(format_species_display("berkshire piglets"), "Berkshire Pig")
        self.assertEqual(format_species_display("katahdin lambs"), "Katahdin Lamb")
        self.assertEqual(format_species_display("boer goats"), "Boer Goat")
        self.assertEqual(format_species_display("quail chicks"), "Quail")
        self.assertEqual(format_species_display("angus beef"), "Angus Beef")
        self.assertEqual(format_species_display("nz rabbits"), "Nz Rabbit")

        self.assertEqual(format_species_preset_label("chicken eggs"), "Chicken (Hatching Eggs)")
        self.assertEqual(format_species_preset_label("chicken chicks"), "Chicken (Day-Old Chicks)")
        self.assertEqual(format_species_preset_label("turkey chicks"), "Turkey (Poults / Chicks)")
        self.assertEqual(format_species_preset_label("cornish cross eggs"), "Cornish Cross (Hatching Eggs)")

        self.assertEqual(get_species_terminology("kunekune pigs")["singular"], "pig")
        self.assertEqual(get_species_terminology("kunekune pigs")["plural"], "pigs")
        self.assertFalse(get_species_terminology("kunekune pigs")["is_poultry"])
        self.assertEqual(get_species_terminology("turkey chicks")["singular"], "turkey")
        self.assertEqual(get_species_terminology("pekin ducklings")["singular"], "duck")
        self.assertEqual(get_species_terminology("toulouse goslings")["plural"], "geese")
        self.assertEqual(get_species_terminology("cornish cross eggs")["singular"], "chicken")
        self.assertEqual(get_species_terminology("quail chicks")["plural"], "quail")
        self.assertEqual(get_species_terminology("angus beef")["singular"], "animal")
        self.assertEqual(get_species_terminology("katahdin lambs")["singular"], "lamb")
        self.assertEqual(get_species_terminology("boer goats")["singular"], "goat")
        self.assertEqual(get_species_terminology("nz rabbits")["singular"], "rabbit")
        # Word-boundary check: "pigeon" must NOT match "pig"
        self.assertEqual(get_species_terminology("pigeon")["singular"], "animal")

        # 2. Create whole-animal Kunekune Pigs batch + Share-mode Pigs batch + Chicken Eggs + Chicken Chicks + Turkey Chicks
        self.c.post("/register", data={"display_name": "Steve", "username": "steve", "password": "secret"})
        batches_cfg = [
            ("Kune Breeders Litter", "kunekune pigs", 5, "whole", "0"),
            ("Pork Shares Fall", "kunekune pigs", 2, "shares", "1"),
            ("Cornish Run #1", "chicken eggs", 10, "whole", "0"),
            ("Cornish Run #2", "chicken chicks", 10, "whole", "0"),
            ("Fall Turkeys", "turkey chicks", 6, "whole", "0"),
        ]
        for bname, sp, qty, su, cpb in batches_cfg:
            self.assertEqual(
                self.c.post(
                    "/batches/save",
                    data={
                        "name": bname,
                        "species": sp,
                        "start_date": "2026-06-01",
                        "initial_quantity": str(qty),
                        "sale_price_per_lb": "8.00",
                        "sale_unit": su,
                        "customer_pays_butcher": cpb,
                        "next_url": "/batches",
                    },
                    follow_redirects=False,
                ).status_code,
                303,
            )

        by_name = {b["name"]: b for b in list_batches()}
        pig_bid = int(by_name["Kune Breeders Litter"]["id"])
        share_bid = int(by_name["Pork Shares Fall"]["id"])
        egg_bid = int(by_name["Cornish Run #1"]["id"])
        chk_bid = int(by_name["Cornish Run #2"]["id"])
        trk_bid = int(by_name["Fall Turkeys"]["id"])

        # Enable pre-orders & public picking on the whole-animal pig batch
        self.c.post(
            f"/batches/{pig_bid}/preorders/settings",
            data={
                "preorder_enabled": "1",
                "sale_unit": "whole",
                "preorder_cap": "4",
                "deposit_per_bird": "50.00",
            },
            follow_redirects=False,
        )
        self.c.post(
            f"/api/batches/{pig_bid}/picking",
            json={"picking_enabled": True, "max_birds_per_customer": 2},
        )
        # Re-fetch updated pig batch for slug
        pig_batch = next(b for b in list_batches() if int(b["id"]) == pig_bid)
        pig_slug = pig_batch["picking_slug"]

        # Harvest Tally on whole-animal pig batch must say pig/pigs, never bird/birds
        tally_html = self.c.get(f"/batches/{pig_bid}/harvest").text
        self.assertIn("Kunekune Pig", tally_html)
        self.assertIn("Tap any weighed pig", tally_html)
        self.assertIn("Log next pig", tally_html)
        self.assertIn("+ Add pig", tally_html)
        self.assertIn("Undo last pig", tally_html)
        self.assertIn("Weighed pigs", tally_html)
        self.assertIn("Assign pig", tally_html)
        self.assertIn("Delete pig", tally_html)
        self.assertIn("Barrow, gilt, freezer...", tally_html)
        self.assertNotIn("Weighed birds", tally_html)
        self.assertNotIn("+ Add bird", tally_html)
        self.assertNotIn("Undo last bird", tally_html)
        self.assertNotIn("Tom, hen", tally_html)

        # Log 2 whole pigs
        p1_res = self.c.post(
            f"/api/batches/{pig_bid}/harvest",
            json={"harvest_date": "2026-10-01", "head_count": 1, "dressed_lbs": 110.0, "for_sale": True},
        ).json()
        p2_res = self.c.post(
            f"/api/batches/{pig_bid}/harvest",
            json={"harvest_date": "2026-10-01", "head_count": 1, "dressed_lbs": 120.0, "for_sale": True},
        ).json()
        p1_id = min(e["id"] for e in p2_res["entries"])
        p2_id = max(e["id"] for e in p2_res["entries"])

        csv_txt = self.c.get(f"/batches/{pig_bid}/harvest.csv").text
        self.assertIn("lb/pig", csv_txt)
        self.assertNotIn("lb/bird", csv_txt)

        # Pre-Orders & public /reserve page for whole pig batch
        po_html = self.c.get(f"/batches/{pig_bid}/preorders").text
        self.assertIn("Reservation Cap (pigs)", po_html)
        self.assertIn("Deposit per Whole Pig ($)", po_html)
        self.assertIn("0 pigs ·", po_html)
        self.assertNotIn("birds ·", po_html)

        anon = TestClient(app)
        res_html = anon.get(f"/reserve/{pig_slug}").text
        self.assertIn("Reserve your Kunekune Pig", res_html)
        self.assertIn("$50.00/pig", res_html)
        self.assertNotIn("/bird", res_html)

        # Public /pick/<slug> for whole pigs says Pig / pigs and ntfy uses Pig #1
        pick_html = anon.get(f"/pick/{pig_slug}").text
        self.assertIn("Pick Your Pig", pick_html)
        self.assertIn("Available pigs", pick_html)
        self.assertIn("Continue to pick a pig", pick_html)
        self.assertNotIn("Available birds", pick_html)

        amber = self.c.post("/api/customers", json={"name": "Amber Colon", "phone": "555-200-0001"}).json()
        amber_ident = anon.post(
            f"/api/pick/{pig_slug}/identify",
            json={"name": "Amber Colon", "contact": "555-200-0001"},
        ).json()
        with patch("homestead.ntfy.send_ntfy_async") as mock_ntfy:
            claim_r = anon.post(
                f"/api/pick/{pig_slug}/claim/{p1_id}",
                json={"customer_id": amber_ident["customer_id"]},
            )
            self.assertEqual(claim_r.status_code, 200)
            mock_ntfy.assert_called_once()
            ntfy_args = mock_ntfy.call_args[0]
            self.assertIn("Pig claimed", ntfy_args[1])
            self.assertIn("Pig #1", ntfy_args[2])

        # Also assign Pig #2 to amber (settled as picked_up) or settle Pig #1 on Pickup Day
        pu_html = self.c.get(f"/batches/{pig_bid}/pickup").text
        self.assertIn("1 pigs waiting", pu_html)
        self.assertIn("Pig #1", pu_html)
        self.assertNotIn("Bird #1", pu_html)
        settle_resp = self.c.post(
            f"/batches/{pig_bid}/pickup/{amber['id']}/settle",
            data={"collected_today": "880.00", "payment_method": "cash", "bird_ids": [str(p1_id)]},
            follow_redirects=True,
        )
        self.assertEqual(settle_resp.status_code, 200)
        self.assertIn("1 pig) paid &amp; picked up", settle_resp.text)
        self.assertIn("<th>Pigs</th>", settle_resp.text)
        self.assertIn("Pig #1", settle_resp.text)

        # 3. Assign Alex to 2 chickens from "chicken eggs" + 2 chickens from "chicken chicks",
        #    Amanda to 1 turkey ("turkey chicks"), and Cara to ½ share of Kunekune Pig ("shares")
        alex = self.c.post("/api/customers", json={"name": "Alex Buyer", "phone": "555-200-0002"}).json()
        amanda = self.c.post("/api/customers", json={"name": "Amanda Buyer", "phone": "555-200-0003"}).json()
        cara = self.c.post("/api/customers", json={"name": "Cara Buyer", "phone": "555-200-0004"}).json()

        for _ in range(2):
            h = self.c.post(
                f"/api/batches/{egg_bid}/harvest",
                json={"harvest_date": "2026-09-15", "head_count": 1, "dressed_lbs": 5.0, "for_sale": True},
            ).json()
            hid = h["entries"][0]["id"]
            self.c.post(f"/api/batches/{egg_bid}/harvest/{hid}/assign", json={"for_sale": True, "customer_id": alex["id"]})

        for _ in range(2):
            h = self.c.post(
                f"/api/batches/{chk_bid}/harvest",
                json={"harvest_date": "2026-09-20", "head_count": 1, "dressed_lbs": 5.0, "for_sale": True},
            ).json()
            hid = h["entries"][0]["id"]
            self.c.post(f"/api/batches/{chk_bid}/harvest/{hid}/assign", json={"for_sale": True, "customer_id": alex["id"]})

        th = self.c.post(
            f"/api/batches/{trk_bid}/harvest",
            json={"harvest_date": "2026-11-15", "head_count": 1, "dressed_lbs": 16.0, "for_sale": True},
        ).json()
        self.c.post(
            f"/api/batches/{trk_bid}/harvest/{th['entries'][0]['id']}/assign",
            json={"for_sale": True, "customer_id": amanda["id"]},
        )

        sh_add = self.c.post(
            f"/api/batches/{share_bid}/harvest",
            json={"harvest_date": "2026-10-15", "dressed_lbs": 120.0, "split_mode": "halves", "for_sale": True},
        ).json()
        half_a_id = sh_add["entries"][0]["shares"][0]["id"]
        self.c.post(
            f"/api/batches/{share_bid}/shares/{half_a_id}/assign",
            json={"for_sale": True, "customer_id": cara["id"]},
        )

        # Verify /batches, /customers, /settings, and /financials?tab=economics
        batches_page = self.c.get("/batches").text
        self.assertIn(">Chicken<", batches_page)
        self.assertIn(">Turkey<", batches_page)
        self.assertIn(">Kunekune Pig<", batches_page)
        self.assertNotIn(">Chicken Eggs<", batches_page)
        self.assertNotIn(">Turkey Chicks<", batches_page)
        self.assertIn("Chicken (Hatching Eggs)", batches_page)
        self.assertIn("Turkey (Poults / Chicks)", batches_page)

        custs_page = self.c.get("/customers").text
        self.assertIn("Livestock assigned", custs_page)
        self.assertNotIn("Birds assigned", custs_page)
        self.assertIn("4 × Chickens", custs_page)
        self.assertIn("1 × Turkey", custs_page)
        self.assertIn("1 × Kunekune Pig", custs_page)
        self.assertIn("½ × Kunekune Pig", custs_page)
        self.assertNotIn("Chicken Eggs", custs_page)
        self.assertNotIn("Chicken Chicks", custs_page)
        self.assertNotIn("Turkey Chicks", custs_page)

        settings_page = self.c.get("/settings").text
        self.assertIn("Chicken (Hatching Eggs)", settings_page)
        self.assertIn("Chicken (Day-Old Chicks)", settings_page)
        self.assertIn("Turkey (Poults / Chicks)", settings_page)

        econ_page = self.c.get("/financials?tab=economics").text
        self.assertIn("Livestock, feed &amp; processing", econ_page)
        self.assertNotIn("Chicks, feed &amp; processing", econ_page)

    def test_open_redirect_validation_blocks_external_and_backslash_targets(self):
        from homestead.database import list_batches
        from shared.web import safe_redirect_path

        # 1. Pure sanitizer checks
        self.assertEqual(safe_redirect_path("//evil.com/phish", "/batches"), "/batches")
        self.assertEqual(safe_redirect_path("/\\evil.com", "/batches"), "/batches")
        self.assertEqual(safe_redirect_path("https://evil.com", "/"), "/")
        self.assertEqual(safe_redirect_path("///evil.com", "/"), "/")
        self.assertEqual(safe_redirect_path("/\r\nLocation: https://evil.com", "/"), "/")
        self.assertEqual(safe_redirect_path("/batches/5?tab=grow", "/"), "/batches/5?tab=grow")

        # 2. HTTP route checks on Homestead POST endpoints accepting next_url
        self.c.post("/register", data={"display_name": "Steve", "username": "steve", "password": "secret"})
        self.c.post(
            "/batches/save",
            data={
                "name": "Redirect Test Batch",
                "species": "turkey chicks",
                "start_date": "2026-06-01",
                "initial_quantity": "5",
                "next_url": "/batches",
            },
            follow_redirects=False,
        )
        bid = int(list_batches()[0]["id"])

        for bad in ("//evil.com", "/\\evil.com", "https://evil.com/login"):
            r_sw = self.c.post(
                "/switch-merchant",
                data={"merchant_id": "1", "next_url": bad},
                follow_redirects=False,
            )
            self.assertEqual(r_sw.status_code, 303)
            self.assertEqual(r_sw.headers["location"], "/")
            self.assertNotIn("evil.com", r_sw.headers["location"])

            r_loss = self.c.post(
                "/batches/log-loss",
                data={"batch_id": str(bid), "loss_date": "2026-06-15", "count": "1", "next_url": bad},
                follow_redirects=False,
            )
            self.assertEqual(r_loss.status_code, 303)
            self.assertTrue(r_loss.headers["location"].startswith("/?msg="))
            self.assertNotIn("evil.com", r_loss.headers["location"])

            r_egg = self.c.post(
                "/eggs/log",
                data={"species": "chicken", "log_date": "2026-06-15", "total_collected": "6", "next_url": bad},
                follow_redirects=False,
            )
            self.assertEqual(r_egg.status_code, 303)
            self.assertTrue(r_egg.headers["location"].startswith("/eggs?species=chicken"))
            self.assertNotIn("evil.com", r_egg.headers["location"])

    def test_alpaca_16_color_picker_fleece_factors_and_fiber_grade(self):
        from datetime import date

        from homestead.db.fiber import (
            ALPACA_ANIMAL_FACTORS,
            ALPACA_CLIP_EXTRA_FACTORS,
            ALPACA_FIBER_GRADES,
            ALPACA_FLEECE_COLORS,
            list_fiber_mill_lots,
            list_fiber_shearings,
        )
        from homestead.db.herds import list_herd_animals, list_herds

        today_iso = date.today().isoformat()
        self.c.post(
            "/register",
            data={"display_name": "Steve", "username": "steve", "password": "secret"},
            follow_redirects=False,
        )

        # 1. Verify official 16 AOA color palette, 7 grades, 6 animal + 2 clip factors
        self.assertEqual(len(ALPACA_FLEECE_COLORS), 16)
        self.assertEqual(len({c["name"] for c in ALPACA_FLEECE_COLORS}), 16)
        self.assertEqual(len({c["code"] for c in ALPACA_FLEECE_COLORS}), 16)
        self.assertEqual(len(ALPACA_FIBER_GRADES), 7)
        self.assertEqual(len(ALPACA_ANIMAL_FACTORS), 6)
        self.assertEqual(len(ALPACA_CLIP_EXTRA_FACTORS), 2)

        # 2. Create Alpaca herd and Breeding Pigs herd
        self.c.post(
            "/herds/save",
            data={"name": "Alpaca Herd", "species": "alpaca", "herd_type": "fiber", "head_count": "2"},
            follow_redirects=False,
        )
        self.c.post(
            "/herds/save",
            data={"name": "Breeding Pigs", "species": "pig", "herd_type": "breeding", "head_count": "2"},
            follow_redirects=False,
        )
        herds_by_name = {h["name"]: int(h["id"]) for h in list_herds(1)}
        alp_id = herds_by_name["Alpaca Herd"]
        pig_id = herds_by_name["Breeding Pigs"]

        # 3. Save & edit named alpaca with 16-color swatch, separate fiber_grade, and factors
        self.c.post(
            f"/herds/{alp_id}/animals/save",
            data={
                "name": "Clover",
                "sex_role": "Female",
                "color": "Medium Rose Grey",
                "fiber_grade": "grade_2",
                "fleece_factors": ["tuxedoed", "suri"],
                "birth_year": "2022",
            },
            follow_redirects=False,
        )
        self.c.post(
            f"/herds/{alp_id}/animals/save",
            data={
                "name": "Apollo",
                "sex_role": "Male",
                "color": "True Black",
                "fiber_grade": "grade_3",
                "fleece_factors": ["roan"],
                "birth_year": "2021",
            },
            follow_redirects=False,
        )

        animals = {a["name"]: a for a in list_herd_animals(1, herd_id=alp_id)}
        clover = animals["Clover"]
        apollo = animals["Apollo"]
        self.assertEqual(clover["color"], "Medium Rose Grey")
        self.assertEqual(clover["color_code"], "MRG")
        self.assertEqual(clover["fiber_grade"], "grade_2")
        self.assertEqual(clover["fiber_grade_label"], "Grade 2 · Baby")
        self.assertEqual(clover["fleece_factor_list"], ["tuxedoed", "suri"])
        self.assertEqual(clover["fleece_factor_labels"], ["Tuxedoed", "Suri"])

        # Edit Clover to add pinto, keeping color and grade intact
        self.c.post(
            f"/herds/{alp_id}/animals/save",
            data={
                "animal_id": str(clover["id"]),
                "name": "Clover",
                "sex_role": "Female",
                "color": "Medium Rose Grey",
                "fiber_grade": "grade_2",
                "fleece_factors": ["tuxedoed", "pinto", "suri"],
                "birth_year": "2022",
            },
            follow_redirects=False,
        )
        clover_edited = {a["name"]: a for a in list_herd_animals(1, herd_id=alp_id)}["Clover"]
        self.assertEqual(clover_edited["fleece_factor_list"], ["tuxedoed", "pinto", "suri"])

        # Alpaca Overview page renders all 16 swatches, color dot MRG, grade, and factor chips
        alp_overview = self.c.get(f"/herds?herd_id={alp_id}").text
        self.assertIn('id="anm-palette"', alp_overview)
        for c in ALPACA_FLEECE_COLORS:
            self.assertIn(f'data-color="{c["name"]}"', alp_overview)
        self.assertIn('data-swatch="MRG"', alp_overview)
        self.assertIn("Grade 2 · Baby", alp_overview)
        self.assertIn("Tuxedoed", alp_overview)
        self.assertIn("Suri", alp_overview)
        self.assertIn('role="tooltip"', alp_overview)
        self.assertIn("trait-tip", alp_overview)
        self.assertIn("Trait guide", alp_overview)
        self.assertIn("white face, white legs/socks", alp_overview)
        self.assertIn("Multi/Fancy covers 3+ colors", alp_overview)
        self.assertIn("6 or more distinct spots", alp_overview)
        self.assertIn("modern grey", alp_overview)
        self.assertIn("dreadlocks", alp_overview)
        self.assertIn("next-to-skin yarn", alp_overview)

        # Breeding Pigs Overview stays simple without alpaca color palette or grade dropdown
        pig_overview = self.c.get(f"/herds?herd_id={pig_id}").text
        self.assertNotIn('id="anm-palette"', pig_overview)
        self.assertNotIn('id="anm-grade"', pig_overview)

        # 4. Shearing auto-inherits color, grade & factors when omitted; explicit override clears/replaces
        self.c.post(
            f"/herds/{alp_id}/shearings/add",
            data={
                "animal_id": str(clover["id"]),
                "shear_date": today_iso,
                "prime_lbs": "4.2",
            },
            follow_redirects=False,
        )
        self.c.post(
            f"/herds/{alp_id}/shearings/add",
            data={
                "animal_id": str(clover["id"]),
                "shear_date": today_iso,
                "fleece_color": "Medium Rose Grey",
                "fiber_grade": "grade_3",
                "fleece_factors_submitted": "1",
                "fleece_factors": ["heavy_vm"],
                "prime_lbs": "3.5",
                "notes": "Explicit override clip",
            },
            follow_redirects=False,
        )
        self.c.post(
            f"/herds/{alp_id}/shearings/add",
            data={
                "animal_id": str(apollo["id"]),
                "shear_date": today_iso,
                "prime_lbs": "5.0",
            },
            follow_redirects=False,
        )

        shearings = list_fiber_shearings(1, herd_id=alp_id)
        inherited = next(s for s in shearings if s["animal_id"] == clover["id"] and not s["notes"])
        overridden = next(s for s in shearings if s["notes"] == "Explicit override clip")
        apollo_sh = next(s for s in shearings if s["animal_id"] == apollo["id"])

        self.assertEqual(inherited["fleece_color"], "Medium Rose Grey")
        self.assertEqual(inherited["color_code"], "MRG")
        self.assertEqual(inherited["fiber_grade"], "grade_2")
        self.assertEqual(inherited["fleece_factor_list"], ["tuxedoed", "pinto", "suri"])
        self.assertTrue(inherited["is_suri"])

        self.assertEqual(overridden["fiber_grade"], "grade_3")
        self.assertEqual(overridden["fleece_factor_list"], ["heavy_vm"])
        self.assertFalse(overridden["is_suri"])

        sh_page = self.c.get(f"/herds?herd_id={alp_id}&tab=shearing").text
        self.assertIn('id="sh-palette"', sh_page)
        self.assertIn('id="sh-grade"', sh_page)
        self.assertIn("Heavy VM", sh_page)
        self.assertIn("Tender / Break", sh_page)
        self.assertIn("Tuxedoed", sh_page)
        self.assertIn('role="tooltip"', sh_page)
        self.assertIn("Trait guide", sh_page)
        self.assertIn("vegetable matter", sh_page)
        self.assertIn("Weak spot along the staple", sh_page)

        # 5. Mill lot mixing Suri (inherited Clover) + non-Suri (Apollo) triggers mixed_suri_warning;
        #    single-type lot has mixed_suri_warning=False
        self.c.post(
            f"/herds/{alp_id}/mill-lots/save",
            data={
                "lot_name": "Mixed Suri Test Lot",
                "inputs_submitted": "1",
                "shearing_ids": [str(inherited["id"]), str(apollo_sh["id"])],
                f"raw_lbs_{inherited['id']}": "2.0",
                f"raw_lbs_{apollo_sh['id']}": "2.0",
            },
            follow_redirects=False,
        )
        self.c.post(
            f"/herds/{alp_id}/mill-lots/save",
            data={
                "lot_name": "Single Type Lot",
                "inputs_submitted": "1",
                "shearing_ids": [str(overridden["id"])],
                f"raw_lbs_{overridden['id']}": "3.0",
            },
            follow_redirects=False,
        )

        lots_by_name = {l["lot_name"]: l for l in list_fiber_mill_lots(1, herd_id=alp_id)}
        self.assertTrue(lots_by_name["Mixed Suri Test Lot"]["mixed_suri_warning"])
        self.assertTrue(lots_by_name["Mixed Suri Test Lot"]["mixed_color_warning"])
        self.assertFalse(lots_by_name["Single Type Lot"]["mixed_suri_warning"])
        self.assertFalse(lots_by_name["Single Type Lot"]["mixed_color_warning"])

        mill_html = self.c.get(f"/herds?herd_id={alp_id}&tab=mill").text
        self.assertIn("Mixed Suri / Huacaya", mill_html)
        self.assertIn("Grade 2 · Baby", mill_html)
        self.assertIn("trait-pill-trigger", mill_html)
        self.assertIn('role="tooltip"', mill_html)

    def test_farm_team_notes_markdown_checklists_comments_roles_radar_and_ntfy(self):
        from unittest.mock import patch

        from homestead.db.batches import list_batches
        from homestead.db.herds import list_herds
        from homestead.db.notes import (
            get_farm_note,
            list_farm_notes,
            parse_note_body,
            reset_checklist_in_body,
            sanitize_note_url,
            toggle_checklist_item_in_body,
        )
        from homestead.ntfy import save_ntfy_config

        # 1. Unit checks on Markdown parser, URL sanitizer, and checklist helpers
        self.assertEqual(sanitize_note_url("javascript:alert(1)"), "")
        self.assertEqual(sanitize_note_url("data:text/html,<script>"), "")
        self.assertEqual(sanitize_note_url("https://example.com/guide"), "https://example.com/guide")

        sample_md = (
            "# Morning SOP\n"
            "Check **water** and *feed* `barrel-1`.\n"
            "Bad <script>alert(1)</script> [click](javascript:alert(2)) [vet](https://vet.example/doc)\n"
            "- [ ] Refill waterers\n"
            "- [x] Check heat lamp\n"
            "- Sweep aisle\n"
        )
        blocks, done, total = parse_note_body(sample_md)
        self.assertEqual((done, total), (1, 2))
        self.assertEqual([b["type"] for b in blocks], ["heading", "p", "checklist", "ul"])

        toggled, is_chk, d2, t2 = toggle_checklist_item_in_body(sample_md, 0)
        self.assertTrue(is_chk)
        self.assertEqual((d2, t2), (2, 2))
        reset_txt, rst_cnt = reset_checklist_in_body(toggled)
        self.assertEqual(rst_cnt, 2)
        self.assertEqual(parse_note_body(reset_txt)[1:], (0, 2))

        # 2. Seed owner, manager, staff, active batch, active herd & ntfy config
        self.c.post(
            "/register",
            data={"display_name": "Steve", "username": "steve", "password": "secret"},
            follow_redirects=False,
        )
        self.c.post(
            "/settings/members",
            data={"username": "sarah", "display_name": "Sarah", "password": "secret", "role": "manager"},
            follow_redirects=False,
        )
        self.c.post(
            "/settings/members",
            data={"username": "mom", "display_name": "Mom", "password": "secret", "role": "staff"},
            follow_redirects=False,
        )
        save_ntfy_config(1, "https://ntfy.sh", "ironclad-farm-test")

        self.c.post(
            "/batches/save",
            data={
                "name": "Fall Broilers",
                "species": "chicken chicks",
                "start_date": "2026-11-01",
                "initial_quantity": "20",
            },
            follow_redirects=False,
        )
        batch_id = int(list_batches(user_id=1)[0]["id"])
        self.c.post(
            "/herds/save",
            data={"name": "Alpaca Herd", "species": "alpaca", "herd_type": "fiber", "head_count": "2"},
            follow_redirects=False,
        )
        herd_id = int(list_herds(1)[0]["id"])

        # 3. Post pinned SOP note with checklist, batch+herd links, XSS payload, and notify_team=1
        with patch("homestead.ntfy.send_ntfy_async") as mock_ntfy:
            r_post = self.c.post(
                "/notes/save",
                data={
                    "title": "Morning Coop & Brooder SOP",
                    "category": "sop",
                    "batch_id": str(batch_id),
                    "herd_id": str(herd_id),
                    "pinned": "1",
                    "notify_team": "1",
                    "body": (
                        "## Steps\n"
                        "- [ ] Refresh brooder water\n"
                        "- [ ] Check **brooder** lamp\n\n"
                        "See <script>alert(1)</script> [bad](javascript:alert(1)) https://example.com/sop"
                    ),
                },
                follow_redirects=False,
            )
            self.assertEqual(r_post.status_code, 303)
            mock_ntfy.assert_called_once()
            self.assertIn("Morning Coop & Brooder SOP", mock_ntfy.call_args[0][1])

        notes = list_farm_notes(1)
        self.assertEqual(len(notes), 1)
        sop_id = notes[0]["id"]

        # Unchecked notify_team does NOT call ntfy; invalid attachment URL rejected cleanly
        with patch("homestead.ntfy.send_ntfy_async") as mock_ntfy:
            self.c.post(
                "/notes/save",
                data={"title": "Fence check needed", "category": "maintenance", "body": "- Fix gate latch"},
                follow_redirects=False,
            )
            mock_ntfy.assert_not_called()

        r_bad_url = self.c.post(
            "/notes/save",
            data={"title": "Bad attachment", "category": "general", "attachment_url": "javascript:alert(1)"},
            follow_redirects=False,
        )
        self.assertIn("Error", r_bad_url.headers["location"])

        # Verify Markdown & XSS neutralization + Radar Pinned Team Notes card
        notes_html = self.c.get("/notes").text
        self.assertIn("Morning Coop &amp; Brooder SOP", notes_html)
        self.assertIn("0/2 done", notes_html)
        self.assertIn("Batch: Fall Broilers", notes_html)
        self.assertIn("Herd: Alpaca Herd", notes_html)
        self.assertNotIn("<script>alert(1)</script>", notes_html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", notes_html)
        self.assertNotIn('href="javascript:', notes_html)
        self.assertIn('href="https://example.com/sop"', notes_html)

        radar_html = self.c.get("/").text
        self.assertIn("Pinned Team Notes", radar_html)
        self.assertIn("Morning Coop &amp; Brooder SOP", radar_html)
        self.assertIn("0/2 done", radar_html)

        # 4. Staff access & permissions: Farm links to /notes, Feed/Financials/Settings stay 403
        c_staff = TestClient(app)
        c_staff.post("/login", data={"username": "mom", "password": "secret"}, follow_redirects=False)
        staff_radar = c_staff.get("/").text
        self.assertIn('href="/notes"', staff_radar)
        self.assertNotIn('href="/financials"', staff_radar)

        staff_notes_resp = c_staff.get("/notes")
        self.assertEqual(staff_notes_resp.status_code, 200)
        self.assertIn("Team Notes", staff_notes_resp.text)
        self.assertNotIn("Feed Schedule", staff_notes_resp.text)
        self.assertNotIn("Financials &amp; Taxes", staff_notes_resp.text)
        self.assertNotIn("Settings &amp; Team", staff_notes_resp.text)
        self.assertEqual(c_staff.get("/feed").status_code, 403)
        self.assertEqual(c_staff.get("/financials").status_code, 403)
        self.assertEqual(c_staff.get("/settings").status_code, 403)

        # Staff can toggle checklist items on owner's SOP and reset checklist
        tog1 = c_staff.post(f"/api/notes/{sop_id}/checklist/0/toggle")
        self.assertEqual(tog1.status_code, 200)
        d_tog = tog1.json()
        self.assertTrue(d_tog["checked"])
        self.assertEqual((d_tog["done"], d_tog["total"]), (1, 2))
        self.assertEqual(d_tog["updated_by_name"], "Mom")

        r_rst = c_staff.post(f"/notes/{sop_id}/reset-checklist", follow_redirects=False)
        self.assertEqual(r_rst.status_code, 303)
        self.assertEqual(get_farm_note(1, sop_id)["check_done"], 0)

        # Staff cannot edit, pin, archive, or delete owner's note -> 403
        self.assertEqual(
            c_staff.post(f"/notes/save", data={"note_id": str(sop_id), "title": "Hijacked"}, follow_redirects=False).status_code,
            403,
        )
        self.assertEqual(c_staff.post(f"/notes/{sop_id}/pin", data={"pinned": "0"}, follow_redirects=False).status_code, 403)
        self.assertEqual(c_staff.post(f"/notes/{sop_id}/archive", data={"archived": "1"}, follow_redirects=False).status_code, 403)
        self.assertEqual(c_staff.post(f"/notes/{sop_id}/delete", follow_redirects=False).status_code, 403)

        # Staff CAN create, edit, and reply with their own note (pin flag ignored for staff)
        c_staff.post(
            "/notes/save",
            data={"title": "Mom afternoon check", "category": "chores", "pinned": "1", "body": "All hens fed"},
            follow_redirects=False,
        )
        mom_note = next(n for n in list_farm_notes(1) if n["title"] == "Mom afternoon check")
        self.assertFalse(mom_note["pinned"])
        self.assertEqual(
            c_staff.post(
                "/notes/save",
                data={"note_id": str(mom_note["id"]), "title": "Mom afternoon check updated", "body": "All fed & watered"},
                follow_redirects=False,
            ).status_code,
            303,
        )

        # 5. Comments + optional ntfy reply + role delete rules + search + archive
        with patch("homestead.ntfy.send_ntfy_async") as mock_ntfy:
            c_staff.post(
                f"/notes/{sop_id}/comments/add",
                data={"body": "Finished Tuesday morning check!", "notify_team": "1"},
                follow_redirects=False,
            )
            mock_ntfy.assert_called_once()
            self.assertIn("Reply on: Morning Coop & Brooder SOP", mock_ntfy.call_args[0][1])

        self.c.post(
            f"/notes/{sop_id}/comments/add",
            data={"body": "Thanks Mom, refill barrel tomorrow."},
            follow_redirects=False,
        )
        sop_full = get_farm_note(1, sop_id)
        self.assertEqual(len(sop_full["comments"]), 2)
        mom_comment_id = sop_full["comments"][0]["id"]
        steve_comment_id = sop_full["comments"][1]["id"]

        # Staff cannot delete owner's reply (403), CAN delete own reply (303)
        self.assertEqual(
            c_staff.post(f"/notes/{sop_id}/comments/{steve_comment_id}/delete", follow_redirects=False).status_code,
            403,
        )
        self.assertEqual(
            c_staff.post(f"/notes/{sop_id}/comments/{mom_comment_id}/delete", follow_redirects=False).status_code,
            303,
        )

        # Manager CAN edit/pin/archive staff note and view Farm subnav
        c_mgr = TestClient(app)
        c_mgr.post("/login", data={"username": "sarah", "password": "secret"}, follow_redirects=False)
        mgr_notes = c_mgr.get("/notes")
        self.assertEqual(mgr_notes.status_code, 200)
        self.assertIn("Feed Schedule", mgr_notes.text)
        self.assertIn("Team Notes", mgr_notes.text)
        self.assertNotIn("Settings &amp; Team", mgr_notes.text)
        self.assertEqual(
            c_mgr.post(f"/notes/{mom_note['id']}/pin", data={"pinned": "1"}, follow_redirects=False).status_code,
            303,
        )
        self.assertTrue(get_farm_note(1, mom_note["id"])["pinned"])
        self.assertEqual(
            c_mgr.post(f"/notes/{mom_note['id']}/archive", data={"archived": "1"}, follow_redirects=False).status_code,
            303,
        )
        self.assertFalse(get_farm_note(1, mom_note["id"])["pinned"])
        self.assertTrue(get_farm_note(1, mom_note["id"])["archived"])

        # Category filter, search (matching comment body), and archived toggle
        search_html = self.c.get("/notes?category=sop&q=refill").text
        self.assertIn("Morning Coop &amp; Brooder SOP", search_html)
        self.assertNotIn("Fence check needed", search_html)
        arch_html = self.c.get("/notes?archived=1").text
        self.assertIn("Mom afternoon check updated", arch_html)

        # 6. Cross-workspace isolation -> 404
        self.c.post(
            "/settings/admin/users",
            data={"username": "other", "display_name": "Other Farm", "password": "secret"},
            follow_redirects=False,
        )
        c_other = TestClient(app)
        c_other.post("/login", data={"username": "other", "password": "secret"}, follow_redirects=False)
        self.assertEqual(c_other.post(f"/api/notes/{sop_id}/checklist/0/toggle").status_code, 404)
        self.assertEqual(
            c_other.post("/notes/save", data={"note_id": str(sop_id), "title": "Nope"}, follow_redirects=False).status_code,
            404,
        )
        self.assertEqual(
            c_other.post(f"/notes/{sop_id}/comments/add", data={"body": "Nope"}, follow_redirects=False).status_code,
            404,
        )
        self.assertEqual(c_other.post(f"/notes/{sop_id}/delete", follow_redirects=False).status_code, 404)

    def test_batch_detail_actuals_vs_planned_linked_financials_receipts_and_insights_phase1(self):
        from homestead.db.batches import list_batches
        from homestead.db.eggs import get_layer_settings
        from homestead.db.herds import list_herds

        # 1. Register owner & add staff user for RBAC check
        self.c.post(
            "/register",
            data={"display_name": "Steve", "username": "steve", "password": "secret"},
            follow_redirects=False,
        )
        self.c.post(
            "/settings/members",
            data={"username": "mom", "display_name": "Mom", "password": "secret", "role": "staff"},
            follow_redirects=False,
        )

        # 2. Create meat batch, log loss/harvest/sale, and link both feed + supplies receipts via Financials
        self.c.post(
            "/batches/save",
            data={
                "name": "Spring Broilers",
                "species": "cornish cross",
                "start_date": "2026-10-01",
                "initial_quantity": "20",
                "unit_cost": "3.50",
                "avg_dressed_weight": "4.5",
                "sale_price_per_lb": "5.50",
                "processing_cost": "5.00",
                "lifecycle_type": "live_arrival",
                "next_url": "/batches",
            },
            follow_redirects=False,
        )
        bid = int(list_batches(user_id=1)[0]["id"])

        self.c.post(
            "/batches/log-loss",
            data={"batch_id": str(bid), "loss_date": "2026-10-10", "count": "2", "reason": "Weak chick", "next_url": f"/batches/{bid}"},
            follow_redirects=False,
        )
        self.c.post(
            "/batches/log-harvest",
            data={
                "batch_id": str(bid),
                "harvest_date": "2026-11-12",
                "head_count": "18",
                "dressed_lbs": "90.0",
                "processing_cost_total": "90.0",
                "next_url": f"/batches/{bid}",
            },
            follow_redirects=False,
        )
        self.c.post(
            "/batches/log-sale",
            data={
                "batch_id": str(bid),
                "entry_date": "2026-11-15",
                "allocation_type": "sale",
                "weight_lbs": "40.0",
                "revenue": "200.0",
                "next_url": f"/batches/{bid}",
            },
            follow_redirects=False,
        )

        # Split receipt on Financials: batch grower feed ($88 / 200 lb) + batch supplies ($45)
        self.c.post(
            "/expenses/receipt",
            data={
                "receipt_date": "2026-10-20",
                "vendor": "Tractor Supply",
                "lines_json": json.dumps([
                    {"kind": "batch_feed", "batch_id": str(bid), "feed_type": "grower", "bags": 4, "feed_lbs": 200, "amount": 88.00, "notes": "Broiler grower"},
                    {"kind": "general", "category": "Supplies & Equipment", "batch_id": str(bid), "amount": 45.00, "notes": "Brooder bulbs"},
                ]),
            },
            follow_redirects=False,
        )
        # Additional batch-linked Feed receipt logged as General Farm Expense (category="Feed")
        self.c.post(
            "/expenses/receipt",
            data={
                "receipt_date": "2026-11-02",
                "vendor": "Local Feed Mill",
                "lines_json": json.dumps([
                    {"kind": "general", "category": "Feed", "batch_id": str(bid), "amount": 60.00, "notes": "Extra bulk pellets"},
                ]),
            },
            follow_redirects=False,
        )

        bd_resp = self.c.get(f"/batches/{bid}")
        self.assertEqual(bd_resp.status_code, 200)
        bd_html = bd_resp.text
        self.assertIn("18 / 18", bd_html)
        self.assertIn("90% survival", bd_html)
        self.assertIn("90.0 lb dressed", bd_html)
        self.assertIn("$200.00 sales", bd_html)
        self.assertIn("Actual vs. Planned Cost", bd_html)
        self.assertIn("$353.00", bd_html)  # 70 acq + 148 feed (88+60) + 90 proc + 45 supplies
        self.assertIn("200.0 lb ($148.00)", bd_html)
        self.assertIn("2.22:1", bd_html)  # 200 / 90 dressed lbs
        self.assertIn("Local Feed Mill", bd_html)
        self.assertIn("Linked Receipts from Financials (1)", bd_html)
        self.assertIn("Brooder bulbs", bd_html)
        self.assertIn("$45.00", bd_html)

        # 3. Seed multi-vendor & multi-month layer/herd feed + eggs for Insights Phase 1
        self.c.post(
            "/herds/save",
            data={"name": "Alpaca Herd", "species": "alpaca", "herd_type": "fiber", "head_count": "3"},
            follow_redirects=False,
        )
        herd_id = int(list_herds(1)[0]["id"])

        for rdate, vendor, lines in [
            (
                "2026-09-10",
                "Country Co-op",
                [
                    {"kind": "layer_feed", "feed_type": "layer", "species_tags": ["chicken"], "bags": 2, "feed_lbs": 100, "amount": 52.00, "notes": "Early fall layer"},
                    {"kind": "layer_feed", "feed_type": "scratch", "species_tags": ["chicken"], "bags": 1, "feed_lbs": 50, "amount": 13.00, "notes": "Sep scratch"},
                ],
            ),
            (
                "2026-10-12",
                "Country Co-op",
                [
                    {"kind": "layer_feed", "feed_type": "layer", "species_tags": ["chicken"], "bags": 2, "feed_lbs": 100, "amount": 52.00, "notes": "Oct layer"},
                    {"kind": "herd_feed", "herd_id": str(herd_id), "feed_category": "hay", "unit": "bales", "quantity": 10, "weight_lbs": 500, "amount": 80.00, "notes": "First cut bales"},
                ],
            ),
            (
                "2026-10-18",
                "Tractor Supply",
                [
                    {"kind": "layer_feed", "feed_type": "scratch", "species_tags": ["chicken"], "bags": 1, "feed_lbs": 50, "amount": 16.00, "notes": "Oct scratch"},
                ],
            ),
            (
                "2026-11-14",
                "Country Co-op",
                [
                    {"kind": "layer_feed", "feed_type": "layer", "species_tags": ["chicken"], "bags": 2, "feed_lbs": 100, "amount": 52.00, "notes": "Nov layer"},
                ],
            ),
            (
                "2026-11-18",
                "Tractor Supply",
                [
                    {"kind": "layer_feed", "feed_type": "scratch", "species_tags": ["chicken"], "bags": 1, "feed_lbs": 10, "amount": 25.00, "notes": "Weight typo outlier"},
                ],
            ),
        ]:
            self.assertEqual(
                self.c.post(
                    "/expenses/receipt",
                    data={"receipt_date": rdate, "vendor": vendor, "lines_json": json.dumps(lines)},
                    follow_redirects=False,
                ).status_code,
                303,
            )

        self.c.post(
            "/eggs/log",
            data={"species": "chicken", "log_date": "2026-10-15", "total_collected": "60", "cracked_unusable": "0"},
            follow_redirects=False,
        )
        self.c.post(
            "/eggs/log",
            data={"species": "chicken", "log_date": "2026-11-16", "total_collected": "120", "cracked_unusable": "0"},
            follow_redirects=False,
        )

        # 4. Verify /financials?tab=insights & all sections render cleanly
        ins_resp = self.c.get("/financials?tab=insights&year=2026")
        self.assertEqual(ins_resp.status_code, 200)
        ins_html = ins_resp.text
        self.assertIn("Farm Insights &amp; Unit Economics", ins_html)
        self.assertIn("<polyline", ins_html)
        self.assertIn("Preset Bag Price Calibration", ins_html)
        self.assertIn("Update Preset to $26.00/bag", ins_html)
        self.assertIn("Price Outlier Alerts", ins_html)
        self.assertIn("Country Co-op", ins_html)
        self.assertIn("Cheapest Grain $/lb", ins_html)
        self.assertIn("$8.00/bale", ins_html)
        self.assertIn("Enterprise Spend &amp; Cash P&amp;L Scorecard", ins_html)
        self.assertIn("Laying Flocks &amp; Eggs", ins_html)
        self.assertIn("Alpaca &amp; Fiber Herds", ins_html)
        self.assertIn("Egg Feed Cost per Dozen by Month", ins_html)
        self.assertIn("$13.60", ins_html)  # Oct: $68 / 5 doz
        self.assertIn("$7.70", ins_html)   # Nov: $77 / 10 doz
        self.assertIn("Batch-to-Batch Actuals &amp; FCR Benchmarks", ins_html)
        self.assertIn("Spring Broilers", ins_html)
        self.assertIn("2.22", ins_html)
        self.assertEqual(self.c.get("/financials?tab=insights&year=all").status_code, 200)

        # 5. Apply 1-click preset calibration sync for layer feed
        r_sync = self.c.post(
            "/financials/insights/sync-preset-price",
            data={"kind": "layer", "price": "26.00", "year": "2026"},
            follow_redirects=False,
        )
        self.assertEqual(r_sync.status_code, 303)
        self.assertIn("tab=insights", r_sync.headers["location"])
        self.assertAlmostEqual(float(get_layer_settings(1)["bag_price"]), 26.0, places=2)

        # 6. Staff role cannot view Insights or mutate preset prices -> 403
        c_staff = TestClient(app)
        c_staff.post("/login", data={"username": "mom", "password": "secret"}, follow_redirects=False)
        self.assertEqual(c_staff.get("/financials?tab=insights").status_code, 403)
        self.assertEqual(
            c_staff.post(
                "/financials/insights/sync-preset-price",
                data={"kind": "layer", "price": "20.00"},
                follow_redirects=False,
            ).status_code,
            403,
        )

    def test_historical_sale_dates_live_pet_sales_and_edit_archived_batch(self):
        from zoneinfo import ZoneInfo
        from homestead.db.batches import list_batch_sales, list_batches, save_batch
        from homestead.db.customers import save_customer
        from homestead.db.shares import list_batch_harvest_shares
        from homestead.scheduling import compute_batch_schedule
        from homestead.tax_books import build_unified_tax_books
        from shared.bridge import list_homestead_paid_transactions

        self.c.post(
            "/register",
            data={"display_name": "Steve", "username": "steve", "password": "secret"},
            follow_redirects=False,
        )
        cust_pet = save_customer(1, name="Alice PetBuyer", phone="555-111-2222")
        cust_meat = save_customer(1, name="Bob MeatBuyer", phone="555-333-4444")

        # 1. Create a historical batch explicitly saved as Archived
        r_save = self.c.post(
            "/batches/save",
            data={
                "name": "Kune Winter Litter",
                "species": "kunekune pigs",
                "start_date": "2025-07-01",
                "initial_quantity": "4",
                "unit_cost": "50.00",
                "avg_dressed_weight": "100.0",
                "sale_price_per_lb": "6.00",
                "processing_cost": "0.00",
                "lifecycle_type": "mammal_breeding",
                "sale_unit": "shares",
                "customer_pays_butcher": "1",
                "is_archived": "1",
                "next_url": "/batches",
            },
            follow_redirects=False,
        )
        self.assertEqual(r_save.status_code, 303)
        batch = list_batches(user_id=1)[0]
        bid = int(batch["id"])
        self.assertEqual(int(batch["active"]), 0)
        self.assertNotIn("Kune Winter Litter", self.c.get("/").text)

        # Editing preserves archived flag whether passed explicitly or via API save_batch without 'active'
        self.c.post(
            "/batches/save",
            data={
                "batch_id": str(bid),
                "name": "Kune Winter Litter Updated",
                "species": "kunekune pigs",
                "start_date": "2025-07-01",
                "initial_quantity": "4",
                "unit_cost": "50.00",
                "avg_dressed_weight": "100.0",
                "sale_price_per_lb": "6.00",
                "processing_cost": "0.00",
                "lifecycle_type": "mammal_breeding",
                "sale_unit": "shares",
                "is_archived": "1",
                "notes": "Updated notes",
                "next_url": "/batches",
            },
            follow_redirects=False,
        )
        b_after = list_batches(user_id=1)[0]
        self.assertEqual(int(b_after["active"]), 0)
        b_no_active = {k: v for k, v in b_after.items() if k != "active"}
        save_batch(b_no_active, batch_id=bid, user_id=1)
        self.assertEqual(int(list_batches(user_id=1)[0]["active"]), 0)

        # 2. Log 1 loss + 1 Live Sale (sold as pet in July 2025) + 2 harvested pig carcasses in Jan 2026
        self.c.post(
            "/batches/log-loss",
            data={"batch_id": str(bid), "loss_date": "2025-07-05", "count": "1", "reason": "Runt", "next_url": f"/batches/{bid}"},
            follow_redirects=False,
        )
        r_live = self.c.post(
            "/batches/log-sale",
            data={
                "batch_id": str(bid),
                "entry_date": "2025-07-15",
                "allocation_type": "live",
                "weight_lbs": "0",
                "head_count": "1",
                "customer_id": str(cust_pet),
                "revenue": "250.00",
                "payment_method": "venmo",
                "notes": "Sold live as pet",
                "next_url": f"/batches/{bid}",
            },
            follow_redirects=False,
        )
        self.assertEqual(r_live.status_code, 303)

        for idx in (1, 2):
            r_h = self.c.post(
                f"/api/batches/{bid}/harvest",
                json={
                    "harvest_date": "2026-01-10",
                    "dressed_lbs": 100.0,
                    "animal_label": f"Pig #{idx}",
                    "split_mode": "halves",
                    "weight_basis": "hanging",
                },
            )
            self.assertEqual(r_h.status_code, 200)

        sched = compute_batch_schedule(list_batches(user_id=1)[0])
        self.assertEqual(sched["surviving_quantity"], 3)
        self.assertEqual(sched["live_sold_head"], 1)
        self.assertEqual(sched["on_farm_head"], 0)
        self.assertAlmostEqual(sched["sold_lbs"], 0.0, places=2)
        self.assertAlmostEqual(sched["actual_sales_revenue"], 250.0, places=2)

        tally_html = self.c.get(f"/batches/{bid}/harvest").text
        self.assertIn("2 / 2", tally_html)
        self.assertIn("1 sold live", tally_html)

        # 3. Assign shares and stamp explicit historical paid_date in Jan 2026
        shares = list_batch_harvest_shares(bid)
        sh_a = int(shares[0]["id"])
        sh_b = int(shares[1]["id"])
        r_asgn_a = self.c.post(
            f"/api/batches/{bid}/shares/{sh_a}/assign",
            json={
                "customer_id": cust_meat,
                "for_sale": True,
                "actual_sale_price": 300.0,
                "payment_status": "paid",
                "pickup_status": "picked_up",
                "payment_method": "cash",
                "paid_date": "2026-01-18",
            },
        )
        self.assertEqual(r_asgn_a.status_code, 200)
        r_asgn_b = self.c.post(
            f"/api/batches/{bid}/shares/{sh_b}/assign",
            json={
                "customer_id": cust_meat,
                "for_sale": True,
                "actual_sale_price": 300.0,
                "payment_status": "unpaid",
                "pickup_status": "assigned",
            },
        )
        self.assertEqual(r_asgn_b.status_code, 200)

        # Settle pending share on Pickup Day with custom paid_date, then edit Completed Pickup paid_date
        self.assertEqual(
            self.c.post(
                f"/batches/{bid}/pickup/{cust_meat}/settle-shares",
                data={
                    "collected_today": "300.00",
                    "payment_method": "check",
                    "paid_date": "2026-01-19",
                    "action": "mark_picked_up",
                    "share_ids": [str(sh_b)],
                },
                follow_redirects=False,
            ).status_code,
            303,
        )
        self.assertEqual(
            self.c.post(
                f"/batches/{bid}/pickup/{cust_meat}/payment-method",
                data={"payment_method": "venmo", "paid_date": "2026-01-22"},
                follow_redirects=False,
            ).status_code,
            303,
        )
        shares_now = {int(s["id"]): s for s in list_batch_harvest_shares(bid)}
        self.assertEqual(shares_now[sh_a]["paid_at"], "2026-01-22")
        self.assertEqual(shares_now[sh_b]["paid_at"], "2026-01-22")
        self.assertEqual(shares_now[sh_b]["payment_method"], "venmo")

        # Update share B paid_date directly via Harvest Tally API
        self.assertEqual(
            self.c.post(
                f"/api/batches/{bid}/shares/{sh_b}/assign",
                json={
                    "customer_id": cust_meat,
                    "for_sale": True,
                    "actual_sale_price": 300.0,
                    "payment_status": "paid",
                    "pickup_status": "picked_up",
                    "payment_method": "card",
                    "paid_date": "2026-01-25",
                },
            ).status_code,
            200,
        )
        self.assertEqual(
            next(s for s in list_batch_harvest_shares(bid) if int(s["id"]) == sh_b)["paid_at"],
            "2026-01-25",
        )

        # 4. Verify Tax Books attributes 2025 vs 2026 cash revenue accurately & Iron Ledger bridge preserves exact dates
        tb_2025 = build_unified_tax_books(1, tax_year=2025)
        tb_2026 = build_unified_tax_books(1, tax_year=2026)
        self.assertAlmostEqual(tb_2025["totals"]["homestead_income"], 250.0, places=2)
        self.assertAlmostEqual(tb_2026["totals"]["homestead_income"], 600.0, places=2)

        bridge_sales = list_homestead_paid_transactions(1, tz=ZoneInfo("America/New_York"))
        dates_seen = {s["date_str"] for s in bridge_sales}
        self.assertIn("2025-07-15", dates_seen)
        self.assertIn("2026-01-22", dates_seen)
        self.assertIn("2026-01-25", dates_seen)
        live_bridge = next(s for s in bridge_sales if s["date_str"] == "2025-07-15")
        self.assertEqual(live_bridge["customer_name"], "Alice PetBuyer")
        self.assertEqual(live_bridge["total_cents"], 25000)

        # Customer profile displays historical live sale & paid dates
        cust_html = self.c.get(f"/customers?selected={cust_pet}").text
        self.assertIn("2025-07-15", cust_html)
        self.assertIn("Live Sale (1 hd)", cust_html)
        self.assertIn("$250.00", cust_html)

        # Edit existing batch_sales date & method inline on batch_detail
        live_sale_id = int(list_batch_sales(bid)[0]["id"])
        self.assertEqual(
            self.c.post(
                f"/batches/{bid}/sales/{live_sale_id}/payment-method",
                data={"payment_method": "cash", "entry_date": "2025-07-20"},
                follow_redirects=False,
            ).status_code,
            303,
        )
        updated_bs = list_batch_sales(bid)[0]
        self.assertEqual(updated_bs["entry_date"], "2025-07-20")
        self.assertEqual(updated_bs["payment_method"], "cash")
        bd_html = self.c.get(f"/batches/{bid}").text
        self.assertIn("1 sold live", bd_html)
        self.assertIn("Live Sale (1 hd)", bd_html)

    def test_bulk_receipts_api_csv_import_and_historical_batches(self):
        from unittest.mock import patch
        from homestead.db.batches import list_batches
        from homestead.scheduling import compute_batch_schedule
        from homestead.tax_books import build_unified_tax_books

        self.c.post(
            "/register",
            data={"display_name": "Steve", "username": "steve", "password": "secret"},
            follow_redirects=False,
        )
        self.c.post(
            "/settings/members",
            data={"username": "mom", "display_name": "Mom", "password": "secret", "role": "staff"},
            follow_redirects=False,
        )

        # Seed existing archived batch & herd for CSV resolution
        self.c.post(
            "/batches/save",
            data={
                "name": "Old Broilers",
                "species": "cornish cross",
                "start_date": "2025-05-01",
                "initial_quantity": "25",
                "is_archived": "1",
                "next_url": "/batches",
            },
            follow_redirects=False,
        )
        self.c.post(
            "/herds/save",
            data={"name": "Sheep Flock", "species": "sheep", "herd_type": "fiber", "head_count": "4"},
            follow_redirects=False,
        )

        # 1. Bulk Receipts UI & template download
        br_resp = self.c.get("/financials/bulk-receipts")
        self.assertEqual(br_resp.status_code, 200)
        self.assertIn("Bulk Receipt Upload &amp; Import", br_resp.text)
        tpl = self.c.get("/financials/bulk-receipts/template.csv")
        self.assertEqual(tpl.status_code, 200)
        self.assertIn("batch_or_herd,feed_type", tpl.text)

        # 2. Queue sequential JSON save + Drive upload & duplicate detection API
        with patch("homestead.gdrive.upload_receipt_to_drive", return_value=("drv-b1", "https://drive.google.com/file/d/drv-b1/view")):
            r_api = self.c.post(
                "/api/receipts/save",
                data={
                    "receipt_date": "2025-06-14",
                    "vendor": "Tractor Supply",
                    "lines_json": json.dumps([
                        {"kind": "general", "category": "Supplies & Equipment", "amount": 64.95, "notes": "Waterer"}
                    ]),
                },
                files={"receipt_file": ("r1.jpg", b"jpeg-bytes", "image/jpeg")},
            )
        self.assertEqual(r_api.status_code, 200)
        d_api = r_api.json()
        self.assertTrue(d_api["ok"])
        self.assertEqual(d_api["saved_count"], 1)
        self.assertAlmostEqual(d_api["total_amount"], 64.95, places=2)

        r_dup = self.c.post(
            "/api/receipts/check-duplicates",
            json={
                "items": [
                    {"date": "2025-06-14", "vendor": "tractor supply", "total": 64.95},
                    {"date": "2025-06-20", "vendor": "tractor supply", "total": 99.00},
                ]
            },
        )
        self.assertEqual(r_dup.status_code, 200)
        self.assertEqual(r_dup.json()["duplicates"], [0])

        # 3. Receipt CSV preview (including dup detection + error detection) + commit
        csv_body = "\n".join([
            "date,vendor,kind,category,batch_or_herd,feed_type,quantity,unit,weight_lbs,amount,notes,receipt_url",
            "2025-06-14,Tractor Supply,general,Supplies & Equipment,,,,,,64.95,Dup waterer,",
            "2025-05-20,Local Mill,batch_feed,,Old Broilers,grower,4,bags,200,88.00,Grower pellets,",
            "2025-11-05,Country Co-op,layer_feed,,all,layer,2,bags,100,46.00,Layer mash,",
            "2026-01-10,Valley Hay,herd_feed,,Sheep Flock,hay,10,bales,500,85.00,Winter hay,",
            "bad-date,Broken Vendor,general,Other,,,,,,12.00,Bad row,",
        ])
        prev = self.c.post(
            "/financials/bulk-receipts/csv-preview",
            files={"csv_file": ("receipts.csv", csv_body.encode("utf-8"), "text/csv")},
        )
        self.assertEqual(prev.status_code, 200)
        self.assertIn("Possible duplicate", prev.text)
        self.assertIn("Invalid or missing date", prev.text)
        self.assertIn("4 valid", prev.text)

        from homestead.bulk_import import parse_receipt_csv
        from homestead.db.herds import list_herds

        parsed_rcpts = parse_receipt_csv(
            csv_body.encode("utf-8"),
            batches=list_batches(user_id=1),
            herds=list_herds(1, include_inactive=True),
            bag_weight=50,
            existing_user_id=1,
        )
        to_commit = [r for r in parsed_rcpts["rows"] if r["is_valid"] and not r["is_duplicate"]]
        self.assertEqual(len(to_commit), 3)
        r_commit = self.c.post(
            "/financials/bulk-receipts/csv-commit",
            data={"rows_json": json.dumps(to_commit)},
            follow_redirects=False,
        )
        self.assertEqual(r_commit.status_code, 303)
        tb_all = build_unified_tax_books(1, tax_year=None)
        self.assertAlmostEqual(tb_all["totals"]["total_expenses"], round(64.95 + 88.0 + 46.0 + 85.0, 2), places=2)

        # 4. Historical Batch Quick Form + CSV Import
        self.assertEqual(self.c.get("/batches/import").status_code, 200)
        self.assertEqual(self.c.get("/batches/import/template.csv").status_code, 200)

        r_quick = self.c.post(
            "/batches/import/quick",
            data={
                "name": "Kune 2024 Cohort",
                "species": "kunekune pigs",
                "start_date": "2024-08-01",
                "initial_quantity": "6",
                "unit_cost": "40.00",
                "sale_unit": "shares",
                "harvest_date": "2025-02-01",
                "harvested_head": "3",
                "dressed_lbs": "250.0",
                "processing_cost": "100.00",
                "feed_lbs": "1200.0",
                "feed_cost": "540.00",
                "losses": "1",
                "live_sold_head": "2",
                "live_sale_date": "2024-10-15",
                "live_sale_revenue": "300.00",
                "meat_sale_date": "2025-02-10",
                "meat_sale_revenue": "750.00",
                "freezer_lbs": "50.0",
                "payment_method": "venmo",
                "notes": "2 pets in 2024 + 3 butchered in 2025",
            },
            follow_redirects=False,
        )
        self.assertEqual(r_quick.status_code, 303)
        kune_b = next(b for b in list_batches(user_id=1) if b["name"] == "Kune 2024 Cohort")
        self.assertEqual(int(kune_b["active"]), 0)
        ks = compute_batch_schedule(kune_b)
        self.assertEqual(ks["surviving_quantity"], 5)
        self.assertEqual(ks["live_sold_head"], 2)
        self.assertEqual(ks["harvested_head"], 3)
        self.assertAlmostEqual(ks["freezer_lbs"], 50.0, places=2)
        self.assertAlmostEqual(ks["sold_lbs"], 200.0, places=2)
        self.assertAlmostEqual(ks["actual_sales_revenue"], 1050.0, places=2)
        self.assertAlmostEqual(build_unified_tax_books(1, tax_year=2024)["totals"]["homestead_income"], 300.0, places=2)
        self.assertAlmostEqual(build_unified_tax_books(1, tax_year=2025)["totals"]["homestead_income"], 750.0, places=2)

        batch_csv = "\n".join([
            "name,species,start_date,initial_quantity,unit_cost,sale_unit,harvest_date,harvested_head,dressed_lbs,processing_cost,feed_lbs,feed_cost,losses,live_sold_head,live_sale_date,live_sale_revenue,meat_sale_date,meat_sale_revenue,freezer_lbs,payment_method,notes",
            "Heritage Turkeys 2024,turkeys,2024-06-01,15,12.00,whole,2024-11-18,14,224.0,112.00,600,264.00,1,0,,0,2024-11-23,1100.00,0,cash,Thanksgiving birds",
        ])
        b_prev = self.c.post(
            "/batches/import/csv-preview",
            files={"csv_file": ("batches.csv", batch_csv.encode("utf-8"), "text/csv")},
        )
        self.assertEqual(b_prev.status_code, 200)
        self.assertIn("Heritage Turkeys 2024", b_prev.text)
        from homestead.bulk_import import parse_batch_csv
        from homestead.db.core import get_species_catalog
        parsed_b = parse_batch_csv(batch_csv.encode("utf-8"), species_catalog=get_species_catalog(user_id=1))
        r_bc = self.c.post(
            "/batches/import/csv-commit",
            data={"rows_json": json.dumps(parsed_b["rows"])},
            follow_redirects=False,
        )
        self.assertEqual(r_bc.status_code, 303)
        turk_b = next(b for b in list_batches(user_id=1) if b["name"] == "Heritage Turkeys 2024")
        self.assertEqual(int(turk_b["active"]), 0)
        self.assertAlmostEqual(build_unified_tax_books(1, tax_year=2024)["totals"]["homestead_income"], 1400.0, places=2)

        # 5. Staff role RBAC -> 403 on all bulk receipt & batch import endpoints
        c_staff = TestClient(app)
        c_staff.post("/login", data={"username": "mom", "password": "secret"}, follow_redirects=False)
        for method, path, kw in [
            ("GET", "/financials/bulk-receipts", {}),
            ("GET", "/financials/bulk-receipts/template.csv", {}),
            ("POST", "/api/receipts/save", {"data": {"lines_json": "[]"}}),
            ("POST", "/api/receipts/check-duplicates", {"json": {"items": []}}),
            ("POST", "/financials/bulk-receipts/csv-preview", {"files": {"csv_file": ("a.csv", b"date\n", "text/csv")}}),
            ("POST", "/financials/bulk-receipts/csv-commit", {"data": {"rows_json": "[]"}}),
            ("GET", "/batches/import", {}),
            ("GET", "/batches/import/template.csv", {}),
            ("POST", "/batches/import/quick", {"data": {"name": "X", "species": "turkeys", "start_date": "2025-01-01", "initial_quantity": "5"}}),
            ("POST", "/batches/import/csv-preview", {"files": {"csv_file": ("b.csv", b"name\n", "text/csv")}}),
            ("POST", "/batches/import/csv-commit", {"data": {"rows_json": "[]"}}),
        ]:
            resp = c_staff.get(path, follow_redirects=False) if method == "GET" else c_staff.post(path, follow_redirects=False, **kw)
            self.assertEqual(resp.status_code, 403, f"Expected 403 for staff {method} {path}")


if __name__ == "__main__":
    unittest.main()
