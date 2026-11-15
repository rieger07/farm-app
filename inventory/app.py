"""FastAPI web app: Inventory, Sales, Quick sale, Invites, and Super Admin pages."""

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
    update_user_password,
)
from inventory import sync, zettle
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
        if not can_access(ctx.role, page):
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
                ("quick_sale", "Quick sale", "/quick-sale"),
                ("settings", "Settings", "/settings"),
            )
            if can_access(ctx.role, k)
        )
    if ctx.is_admin:
        nav.append(("admin", "Admin", "/admin"))
    return templates.TemplateResponse(
        request,
        name,
        {
            "ctx": ctx,
            "active": active,
            "zettle_connected": sync.is_connected(ctx.merchant_id) if ctx.merchant_id else False,
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
    created_mid = None
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
    resp = RedirectResponse("/", status_code=303)
    _set_session_cookies(resp, user["id"], merchant_id=mid)
    return resp


# ---- Inventory, Sales, Quick Sale -----------------------------------------

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
