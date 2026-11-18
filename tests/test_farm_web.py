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


if __name__ == "__main__":
    unittest.main()
