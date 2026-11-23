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
        r_b = self.c.post("/batches/save", data={
            "name": "Spring Broilers",
            "species": "cornish cross",
            "start_date": "2026-11-01",
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


if __name__ == "__main__":
    unittest.main()
