"""FastAPI web app: Inventory, Sales, Quick sale, Invites, and Super Admin pages."""

import asyncio
import csv
import io
import json
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from shared.web import RevalidatingStatic, mount_shared_static

from homestead.database import (
    authenticate_user,
    create_user,
    create_user_session,
    delete_user_session,
    get_user_by_session_token,
    get_user_by_username,
    update_user_password,
)
from inventory import billing, sync, zettle
from inventory.db import init_inventory_db
from inventory.merchants import (
    ROLES,
    add_member,
    can_access,
    count_merchants,
    create_invite,
    create_merchant,
    delete_invite,
    delete_merchant,
    get_invite,
    get_membership,
    get_payment_handles,
    home_page,
    list_all_merchants,
    list_all_users_with_memberships,
    list_invites,
    list_members,
    list_user_merchants,
    redeem_invite,
    remove_member,
    rename_merchant,
    set_user_admin,
    update_payment_handles,
)
from inventory.sales import (
    count_sales,
    export_rows,
    market_insights,
    money,
    recent_sales,
    record_cash_sale,
    sales_summary,
    update_sale_payment_type,
    void_quick_sale,
)
from inventory.markets import (
    WEATHER_OPTIONS,
    archive_marketplace,
    close_market_day,
    compute_market_day_status,
    delete_market_day,
    get_market_day,
    list_marketplaces,
    list_recent_and_upcoming_market_days,
    list_sale_dates,
    marketplace_analytics,
    marketplace_utc_windows,
    recommend_cash_drawer,
    reopen_market_day,
    restock_market_day_item,
    save_market_day,
    save_market_day_pack,
    save_marketplace,
    suggest_pack_list,
)
from inventory.stock import (
    ITEM_TYPES,
    add_ledger_entry,
    fmt_qty,
    list_sku_history,
    list_stock,
    set_item_type,
    set_low_stock_threshold,
    set_quick_sale_visibility,
    set_sell_mode,
    set_stock_count,
)

BASE = Path(__file__).parent
COOKIE = "inv_session"
MERCHANT_COOKIE = "inv_merchant"
SESSION_DAYS = 90  # long-lived so staff stay logged in at the stall


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_inventory_db()
    task = None
    interval = int(os.environ.get("ZETTLE_POLL_SECONDS", "300"))
    if zettle.configured() and interval > 0:
        async def poll():
            await asyncio.sleep(5)
            while True:
                await asyncio.to_thread(sync.sync_all)
                await asyncio.sleep(interval)

        task = asyncio.create_task(poll())
    yield
    if task:
        task.cancel()


def asset(name: str) -> str:
    """URL for a static file with a version query so a new deploy busts every cache."""
    try:
        version = int((BASE / "static" / name).stat().st_mtime)
    except OSError:
        version = 0
    return f"/static/{name}?v={version}"


app = FastAPI(title="iron ledger", lifespan=lifespan)
app.mount("/static", RevalidatingStatic(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
mount_shared_static(app, templates)
templates.env.filters["money"] = money
templates.env.globals["asset"] = asset


def local_tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("INVENTORY_TZ", "America/New_York"))


def utc_bounds(d0: date, d1: date) -> tuple[str, str]:
    """UTC ISO bounds [start, end) for the local dates d0 (inclusive) to d1 (exclusive)."""
    tz = local_tz()

    def conv(d: date) -> str:
        local = datetime(d.year, d.month, d.day, tzinfo=tz)
        return local.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")

    return conv(d0), conv(d1)


def to_local(occurred_at: str) -> datetime:
    return datetime.fromisoformat(occurred_at).replace(tzinfo=timezone.utc).astimezone(local_tz())


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


MAX_RANGE_DAYS = 366


def resolve_range(
    period: str, date_from: str = "", date_to: str = "", market_date: str = ""
) -> dict:
    """Turn a period choice into local date bounds plus the window the chart covers."""
    today = datetime.now(local_tz()).date()
    one = timedelta(days=1)
    if period == "day":
        d = _parse_date(market_date)
        if d:
            return {
                "period": "day",
                "label": d.strftime("%a %b %-d, %Y"),
                "start": d,
                "end": d + one,
                "chart_start": max(d - timedelta(days=6), d - timedelta(days=6)),
                "chart_end": d + one,
                "date_from": d.isoformat(),
                "date_to": d.isoformat(),
                "market_date": d.isoformat(),
            }
        period = "today"
    if period == "custom":
        d0, d1 = _parse_date(date_from), _parse_date(date_to)
        if d0 and d1:
            if d0 > d1:
                d0, d1 = d1, d0
            start, end = max(d0, d1 - timedelta(days=MAX_RANGE_DAYS - 1)), d1 + one
            label = (
                start.strftime("%b %-d, %Y") if start == d1
                else f"{start.strftime('%b %-d, %Y')} – {d1.strftime('%b %-d, %Y')}"
            )
            return {"period": "custom", "label": label, "start": start, "end": end,
                    "chart_start": start, "chart_end": end,
                    "date_from": start.isoformat(), "date_to": d1.isoformat(),
                    "market_date": ""}
        period = "week"  # incomplete custom range: fall back
    if period == "yesterday":
        start, end, label = today - one, today, "Yesterday"
    elif period == "month":
        start, end, label = today - timedelta(days=29), today + one, "Last 30 days"
    elif period == "quarter":
        start, end, label = today - timedelta(days=89), today + one, "Last 90 days"
    elif period == "all":
        start, end, label = date(2020, 1, 1), today + one, "All time"
    elif period == "week":
        start, end, label = today - timedelta(days=6), today + one, "Last 7 days"
    else:
        period, start, end, label = "today", today, today + one, "Today"
    if period in ("today", "yesterday"):
        chart_start, chart_end = today - timedelta(days=6), today + one
    elif period == "all":
        chart_start, chart_end = today - timedelta(days=89), today + one
    else:
        chart_start, chart_end = start, end
    return {"period": period, "label": label, "start": start, "end": end,
            "chart_start": chart_start, "chart_end": chart_end,
            "date_from": start.isoformat(), "date_to": (end - one).isoformat(),
            "market_date": ""}


def chart_buckets(chart_start: date, chart_end: date) -> list[dict]:
    """Daily buckets for up to 31 days, weekly buckets beyond that."""
    n = (chart_end - chart_start).days
    step = 1 if n <= 31 else 7
    out, d = [], chart_start
    while d < chart_end:
        nxt = min(d + timedelta(days=step), chart_end)
        if step == 7:
            label, title = d.strftime("%b %-d"), f"Week of {d.strftime('%b %-d')}"
        else:
            label = d.strftime("%a") if n <= 7 else d.strftime("%-d")
            title = d.strftime("%a %b %-d")
        out.append({"d0": d, "d1": nxt, "label": label, "title": title})
        d = nxt
    return out


class Ctx:
    def __init__(self, user: dict, membership: dict | None, workspaces: list[dict]):
        self.user = user
        self.membership = membership or {
            "merchant_id": 0,
            "merchant_name": "No Workspace",
            "role": "owner" if user.get("is_admin") else "staff",
        }
        self.workspaces = workspaces

    @property
    def is_admin(self) -> bool:
        return bool(self.user.get("is_admin"))

    @property
    def role(self) -> str:
        return self.membership["role"]

    @property
    def merchant_id(self) -> int:
        return int(self.membership["merchant_id"])


class NeedsLogin(Exception):
    pass


class Forbidden(Exception):
    def __init__(self, redirect: str):
        self.redirect = redirect


@app.exception_handler(NeedsLogin)
async def _needs_login(request: Request, exc: NeedsLogin):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "login_required"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(Forbidden)
async def _forbidden(request: Request, exc: Forbidden):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return RedirectResponse(exc.redirect, status_code=303)


def current_ctx(request: Request) -> Ctx:
    user = get_user_by_session_token(request.cookies.get(COOKIE, ""))
    if not user:
        raise NeedsLogin()
    raw_mid = request.cookies.get(MERCHANT_COOKIE, "")
    preferred_mid = int(raw_mid) if raw_mid.isdigit() else None
    is_admin = bool(user.get("is_admin"))
    membership = get_membership(user["id"], merchant_id=preferred_mid, is_admin=is_admin)
    if not membership and not is_admin:
        raise NeedsLogin()
    workspaces = list_user_merchants(user["id"], is_admin=is_admin)
    return Ctx(user, membership, workspaces)


def require(page: str):
    def dep(ctx: Ctx = Depends(current_ctx)) -> Ctx:
        if page == "admin":
            if not ctx.is_admin:
                raise Forbidden("/" + home_page(ctx.role).replace("_", "-"))
            return ctx
        if ctx.merchant_id == 0 and ctx.is_admin:
            raise Forbidden("/admin")
        if page != "billing" and not ctx.is_admin:
            sub = billing.get_subscription(ctx.merchant_id)
            allowed, _ = billing.is_access_allowed(sub)
            if not allowed:
                raise Forbidden("/billing")
        if page != "billing" and not can_access(ctx.role, page):
            raise Forbidden("/" + home_page(ctx.role).replace("_", "-"))
        return ctx

    return dep


def render(request: Request, name: str, ctx: Ctx, active: str, **extra):
    nav = []
    if ctx.merchant_id != 0:
        nav.extend(
            (k, label, path)
            for k, label, path in (
                ("inventory", "Inventory", "/inventory"),
                ("sales", "Sales", "/sales"),
                ("insights", "Insights", "/insights"),
                ("market_day", "Market day", "/market-day"),
                ("quick_sale", "Quick sale", "/quick-sale"),
                ("settings", "Settings", "/settings"),
            )
            if can_access(ctx.role, k)
        )
    if ctx.is_admin:
        nav.append(("admin", "Admin", "/admin"))
    sub = billing.get_subscription(ctx.merchant_id) if ctx.merchant_id else None
    _, grace_flag = billing.is_access_allowed(sub) if sub else (True, "")
    return templates.TemplateResponse(
        request,
        name,
        {
            "ctx": ctx,
            "active": active,
            "zettle_connected": sync.is_connected(ctx.merchant_id) if ctx.merchant_id else False,
            "billing_sub": sub,
            "billing_grace": grace_flag == "grace",
            "nav": nav,
            **extra,
        },
    )


def _set_session_cookies(resp: RedirectResponse, user_id: int, merchant_id: int | None = None):
    token = create_user_session(user_id, days_valid=SESSION_DAYS)
    secure = os.environ.get("INVENTORY_COOKIE_SECURE", "0") == "1"
    resp.set_cookie(
        COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=secure
    )
    if merchant_id is not None:
        resp.set_cookie(
            MERCHANT_COOKIE,
            str(merchant_id),
            max_age=SESSION_DAYS * 86400,
            httponly=True,
            samesite="lax",
            secure=secure,
        )


@app.get("/healthz")
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


@app.post("/api/contact")
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


@app.get("/")
def index(ctx: Ctx = Depends(current_ctx)):
    if ctx.merchant_id == 0 and ctx.is_admin:
        return RedirectResponse("/admin", status_code=303)
    return RedirectResponse("/" + home_page(ctx.role).replace("_", "-"), status_code=303)


@app.post("/switch-merchant")
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


@app.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": None, "first_run": count_merchants() == 0, "stripe_configured": billing.configured()},
    )


@app.post("/login")
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


@app.post("/logout")
def logout(request: Request):
    delete_user_session(request.cookies.get(COOKIE, ""))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    resp.delete_cookie(MERCHANT_COOKIE)
    return resp


# ---- One-time Invite Onboarding (/invite/<token>) -------------------------

@app.get("/invite/{token}")
def invite_page(request: Request, token: str):
    inv = get_invite(token)
    return templates.TemplateResponse(
        request,
        "invite.html",
        {"invite": inv, "token": token, "error": None},
        status_code=200 if (inv and inv["valid"]) else 404,
    )


@app.post("/invite/{token}")
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
                claim_unowned_data=False,
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


# ---- Inventory, Sales, Quick Sale -----------------------------------------

_STOCK_PRIORITY = {
    "count_needed": 0,
    "out_of_stock": 1,
    "low": 2,
    "in_stock": 3,
}


@app.get("/inventory")
def inventory_page(
    request: Request, msg: str = "", ctx: Ctx = Depends(require("inventory"))
):
    rows = list_stock(ctx.merchant_id)
    rows.sort(key=lambda r: _STOCK_PRIORITY.get(r["status"], 9))
    return render(
        request, "inventory.html", ctx, "inventory",
        rows=rows,
        units=fmt_qty(sum(max(r["on_hand"], 0) for r in rows if r.get("sell_mode") != "lb_stock_lb")),
        lbs=fmt_qty(sum(max(r["on_hand"], 0) for r in rows if r.get("sell_mode") == "lb_stock_lb")),
        low=sum(1 for r in rows if r["status"] in ("low", "out_of_stock", "count_needed")),
        categories=sorted({r["category"] for r in rows if r["category"]}),
        msg=msg,
    )


@app.post("/inventory/stock")
def inventory_update_stock(
    sku_id: int = Form(...),
    mode: str = Form("set"),
    quantity: float = Form(...),
    low_stock_threshold: float = Form(10),
    sell_mode: str = Form(""),
    quick_sale_present: str = Form(""),
    show_in_quick_sale: bool = Form(False),
    note: str = Form(""),
    ctx: Ctx = Depends(require("inventory")),
):
    try:
        set_low_stock_threshold(ctx.merchant_id, sku_id, low_stock_threshold)
        if sell_mode:
            set_sell_mode(ctx.merchant_id, sku_id, sell_mode)
        if quick_sale_present:
            set_quick_sale_visibility(ctx.merchant_id, sku_id, show_in_quick_sale)
        qty = fmt_qty(quantity)
        unit_lbl = "lb" if sell_mode == "lb_stock_lb" else "units"
        if mode == "set":
            set_stock_count(
                ctx.merchant_id, sku_id, qty, note=note, created_by=ctx.user["id"]
            )
            msg = f"Set stock count to {qty} {unit_lbl}."
        elif mode == "restock":
            if qty != 0:
                add_ledger_entry(
                    ctx.merchant_id,
                    sku_id,
                    abs(qty),
                    "restock",
                    note=note or "Restock",
                    created_by=ctx.user["id"],
                )
            msg = f"Added {abs(qty)} {unit_lbl} to stock."
        else:
            if qty != 0:
                add_ledger_entry(
                    ctx.merchant_id,
                    sku_id,
                    qty,
                    "adjustment",
                    note=note or "Adjustment",
                    created_by=ctx.user["id"],
                )
            msg = f"Adjusted stock by {qty:+} {unit_lbl}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/inventory?msg={quote_plus(msg)}", status_code=303)


@app.get("/api/stock/{sku_id}/history")
def api_stock_history(sku_id: int, ctx: Ctx = Depends(require("inventory"))):
    rows = list_sku_history(ctx.merchant_id, sku_id, limit=8)
    for r in rows:
        r["local_time"] = (
            to_local(r["created_at"])
            .strftime("%b %-d · %-I:%M %p")
            .replace("AM", "am")
            .replace("PM", "pm")
        )
    return {"entries": rows}


def _sales_query(
    period: str, date_from: str, date_to: str, market_date: str = ""
) -> dict:
    rng = resolve_range(period, date_from, date_to, market_date)
    rng["since"], rng["until"] = utc_bounds(rng["start"], rng["end"])
    return rng


def _parse_dollars_to_cents(raw: str | None) -> int | None:
    text = (raw or "").strip().replace("$", "").replace(",", "")
    if not text:
        return None
    return max(0, round(float(text) * 100))


def _resolve_marketplace_windows(
    merchant_id: int, marketplace: str, rng: dict
) -> tuple[int | None, list[tuple[str, str]] | None]:
    mp_id = int(marketplace) if (marketplace or "").strip().isdigit() else None
    if not mp_id:
        return None, None
    return mp_id, marketplace_utc_windows(
        merchant_id, mp_id, rng["date_from"], rng["date_to"], local_tz()
    )


@app.get("/sales")
def sales_page(
    request: Request,
    period: str = "today",
    source: str = "all",
    date_from: str = Query("", alias="from"),
    date_to: str = Query("", alias="to"),
    market_date: str = Query("", alias="date"),
    marketplace: str = Query(""),
    limit: int = 25,
    msg: str = "",
    ctx: Ctx = Depends(require("sales")),
):
    rng = _sales_query(period, date_from, date_to, market_date)
    src = None if source == "all" else source
    limit = max(25, min(int(limit), 500))
    mp_id, utc_windows = _resolve_marketplace_windows(ctx.merchant_id, marketplace, rng)
    summary = sales_summary(ctx.merchant_id, rng["since"], rng["until"], utc_windows=utc_windows)

    days = []
    for b in chart_buckets(rng["chart_start"], rng["chart_end"]):
        s0, s1 = utc_bounds(b["d0"], b["d1"])
        d = sales_summary(ctx.merchant_id, s0, s1, utc_windows=utc_windows)
        days.append({
            "label": b["label"],
            "title": b["title"],
            "card_cents": d["card_cents"],
            "digital_cents": d["digital_cents"],
            "cash_cents": d["cash_cents"],
            "zettle_card_cents": d["zettle_card_cents"],
            "zettle_cash_cents": d["zettle_cash_cents"],
            "quick_cash_cents": d["quick_cash_cents"],
        })
    peak = max((d["card_cents"] + d["digital_cents"] + d["cash_cents"] for d in days), default=0) or 1
    best = max(days, key=lambda d: d["card_cents"] + d["digital_cents"] + d["cash_cents"])

    txns = recent_sales(
        ctx.merchant_id, rng["since"], rng["until"], src, limit=limit, utc_windows=utc_windows
    )
    total_count = count_sales(
        ctx.merchant_id, rng["since"], rng["until"], src, utc_windows=utc_windows
    )
    today = datetime.now(local_tz()).date()
    for t in txns:
        local = to_local(t["occurred_at"])
        t["date_label"] = (
            "Today" if local.date() == today
            else "Yesterday" if local.date() == today - timedelta(days=1)
            else local.strftime("%b %-d")
        )
        t["time_label"] = local.strftime("%-I:%M %p").lower()
        t["full_label"] = local.strftime("%a %b %-d, %Y · %-I:%M %p").replace("AM", "am").replace("PM", "pm")

    active_day_str = rng["date_from"] if rng["period"] in ("today", "yesterday", "day") else ""
    active_market_day = get_market_day(ctx.merchant_id, active_day_str) if active_day_str else None
    marketplaces = list_marketplaces(ctx.merchant_id)

    export_qs = f"period={rng['period']}&source={source}"
    if rng["period"] == "custom":
        export_qs += f"&from={rng['date_from']}&to={rng['date_to']}"
    elif rng["period"] == "day":
        export_qs += f"&date={rng['market_date']}"
    if mp_id:
        export_qs += f"&marketplace={mp_id}"
    return render(
        request, "sales.html", ctx, "sales",
        summary=summary, days=days, peak=peak, best=best, txns=txns,
        period=rng["period"], period_label=rng["label"], source=source,
        date_from=rng["date_from"], date_to=rng["date_to"],
        market_date=rng["market_date"] or today.isoformat(),
        sale_dates=list_sale_dates(ctx.merchant_id, local_tz()),
        marketplaces=marketplaces,
        selected_marketplace=str(mp_id or ""),
        active_day_str=active_day_str,
        active_market_day=active_market_day,
        weather_options=WEATHER_OPTIONS,
        today_label=datetime.now(local_tz()).strftime("%-d %b %Y"),
        total_count=total_count, has_more=len(txns) < total_count, next_limit=limit + 25,
        export_qs=export_qs, weekly=(rng["chart_end"] - rng["chart_start"]).days > 31,
        msg=msg,
    )


@app.get("/insights")
def insights_page(
    request: Request,
    period: str = "quarter",
    date_from: str = Query("", alias="from"),
    date_to: str = Query("", alias="to"),
    market_date: str = Query("", alias="date"),
    marketplace: str = Query(""),
    msg: str = "",
    ctx: Ctx = Depends(require("insights")),
):
    rng = _sales_query(period, date_from, date_to, market_date)
    mp_id, utc_windows = _resolve_marketplace_windows(ctx.merchant_id, marketplace, rng)
    data = market_insights(ctx.merchant_id, rng["since"], rng["until"], utc_windows=utc_windows)
    summary = sales_summary(ctx.merchant_id, rng["since"], rng["until"], utc_windows=utc_windows)
    mp_analytics = marketplace_analytics(
        ctx.merchant_id, rng["date_from"], rng["date_to"], rng["since"], rng["until"], local_tz()
    )
    drawer = recommend_cash_drawer(
        ctx.merchant_id, rng["since"], rng["until"], local_tz(), utc_windows=utc_windows
    )
    pack_list = suggest_pack_list(
        ctx.merchant_id, rng["since"], rng["until"], local_tz(), utc_windows=utc_windows
    )
    marketplaces = list_marketplaces(ctx.merchant_id)
    selected_mp_name = next((m["name"] for m in marketplaces if str(m["id"]) == str(mp_id or "")), "")
    for so in data["sellouts"]:
        local = to_local(so["last_sold_at"])
        so["last_sold_label"] = (
            local.strftime("%b %-d · %-I:%M %p")
            .replace("AM", "am")
            .replace("PM", "pm")
        )
    active_day_str = rng["date_from"] if rng["period"] in ("today", "yesterday", "day") else ""
    active_market_day = get_market_day(ctx.merchant_id, active_day_str) if active_day_str else None
    return render(
        request, "insights.html", ctx, "insights",
        insights=data,
        summary=summary,
        mp_analytics=mp_analytics,
        drawer=drawer,
        pack_list=pack_list,
        marketplaces=marketplaces,
        selected_marketplace=str(mp_id or ""),
        selected_mp_name=selected_mp_name,
        active_day_str=active_day_str,
        active_market_day=active_market_day,
        weather_options=WEATHER_OPTIONS,
        period=rng["period"],
        period_label=rng["label"],
        date_from=rng["date_from"],
        date_to=rng["date_to"],
        market_date=rng["market_date"] or datetime.now(local_tz()).date().isoformat(),
        sale_dates=list_sale_dates(ctx.merchant_id, local_tz()),
        msg=msg,
    )


@app.post("/market-days/save")
def market_day_save(
    market_date: str = Form(...),
    marketplace_id: int = Form(...),
    booth_fee: str = Form(""),
    travel_cost: str = Form(""),
    weather: str = Form(""),
    notes: str = Form(""),
    next_url: str = Form("/insights"),
    ctx: Ctx = Depends(require("sales")),
):
    try:
        save_market_day(
            ctx.merchant_id,
            market_date,
            marketplace_id,
            booth_fee_cents=_parse_dollars_to_cents(booth_fee),
            travel_cost_cents=_parse_dollars_to_cents(travel_cost),
            weather=weather,
            notes=notes,
        )
        msg = f"Tagged {market_date} market day."
    except ValueError as exc:
        msg = f"Error: {exc}"
    target = next_url if next_url.startswith("/") else "/insights"
    sep = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{sep}msg={quote_plus(msg)}", status_code=303)


@app.post("/market-days/delete")
def market_day_delete(
    market_date: str = Form(...),
    next_url: str = Form("/insights"),
    ctx: Ctx = Depends(require("sales")),
):
    delete_market_day(ctx.merchant_id, market_date)
    target = next_url if next_url.startswith("/") else "/insights"
    sep = "&" if "?" in target else "?"
    return RedirectResponse(
        f"{target}{sep}msg={quote_plus(f'Removed market tag for {market_date}.')}",
        status_code=303,
    )


def _csv_safe(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@app.post("/sales/{sale_id}/payment-type")
def sales_retag_payment_type(
    sale_id: int,
    payment_type: str = Form(...),
    next_url: str = Form("/sales"),
    ctx: Ctx = Depends(require("sales")),
):
    try:
        update_sale_payment_type(ctx.merchant_id, sale_id, payment_type)
        msg = f"Updated sale #{sale_id} to {payment_type.capitalize()}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    target = next_url if next_url.startswith("/") else "/sales"
    sep = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{sep}msg={quote_plus(msg)}", status_code=303)


@app.post("/sales/{sale_id}/void")
def sales_void_quick_sale(
    sale_id: int,
    restore_stock: str = Form(""),
    next_url: str = Form("/sales"),
    ctx: Ctx = Depends(require("sales")),
):
    try:
        res = void_quick_sale(
            ctx.merchant_id,
            sale_id,
            created_by=ctx.user["id"],
            restore_stock=bool(restore_stock),
        )
        stock_note = (
            f" and restored stock for {res['restored_items']} item(s)"
            if restore_stock and res["restored_items"]
            else " (stock unchanged)"
        )
        msg = f"Voided Quick sale #{sale_id}{stock_note}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    target = next_url if next_url.startswith("/") else "/sales"
    sep = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{sep}msg={quote_plus(msg)}", status_code=303)


@app.get("/sales/export.csv")
def sales_export(
    period: str = "today",
    source: str = "all",
    date_from: str = Query("", alias="from"),
    date_to: str = Query("", alias="to"),
    market_date: str = Query("", alias="date"),
    marketplace: str = Query(""),
    ctx: Ctx = Depends(require("sales")),
):
    rng = _sales_query(period, date_from, date_to, market_date)
    _, utc_windows = _resolve_marketplace_windows(ctx.merchant_id, marketplace, rng)
    rows = export_rows(
        ctx.merchant_id,
        rng["since"],
        rng["until"],
        None if source == "all" else source,
        utc_windows=utc_windows,
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["sale_id", "date", "time", "source", "payment_type", "status", "cashier",
                "item", "quantity", "unit_price", "line_subtotal", "sale_total"])
    dollars = lambda c: f"{c / 100:.2f}"
    for r in rows:
        local = to_local(r["occurred_at"])
        sign = -1 if r["status"] == "refunded" else 1
        qty, unit = r["quantity"], r["unit_price_cents"]
        w.writerow([
            r["sale_id"], local.strftime("%Y-%m-%d"), local.strftime("%H:%M:%S"),
            r["source"], r["payment_type"] or ("cash" if r["source"] == "cash" else ""),
            r["status"], r["cashier"] or "",
            _csv_safe(r["item"]), "" if qty is None else fmt_qty(qty),
            "" if unit is None else dollars(unit),
            "" if qty is None else dollars(round(sign * qty * unit)), dollars(r["total_cents"]),
        ])
    name = f"ironledger-sales-{rng['date_from']}_to_{rng['date_to']}.csv"
    return Response(
        buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.get("/market-day")
def market_day_page(
    request: Request,
    market_date: str = Query("", alias="date"),
    edit: int = 0,
    msg: str = "",
    ctx: Ctx = Depends(require("market_day")),
):
    tz = local_tz()
    today_d = datetime.now(tz).date()
    today_iso = today_d.isoformat()
    tomorrow_iso = (today_d + timedelta(days=1)).isoformat()
    days_until_sat = (5 - today_d.weekday()) % 7
    next_sat_iso = (today_d + timedelta(days=days_until_sat or 7)).isoformat()

    selected_date = (market_date or "").strip() or today_iso
    try:
        date.fromisoformat(selected_date)
    except ValueError:
        selected_date = today_iso

    mday_status = compute_market_day_status(ctx.merchant_id, selected_date, tz)
    marketplaces = list_marketplaces(ctx.merchant_id)
    recent_days = list_recent_and_upcoming_market_days(ctx.merchant_id, today_iso, limit=8)

    rng = _sales_query("quarter", "", "")
    default_pack = suggest_pack_list(ctx.merchant_id, rng["since"], rng["until"], tz)
    default_map = {
        str(it["sku_id"]): it["pack_qty"]
        for it in default_pack["items"]
        if it.get("sku_id") is not None
    }
    suggested_by_mp: dict[str, dict] = {"default": default_map}
    for mp in marketplaces:
        wins = marketplace_utc_windows(ctx.merchant_id, mp["id"], rng["date_from"], rng["date_to"], tz)
        if wins:
            mp_pack = suggest_pack_list(ctx.merchant_id, rng["since"], rng["until"], tz, utc_windows=wins)
            mp_map = {
                str(it["sku_id"]): it["pack_qty"]
                for it in mp_pack["items"]
                if it.get("sku_id") is not None
            }
            suggested_by_mp[str(mp["id"])] = mp_map or default_map
        else:
            suggested_by_mp[str(mp["id"])] = default_map

    drawer = recommend_cash_drawer(ctx.merchant_id, rng["since"], rng["until"], tz)
    categories = sorted({s["category"] for s in mday_status["skus"] if s["category"]})

    return render(
        request,
        "market_day.html",
        ctx,
        "market_day",
        md=mday_status,
        marketplaces=marketplaces,
        recent_days=recent_days,
        suggested_by_mp=suggested_by_mp,
        recommended_cash_cents=drawer["recommended_total_cents"],
        categories=categories,
        weather_options=WEATHER_OPTIONS,
        today_iso=today_iso,
        tomorrow_iso=tomorrow_iso,
        next_sat_iso=next_sat_iso,
        show_pack_editor=bool(edit) or mday_status["state"] == "unstarted" or mday_status["packed_sku_count"] == 0,
        msg=msg,
    )


@app.post("/market-day/pack")
async def market_day_pack_submit(
    request: Request,
    ctx: Ctx = Depends(require("market_day")),
):
    form = await request.form()
    market_date = str(form.get("market_date") or "").strip()
    raw_mp = str(form.get("marketplace_id") or "").strip()
    mp_id = int(raw_mp) if raw_mp.isdigit() else None
    new_mp = str(form.get("new_marketplace_name") or "").strip()
    booth_cents = _parse_dollars_to_cents(str(form.get("booth_fee") or ""))
    travel_cents = _parse_dollars_to_cents(str(form.get("travel_cost") or ""))
    start_cash_cents = _parse_dollars_to_cents(str(form.get("starting_cash") or "")) or 0
    weather = str(form.get("weather") or "").strip()
    notes = str(form.get("notes") or "").strip()

    brought_by_sku: dict[int, float] = {}
    for k, v in form.items():
        if k.startswith("brought_"):
            sid_str = k[len("brought_"):]
            if sid_str.isdigit():
                raw_val = str(v or "").strip().replace(",", ".")
                try:
                    qty = max(0.0, float(raw_val)) if raw_val else 0.0
                except ValueError:
                    qty = 0.0
                brought_by_sku[int(sid_str)] = qty

    try:
        save_market_day_pack(
            ctx.merchant_id,
            market_date=market_date,
            marketplace_id=mp_id,
            brought_by_sku=brought_by_sku,
            new_marketplace_name=new_mp,
            booth_fee_cents=booth_cents,
            travel_cost_cents=travel_cents,
            starting_cash_cents=start_cash_cents,
            weather=weather,
            notes=notes,
        )
        packed_n = sum(1 for q in brought_by_sku.values() if q > 1e-9)
        today_iso = datetime.now(local_tz()).date().isoformat()
        if market_date > today_iso:
            msg = f"Saved pack-ahead list for {market_date} ({packed_n} products)."
        else:
            msg = f"Market Day active for {market_date} ({packed_n} products packed)."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(
        f"/market-day?date={quote_plus(market_date)}&msg={quote_plus(msg)}",
        status_code=303,
    )


@app.post("/market-day/restock")
def market_day_restock_submit(
    market_date: str = Form(...),
    sku_id: int = Form(...),
    delta_qty: float = Form(1.0),
    ctx: Ctx = Depends(require("market_day")),
):
    try:
        new_q = restock_market_day_item(ctx.merchant_id, market_date, sku_id, delta_qty)
        msg = f"Updated booth brought count to {new_q}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(
        f"/market-day?date={quote_plus(market_date)}&msg={quote_plus(msg)}",
        status_code=303,
    )


@app.post("/market-day/sync")
def market_day_sync_now(
    market_date: str = Form(""),
    ctx: Ctx = Depends(require("market_day")),
):
    target_date = (market_date or "").strip() or datetime.now(local_tz()).date().isoformat()
    if sync.is_connected(ctx.merchant_id):
        res = sync.sync_merchant(ctx.merchant_id)
        if res.get("error"):
            msg = f"Zettle sync error: {res['error']}"
        else:
            msg = f"Synced Zettle ({res.get('new_sales', 0)} new card sales)."
    else:
        msg = "Zettle is not connected for this workspace."
    return RedirectResponse(
        f"/market-day?date={quote_plus(target_date)}&msg={quote_plus(msg)}",
        status_code=303,
    )


@app.post("/market-day/close")
async def market_day_close_submit(
    request: Request,
    ctx: Ctx = Depends(require("market_day")),
):
    form = await request.form()
    market_date = str(form.get("market_date") or "").strip()
    counted_cash = _parse_dollars_to_cents(str(form.get("counted_cash") or ""))
    weather = str(form.get("weather") or "").strip()
    notes = str(form.get("notes") or "").strip()

    if sync.is_connected(ctx.merchant_id):
        try:
            sync.sync_merchant(ctx.merchant_id)
        except Exception:
            pass

    write_offs: dict[int, float] = {}
    counted_lefts: dict[int, float | None] = {}
    for k, v in form.items():
        if k.startswith("writeoff_"):
            sid_str = k[len("writeoff_"):]
            if sid_str.isdigit():
                raw = str(v or "").strip().replace(",", ".")
                try:
                    write_offs[int(sid_str)] = max(0.0, float(raw)) if raw else 0.0
                except ValueError:
                    write_offs[int(sid_str)] = 0.0
        elif k.startswith("counted_left_"):
            sid_str = k[len("counted_left_"):]
            if sid_str.isdigit():
                raw = str(v or "").strip().replace(",", ".")
                if raw == "":
                    counted_lefts[int(sid_str)] = None
                else:
                    try:
                        counted_lefts[int(sid_str)] = max(0.0, float(raw))
                    except ValueError:
                        counted_lefts[int(sid_str)] = None

    try:
        res = close_market_day(
            ctx.merchant_id,
            market_date=market_date,
            tz=local_tz(),
            counted_cash_cents=counted_cash,
            write_offs=write_offs,
            counted_lefts=counted_lefts,
            weather=weather,
            notes=notes,
            created_by=ctx.user["id"],
        )
        adj = res["adjustments_written"]
        extra = f" ({adj} stock adjustment{'s' if adj != 1 else ''} recorded)" if adj else ""
        msg = f"Closed out Market Day for {market_date}{extra}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(
        f"/market-day?date={quote_plus(market_date)}&msg={quote_plus(msg)}",
        status_code=303,
    )


@app.post("/market-day/reopen")
def market_day_reopen_submit(
    market_date: str = Form(...),
    ctx: Ctx = Depends(require("market_day")),
):
    try:
        res = reopen_market_day(ctx.merchant_id, market_date, created_by=ctx.user["id"])
        rev = res["reversed_adjustments"]
        extra = f" (reversed {rev} closeout adjustment{'s' if rev != 1 else ''})" if rev else ""
        msg = f"Reopened Market Day for {market_date}{extra}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(
        f"/market-day?date={quote_plus(market_date)}&msg={quote_plus(msg)}",
        status_code=303,
    )


@app.get("/quick-sale")
def quick_sale_page(request: Request, ctx: Ctx = Depends(require("quick_sale"))):
    tz = local_tz()
    today_iso = datetime.now(tz).date().isoformat()
    md = compute_market_day_status(ctx.merchant_id, today_iso, tz)
    live_booth = md["state"] == "live" and md["packed_sku_count"] > 0
    booth_by_sku = {
        it["sku_id"]: it for it in md["skus"]
    } if live_booth else {}

    skus = []
    for r in list_stock(ctx.merchant_id):
        if not r.get("show_in_quick_sale", 1):
            continue
        b_it = booth_by_sku.get(r["id"])
        skus.append(
            {
                "id": r["id"],
                "name": r["name"],
                "variant": r["variant_name"],
                "category": r["category"],
                "price_cents": r["price_cents"],
                "on_hand": r["on_hand"],
                "sell_mode": r.get("sell_mode", "unit"),
                "booth_packed": bool(b_it and b_it["is_packed"]),
                "booth_brought": b_it["brought_qty"] if b_it else None,
                "booth_left": b_it["left_at_booth"] if (b_it and b_it["is_packed"]) else None,
            }
        )

    active_booth_banner = (
        {
            "active": True,
            "market_date": today_iso,
            "marketplace_name": md["market_day"]["marketplace_name"],
            "packed_sku_count": md["packed_sku_count"],
            "gross_sales_cents": md["gross_sales_cents"],
            "booth_cost_cents": md["booth_cost_cents"],
            "breakeven_pct": md["breakeven_pct"],
            "overall_sell_through_pct": md["overall_sell_through_pct"],
        }
        if live_booth
        else {"active": False}
    )

    return render(
        request, "quick_sale.html", ctx, "quick_sale",
        skus=skus, categories=sorted({s["category"] for s in skus if s["category"]}),
        payment_handles=get_payment_handles(ctx.merchant_id),
        active_booth=active_booth_banner,
    )


@app.post("/api/cash-sale")
async def api_cash_sale(request: Request, ctx: Ctx = Depends(require("quick_sale"))):
    body = await request.json()
    try:
        result = record_cash_sale(
            ctx.merchant_id,
            str(body.get("client_uuid", "")),
            body.get("items", []),
            cash_received_cents=body.get("cash_received_cents"),
            created_by=ctx.user["id"],
            occurred_at=body.get("occurred_at"),
            payment_type=str(body.get("payment_type") or "cash"),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result


@app.get("/sw.js")
def service_worker():
    return FileResponse(BASE / "static" / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(BASE / "static" / "manifest.webmanifest",
                        media_type="application/manifest+json")


def _redirect_uri(request: Request) -> str:
    env_uri = os.environ.get("ZETTLE_REDIRECT_URI", "").strip()
    if env_uri:
        return env_uri
    base_url = os.environ.get("APP_BASE_URL", "").strip()
    if base_url:
        return base_url.rstrip("/") + "/zettle/callback"
    host = (request.url.hostname or "").lower()
    if host in ("", "localhost", "127.0.0.1", "0.0.0.0") or "rieger.page" in host:
        return "https://ledger.ironcladsystems.dev/zettle/callback"
    return str(request.url_for("zettle_callback"))


def _format_last_sync(iso_ts: str | None) -> str:
    if not iso_ts:
        return "Never"
    try:
        local_dt = to_local(iso_ts)
        now_local = datetime.now(local_tz())
        diff_sec = max(0, int((now_local - local_dt).total_seconds()))
        if diff_sec < 60:
            rel = "just now"
        elif diff_sec < 3600:
            rel = f"{diff_sec // 60} min ago"
        elif diff_sec < 86400:
            hrs = diff_sec // 3600
            rel = f"{hrs} hr ago" if hrs == 1 else f"{hrs} hrs ago"
        else:
            days = diff_sec // 86400
            rel = f"{days} day ago" if days == 1 else f"{days} days ago"
        time_str = local_dt.strftime("%-I:%M %p")
        if local_dt.date() != now_local.date():
            time_str = f"{local_dt.strftime('%b %-d')}, {time_str}"
        return f"{rel} ({time_str})"
    except Exception:
        return str(iso_ts)


@app.get("/settings")
def settings_page(request: Request, msg: str = "", ctx: Ctx = Depends(require("settings"))):
    status = sync.connection_status(ctx.merchant_id)
    return render(
        request, "settings.html", ctx, "settings",
        status=status,
        last_sync_display=_format_last_sync(status.get("last_sync")),
        configured=zettle.configured(),
        sub=billing.get_subscription(ctx.merchant_id),
        stripe_configured=billing.configured(),
        members=list_members(ctx.merchant_id),
        roles=ROLES,
        all_skus=list_stock(ctx.merchant_id, include_fees=True),
        marketplaces=list_marketplaces(ctx.merchant_id),
        payment_handles=get_payment_handles(ctx.merchant_id),
        redirect_uri=_redirect_uri(request),
        msg=msg,
    )


@app.post("/settings/payment-handles")
def settings_payment_handles(
    venmo_handle: str = Form(""),
    paypal_handle: str = Form(""),
    ctx: Ctx = Depends(require("settings")),
):
    update_payment_handles(ctx.merchant_id, venmo_handle=venmo_handle, paypal_handle=paypal_handle)
    return RedirectResponse(
        f"/settings?msg={quote_plus('Saved Venmo & PayPal.Me handles.')}",
        status_code=303,
    )


@app.post("/settings/marketplaces")
def settings_save_marketplace(
    marketplace_id: str = Form(""),
    name: str = Form(...),
    location: str = Form(""),
    booth_fee: str = Form("0"),
    travel_cost: str = Form("0"),
    ctx: Ctx = Depends(require("settings")),
):
    try:
        mp_id = int(marketplace_id) if marketplace_id.strip().isdigit() else None
        save_marketplace(
            ctx.merchant_id,
            name=name,
            location=location,
            booth_fee_cents=_parse_dollars_to_cents(booth_fee) or 0,
            travel_cost_cents=_parse_dollars_to_cents(travel_cost) or 0,
            marketplace_id=mp_id,
        )
        msg = f"Saved marketplace {name.strip()}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/settings?msg={quote_plus(msg)}", status_code=303)


@app.post("/settings/marketplaces/{marketplace_id}/archive")
def settings_archive_marketplace(
    marketplace_id: int,
    ctx: Ctx = Depends(require("settings")),
):
    archive_marketplace(ctx.merchant_id, marketplace_id)
    return RedirectResponse(
        f"/settings?msg={quote_plus('Archived marketplace.')}", status_code=303
    )


@app.post("/settings/item-type")
def settings_item_type(
    sku_id: int = Form(...),
    item_type: str = Form("product"),
    ctx: Ctx = Depends(require("settings")),
):
    if item_type not in ITEM_TYPES:
        item_type = "product"
    set_item_type(ctx.merchant_id, sku_id, item_type)
    label = "excluded fee / non-inventory" if item_type == "fee" else "regular inventory product"
    return RedirectResponse(
        f"/settings?msg={quote_plus(f'Updated item classification to {label}.')}",
        status_code=303,
    )


@app.post("/settings/members")
def settings_add_member(
    username: str = Form(...),
    display_name: str = Form(""),
    password: str = Form(""),
    role: str = Form("staff"),
    ctx: Ctx = Depends(require("settings")),
):
    try:
        clean_user = username.strip()
        existing = get_user_by_username(clean_user)
        if existing:
            uid = existing["id"]
        else:
            user = create_user(
                clean_user,
                password,
                display_name=display_name,
                is_admin=False,
                claim_unowned_data=False,
            )
            uid = user["id"]
        add_member(ctx.merchant_id, uid, role)
        msg = f"Saved member {clean_user} ({role})."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/settings?msg={quote_plus(msg)}", status_code=303)


@app.post("/settings/members/remove")
def settings_remove_member(
    user_id: int = Form(...),
    ctx: Ctx = Depends(require("settings")),
):
    try:
        remove_member(ctx.merchant_id, user_id)
        msg = "Removed member."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/settings?msg={quote_plus(msg)}", status_code=303)


@app.get("/zettle/connect")
def zettle_connect(request: Request, ctx: Ctx = Depends(require("settings"))):
    if not zettle.configured():
        raise HTTPException(400, "ZETTLE_CLIENT_ID / ZETTLE_CLIENT_SECRET are not set.")
    state = secrets.token_urlsafe(24)
    resp = RedirectResponse(zettle.build_authorize_url(_redirect_uri(request), state), 303)
    resp.set_cookie("zettle_state", state, max_age=600, httponly=True, samesite="lax",
                    secure=os.environ.get("INVENTORY_COOKIE_SECURE", "0") == "1")
    return resp


@app.get("/zettle/callback", name="zettle_callback")
def zettle_callback(request: Request, code: str = "", state: str = "", error: str = "",
                    ctx: Ctx = Depends(require("settings"))):
    expected = request.cookies.get("zettle_state", "")
    if error or not code or not expected or not secrets.compare_digest(state, expected):
        return RedirectResponse("/settings?msg=Zettle+connection+was+not+completed.", 303)
    try:
        tokens = zettle.exchange_code(code, _redirect_uri(request))
    except zettle.ZettleError:
        return RedirectResponse("/settings?msg=Zettle+rejected+the+authorization.", 303)
    sync.save_connection(ctx.merchant_id, tokens)
    resp = RedirectResponse("/settings?msg=Connected.+First+sync+running.", 303)
    resp.delete_cookie("zettle_state")
    asyncio.get_running_loop().run_in_executor(None, sync.sync_merchant, ctx.merchant_id)
    return resp


@app.post("/zettle/sync")
def zettle_sync_now(ctx: Ctx = Depends(require("settings"))):
    result = sync.sync_merchant(ctx.merchant_id)
    msg = (f"Sync failed: {result['error']}" if result["error"] else
           f"Synced {result['products']} products, {result['purchases']} new sales.")
    return RedirectResponse(f"/settings?msg={quote_plus(msg)}", 303)


# ---- Super Admin (/admin) -------------------------------------------------

@app.get("/admin")
def admin_page(
    request: Request,
    msg: str = "",
    invite_token: str = "",
    checkout_url: str = "",
    ctx: Ctx = Depends(require("admin")),
):
    merchants = list_all_merchants()
    for m in merchants:
        m["members"] = list_members(m["id"])
    base_url = str(request.base_url).rstrip("/")
    invite_url = f"{base_url}/invite/{invite_token}" if invite_token else ""
    return render(
        request,
        "admin.html",
        ctx,
        "admin",
        merchants=merchants,
        users=list_all_users_with_memberships(),
        invites=list_invites(),
        roles=ROLES,
        base_url=base_url,
        invite_url=invite_url,
        checkout_url=checkout_url,
        stripe_configured=billing.configured(),
        stripe_webhook_configured=billing.webhook_configured(),
        price_catalog=billing.price_catalog(),
        msg=msg,
    )


@app.post("/admin/merchants/create")
def admin_create_merchant(
    merchant_name: str = Form(...),
    owner_username: str = Form(""),
    owner_display_name: str = Form(""),
    owner_password: str = Form(""),
    ctx: Ctx = Depends(require("admin")),
):
    try:
        owner_id = None
        clean_user = owner_username.strip()
        if clean_user:
            existing = get_user_by_username(clean_user)
            if existing:
                owner_id = existing["id"]
            else:
                created = create_user(
                    clean_user,
                    owner_password,
                    display_name=owner_display_name,
                    is_admin=False,
                    claim_unowned_data=False,
                )
                owner_id = created["id"]
        mid = create_merchant(merchant_name, owner_user_id=owner_id)
        msg = f"Created workspace '{merchant_name.strip()}' (#{mid})."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/admin?msg={quote_plus(msg)}", status_code=303)


@app.post("/admin/merchants/rename")
def admin_rename_merchant(
    merchant_id: int = Form(...),
    name: str = Form(...),
    ctx: Ctx = Depends(require("admin")),
):
    try:
        rename_merchant(merchant_id, name)
        msg = f"Renamed workspace to '{name.strip()}'."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/admin?msg={quote_plus(msg)}", status_code=303)


@app.post("/admin/merchants/delete")
def admin_delete_merchant(
    merchant_id: int = Form(...),
    ctx: Ctx = Depends(require("admin")),
):
    delete_merchant(merchant_id)
    return RedirectResponse(
        f"/admin?msg={quote_plus('Deleted workspace.')}", status_code=303
    )


@app.post("/admin/merchants/sync")
def admin_sync_merchant(
    merchant_id: int = Form(...),
    ctx: Ctx = Depends(require("admin")),
):
    result = sync.sync_merchant(merchant_id)
    msg = (
        f"Sync failed: {result['error']}"
        if result["error"]
        else f"Synced {result['products']} products, {result['purchases']} new sales."
    )
    return RedirectResponse(f"/admin?msg={quote_plus(msg)}", status_code=303)


@app.post("/admin/members/save")
def admin_save_member(
    merchant_id: int = Form(...),
    username: str = Form(...),
    display_name: str = Form(""),
    password: str = Form(""),
    role: str = Form("owner"),
    ctx: Ctx = Depends(require("admin")),
):
    try:
        clean_user = username.strip()
        existing = get_user_by_username(clean_user)
        if existing:
            uid = existing["id"]
        else:
            created = create_user(
                clean_user,
                password,
                display_name=display_name,
                is_admin=False,
                claim_unowned_data=False,
            )
            uid = created["id"]
        add_member(merchant_id, uid, role)
        msg = f"Assigned {clean_user} as {role}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/admin?msg={quote_plus(msg)}", status_code=303)


@app.post("/admin/members/remove")
def admin_remove_member(
    merchant_id: int = Form(...),
    user_id: int = Form(...),
    ctx: Ctx = Depends(require("admin")),
):
    try:
        remove_member(merchant_id, user_id, allow_zero_owners=True)
        msg = "Removed member from workspace."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/admin?msg={quote_plus(msg)}", status_code=303)


@app.post("/admin/invites/create")
def admin_create_invite(
    target_merchant_id: str = Form(""),
    role: str = Form("owner"),
    default_merchant_name: str = Form(""),
    note: str = Form(""),
    days_valid: int = Form(14),
    ctx: Ctx = Depends(require("admin")),
):
    try:
        mid = int(target_merchant_id) if target_merchant_id.strip().isdigit() else None
        token = create_invite(
            merchant_id=mid,
            role=role,
            default_merchant_name=default_merchant_name,
            note=note,
            created_by=ctx.user["id"],
            days_valid=days_valid,
        )
        return RedirectResponse(
            f"/admin?msg={quote_plus('Created one-time invite link.')}&invite_token={quote_plus(token)}",
            status_code=303,
        )
    except ValueError as exc:
        return RedirectResponse(f"/admin?msg={quote_plus(f'Error: {exc}')}", status_code=303)


@app.post("/admin/invites/delete")
def admin_delete_invite(
    token: str = Form(...),
    ctx: Ctx = Depends(require("admin")),
):
    delete_invite(token)
    return RedirectResponse(f"/admin?msg={quote_plus('Deleted invite.')}", status_code=303)


@app.post("/admin/users/password")
def admin_reset_password(
    user_id: int = Form(...),
    new_password: str = Form(...),
    ctx: Ctx = Depends(require("admin")),
):
    try:
        update_user_password(user_id, new_password)
        msg = "Updated user password."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/admin?msg={quote_plus(msg)}", status_code=303)


@app.post("/admin/users/admin")
def admin_toggle_user_admin(
    user_id: int = Form(...),
    is_admin: int = Form(0),
    ctx: Ctx = Depends(require("admin")),
):
    try:
        set_user_admin(user_id, bool(is_admin))
        msg = "Updated Super Admin status."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/admin?msg={quote_plus(msg)}", status_code=303)


# ---- Stripe Billing, Signup & Webhooks ------------------------------------

@app.get("/signup")
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


@app.post("/signup/checkout")
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


@app.get("/onboarding/complete")
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


@app.post("/webhooks/stripe")
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


@app.get("/billing")
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


@app.post("/billing/portal")
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


@app.post("/admin/merchants/billing")
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


@app.post("/admin/billing/checkout-link")
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
