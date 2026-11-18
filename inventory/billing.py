"""Stripe subscription billing, webhook verification, and lifetime/comp passes."""

from datetime import datetime, timedelta
import hashlib
import hmac
import json
import os
import time

import httpx

from inventory.db import get_connection
from inventory.merchants import OWNER, create_invite

STRIPE_API = "https://api.stripe.com/v1"


def _now_dt() -> datetime:
    return datetime.utcnow()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def price_catalog() -> dict[str, dict]:
    """Configured Stripe Price IDs from environment variables."""
    out: dict[str, dict] = {}
    monthly = os.environ.get("STRIPE_PRICE_MONTHLY", "").strip()
    annual = os.environ.get("STRIPE_PRICE_ANNUAL", "").strip()
    if monthly:
        out["monthly"] = {
            "price_id": monthly,
            "label": os.environ.get("STRIPE_LABEL_MONTHLY", "Monthly Plan"),
            "price_display": os.environ.get("STRIPE_DISPLAY_MONTHLY", "$29 / month"),
        }
    if annual:
        out["annual"] = {
            "price_id": annual,
            "label": os.environ.get("STRIPE_LABEL_ANNUAL", "Annual Plan"),
            "price_display": os.environ.get("STRIPE_DISPLAY_ANNUAL", "$290 / year"),
        }
    return out


def configured() -> bool:
    return bool(os.environ.get("STRIPE_SECRET_KEY", "").strip() and price_catalog())


def webhook_configured() -> bool:
    return bool(os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip())


def _secret_key() -> str:
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        raise ValueError("STRIPE_SECRET_KEY is not configured.")
    return key


def verify_webhook_signature(
    payload: bytes,
    sig_header: str,
    secret: str | None = None,
    tolerance_seconds: int = 300,
) -> bool:
    """Verify Stripe's ``Stripe-Signature: t=...,v1=...`` HMAC-SHA256 header."""
    wh_secret = (secret if secret is not None else os.environ.get("STRIPE_WEBHOOK_SECRET", "")).strip()
    if not wh_secret or not sig_header:
        return False
    parts = {}
    v1_sigs = []
    for item in sig_header.split(","):
        k, _, v = item.strip().partition("=")
        if k == "t":
            parts["t"] = v
        elif k == "v1":
            v1_sigs.append(v)
    if "t" not in parts or not v1_sigs:
        return False
    try:
        ts = int(parts["t"])
    except ValueError:
        return False
    if tolerance_seconds > 0 and abs(int(time.time()) - ts) > tolerance_seconds:
        return False
    signed = f"{parts['t']}.".encode("utf-8") + payload
    expected = hmac.new(wh_secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in v1_sigs)


def _stripe_post(path: str, data: dict, http: httpx.Client | None = None) -> dict:
    headers = {"Authorization": f"Bearer {_secret_key()}"}
    close = http is None
    client = http or httpx.Client(timeout=15)
    try:
        resp = client.post(f"{STRIPE_API}{path}", data=data, headers=headers)
        if resp.status_code >= 400:
            raise ValueError(f"Stripe API error ({resp.status_code}): {resp.text[:200]}")
        return resp.json()
    finally:
        if close:
            client.close()


def _stripe_get(path: str, http: httpx.Client | None = None) -> dict:
    headers = {"Authorization": f"Bearer {_secret_key()}"}
    close = http is None
    client = http or httpx.Client(timeout=15)
    try:
        resp = client.get(f"{STRIPE_API}{path}", headers=headers)
        if resp.status_code >= 400:
            raise ValueError(f"Stripe API error ({resp.status_code}): {resp.text[:200]}")
        return resp.json()
    finally:
        if close:
            client.close()


def create_checkout_session(
    plan_key: str,
    success_url: str,
    cancel_url: str,
    customer_email: str = "",
    merchant_name: str = "",
    http: httpx.Client | None = None,
) -> dict:
    catalog = price_catalog()
    if plan_key not in catalog:
        raise ValueError(f"Unknown or unconfigured plan: {plan_key}")
    data: dict[str, str] = {
        "mode": "subscription",
        "line_items[0][price]": catalog[plan_key]["price_id"],
        "line_items[0][quantity]": "1",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata[plan_key]": plan_key,
    }
    if merchant_name.strip():
        data["metadata[merchant_name]"] = merchant_name.strip()[:80]
    if customer_email.strip():
        data["customer_email"] = customer_email.strip()
    trial_days = int(os.environ.get("STRIPE_TRIAL_DAYS", "0") or 0)
    if trial_days > 0:
        data["subscription_data[trial_period_days]"] = str(trial_days)
    return _stripe_post("/checkout/sessions", data, http=http)


def fetch_checkout_session(session_id: str, http: httpx.Client | None = None) -> dict:
    return _stripe_get(f"/checkout/sessions/{session_id}", http=http)


def create_portal_session(
    customer_id: str, return_url: str, http: httpx.Client | None = None
) -> dict:
    return _stripe_post(
        "/billing_portal/sessions",
        {"customer": customer_id, "return_url": return_url},
        http=http,
    )


def get_subscription(merchant_id: int) -> dict:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM merchant_subscriptions WHERE merchant_id = ?",
            (int(merchant_id),),
        ).fetchone()
        if row:
            return dict(row)
        return {
            "merchant_id": int(merchant_id),
            "stripe_customer_id": None,
            "stripe_subscription_id": None,
            "plan_key": "lifetime",
            "status": "comp",
            "current_period_end": None,
            "grace_until": None,
            "updated_at": "",
        }
    finally:
        conn.close()


def upsert_subscription(
    merchant_id: int,
    status: str,
    plan_key: str = "standard",
    stripe_customer_id: str | None = None,
    stripe_subscription_id: str | None = None,
    current_period_end: str | None = None,
    grace_until: str | None = None,
) -> None:
    now = _iso(_now_dt())
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO merchant_subscriptions
                (merchant_id, stripe_customer_id, stripe_subscription_id, plan_key,
                 status, current_period_end, grace_until, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(merchant_id) DO UPDATE SET
                stripe_customer_id = COALESCE(excluded.stripe_customer_id, merchant_subscriptions.stripe_customer_id),
                stripe_subscription_id = COALESCE(excluded.stripe_subscription_id, merchant_subscriptions.stripe_subscription_id),
                plan_key = excluded.plan_key,
                status = excluded.status,
                current_period_end = COALESCE(excluded.current_period_end, merchant_subscriptions.current_period_end),
                grace_until = excluded.grace_until,
                updated_at = excluded.updated_at
            """,
            (
                int(merchant_id),
                stripe_customer_id,
                stripe_subscription_id,
                plan_key,
                status,
                current_period_end,
                grace_until,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def grant_lifetime(merchant_id: int) -> None:
    upsert_subscription(merchant_id, status="comp", plan_key="lifetime", grace_until=None)


def extend_grace(merchant_id: int, days: int = 7) -> None:
    sub = get_subscription(merchant_id)
    until = _iso(_now_dt() + timedelta(days=max(1, int(days))))
    upsert_subscription(
        merchant_id,
        status="past_due",
        plan_key=sub["plan_key"],
        grace_until=until,
    )


def suspend_merchant(merchant_id: int) -> None:
    sub = get_subscription(merchant_id)
    upsert_subscription(
        merchant_id,
        status="canceled",
        plan_key=sub["plan_key"],
        grace_until=None,
    )


def is_access_allowed(sub: dict) -> tuple[bool, str]:
    status = sub.get("status", "comp")
    if status in ("comp", "active", "trialing"):
        return True, ""
    if status == "past_due":
        grace = sub.get("grace_until") or ""
        if grace and grace >= _iso(_now_dt()):
            return True, "grace"
        return False, "past_due"
    return False, "canceled"


def record_checkout_invite(
    session_id: str,
    invite_token: str,
    stripe_customer_id: str | None,
    stripe_subscription_id: str | None,
    plan_key: str,
) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO billing_checkout_invites
                (stripe_session_id, invite_token, stripe_customer_id,
                 stripe_subscription_id, plan_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                invite_token,
                stripe_customer_id,
                stripe_subscription_id,
                plan_key,
                _iso(_now_dt()),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_checkout_invite_by_session(session_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM billing_checkout_invites WHERE stripe_session_id = ?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_checkout_invite_by_token(token: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM billing_checkout_invites WHERE invite_token = ?",
            (token,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def ensure_invite_for_checkout_session(session: dict) -> str:
    sid = str(session.get("id") or "")
    if not sid:
        raise ValueError("Missing checkout session id.")
    existing = get_checkout_invite_by_session(sid)
    if existing:
        return existing["invite_token"]
    meta = session.get("metadata") or {}
    plan_key = str(meta.get("plan_key") or "monthly")
    merchant_name = str(meta.get("merchant_name") or "")
    email = str(
        session.get("customer_email")
        or (session.get("customer_details") or {}).get("email")
        or ""
    )
    token = create_invite(
        merchant_id=None,
        role=OWNER,
        default_merchant_name=merchant_name,
        note=f"Stripe ({plan_key}) {email}".strip(),
        days_valid=30,
    )
    record_checkout_invite(
        sid,
        token,
        session.get("customer"),
        session.get("subscription"),
        plan_key,
    )
    return token


def _find_merchant_by_stripe(customer_id: str | None, subscription_id: str | None) -> int | None:
    conn = get_connection()
    try:
        if subscription_id:
            r = conn.execute(
                "SELECT merchant_id FROM merchant_subscriptions WHERE stripe_subscription_id = ?",
                (subscription_id,),
            ).fetchone()
            if r:
                return int(r["merchant_id"])
        if customer_id:
            r = conn.execute(
                "SELECT merchant_id FROM merchant_subscriptions WHERE stripe_customer_id = ?",
                (customer_id,),
            ).fetchone()
            if r:
                return int(r["merchant_id"])
        return None
    finally:
        conn.close()


def process_webhook_event(event: dict) -> dict:
    etype = str(event.get("type") or "")
    obj = (event.get("data") or {}).get("object") or {}

    if etype == "checkout.session.completed":
        if obj.get("mode") == "subscription" and obj.get("payment_status") in ("paid", "no_payment_required"):
            token = ensure_invite_for_checkout_session(obj)
            return {"handled": True, "action": "invite_created", "token": token}
        return {"handled": False}

    sub_id = obj.get("subscription") or (obj.get("id") if etype.startswith("customer.subscription.") else None)
    cust_id = obj.get("customer")
    mid = _find_merchant_by_stripe(cust_id, sub_id)
    if not mid:
        return {"handled": False, "reason": "unknown_merchant"}

    cur = get_subscription(mid)
    if cur["status"] == "comp":
        return {"handled": True, "action": "ignored_comp"}

    if etype == "invoice.paid":
        period_end = None
        lines = (obj.get("lines") or {}).get("data") or []
        if lines and (lines[0].get("period") or {}).get("end"):
            period_end = _iso(datetime.utcfromtimestamp(int(lines[0]["period"]["end"])))
        upsert_subscription(
            mid,
            status="active",
            plan_key=cur["plan_key"],
            stripe_customer_id=cust_id,
            stripe_subscription_id=sub_id,
            current_period_end=period_end,
            grace_until=None,
        )
        return {"handled": True, "action": "activated"}

    if etype == "invoice.payment_failed":
        extend_grace(mid, days=7)
        return {"handled": True, "action": "past_due_grace"}

    if etype == "customer.subscription.deleted":
        suspend_merchant(mid)
        return {"handled": True, "action": "canceled"}

    if etype == "customer.subscription.updated":
        st = str(obj.get("status") or "active")
        mapped = st if st in ("trialing", "active", "past_due", "canceled") else "active"
        period_end = (
            _iso(datetime.utcfromtimestamp(int(obj["current_period_end"])))
            if obj.get("current_period_end")
            else None
        )
        grace = cur["grace_until"] if mapped == "past_due" else None
        if mapped == "past_due" and not grace:
            grace = _iso(_now_dt() + timedelta(days=7))
        upsert_subscription(
            mid,
            status=mapped,
            plan_key=cur["plan_key"],
            stripe_customer_id=cust_id,
            stripe_subscription_id=sub_id,
            current_period_end=period_end,
            grace_until=grace,
        )
        return {"handled": True, "action": f"updated_{mapped}"}

    return {"handled": False}
