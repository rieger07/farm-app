"""Health check, contact API, workspace switcher, login/logout, and invite onboarding."""

import os

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from inventory import billing
from inventory.merchants import (
    count_merchants,
    create_merchant,
    get_invite,
    get_membership,
    home_page,
    redeem_invite,
)
from inventory.web import (
    COOKIE,
    MERCHANT_COOKIE,
    SESSION_DAYS,
    Ctx,
    _set_session_cookies,
    current_ctx,
    templates,
)
from shared.auth import (
    authenticate_user,
    create_user,
    delete_user_session,
    get_user_by_username,
)

router = APIRouter()


@router.get("/healthz")
def healthz():
    return {"status": "ok"}


_CONTACT_BUCKETS: dict[str, list[float]] = {}


def _send_resend_email(payload: dict) -> None:
    import httpx

    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("RESEND_API_KEY is not configured.")
    from_addr = os.environ.get(
        "CONTACT_FROM_EMAIL", "Ironclad Systems <onboarding@resend.dev>"
    ).strip()
    to_addr = os.environ.get("CONTACT_TO_EMAIL", "steve.rieger.dev@gmail.com").strip()
    resp = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "from": from_addr,
            "to": [to_addr],
            "reply_to": payload["email"],
            "subject": f"[Ironclad Systems] {payload['engagement']} inquiry from {payload['name']}",
            "text": (
                f"Name: {payload['name']}\n"
                f"Email: {payload['email']}\n"
                f"Inquiry: {payload['engagement']}\n\n"
                f"{payload['message']}\n"
            ),
        },
        timeout=10,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Email delivery failed ({resp.status_code}).")


@router.post("/api/contact")
async def api_contact(request: Request):
    import time

    body = await request.json()
    if str(body.get("website") or "").strip():  # honeypot triggered
        return {"ok": True}

    name = str(body.get("name") or "").strip()
    email = str(body.get("email") or "").strip()
    engagement = str(body.get("engagement") or "Consulting").strip()
    message = str(body.get("message") or "").strip()
    if not name or "@" not in email or not message:
        raise HTTPException(400, "Please fill in your name, a valid email, and a message.")

    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")
    now = time.monotonic()
    recent = [t for t in _CONTACT_BUCKETS.get(ip, []) if now - t < 600]
    if len(recent) >= 5:
        raise HTTPException(429, "Too many inquiries. Please try again in a few minutes.")
    recent.append(now)
    _CONTACT_BUCKETS[ip] = recent

    try:
        _send_resend_email({
            "name": name[:120],
            "email": email[:160],
            "engagement": engagement[:80],
            "message": message[:4000],
        })
    except RuntimeError as exc:
        raise HTTPException(502, str(exc))
    return {"ok": True}


@router.get("/")
def index(ctx: Ctx = Depends(current_ctx)):
    if ctx.merchant_id == 0 and ctx.is_admin:
        return RedirectResponse("/admin", status_code=303)
    return RedirectResponse("/" + home_page(ctx.role).replace("_", "-"), status_code=303)


@router.post("/switch-merchant")
def switch_merchant(
    merchant_id: int = Form(...),
    next_url: str = Form("/"),
    ctx: Ctx = Depends(current_ctx),
):
    target = get_membership(ctx.user["id"], merchant_id=merchant_id, is_admin=ctx.is_admin)
    if not target:
        raise HTTPException(403, "Not allowed to access that workspace.")
    dest = next_url if next_url.startswith("/") else "/"
    resp = RedirectResponse(dest, status_code=303)
    resp.set_cookie(
        MERCHANT_COOKIE,
        str(target["merchant_id"]),
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=os.environ.get("INVENTORY_COOKIE_SECURE", "0") == "1",
    )
    return resp


@router.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": None, "first_run": count_merchants() == 0, "stripe_configured": billing.configured()},
    )


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    merchant_name: str = Form(""),
    display_name: str = Form(""),
):
    first_run = count_merchants() == 0
    user = authenticate_user(username, password)
    created_mid = None
    if first_run:
        clean_merchant = merchant_name.strip() or f"{display_name.strip() or username.strip()}'s Business"
        if not user:
            if get_user_by_username(username):
                return templates.TemplateResponse(
                    request,
                    "login.html",
                    {"error": "Username already exists with a different password.", "first_run": True},
                    status_code=401,
                )
            try:
                user = create_user(username, password, display_name=display_name)
            except ValueError as exc:
                return templates.TemplateResponse(
                    request,
                    "login.html",
                    {"error": str(exc), "first_run": True},
                    status_code=400,
                )
        created_mid = create_merchant(clean_merchant, user["id"])
    elif not user or (not get_membership(user["id"], is_admin=bool(user.get("is_admin"))) and not user.get("is_admin")):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Wrong username or password.", "first_run": False},
            status_code=401,
        )
    resp = RedirectResponse("/", status_code=303)
    _set_session_cookies(resp, user["id"], merchant_id=created_mid)
    return resp


@router.post("/logout")
def logout(request: Request):
    delete_user_session(request.cookies.get(COOKIE, ""))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    resp.delete_cookie(MERCHANT_COOKIE)
    return resp


# ---- One-time Invite Onboarding (/invite/<token>) -------------------------

@router.get("/invite/{token}")
def invite_page(request: Request, token: str):
    inv = get_invite(token)
    return templates.TemplateResponse(
        request,
        "invite.html",
        {"invite": inv, "token": token, "error": None},
        status_code=200 if (inv and inv["valid"]) else 404,
    )


@router.post("/invite/{token}")
def invite_accept(
    request: Request,
    token: str,
    username: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(""),
    merchant_name: str = Form(""),
):
    inv = get_invite(token)
    if not inv or not inv["valid"]:
        return templates.TemplateResponse(
            request,
            "invite.html",
            {"invite": inv, "token": token, "error": "This invite link is expired or already used."},
            status_code=400,
        )
    clean_user = username.strip()
    user = authenticate_user(clean_user, password)
    if not user:
        if get_user_by_username(clean_user):
            return templates.TemplateResponse(
                request,
                "invite.html",
                {
                    "invite": inv,
                    "token": token,
                    "error": "That username already exists. Enter its current password to link it, or pick another username.",
                },
                status_code=400,
            )
        try:
            user = create_user(
                clean_user,
                password,
                display_name=display_name,
                is_admin=False,
            )
        except ValueError as exc:
            return templates.TemplateResponse(
                request,
                "invite.html",
                {"invite": inv, "token": token, "error": str(exc)},
                status_code=400,
            )
    try:
        mid = redeem_invite(token, user["id"], merchant_name=merchant_name)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "invite.html",
            {"invite": inv, "token": token, "error": str(exc)},
            status_code=400,
        )
    chk = billing.get_checkout_invite_by_token(token)
    if chk and inv.get("merchant_id") is None:
        billing.upsert_subscription(
            mid,
            status="active",
            plan_key=chk["plan_key"],
            stripe_customer_id=chk["stripe_customer_id"],
            stripe_subscription_id=chk["stripe_subscription_id"],
        )
    resp = RedirectResponse("/", status_code=303)
    _set_session_cookies(resp, user["id"], merchant_id=mid)
    return resp
