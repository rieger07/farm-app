"""FastAPI web app: Inventory, Sales and Quick sale pages."""

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from homestead.database import (
    authenticate_user,
    create_user,
    create_user_session,
    delete_user_session,
    get_user_by_session_token,
    get_user_by_username,
)
from inventory import sync, zettle
from inventory.db import init_inventory_db
from inventory.merchants import (
    ROLES,
    add_member,
    can_access,
    count_merchants,
    create_merchant,
    get_membership,
    home_page,
    list_members,
    remove_member,
)
from inventory.sales import (
    money,
    recent_sales,
    record_cash_sale,
    sales_summary,
)
from inventory.stock import (
    add_ledger_entry,
    list_stock,
    set_low_stock_threshold,
    set_stock_count,
)

BASE = Path(__file__).parent
COOKIE = "inv_session"
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


app = FastAPI(title="Hive & Harvest", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.filters["money"] = money


def local_tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("INVENTORY_TZ", "America/New_York"))


def local_day_bounds(day_offset: int = 0) -> tuple[str, str]:
    """UTC ISO bounds [start, end) of the local day offset from today."""
    tz = local_tz()
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today + timedelta(days=day_offset)
    end = start + timedelta(days=1)
    to_utc = lambda d: d.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    return to_utc(start), to_utc(end)


class Ctx:
    def __init__(self, user: dict, membership: dict):
        self.user, self.membership = user, membership

    @property
    def role(self) -> str:
        return self.membership["role"]

    @property
    def merchant_id(self) -> int:
        return self.membership["merchant_id"]


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
    membership = get_membership(user["id"])
    if not membership:
        raise NeedsLogin()
    return Ctx(user, membership)


def require(page: str):
    def dep(ctx: Ctx = Depends(current_ctx)) -> Ctx:
        if not can_access(ctx.role, page):
            raise Forbidden("/" + home_page(ctx.role).replace("_", "-"))
        return ctx

    return dep


def render(request: Request, name: str, ctx: Ctx, active: str, **extra):
    return templates.TemplateResponse(
        request,
        name,
        {
            "ctx": ctx,
            "active": active,
            "zettle_connected": sync.is_connected(ctx.merchant_id),
            "nav": [
                (k, label, path)
                for k, label, path in (
                    ("inventory", "Inventory", "/inventory"),
                    ("sales", "Sales", "/sales"),
                    ("quick_sale", "Quick sale", "/quick-sale"),
                    ("settings", "Settings", "/settings"),
                )
                if can_access(ctx.role, k)
            ],
            **extra,
        },
    )


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/")
def index(ctx: Ctx = Depends(current_ctx)):
    return RedirectResponse("/" + home_page(ctx.role).replace("_", "-"), status_code=303)


@app.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": None, "first_run": count_merchants() == 0},
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
    if first_run:
        clean_merchant = merchant_name.strip() or f"{display_name.strip() or username.strip()}'s Apiary"
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
        create_merchant(clean_merchant, user["id"])
    elif not user or not get_membership(user["id"]):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Wrong username or password.", "first_run": False},
            status_code=401,
        )
    token = create_user_session(user["id"], days_valid=SESSION_DAYS)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
        secure=os.environ.get("INVENTORY_COOKIE_SECURE", "0") == "1",
    )
    return resp


@app.post("/logout")
def logout(request: Request):
    delete_user_session(request.cookies.get(COOKIE, ""))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/inventory")
def inventory_page(
    request: Request, msg: str = "", ctx: Ctx = Depends(require("inventory"))
):
    rows = list_stock(ctx.merchant_id)
    return render(
        request, "inventory.html", ctx, "inventory",
        rows=rows,
        units=sum(max(r["on_hand"], 0) for r in rows),
        low=sum(1 for r in rows if r["status"] in ("low", "count_needed")),
        categories=sorted({r["category"] for r in rows if r["category"]}),
        msg=msg,
    )


@app.post("/inventory/stock")
def inventory_update_stock(
    sku_id: int = Form(...),
    mode: str = Form("set"),
    quantity: int = Form(...),
    low_stock_threshold: int = Form(10),
    note: str = Form(""),
    ctx: Ctx = Depends(require("inventory")),
):
    try:
        set_low_stock_threshold(ctx.merchant_id, sku_id, low_stock_threshold)
        if mode == "set":
            set_stock_count(
                ctx.merchant_id, sku_id, quantity, note=note, created_by=ctx.user["id"]
            )
            msg = f"Set stock count to {quantity} units."
        elif mode == "restock":
            if quantity != 0:
                add_ledger_entry(
                    ctx.merchant_id,
                    sku_id,
                    abs(quantity),
                    "restock",
                    note=note or "Restock",
                    created_by=ctx.user["id"],
                )
            msg = f"Added {abs(quantity)} units to stock."
        else:
            if quantity != 0:
                add_ledger_entry(
                    ctx.merchant_id,
                    sku_id,
                    quantity,
                    "adjustment",
                    note=note or "Adjustment",
                    created_by=ctx.user["id"],
                )
            msg = f"Adjusted stock by {quantity:+d} units."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/inventory?msg={quote_plus(msg)}", status_code=303)


@app.get("/sales")
def sales_page(
    request: Request, period: str = "today", source: str = "all",
    ctx: Ctx = Depends(require("sales")),
):
    if period == "week":
        since, _ = local_day_bounds(-6)
        _, until = local_day_bounds(0)
        label = "Last 7 days"
    elif period == "yesterday":
        since, until = local_day_bounds(-1)
        label = "Yesterday"
    else:
        period = "today"
        since, until = local_day_bounds(0)
        label = "Today"
    summary = sales_summary(ctx.merchant_id, since, until)

    days = []
    for i in range(7):
        s, e = local_day_bounds(-6 + i)
        d = sales_summary(ctx.merchant_id, s, e)
        local = datetime.fromisoformat(s).replace(tzinfo=timezone.utc).astimezone(local_tz())
        days.append({"label": local.strftime("%a"), "zettle_cents": d["zettle_cents"],
                     "cash_cents": d["cash_cents"]})
    peak = max((d["zettle_cents"] + d["cash_cents"] for d in days), default=0) or 1
    best = max(days, key=lambda d: d["zettle_cents"] + d["cash_cents"])

    txns = recent_sales(ctx.merchant_id, since, until,
                        None if source == "all" else source)
    tz = local_tz()
    for t in txns:
        t["local_time"] = (
            datetime.fromisoformat(t["occurred_at"]).replace(tzinfo=timezone.utc)
            .astimezone(tz).strftime("%-I:%M %p").lower()
        )
    return render(
        request, "sales.html", ctx, "sales",
        summary=summary, days=days, peak=peak, best=best, txns=txns,
        period=period, period_label=label, source=source,
        today_label=datetime.now(tz).strftime("%-d %b %Y"),
    )


@app.get("/quick-sale")
def quick_sale_page(request: Request, ctx: Ctx = Depends(require("quick_sale"))):
    skus = [
        {"id": r["id"], "name": r["name"], "variant": r["variant_name"],
         "category": r["category"], "price_cents": r["price_cents"]}
        for r in list_stock(ctx.merchant_id)
    ]
    return render(
        request, "quick_sale.html", ctx, "quick_sale",
        skus=skus, categories=sorted({s["category"] for s in skus if s["category"]}),
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
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result


@app.get("/sw.js")
def service_worker():
    # Served from root so its scope covers /quick-sale.
    return FileResponse(BASE / "static" / "sw.js", media_type="application/javascript")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(BASE / "static" / "manifest.webmanifest",
                        media_type="application/manifest+json")


def _redirect_uri(request: Request) -> str:
    return os.environ.get("ZETTLE_REDIRECT_URI") or str(request.url_for("zettle_callback"))


@app.get("/settings")
def settings_page(request: Request, msg: str = "", ctx: Ctx = Depends(require("settings"))):
    return render(
        request, "settings.html", ctx, "settings",
        status=sync.connection_status(ctx.merchant_id),
        configured=zettle.configured(),
        members=list_members(ctx.merchant_id),
        roles=ROLES,
        redirect_uri=_redirect_uri(request),
        msg=msg,
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
                clean_user, password, display_name=display_name, claim_unowned_data=False
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
    return RedirectResponse("/settings?msg=" + msg.replace(" ", "+"), 303)
