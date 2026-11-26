import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from inventory import billing
from inventory.app import app
from inventory.db import init_inventory_db
from inventory.merchants import create_merchant
from shared.auth import create_user


def _sign(payload: bytes, secret: str) -> str:
    ts = str(int(time.time()))
    mac = hmac.new(secret.encode("utf-8"), f"{ts}.".encode("utf-8") + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


class BillingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_env = {
            k: os.environ.get(k)
            for k in ("FARM_DB_PATH", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PRICE_MONTHLY")
        }
        os.environ["FARM_DB_PATH"] = os.path.join(self.tmp.name, "test.db")
        os.environ["STRIPE_SECRET_KEY"] = "sk_test_123"
        os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test_123"
        os.environ["STRIPE_PRICE_MONTHLY"] = "price_monthly_123"
        init_inventory_db()
        self.admin = create_user("admin", "adminpass", "Admin", is_admin=True)
        self.zach = create_user("zach", "zachpass", "Zach", is_admin=False)
        self.mid = create_merchant("Willow Creek Apiary", self.zach["id"])

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _login(self, username: str, password: str) -> TestClient:
        c = TestClient(app)
        r = c.post("/login", data={"username": username, "password": password}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        return c

    def test_webhook_signature_and_checkout_to_invite_flow(self):
        c = TestClient(app)
        signup_html = c.get("/signup").text
        self.assertIn("Monthly Plan", signup_html)

        event = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_abc",
                    "mode": "subscription",
                    "payment_status": "paid",
                    "customer": "cus_123",
                    "subscription": "sub_123",
                    "customer_email": "newfarm@example.com",
                    "metadata": {"plan_key": "monthly", "merchant_name": "Oak Ridge Farm"},
                }
            },
        }
        body = json.dumps(event).encode("utf-8")
        self.assertEqual(c.post("/webhooks/stripe", content=body, headers={"stripe-signature": "bad"}).status_code, 400)

        resp = c.post(
            "/webhooks/stripe",
            content=body,
            headers={"stripe-signature": _sign(body, "whsec_test_123")},
        )
        self.assertEqual(resp.status_code, 200)
        token = resp.json()["token"]

        # /onboarding/complete redirects straight to the generated invite
        done = c.get("/onboarding/complete?session_id=cs_test_abc", follow_redirects=False)
        self.assertEqual(done.status_code, 303)
        self.assertEqual(done.headers["location"], f"/invite/{token}")

        # Redeeming the invite creates the merchant and links active Stripe subscription
        acc = c.post(
            f"/invite/{token}",
            data={"username": "oakfarmer", "password": "secretpw", "display_name": "Oak", "merchant_name": "Oak Ridge Farm"},
            follow_redirects=False,
        )
        self.assertEqual(acc.status_code, 303)
        settings = c.get("/settings").text
        self.assertIn("Active", settings)
        self.assertIn("Manage billing", settings)

    def test_grace_period_enforcement_and_admin_lifetime_override(self):
        # Default merchant (Zach) starts as Lifetime (comp)
        zach_c = self._login("zach", "zachpass")
        self.assertIn("Lifetime access", zach_c.get("/settings").text)

        # Simulate a paid subscription failing then canceling
        billing.upsert_subscription(
            self.mid,
            status="active",
            plan_key="monthly",
            stripe_customer_id="cus_z",
            stripe_subscription_id="sub_z",
        )
        fail_ev = json.dumps({
            "type": "invoice.payment_failed",
            "data": {"object": {"customer": "cus_z", "subscription": "sub_z"}},
        }).encode("utf-8")
        c = TestClient(app)
        self.assertEqual(
            c.post("/webhooks/stripe", content=fail_ev, headers={"stripe-signature": _sign(fail_ev, "whsec_test_123")}).status_code,
            200,
        )
        # During 7-day grace, inventory still loads with an amber warning banner
        inv = zach_c.get("/inventory")
        self.assertEqual(inv.status_code, 200)
        self.assertIn("Payment failed.", inv.text)

        # Cancel subscription -> access redirects to /billing
        del_ev = json.dumps({
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": "sub_z", "customer": "cus_z"}},
        }).encode("utf-8")
        self.assertEqual(
            c.post("/webhooks/stripe", content=del_ev, headers={"stripe-signature": _sign(del_ev, "whsec_test_123")}).status_code,
            200,
        )
        blocked = zach_c.get("/inventory", follow_redirects=False)
        self.assertEqual(blocked.status_code, 303)
        self.assertEqual(blocked.headers["location"], "/billing")

        # Super Admin grants lifetime access -> Zach can access /inventory again
        admin_c = self._login("admin", "adminpass")
        self.assertIn("Paused", admin_c.get("/admin").text)
        admin_c.post("/admin/merchants/billing", data={"merchant_id": str(self.mid), "action": "comp"}, follow_redirects=False)
        self.assertEqual(zach_c.get("/inventory").status_code, 200)
        self.assertIn("Lifetime", admin_c.get("/admin").text)


if __name__ == "__main__":
    unittest.main()
