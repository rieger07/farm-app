"""Homestead Planner — FastAPI web app."""

from contextlib import asynccontextmanager
from datetime import date
import os
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from homestead.calendar_export import create_gcal_link, generate_ics_calendar
from homestead.database import (
    add_batch_harvest,
    add_batch_loss,
    add_batch_sale,
    authenticate_user,
    count_users,
    create_user,
    create_user_session,
    delete_batch,
    delete_batch_harvest,
    delete_batch_loss,
    delete_batch_sale,
    delete_species_preset,
    delete_user,
    delete_user_session,
    export_backup_json,
    get_bag_weight,
    get_barrel_capacity,
    get_species_catalog,
    get_user_by_session_token,
    import_backup_json,
    init_db,
    is_public_registration_enabled,
    list_batch_harvests,
    list_batch_losses,
    list_batch_sales,
    list_batches,
    list_users,
    save_batch,
    save_species_preset,
    set_bag_weight,
    set_barrel_capacity,
    set_batch_active,
    set_batch_actual_feed,
    set_public_registration_enabled,
    update_user_password,
    verify_password,
)
from homestead.financials import (
    compute_purchasing_schedule,
    compute_roi_summary_table,
    compute_unified_financials_table,
)
from homestead.scheduling import (
    compute_batch_schedule,
    compute_combined_feed_schedule,
    compute_upcoming_milestones,
    get_batch_status_badge,
)

BASE = Path(__file__).parent / "homestead"
COOKIE = "homestead_session"
NAV = [
    ("radar", "Radar", "/"),
    ("batches", "Batches", "/batches"),
    ("feed", "Feed", "/feed"),
    ("financials", "Financials", "/financials"),
    ("settings", "Settings", "/settings"),
]


class _LoginRedirect(Exception):
    pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


class RevalidatingStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app = FastAPI(title="Homestead Planner", lifespan=lifespan)
app.mount("/static", RevalidatingStatic(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))


def _asset(name: str) -> str:
    p = BASE / "static" / name
    return f"/static/{name}?v={int(p.stat().st_mtime)}" if p.exists() else f"/static/{name}"


templates.env.globals["asset"] = _asset


@app.exception_handler(_LoginRedirect)
def _on_login_redirect(_, __):
    return RedirectResponse("/login", status_code=303)


def current_user(request: Request) -> dict:
    tok = request.cookies.get(COOKIE)
    user = get_user_by_session_token(tok) if tok else None
    if not user:
        raise _LoginRedirect()
    return user


def _cookie_secure(request: Request) -> bool:
    if os.environ.get("FARM_COOKIE_SECURE", "1") == "0":
        return False
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def _set_session_cookie(resp: Response, request: Request, user_id: int) -> None:
    tok = create_user_session(user_id)
    resp.set_cookie(
        COOKIE, tok, httponly=True, samesite="lax",
        secure=_cookie_secure(request), max_age=30 * 86400,
    )


def _schedules_for_user(user_id: int, include_archived: bool = False) -> list[dict]:
    bag_weight = get_bag_weight(user_id=user_id)
    batches = list_batches(user_id=user_id)
    out = []
    for b in batches:
        if not include_archived and not b.get("active", 1):
            continue
        s = compute_batch_schedule(b, bag_weight=bag_weight)
        s["status_badge"] = get_batch_status_badge(s)
        out.append(s)
    return out


def _get_owned_batch(batch_id: int, user: dict) -> dict:
    for b in list_batches(user_id=user["id"]):
        if int(b["id"]) == int(batch_id):
            return b
    raise HTTPException(status_code=404, detail="Batch not found.")


def _render(request: Request, tpl: str, user: dict, active: str, **kw) -> HTMLResponse:
    active_count = sum(1 for b in list_batches(user_id=user["id"]) if b.get("active", 1))
    return templates.TemplateResponse(
        request, tpl,
        {
            "request": request,
            "user": user,
            "nav": NAV,
            "active": active,
            "active_count": active_count,
            "today_iso": date.today().isoformat(),
            **kw,
        },
    )


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/login")
def login_page(request: Request, msg: str = ""):
    tok = request.cookies.get(COOKIE)
    if tok and get_user_by_session_token(tok):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html",
        {
            "request": request,
            "msg": msg,
            "first_setup": count_users() == 0,
            "reg_open": is_public_registration_enabled(),
        },
    )


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    user = authenticate_user(username, password)
    if not user:
        return RedirectResponse("/login?msg=" + quote_plus("Invalid username or password."), status_code=303)
    resp = RedirectResponse("/", status_code=303)
    _set_session_cookie(resp, request, user["id"])
    return resp


@app.post("/register")
def register_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(""),
):
    if count_users() > 0 and not is_public_registration_enabled():
        return RedirectResponse("/login?msg=" + quote_plus("Registration is closed."), status_code=303)
    try:
        user = create_user(username, password, display_name=display_name)
    except ValueError as exc:
        return RedirectResponse("/login?msg=" + quote_plus(str(exc)), status_code=303)
    resp = RedirectResponse("/", status_code=303)
    _set_session_cookie(resp, request, user["id"])
    return resp


@app.post("/logout")
def logout_submit(request: Request):
    tok = request.cookies.get(COOKIE)
    if tok:
        delete_user_session(tok)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/")
def radar_page(request: Request, msg: str = "", user: dict = Depends(current_user)):
    schedules = _schedules_for_user(user["id"], include_archived=False)
    milestones = [m for m in compute_upcoming_milestones(schedules) if m["days_away"] <= 14]
    return _render(
        request, "radar.html", user, "radar",
        schedules=schedules,
        milestones=milestones,
        total_head=sum(s["surviving_quantity"] for s in schedules),
        total_losses=sum(s["initial_quantity"] - s["surviving_quantity"] for s in schedules),
        species_catalog=get_species_catalog(user_id=user["id"]),
        msg=msg,
    )


@app.get("/batches")
def batches_page(request: Request, msg: str = "", user: dict = Depends(current_user)):
    schedules = _schedules_for_user(user["id"], include_archived=True)
    return _render(
        request, "batches.html", user, "batches",
        schedules=schedules,
        species_catalog=get_species_catalog(user_id=user["id"]),
        msg=msg,
    )


@app.get("/batches/{batch_id}")
def batch_detail_page(request: Request, batch_id: int, msg: str = "", user: dict = Depends(current_user)):
    b = _get_owned_batch(batch_id, user)
    bag_weight = get_bag_weight(user_id=user["id"])
    s = compute_batch_schedule(b, bag_weight=bag_weight)
    s["status_badge"] = get_batch_status_badge(s)
    gcal_url = create_gcal_link(
        f"Process {b['name']}",
        s["proc_date"],
        s["proc_date"],
        f"{s['surviving_quantity']} {b['species']}",
    )
    return _render(
        request, "batch_detail.html", user, "batches",
        b=b, s=s, gcal_url=gcal_url,
        losses=list_batch_losses(batch_id),
        harvests=list_batch_harvests(batch_id),
        sales=list_batch_sales(batch_id),
        species_catalog=get_species_catalog(user_id=user["id"]),
        msg=msg,
    )


@app.post("/batches/save")
def batch_save_submit(
    batch_id: str = Form(""),
    name: str = Form(...),
    species: str = Form(...),
    start_date: str = Form(...),
    initial_quantity: int = Form(...),
    unit_cost: float = Form(0.0),
    avg_dressed_weight: float = Form(0.0),
    sale_price_per_lb: float = Form(0.0),
    processing_cost: float = Form(0.0),
    lifecycle_type: str = Form("live_arrival"),
    notes: str = Form(""),
    next_url: str = Form("/batches"),
    user: dict = Depends(current_user),
):
    bid = int(batch_id) if batch_id.strip() else None
    if bid is not None:
        _get_owned_batch(bid, user)
    payload = {
        "name": name.strip(),
        "species": species.strip().lower(),
        "start_date": start_date,
        "quantity": initial_quantity,
        "unit_cost": unit_cost,
        "expected_yield_lbs": avg_dressed_weight,
        "sale_price_per_lb": sale_price_per_lb,
        "processing_cost": processing_cost,
        "lifecycle_type": lifecycle_type,
        "notes": notes.strip(),
    }
    save_batch(payload, batch_id=bid, user_id=user["id"])
    target = next_url if next_url.startswith("/") else "/batches"
    return RedirectResponse(f"{target}?msg=" + quote_plus(f"Saved {name.strip()}."), status_code=303)


@app.post("/batches/{batch_id}/toggle-active")
def batch_toggle_active(batch_id: int, user: dict = Depends(current_user)):
    b = _get_owned_batch(batch_id, user)
    set_batch_active(batch_id, not bool(b.get("active", 1)))
    return RedirectResponse("/batches?msg=" + quote_plus("Updated batch status."), status_code=303)


@app.post("/batches/{batch_id}/delete")
def batch_delete_submit(batch_id: int, user: dict = Depends(current_user)):
    _get_owned_batch(batch_id, user)
    delete_batch(batch_id)
    return RedirectResponse("/batches?msg=" + quote_plus("Batch deleted."), status_code=303)


@app.post("/batches/log-loss")
def batch_log_loss(
    batch_id: int = Form(...),
    loss_date: str = Form(...),
    count: int = Form(...),
    reason: str = Form(""),
    next_url: str = Form("/"),
    user: dict = Depends(current_user),
):
    _get_owned_batch(batch_id, user)
    add_batch_loss(batch_id, loss_date, max(1, count), reason.strip())
    target = next_url if next_url.startswith("/") else "/"
    return RedirectResponse(f"{target}?msg=" + quote_plus(f"Logged {count} loss."), status_code=303)


@app.post("/batches/{batch_id}/losses/{loss_id}/delete")
def batch_loss_delete(batch_id: int, loss_id: int, user: dict = Depends(current_user)):
    _get_owned_batch(batch_id, user)
    delete_batch_loss(loss_id)
    return RedirectResponse(f"/batches/{batch_id}?msg=" + quote_plus("Loss removed."), status_code=303)


@app.post("/batches/log-harvest")
def batch_log_harvest(
    batch_id: int = Form(...),
    harvest_date: str = Form(...),
    head_count: int = Form(...),
    dressed_lbs: float = Form(...),
    processing_cost_total: float = Form(0.0),
    notes: str = Form(""),
    next_url: str = Form("/"),
    user: dict = Depends(current_user),
):
    _get_owned_batch(batch_id, user)
    add_batch_harvest(batch_id, harvest_date, max(1, head_count), dressed_lbs, processing_cost_total, notes.strip())
    target = next_url if next_url.startswith("/") else "/"
    return RedirectResponse(f"{target}?msg=" + quote_plus(f"Logged {head_count} harvested."), status_code=303)


@app.post("/batches/{batch_id}/harvests/{harvest_id}/delete")
def batch_harvest_delete(batch_id: int, harvest_id: int, user: dict = Depends(current_user)):
    _get_owned_batch(batch_id, user)
    delete_batch_harvest(harvest_id)
    return RedirectResponse(f"/batches/{batch_id}?msg=" + quote_plus("Harvest removed."), status_code=303)


@app.post("/batches/log-sale")
def batch_log_sale(
    batch_id: int = Form(...),
    entry_date: str = Form(...),
    allocation_type: str = Form("sale"),
    weight_lbs: float = Form(...),
    revenue: float = Form(0.0),
    notes: str = Form(""),
    next_url: str = Form("/"),
    user: dict = Depends(current_user),
):
    _get_owned_batch(batch_id, user)
    add_batch_sale(batch_id, entry_date, allocation_type, weight_lbs, revenue, notes.strip())
    target = next_url if next_url.startswith("/") else "/"
    return RedirectResponse(f"{target}?msg=" + quote_plus("Saved sale/freezer entry."), status_code=303)


@app.post("/batches/{batch_id}/sales/{sale_id}/delete")
def batch_sale_delete(batch_id: int, sale_id: int, user: dict = Depends(current_user)):
    _get_owned_batch(batch_id, user)
    delete_batch_sale(sale_id)
    return RedirectResponse(f"/batches/{batch_id}?msg=" + quote_plus("Entry removed."), status_code=303)


@app.post("/batches/log-feed")
def batch_log_feed(
    batch_id: int = Form(...),
    actual_feed_lbs: str = Form(""),
    actual_feed_cost: str = Form(""),
    next_url: str = Form("/"),
    user: dict = Depends(current_user),
):
    _get_owned_batch(batch_id, user)
    lbs = float(actual_feed_lbs) if actual_feed_lbs.strip() else None
    cost = float(actual_feed_cost) if actual_feed_cost.strip() else None
    set_batch_actual_feed(batch_id, lbs, cost)
    target = next_url if next_url.startswith("/") else "/"
    return RedirectResponse(f"{target}?msg=" + quote_plus("Saved actual feed."), status_code=303)


@app.get("/calendar.ics")
def calendar_ics(user: dict = Depends(current_user)):
    schedules = _schedules_for_user(user["id"], include_archived=False)
    ics = generate_ics_calendar(schedules)
    return Response(
        ics, media_type="text/calendar",
        headers={"Content-Disposition": 'attachment; filename="homestead-schedule.ics"'},
    )


@app.get("/feed")
def feed_page(request: Request, msg: str = "", user: dict = Depends(current_user)):
    barrel_cap = get_barrel_capacity(user_id=user["id"])
    bag_wt = get_bag_weight(user_id=user["id"])
    schedules = _schedules_for_user(user["id"], include_archived=False)
    weekly_feed, monthly_feed, feed_stats = compute_combined_feed_schedule(schedules, barrel_capacity=barrel_cap)
    purchase_events, _ = compute_purchasing_schedule(schedules, bag_weight=bag_wt)
    today_iso = date.today().isoformat()
    next_feed = next(
        (e for e in purchase_events if e["Feed Bags"] > 0 and e["Date"] >= today_iso),
        next((e for e in purchase_events if e["Feed Bags"] > 0), None),
    )
    return _render(
        request, "feed.html", user, "feed",
        barrel_capacity=barrel_cap,
        bag_weight=bag_wt,
        feed_stats=feed_stats,
        next_feed=next_feed,
        purchase_events=purchase_events,
        monthly_feed=monthly_feed,
        weekly_feed=weekly_feed,
        msg=msg,
    )


@app.post("/feed/settings")
def feed_settings_submit(
    barrel_capacity: int = Form(...),
    bag_weight: int = Form(...),
    user: dict = Depends(current_user),
):
    set_barrel_capacity(max(1, barrel_capacity), user_id=user["id"])
    set_bag_weight(max(1, bag_weight), user_id=user["id"])
    return RedirectResponse("/feed?msg=" + quote_plus("Updated feed container settings."), status_code=303)


@app.get("/financials")
def financials_page(request: Request, user: dict = Depends(current_user)):
    schedules = _schedules_for_user(user["id"], include_archived=False)
    unified_rows = compute_unified_financials_table(schedules)
    roi_rows = compute_roi_summary_table(schedules)
    totals = {
        "est_rev": sum(s["estimated_revenue"] for s in schedules),
        "total_cost": sum(s["total_budget_cost"] for s in schedules),
        "est_profit": sum(s["estimated_profit"] for s in schedules),
        "cash_sales": sum(s["actual_sales_revenue"] for s in schedules),
    }
    chart_peak = max(
        (max(s["total_budget_cost"], s["estimated_revenue"]) for s in schedules),
        default=1,
    ) or 1
    return _render(
        request, "financials.html", user, "financials",
        schedules=schedules,
        unified_rows=unified_rows,
        roi_rows=roi_rows,
        totals=totals,
        chart_peak=chart_peak,
    )


@app.get("/backup.json")
def backup_download(user: dict = Depends(current_user)):
    data = export_backup_json(user_id=user["id"])
    return Response(
        data, media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="homestead-backup.json"'},
    )


@app.get("/settings")
def settings_page(request: Request, msg: str = "", user: dict = Depends(current_user)):
    return _render(
        request, "settings.html", user, "settings",
        species_catalog=get_species_catalog(user_id=user["id"]),
        reg_open=is_public_registration_enabled(),
        users=list_users() if user.get("is_admin") else [],
        msg=msg,
    )


@app.post("/settings/species")
def settings_species_save(
    species: str = Form(...),
    lifecycle_type: str = Form("live_arrival"),
    growth_days: int = Form(56),
    starter_days: int = Form(21),
    starter_lbs_per_day: float = Form(0.12),
    grower_lbs_per_day: float = Form(0.30),
    unit_cost: float = Form(3.5),
    avg_dressed_weight: float = Form(4.5),
    sale_price_per_lb: float = Form(5.5),
    processing_cost: float = Form(5.0),
    starter_bag_price: float = Form(28.0),
    grower_bag_price: float = Form(24.0),
    apply_active: str = Form(""),
    user: dict = Depends(current_user),
):
    save_species_preset(
        species,
        {
            "lifecycle_type": lifecycle_type,
            "growth_days": growth_days,
            "starter_days": starter_days,
            "starter_lbs_per_day": starter_lbs_per_day,
            "grower_lbs_per_day": grower_lbs_per_day,
            "unit_cost": unit_cost,
            "avg_dressed_weight": avg_dressed_weight,
            "sale_price_per_lb": sale_price_per_lb,
            "processing_cost": processing_cost,
            "starter_bag_price": starter_bag_price,
            "grower_bag_price": grower_bag_price,
        },
        user_id=user["id"],
        apply_to_active_batches=bool(apply_active),
    )
    return RedirectResponse("/settings?msg=" + quote_plus(f"Saved {species} preset."), status_code=303)


@app.post("/settings/species/delete")
def settings_species_delete(species: str = Form(...), user: dict = Depends(current_user)):
    delete_species_preset(species, user_id=user["id"])
    return RedirectResponse("/settings?msg=" + quote_plus(f"Deleted {species} preset."), status_code=303)


@app.post("/settings/restore")
async def settings_restore(
    backup_file: UploadFile = File(...),
    replace_existing: str = Form(""),
    user: dict = Depends(current_user),
):
    raw = (await backup_file.read()).decode("utf-8")
    try:
        import_backup_json(raw, user_id=user["id"], replace_existing=bool(replace_existing))
        msg = "Backup restored."
    except Exception as exc:
        msg = f"Restore failed: {exc}"
    return RedirectResponse("/settings?msg=" + quote_plus(msg), status_code=303)


@app.post("/settings/password")
def settings_password(
    current_password: str = Form(...),
    new_password: str = Form(...),
    user: dict = Depends(current_user),
):
    if not verify_password(current_password, user["password_hash"], user["password_salt"]):
        return RedirectResponse("/settings?msg=" + quote_plus("Current password is incorrect."), status_code=303)
    try:
        update_user_password(user["id"], new_password)
        msg = "Password updated."
    except ValueError as exc:
        msg = str(exc)
    return RedirectResponse("/settings?msg=" + quote_plus(msg), status_code=303)


@app.post("/settings/admin/registration")
def settings_admin_registration(enabled: str = Form(...), user: dict = Depends(current_user)):
    if not user.get("is_admin"):
        raise HTTPException(status_code=403)
    set_public_registration_enabled(enabled == "1")
    return RedirectResponse("/settings?msg=" + quote_plus("Updated registration setting."), status_code=303)


@app.post("/settings/admin/users")
def settings_admin_add_user(
    username: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(""),
    user: dict = Depends(current_user),
):
    if not user.get("is_admin"):
        raise HTTPException(status_code=403)
    try:
        create_user(username, password, display_name=display_name, is_admin=False, claim_unowned_data=False)
        msg = f"Created user {username}."
    except ValueError as exc:
        msg = str(exc)
    return RedirectResponse("/settings?msg=" + quote_plus(msg), status_code=303)


@app.post("/settings/admin/users/{uid}/delete")
def settings_admin_delete_user(uid: int, user: dict = Depends(current_user)):
    if not user.get("is_admin"):
        raise HTTPException(status_code=403)
    if uid != user["id"]:
        delete_user(uid)
    return RedirectResponse("/settings?msg=" + quote_plus("User deleted."), status_code=303)
