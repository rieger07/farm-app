"""Stripe subscription billing, self-serve signup checkout, and webhook routes."""

import json
import os
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from inventory import billing
from inventory.web import Ctx, render, require, templates

router = APIRouter()


@router.get("/signup")
def signup_page(request: Request, msg: str = ""):
    return templates.TemplateResponse(
        request,
        "signup.html",
        {
            "plans": billing.price_catalog(),
            "stripe_configured": billing.configured(),
            "trial_days": int(os.environ.get("STRIPE_TRIAL_DAYS", "0") or 0),
            "msg": msg,
        },
    )


@router.post("/signup/checkout")
def signup_checkout(
    request: Request,
    plan_key: str = Form(...),
    merchant_name: str = Form(""),
    email: str = Form(""),
):
    if not billing.configured():
        return RedirectResponse("/signup?msg=Stripe+is+not+configured+yet.", status_code=303)
    base = str(request.base_url).rstrip("/")
    try:
        sess = billing.create_checkout_session(
            plan_key=plan_key,
            success_url=f"{base}/onboarding/complete?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}/signup?msg=Checkout+canceled.",
            customer_email=email,
            merchant_name=merchant_name,
        )
        return RedirectResponse(sess["url"], status_code=303)
    except ValueError as exc:
        return RedirectResponse(f"/signup?msg={quote_plus(str(exc))}", status_code=303)


@router.get("/onboarding/complete")
def onboarding_complete(session_id: str = ""):
    if not session_id:
        return RedirectResponse("/signup?msg=Missing+checkout+session.", status_code=303)
    existing = billing.get_checkout_invite_by_session(session_id)
    if existing:
        return RedirectResponse(f"/invite/{existing['invite_token']}", status_code=303)
    if not billing.configured():
        return RedirectResponse("/signup?msg=Stripe+is+not+configured.", status_code=303)
    try:
        sess = billing.fetch_checkout_session(session_id)
        if sess.get("payment_status") not in ("paid", "no_payment_required"):
            return RedirectResponse("/signup?msg=Payment+is+still+pending.", status_code=303)
        token = billing.ensure_invite_for_checkout_session(sess)
        return RedirectResponse(f"/invite/{token}", status_code=303)
    except ValueError as exc:
        return RedirectResponse(f"/signup?msg={quote_plus(str(exc))}", status_code=303)


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    if not billing.verify_webhook_signature(payload, sig):
        raise HTTPException(400, "Invalid Stripe signature.")
    try:
        event = json.loads(payload.decode("utf-8"))
    except ValueError:
        raise HTTPException(400, "Invalid JSON payload.")
    return billing.process_webhook_event(event)


@router.get("/billing")
def billing_page(request: Request, msg: str = "", ctx: Ctx = Depends(require("billing"))):
    sub = billing.get_subscription(ctx.merchant_id)
    allowed, flag = billing.is_access_allowed(sub)
    return render(
        request,
        "billing.html",
        ctx,
        "settings",
        sub=sub,
        allowed=allowed,
        grace=(flag == "grace"),
        can_portal=bool(sub.get("stripe_customer_id") and billing.configured()),
        msg=msg,
    )


@router.post("/billing/portal")
def billing_portal(request: Request, ctx: Ctx = Depends(require("billing"))):
    if ctx.role not in ("owner", "manager") and not ctx.is_admin:
        raise HTTPException(403, "Only owners or managers can open billing.")
    sub = billing.get_subscription(ctx.merchant_id)
    cid = sub.get("stripe_customer_id")
    if not cid or not billing.configured():
        return RedirectResponse("/billing?msg=No+Stripe+customer+linked.", status_code=303)
    base = str(request.base_url).rstrip("/")
    try:
        sess = billing.create_portal_session(cid, return_url=f"{base}/settings")
        return RedirectResponse(sess["url"], status_code=303)
    except ValueError as exc:
        return RedirectResponse(f"/billing?msg={quote_plus(str(exc))}", status_code=303)


@router.post("/admin/merchants/billing")
def admin_merchant_billing(
    merchant_id: int = Form(...),
    action: str = Form(...),
    ctx: Ctx = Depends(require("admin")),
):
    if action == "comp":
        billing.grant_lifetime(merchant_id)
        msg = f"Granted lifetime (comp) access to merchant #{merchant_id}."
    elif action == "grace":
        billing.extend_grace(merchant_id, days=7)
        msg = f"Extended 7-day grace period for merchant #{merchant_id}."
    elif action == "suspend":
        billing.suspend_merchant(merchant_id)
        msg = f"Suspended billing access for merchant #{merchant_id}."
    elif action == "activate":
        sub = billing.get_subscription(merchant_id)
        billing.upsert_subscription(merchant_id, status="active", plan_key=sub["plan_key"])
        msg = f"Activated merchant #{merchant_id}."
    else:
        msg = f"Unknown billing action: {action}"
    return RedirectResponse(f"/admin?msg={quote_plus(msg)}", status_code=303)


@router.post("/admin/billing/checkout-link")
def admin_checkout_link(
    request: Request,
    plan_key: str = Form(...),
    merchant_name: str = Form(""),
    email: str = Form(""),
    ctx: Ctx = Depends(require("admin")),
):
    if not billing.configured():
        return RedirectResponse("/admin?msg=Stripe+is+not+configured+yet.", status_code=303)
    base = str(request.base_url).rstrip("/")
    try:
        sess = billing.create_checkout_session(
            plan_key=plan_key,
            success_url=f"{base}/onboarding/complete?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}/signup?msg=Checkout+canceled.",
            customer_email=email,
            merchant_name=merchant_name,
        )
        return RedirectResponse(
            f"/admin?checkout_url={quote_plus(sess['url'])}&msg=Checkout+link+created.",
            status_code=303,
        )
    except ValueError as exc:
        return RedirectResponse(f"/admin?msg={quote_plus(str(exc))}", status_code=303)
