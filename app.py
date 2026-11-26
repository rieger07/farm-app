"""Homestead Planner — FastAPI web app."""

import asyncio
from contextlib import asynccontextmanager
import csv
from datetime import date, datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import quote_plus

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from homestead.calendar_export import create_gcal_link, generate_ics_calendar
from homestead.database import (
    EXPENSE_CATEGORIES,
    HOMESTEAD_ROLES,
    PAYMENT_STATUSES,
    PICKUP_STATUSES,
    PREFERRED_CONTACTS,
    acknowledge_milestone,
    add_batch_feed_log,
    add_batch_harvest,
    add_batch_loss,
    add_batch_sale,
    add_egg_log,
    add_farm_expense,
    add_homestead_merchant_member,
    add_layer_feed_log,
    add_layer_flock_event,
    authenticate_user,
    batch_preorder_counts,
    can_access_homestead_page,
    claim_bird_atomically,
    compute_egg_dashboard,
    count_users,
    create_batch_preorder,
    create_homestead_merchant,
    create_public_preorder_atomic,
    create_user,
    create_user_session,
    customer_insights_summary,
    delete_batch,
    delete_batch_feed_log,
    delete_batch_harvest,
    delete_batch_loss,
    delete_batch_preorder,
    delete_batch_sale,
    delete_customer,
    delete_egg_log,
    delete_egg_pack_log,
    delete_farm_expense,
    delete_layer_feed_log,
    delete_layer_flock_event,
    delete_species_preset,
    delete_user,
    delete_user_session,
    disconnect_google_drive,
    export_backup_json,
    find_or_create_customer,
    get_bag_weight,
    get_barrel_capacity,
    get_batch_by_picking_slug,
    get_customer,
    get_customer_batch_preorder_summary,
    get_farm_pickup_settings,
    get_google_drive_connection,
    get_layer_settings,
    get_species_catalog,
    get_user_by_session_token,
    get_user_by_username,
    graduate_batch_to_layers,
    import_backup_json,
    init_db,
    is_batch_graduated_to_layers,
    is_public_registration_enabled,
    list_acknowledged_milestones,
    list_available_ledger_skus,
    list_batch_feed_logs,
    list_batch_harvests,
    list_batch_losses,
    list_batch_preorders,
    list_batch_sales,
    list_batches,
    list_customer_birds,
    list_customers,
    list_egg_logs,
    list_egg_pack_logs,
    list_farm_expenses,
    list_homestead_merchant_members,
    list_layer_feed_logs,
    list_layer_flock_events,
    list_users,
    merge_customers,
    normalize_egg_species,
    pack_egg_cartons,
    remove_homestead_merchant_member,
    rename_homestead_merchant,
    resolve_homestead_workspace,
    save_batch,
    save_customer,
    save_egg_species_settings,
    save_farm_pickup_settings,
    save_google_drive_connection,
    save_layer_settings,
    save_species_preset,
    set_bag_weight,
    set_barrel_capacity,
    set_batch_active,
    set_batch_actual_feed,
    set_public_registration_enabled,
    settle_customer_batch_pickup,
    update_batch_picking_settings,
    update_batch_preorder,
    update_batch_preorder_settings,
    update_google_drive_folder,
    update_harvest_assignment,
    update_user_password,
    verify_password,
    EGG_SPECIES_DEFAULTS,
    EGG_SPECIES_ORDER,
)
from homestead.gdrive import (
    GoogleDriveError,
    build_auth_url,
    exchange_code_for_tokens,
    get_redirect_uri,
    is_gdrive_configured,
    upload_receipt_to_drive,
)
from homestead.ntfy import (
    get_ntfy_config,
    save_ntfy_config,
    send_ntfy_async,
    send_ntfy_notification,
)
from inventory.crypto import encrypt_token
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
MERCHANT_COOKIE = "homestead_merchant"
NAV = [
    ("radar", "Today", "/"),
    ("batches", "Batches", "/batches"),
    ("eggs", "Eggs", "/eggs"),
    ("customers", "Customers", "/customers"),
    ("farm", "Farm", "/financials"),
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
templates.env.filters["money"] = lambda v: f"${float(v or 0):,.2f}"
templates.env.filters["money0"] = lambda v: f"${float(v or 0):,.0f}"


@app.exception_handler(_LoginRedirect)
def _on_login_redirect(_, __):
    return RedirectResponse("/login", status_code=303)


def current_user(request: Request) -> dict:
    tok = request.cookies.get(COOKIE)
    user = get_user_by_session_token(tok) if tok else None
    if not user:
        raise _LoginRedirect()
    raw_mid = (request.cookies.get(MERCHANT_COOKIE) or "").strip()
    pref_mid = int(raw_mid) if raw_mid.isdigit() else None
    return resolve_homestead_workspace(user, preferred_merchant_id=pref_mid)


@app.post("/switch-merchant")
def switch_merchant_submit(
    request: Request,
    merchant_id: int = Form(...),
    next_url: str = Form("/"),
    user: dict = Depends(current_user),
):
    allowed = {int(m["merchant_id"]) for m in user.get("merchants", [])}
    target = next_url if next_url.startswith("/") else "/"
    resp = RedirectResponse(target, status_code=303)
    if int(merchant_id) in allowed:
        resp.set_cookie(
            MERCHANT_COOKIE,
            str(int(merchant_id)),
            httponly=True,
            samesite="lax",
            secure=_cookie_secure(request),
            max_age=30 * 86400,
        )
    return resp


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
    role = user.get("role") or "owner"
    if not can_access_homestead_page(role, active):
        raise HTTPException(status_code=403, detail="Your role does not have access to this page.")
    can_farm = any(can_access_homestead_page(role, p) for p in ("feed", "financials", "settings"))
    visible_nav = [
        item for item in NAV
        if (can_farm if item[0] == "farm" else can_access_homestead_page(role, item[0]))
    ]
    nav_active = "farm" if active in ("feed", "financials", "settings") else active
    active_count = sum(1 for b in list_batches(user_id=user["id"]) if b.get("active", 1))
    return templates.TemplateResponse(
        request, tpl,
        {
            "request": request,
            "user": user,
            "nav": visible_nav,
            "active": nav_active,
            "sub_active": active,
            "active_count": active_count,
            "today_iso": date.today().isoformat(),
            **kw,
        },
    )


def optional_user(request: Request) -> dict | None:
    tok = request.cookies.get(COOKIE)
    return get_user_by_session_token(tok) if tok else None


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/home")
def public_home_page(request: Request):
    return templates.TemplateResponse(request, "home.html", {"request": request})


@app.get("/privacy")
def privacy_page(request: Request):
    return templates.TemplateResponse(request, "privacy.html", {"request": request})


@app.get("/terms")
def terms_page(request: Request):
    return templates.TemplateResponse(request, "terms.html", {"request": request})


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


def _build_timeline(schedules: list[dict]) -> dict | None:
    if not schedules:
        return None
    min_d = min(s["start_date"] for s in schedules)
    max_d = max(s["proc_date"] for s in schedules)
    span = max(1, (max_d - min_d).days)
    pct = lambda d: round(max(0.0, min(100.0, (d - min_d).days / span * 100)), 2)

    today = date.today()
    today_pct = pct(today) if min_d <= today <= max_d else None

    months = []
    cur = min_d.replace(day=1)
    while cur <= max_d:
        if cur >= min_d:
            months.append({"label": cur.strftime("%b '%y"), "pct": pct(cur)})
        cur = date(cur.year + (cur.month // 12), (cur.month % 12) + 1, 1)

    rows = []
    for s in schedules:
        b = s["batch"]
        rows.append({
            "id": b["id"],
            "name": b["name"],
            "span_label": f"{s['start_date'].strftime('%b %-d')} → {s['proc_date'].strftime('%b %-d')}",
            "incub_left": pct(s["start_date"]),
            "incub_w": round(max(0.0, pct(s["proc_base"]) - pct(s["start_date"])), 2),
            "start_left": pct(s["proc_base"]),
            "start_w": round(max(0.0, pct(s["transition_date"]) - pct(s["proc_base"])), 2),
            "grow_left": pct(s["transition_date"]),
            "grow_w": round(max(0.0, pct(s["proc_date"]) - pct(s["transition_date"])), 2),
        })
    return {"rows": rows, "months": months, "today_pct": today_pct}


@app.get("/")
def radar_page(request: Request, msg: str = "", user: dict | None = Depends(optional_user)):
    if user is None:
        return templates.TemplateResponse(request, "home.html", {"request": request})
    schedules = _schedules_for_user(user["id"], include_archived=False)
    active_schedules = [s for s in schedules if not s["is_planned"]]
    planned_schedules = [s for s in schedules if s["is_planned"]]
    acked = list_acknowledged_milestones([s["batch"]["id"] for s in schedules])
    milestones = [
        m for m in compute_upcoming_milestones(schedules)
        if m["days_away"] <= 14 and (int(m["batch_id"]), str(m["task"])) not in acked
    ]
    return _render(
        request, "radar.html", user, "radar",
        schedules=schedules,
        active_schedules=active_schedules,
        planned_schedules=planned_schedules,
        milestones=milestones,
        timeline=_build_timeline(schedules),
        total_head=sum(s["on_farm_head"] for s in active_schedules),
        total_losses=sum(s["initial_quantity"] - s["surviving_quantity"] for s in active_schedules),
        bag_weight=get_bag_weight(user_id=user["id"]),
        egg_dash=compute_egg_dashboard(user["id"], bag_weight=get_bag_weight(user_id=user["id"])),
        species_catalog=get_species_catalog(user_id=user["id"]),
        msg=msg,
    )


@app.post("/milestones/ack")
def milestone_ack_submit(
    batch_id: int = Form(...),
    task: str = Form(...),
    user: dict = Depends(current_user),
):
    _get_owned_batch(batch_id, user)
    acknowledge_milestone(batch_id, task, done=True)
    return RedirectResponse("/?msg=" + quote_plus(f"Checked off: {task}"), status_code=303)


@app.get("/batches")
def batches_page(request: Request, msg: str = "", user: dict = Depends(current_user)):
    schedules = _schedules_for_user(user["id"], include_archived=True)
    po_counts = batch_preorder_counts(user["id"])
    return _render(
        request, "batches.html", user, "batches",
        schedules=schedules,
        po_counts=po_counts,
        species_catalog=get_species_catalog(user_id=user["id"]),
        msg=msg,
    )


def _batch_stage_info(b: dict, s: dict, user_id: int, tally: dict | None = None) -> dict:
    if tally is None:
        tally = _harvest_tally_payload(b, user_id)
    po_summary = batch_preorder_counts(user_id).get(
        int(b["id"]), {"reserved_qty": 0, "waitlist_qty": 0, "deposits_paid": 0.0}
    )
    by_cust_stat: dict[int, bool] = {}
    for e in tally["entries"]:
        if not e["for_sale"] or not e["customer_id"]:
            continue
        cid = int(e["customer_id"])
        done = e["pickup_status"] == "picked_up" and e["payment_status"] == "paid"
        by_cust_stat[cid] = by_cust_stat.get(cid, True) and done
    pending_pickup = sum(1 for done in by_cust_stat.values() if not done)
    completed_pickup = sum(1 for done in by_cust_stat.values() if done)
    if pending_pickup > 0:
        current_stage = "pickup"
    elif s.get("harvested_head", 0) > 0 or b.get("picking_enabled"):
        current_stage = "harvest"
    elif b.get("preorder_enabled") or po_summary["reserved_qty"] > 0:
        current_stage = "preorders"
    else:
        current_stage = "grow"
    return {
        "po_summary": po_summary,
        "pending_pickup_customers": pending_pickup,
        "completed_pickup_customers": completed_pickup,
        "current_stage": current_stage,
    }


@app.get("/batches/{batch_id}")
def batch_detail_page(request: Request, batch_id: int, msg: str = "", user: dict = Depends(current_user)):
    b = _get_owned_batch(batch_id, user)
    bag_weight = get_bag_weight(user_id=user["id"])
    s = compute_batch_schedule(b, bag_weight=bag_weight)
    s["status_badge"] = get_batch_status_badge(s)
    gcal_arrival_url = create_gcal_link(
        f"{b['name']} - {s['initial_label']}",
        s["start_date"],
        s["end_date"],
        f"{s['initial_quantity']} {b['species']}",
    )
    gcal_url = create_gcal_link(
        f"Process {b['name']}",
        s["proc_date"],
        s["proc_date"],
        f"{s['surviving_quantity']} {b['species']}",
    )
    tally = _harvest_tally_payload(b, user["id"])
    tally_by_id = {int(e["id"]): e for e in tally["entries"]}
    raw_harvests = list_batch_harvests(batch_id)
    enriched_harvests = []
    for h in raw_harvests:
        row = dict(h)
        ti = tally_by_id.get(int(h["id"]))
        row["tally"] = ti
        enriched_harvests.append(row)

    tally_allocations = []
    for e in tally["entries"]:
        if not e["for_sale"]:
            tally_allocations.append({**e, "alloc_kind": "freezer"})
        else:
            is_sold = (
                e["payment_status"] == "paid"
                or e["pickup_status"] == "picked_up"
                or e["actual_sale_price"] is not None
            )
            if is_sold:
                tally_allocations.append({**e, "alloc_kind": "sale"})
            elif e["customer_id"]:
                tally_allocations.append({**e, "alloc_kind": "assigned"})

    stage_info = _batch_stage_info(b, s, user["id"], tally=tally)
    po_summary = stage_info["po_summary"]
    pending_pickup_customers = stage_info["pending_pickup_customers"]

    return _render(
        request, "batch_detail.html", user, "batches",
        b=b, s=s, gcal_url=gcal_url, gcal_arrival_url=gcal_arrival_url,
        losses=list_batch_losses(batch_id),
        harvests=enriched_harvests,
        sales=list_batch_sales(batch_id),
        tally=tally,
        tally_allocations=tally_allocations,
        po_summary=po_summary,
        pending_pickup_customers=pending_pickup_customers,
        stage_info=stage_info,
        batch_stage="grow",
        is_graduated=is_batch_graduated_to_layers(user["id"], batch_id),
        feed_logs=list_batch_feed_logs(batch_id),
        bag_weight=bag_weight,
        gdrive_connected=bool(get_google_drive_connection(user["id"])),
        species_catalog=get_species_catalog(user_id=user["id"]),
        msg=msg,
    )


_PICK_SUBSCRIBERS: dict[str, set[asyncio.Queue]] = {}


def _broadcast_pick_event(slug: str | None, payload: dict) -> None:
    if not slug:
        return
    data = json.dumps(payload)
    for q in list(_PICK_SUBSCRIBERS.get(slug, ())):
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            pass


def _fmt_lb_oz(lbs: float) -> str:
    total_oz = round(max(0.0, float(lbs or 0)) * 16)
    whole_lb, rem_oz = divmod(total_oz, 16)
    return f"{whole_lb} lb {rem_oz} oz"


def _harvest_tally_payload(batch: dict, user_id: int) -> dict:
    bag_weight = get_bag_weight(user_id=user_id)
    s = compute_batch_schedule(batch, bag_weight=bag_weight)
    harvests = sorted(
        list_batch_harvests(batch["id"]),
        key=lambda h: int(h.get("id") or 0),
        reverse=True,
    )
    customers = list_customers(user_id)
    cust_by_id = {int(c["id"]): c for c in customers}

    harvested_head = sum(int(h.get("head_count") or 0) for h in harvests)
    surviving = int(s["surviving_quantity"])
    remaining = max(0, surviving - harvested_head)
    total_lbs = round(sum(float(h.get("dressed_lbs") or 0) for h in harvests), 2)
    per_bird = [
        round(float(h["dressed_lbs"]) / max(1, int(h["head_count"])), 2)
        for h in harvests
        if float(h.get("dressed_lbs") or 0) > 0
    ]
    avg_lbs = round(total_lbs / harvested_head, 2) if harvested_head > 0 else 0.0
    min_lbs = min(per_bird) if per_bird else 0.0
    max_lbs = max(per_bird) if per_bird else 0.0
    sale_price = float(batch.get("sale_price_per_lb") or 0.0)
    est_total_lbs = round(surviving * float(batch.get("avg_dressed_weight") or 0.0), 1)
    progress_pct = min(100, round(harvested_head / surviving * 100)) if surviving > 0 else 0

    for_sale_lbs = 0.0
    for_sale_value = 0.0
    available_count = 0
    assigned_count = 0
    picked_up_count = 0
    not_for_sale_count = 0
    entries = []
    for idx, h in enumerate(harvests):
        hc = max(1, int(h.get("head_count") or 1))
        dl = round(float(h.get("dressed_lbs") or 0), 2)
        for_sale = bool(1 if h.get("for_sale") is None else h.get("for_sale"))
        cid = int(h["customer_id"]) if h.get("customer_id") else None
        cust = cust_by_id.get(cid) if cid else None
        dep = round(float(h.get("deposit_paid") or 0.0), 2)
        pay_stat = h.get("payment_status") or "unpaid"
        pick_stat = h.get("pickup_status") or ("assigned" if cid else "unassigned")
        est_price = round(dl * sale_price, 2) if for_sale else 0.0
        actual_p = (
            round(float(h["actual_sale_price"]), 2)
            if h.get("actual_sale_price") is not None and for_sale
            else None
        )
        effective_price = actual_p if actual_p is not None else est_price
        tip_diff = round(effective_price - est_price, 2) if for_sale else 0.0
        bal_due = round(max(0.0, effective_price - dep), 2) if for_sale else 0.0

        if for_sale:
            for_sale_lbs += dl
            for_sale_value += effective_price
            if not cid:
                available_count += 1
            elif pick_stat == "picked_up":
                picked_up_count += 1
            else:
                assigned_count += 1
        else:
            not_for_sale_count += 1

        entries.append({
            "id": int(h["id"]),
            "num": len(harvests) - idx,
            "harvest_date": h["harvest_date"],
            "head_count": hc,
            "dressed_lbs": dl,
            "lb_oz": _fmt_lb_oz(dl),
            "per_bird_lbs": round(dl / hc, 2),
            "notes": h.get("notes") or "",
            "for_sale": for_sale,
            "customer_id": cid,
            "customer_name": cust["name"] if cust else "",
            "customer_phone": cust["phone"] if cust else "",
            "customer_email": cust["email"] if cust else "",
            "customer_pref": cust["preferred_contact"] if cust else "sms",
            "deposit_paid": dep,
            "payment_status": pay_stat,
            "pickup_status": pick_stat,
            "est_price": est_price,
            "actual_sale_price": actual_p,
            "effective_price": effective_price,
            "tip_diff": tip_diff,
            "balance_due": bal_due,
            "claimed_at": h.get("claimed_at") or "",
        })

    est_value = round(for_sale_value, 2)
    return {
        "batch_id": int(batch["id"]),
        "batch_name": batch["name"],
        "harvested_head": harvested_head,
        "surviving_quantity": surviving,
        "remaining_head": remaining,
        "progress_pct": progress_pct,
        "total_lbs": total_lbs,
        "for_sale_lbs": round(for_sale_lbs, 2),
        "est_total_lbs": est_total_lbs,
        "avg_lbs": avg_lbs,
        "min_lbs": min_lbs,
        "max_lbs": max_lbs,
        "sale_price_per_lb": sale_price,
        "est_value": est_value,
        "available_count": available_count,
        "assigned_count": assigned_count,
        "picked_up_count": picked_up_count,
        "not_for_sale_count": not_for_sale_count,
        "picking_enabled": bool(batch.get("picking_enabled")),
        "picking_preorder_only": bool(batch.get("picking_preorder_only")),
        "picking_slug": batch.get("picking_slug") or "",
        "max_birds_per_customer": batch.get("max_birds_per_customer"),
        "entries": entries,
    }


@app.get("/batches/{batch_id}/harvest")
def batch_harvest_tally_page(
    request: Request, batch_id: int, user: dict = Depends(current_user)
):
    b = _get_owned_batch(batch_id, user)
    bag_weight = get_bag_weight(user_id=user["id"])
    s = compute_batch_schedule(b, bag_weight=bag_weight)
    tally = _harvest_tally_payload(b, user["id"])
    stage_info = _batch_stage_info(b, s, user["id"], tally=tally)
    pick_url = f"{str(request.base_url).rstrip('/')}/pick/{tally['picking_slug']}" if tally["picking_slug"] else ""
    return _render(
        request, "harvest_tally.html", user, "batches",
        b=b, s=s, tally=tally,
        stage_info=stage_info,
        batch_stage="harvest",
        customers=list_customers(user["id"]),
        pick_url=pick_url,
    )


@app.post("/api/batches/{batch_id}/harvest")
async def api_batch_harvest_add(
    batch_id: int, request: Request, user: dict = Depends(current_user)
):
    b = _get_owned_batch(batch_id, user)
    payload = await request.json()
    hdate = str(payload.get("harvest_date") or date.today().isoformat()).strip()
    head_count = max(1, int(payload.get("head_count") or 1))
    dressed_lbs = float(payload.get("dressed_lbs") or 0)
    if dressed_lbs <= 0:
        raise HTTPException(status_code=400, detail="Weight must be greater than 0.")
    notes = str(payload.get("notes") or "").strip()
    proc_cost = float(payload.get("processing_cost_total") or 0.0)
    for_sale = bool(payload.get("for_sale", True))
    bird_id = add_batch_harvest(
        batch_id, hdate, head_count, round(dressed_lbs, 2), proc_cost, notes, for_sale=for_sale
    )
    _broadcast_pick_event(
        b.get("picking_slug"),
        {"bird_id": bird_id, "status": "available" if for_sale else "hidden", "reload": True},
    )
    return _harvest_tally_payload(b, user["id"])


@app.post("/api/batches/{batch_id}/harvest/{harvest_id}/assign")
async def api_batch_harvest_assign(
    batch_id: int, harvest_id: int, request: Request, user: dict = Depends(current_user)
):
    b = _get_owned_batch(batch_id, user)
    payload = await request.json()
    raw_cid = payload.get("customer_id")
    cid = int(raw_cid) if raw_cid not in (None, "", 0, "0") else None
    if cid is not None and not get_customer(user["id"], cid):
        raise HTTPException(status_code=404, detail="Customer not found.")
    for_sale = bool(payload.get("for_sale", True))
    deposit_paid = float(payload.get("deposit_paid") or 0.0)
    raw_actual = payload.get("actual_sale_price")
    actual_sale_price = (
        float(raw_actual)
        if raw_actual not in (None, "")
        else None
    )
    payment_status = str(payload.get("payment_status") or "unpaid")
    pickup_status = str(payload.get("pickup_status") or ("assigned" if cid else "unassigned"))
    notes = payload.get("notes")
    # Auto-apply unapplied paid pre-order deposit if newly assigning with no manual deposit entered
    if cid is not None and for_sale and deposit_paid == 0.0 and payment_status == "unpaid":
        existing_hv = next((h for h in list_batch_harvests(batch_id) if int(h["id"]) == int(harvest_id)), None)
        prev_cid = int(existing_hv["customer_id"]) if existing_hv and existing_hv.get("customer_id") else None
        if prev_cid != cid:
            po_sum = get_customer_batch_preorder_summary(batch_id, cid)
            rem = float(po_sum.get("remaining_unapplied_deposit") or 0.0)
            if rem > 0.0:
                per_b = float(po_sum.get("deposit_per_bird") or 0.0)
                if po_sum["reserved_qty"] > 0 and (po_sum["claimed_count"] + 1) >= po_sum["reserved_qty"]:
                    deposit_paid = rem
                elif per_b > 0.0:
                    deposit_paid = round(min(rem, per_b), 2)
                else:
                    deposit_paid = rem
                payment_status = "deposit"
    update_harvest_assignment(
        batch_id,
        harvest_id,
        customer_id=cid,
        for_sale=for_sale,
        deposit_paid=deposit_paid,
        actual_sale_price=actual_sale_price,
        payment_status=payment_status,
        pickup_status=pickup_status,
        notes=str(notes) if notes is not None else None,
    )
    pub_status = "hidden" if not for_sale else ("claimed" if cid else "available")
    _broadcast_pick_event(
        b.get("picking_slug"), {"bird_id": int(harvest_id), "status": pub_status}
    )
    return _harvest_tally_payload(b, user["id"])


@app.delete("/api/batches/{batch_id}/harvest/{harvest_id}")
def api_batch_harvest_delete(
    batch_id: int, harvest_id: int, user: dict = Depends(current_user)
):
    b = _get_owned_batch(batch_id, user)
    delete_batch_harvest(harvest_id)
    _broadcast_pick_event(
        b.get("picking_slug"), {"bird_id": int(harvest_id), "status": "hidden"}
    )
    return _harvest_tally_payload(b, user["id"])


@app.post("/api/batches/{batch_id}/picking")
async def api_batch_picking_settings(
    batch_id: int, request: Request, user: dict = Depends(current_user)
):
    _get_owned_batch(batch_id, user)
    payload = await request.json()
    enabled = bool(payload.get("picking_enabled"))
    raw_max = payload.get("max_birds_per_customer")
    max_birds = int(raw_max) if raw_max not in (None, "", 0, "0") else None
    po_only = bool(payload["picking_preorder_only"]) if "picking_preorder_only" in payload else None
    updated = update_batch_picking_settings(
        user["id"], batch_id, enabled, max_birds, picking_preorder_only=po_only
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Batch not found.")
    return {
        "picking_enabled": bool(updated.get("picking_enabled")),
        "picking_preorder_only": bool(updated.get("picking_preorder_only")),
        "picking_slug": updated.get("picking_slug") or "",
        "max_birds_per_customer": updated.get("max_birds_per_customer"),
    }


# ---------------------------------------------------------------------------
# Customer Directory
# ---------------------------------------------------------------------------
@app.get("/customers")
def customers_page(
    request: Request,
    q: str = "",
    selected: str = "",
    msg: str = "",
    user: dict = Depends(current_user),
):
    insights = customer_insights_summary(user["id"], search=q)
    customers = insights["customers"]
    sel_id = int(selected) if selected.strip().isdigit() else None
    sel_customer = next((c for c in customers if int(c["id"]) == sel_id), None) if sel_id else None
    if sel_id and not sel_customer:
        sel_customer = get_customer(user["id"], sel_id)
    sel_birds = list_customer_birds(user["id"], sel_id) if sel_customer else []
    for bird in sel_birds:
        bird["lb_oz"] = _fmt_lb_oz(bird["dressed_lbs"])
        calc_p = round(float(bird["dressed_lbs"] or 0) * float(bird["sale_price_per_lb"] or 0), 2)
        act_p = (
            round(float(bird["actual_sale_price"]), 2)
            if bird.get("actual_sale_price") is not None
            else calc_p
        )
        bird["calculated_price"] = calc_p
        bird["actual_price"] = act_p
        bird["tip_diff"] = round(act_p - calc_p, 2)
    return _render(
        request, "customers.html", user, "customers",
        customers=customers,
        insights=insights,
        q=q,
        sel_customer=sel_customer,
        sel_birds=sel_birds,
        preferred_contacts=PREFERRED_CONTACTS,
        msg=msg,
    )


@app.post("/customers/save")
def customers_save_submit(
    customer_id: str = Form(""),
    name: str = Form(...),
    phone: str = Form(""),
    email: str = Form(""),
    preferred_contact: str = Form("sms"),
    notes: str = Form(""),
    user: dict = Depends(current_user),
):
    cid = int(customer_id) if customer_id.strip().isdigit() else None
    try:
        saved_id = save_customer(
            user["id"], name=name, phone=phone, email=email,
            preferred_contact=preferred_contact, notes=notes, customer_id=cid,
        )
        return RedirectResponse(
            f"/customers?selected={saved_id}&msg=" + quote_plus(f"Saved {name.strip()}."),
            status_code=303,
        )
    except ValueError as exc:
        return RedirectResponse("/customers?msg=" + quote_plus(str(exc)), status_code=303)


@app.post("/customers/{customer_id}/delete")
def customers_delete_submit(customer_id: int, user: dict = Depends(current_user)):
    delete_customer(user["id"], customer_id)
    return RedirectResponse("/customers?msg=" + quote_plus("Customer deleted."), status_code=303)


@app.post("/customers/{customer_id}/merge")
def customers_merge_submit(
    customer_id: int,
    target_customer_id: int = Form(...),
    user: dict = Depends(current_user),
):
    try:
        merge_customers(user["id"], customer_id, target_customer_id)
        return RedirectResponse(
            f"/customers?selected={target_customer_id}&msg=" + quote_plus("Merged duplicate customer records."),
            status_code=303,
        )
    except ValueError as exc:
        return RedirectResponse(
            f"/customers?selected={customer_id}&msg=" + quote_plus(str(exc)),
            status_code=303,
        )


@app.post("/api/customers")
async def api_customer_create(request: Request, user: dict = Depends(current_user)):
    payload = await request.json()
    try:
        cid = save_customer(
            user["id"],
            name=str(payload.get("name") or ""),
            phone=str(payload.get("phone") or ""),
            email=str(payload.get("email") or ""),
            preferred_contact=str(payload.get("preferred_contact") or "sms"),
            notes=str(payload.get("notes") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return get_customer(user["id"], cid)


# ---------------------------------------------------------------------------
# Public Live Customer Picking Page (/pick/<slug>) + SSE
# ---------------------------------------------------------------------------
def _public_picking_birds(batch: dict) -> list[dict]:
    """Return ONLY for_sale birds with ZERO buyer PII."""
    harvests = sorted(
        list_batch_harvests(batch["id"]),
        key=lambda h: int(h.get("id") or 0),
    )
    price = float(batch.get("sale_price_per_lb") or 0.0)
    birds = []
    for idx, h in enumerate(harvests, start=1):
        for_sale = bool(1 if h.get("for_sale") is None else h.get("for_sale"))
        if not for_sale:
            continue
        dl = round(float(h.get("dressed_lbs") or 0), 2)
        claimed = bool(h.get("customer_id"))
        birds.append({
            "id": int(h["id"]),
            "num": idx,
            "dressed_lbs": dl,
            "lb_oz": _fmt_lb_oz(dl),
            "est_price": round(dl * price, 2),
            "status": "claimed" if claimed else "available",
        })
    birds.sort(key=lambda x: (1 if x["status"] == "claimed" else 0, x["dressed_lbs"]))
    return birds


@app.get("/pick/{slug}")
def public_picking_page(request: Request, slug: str):
    batch = get_batch_by_picking_slug(slug)
    if not batch:
        raise HTTPException(status_code=404, detail="Picking page not found.")
    enabled = bool(batch.get("picking_enabled"))
    birds = _public_picking_birds(batch) if enabled else []
    return templates.TemplateResponse(
        request, "pick.html",
        {
            "request": request,
            "batch_name": batch["name"],
            "species": batch["species"],
            "sale_price_per_lb": float(batch.get("sale_price_per_lb") or 0.0),
            "max_birds_per_customer": batch.get("max_birds_per_customer"),
            "picking_enabled": enabled,
            "picking_preorder_only": bool(batch.get("picking_preorder_only")),
            "pickup_date": batch.get("pickup_date") or "",
            "pickup_window": batch.get("pickup_window") or "",
            "slug": slug,
            "birds": birds,
        },
    )


@app.post("/api/pick/{slug}/identify")
async def api_public_pick_identify(slug: str, request: Request):
    batch = get_batch_by_picking_slug(slug)
    if not batch or not batch.get("picking_enabled"):
        raise HTTPException(status_code=404, detail="Picking is not currently open.")
    payload = await request.json()
    try:
        cust = find_or_create_customer(
            int(batch["user_id"]),
            str(payload.get("name") or ""),
            str(payload.get("contact") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    po_summary = get_customer_batch_preorder_summary(int(batch["id"]), int(cust["id"]))
    po_only = bool(batch.get("picking_preorder_only"))
    if po_only and not po_summary["has_reservation"]:
        raise HTTPException(
            status_code=403,
            detail="Early bird picking is currently open to pre-order customers only. Please use the phone number or email from your reservation.",
        )

    base_max = batch.get("max_birds_per_customer")
    if po_only:
        effective_max = po_summary["reserved_qty"]
    elif po_summary["has_reservation"] and base_max and po_summary["reserved_qty"] > int(base_max):
        effective_max = po_summary["reserved_qty"]
    else:
        effective_max = base_max

    harvests = sorted(list_batch_harvests(batch["id"]), key=lambda h: int(h.get("id") or 0))
    price = float(batch.get("sale_price_per_lb") or 0.0)
    mine = []
    for idx, h in enumerate(harvests, start=1):
        if h.get("customer_id") and int(h["customer_id"]) == int(cust["id"]) and bool(1 if h.get("for_sale") is None else h.get("for_sale")):
            dl = round(float(h.get("dressed_lbs") or 0), 2)
            mine.append({
                "id": int(h["id"]),
                "num": idx,
                "dressed_lbs": dl,
                "lb_oz": _fmt_lb_oz(dl),
                "est_price": round(dl * price, 2),
                "deposit_paid": round(float(h.get("deposit_paid") or 0.0), 2),
            })
    return {
        "customer_id": int(cust["id"]),
        "name": cust["name"],
        "my_birds": mine,
        "effective_max_birds": effective_max,
        "reserved_qty": po_summary["reserved_qty"],
        "unapplied_deposit": po_summary["remaining_unapplied_deposit"],
    }


@app.post("/api/pick/{slug}/claim/{bird_id}")
async def api_public_pick_claim(slug: str, bird_id: int, request: Request):
    batch = get_batch_by_picking_slug(slug)
    if not batch or not batch.get("picking_enabled"):
        raise HTTPException(status_code=404, detail="Picking is not currently open.")
    payload = await request.json()
    cid = payload.get("customer_id")
    if not cid or not get_customer(int(batch["user_id"]), int(cid)):
        raise HTTPException(status_code=400, detail="Please enter your name and contact info first.")

    po_summary = get_customer_batch_preorder_summary(int(batch["id"]), int(cid))
    po_only = bool(batch.get("picking_preorder_only"))
    if po_only and not po_summary["has_reservation"]:
        raise HTTPException(
            status_code=403,
            detail="Early bird picking is currently open to pre-order customers only.",
        )
    base_max = batch.get("max_birds_per_customer")
    if po_only:
        effective_max = po_summary["reserved_qty"]
    elif po_summary["has_reservation"] and base_max and po_summary["reserved_qty"] > int(base_max):
        effective_max = po_summary["reserved_qty"]
    else:
        effective_max = base_max

    ok, reason = claim_bird_atomically(
        int(batch["id"]),
        int(bird_id),
        int(cid),
        max_birds_per_customer=effective_max,
        apply_preorder_deposit=True,
    )
    if not ok:
        if reason == "limit_reached":
            raise HTTPException(
                status_code=409,
                detail=f"Limit of {effective_max} bird(s) per customer reached.",
            )
        raise HTTPException(
            status_code=409,
            detail="Someone just grabbed that bird—please pick another!",
        )
    _broadcast_pick_event(slug, {"bird_id": int(bird_id), "status": "claimed"})
    harvests = sorted(list_batch_harvests(batch["id"]), key=lambda h: int(h.get("id") or 0))
    price = float(batch.get("sale_price_per_lb") or 0.0)
    claimed_info = None
    for idx, h in enumerate(harvests, start=1):
        if int(h["id"]) == int(bird_id):
            dl = round(float(h.get("dressed_lbs") or 0), 2)
            claimed_info = {
                "id": int(h["id"]),
                "num": idx,
                "dressed_lbs": dl,
                "lb_oz": _fmt_lb_oz(dl),
                "est_price": round(dl * price, 2),
                "deposit_paid": round(float(h.get("deposit_paid") or 0.0), 2),
            }
            break
    if claimed_info:
        cust = get_customer(int(batch["user_id"]), int(cid))
        buyer_name = cust["name"] if cust else "A customer"
        click_url = f"{str(request.base_url).rstrip('/')}/batches/{batch['id']}/harvest"
        dep_note = f" (Deposit ${claimed_info['deposit_paid']:.2f} applied)" if claimed_info["deposit_paid"] > 0 else ""
        send_ntfy_async(
            int(batch["user_id"]),
            f"Bird claimed - {batch['name']}",
            f"{buyer_name} claimed Bird #{claimed_info['num']} "
            f"({claimed_info['dressed_lbs']:.2f} lb / {claimed_info['lb_oz']} - "
            f"${claimed_info['est_price']:.2f}){dep_note}",
            click_url=click_url,
        )
    return {"ok": True, "bird": claimed_info}


@app.get("/api/pick/{slug}/events")
async def api_public_pick_events(slug: str, request: Request):
    batch = get_batch_by_picking_slug(slug)
    if not batch or not batch.get("picking_enabled"):
        raise HTTPException(status_code=404, detail="Picking is not currently open.")
    q: asyncio.Queue = asyncio.Queue(maxsize=64)
    _PICK_SUBSCRIBERS.setdefault(slug, set()).add(q)

    async def event_stream():
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25.0)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            subs = _PICK_SUBSCRIBERS.get(slug)
            if subs:
                subs.discard(q)
                if not subs:
                    _PICK_SUBSCRIBERS.pop(slug, None)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Batch Pre-Orders, Waitlist & Public Reservation (/reserve/<slug>)
# ---------------------------------------------------------------------------
def _effective_preorder_cap(batch: dict, schedule: dict | None = None) -> int:
    raw = batch.get("preorder_cap")
    if raw is not None and int(raw) > 0:
        return int(raw)
    if schedule is not None:
        return max(1, int(schedule.get("surviving_quantity") or batch.get("initial_quantity") or 1))
    return max(1, int(batch.get("initial_quantity") or 1))


@app.get("/batches/{batch_id}/preorders")
def batch_preorders_page(
    request: Request, batch_id: int, msg: str = "", user: dict = Depends(current_user)
):
    b = _get_owned_batch(batch_id, user)
    bag_weight = get_bag_weight(user_id=user["id"])
    s = compute_batch_schedule(b, bag_weight=bag_weight)
    preorders = list_batch_preorders(batch_id)
    reserved = [p for p in preorders if p["status"] == "reserved"]
    waitlist = [p for p in preorders if p["status"] == "waitlist"]
    cancelled = [p for p in preorders if p["status"] == "cancelled"]

    cap = _effective_preorder_cap(b, s)
    reserved_qty = sum(int(p["quantity"] or 0) for p in reserved)
    waitlist_qty = sum(int(p["quantity"] or 0) for p in waitlist)
    spots_left = max(0, cap - reserved_qty)
    dep_collected = round(sum(float(p["deposit_paid"] or 0.0) for p in reserved), 2)
    dep_expected = round(
        sum(int(p["quantity"] or 0) * float(p["deposit_per_bird"] or 0.0) for p in reserved), 2
    )
    dep_pending = round(max(0.0, dep_expected - dep_collected), 2)

    slug = b.get("picking_slug") or ""
    base = str(request.base_url).rstrip("/")
    reserve_url = f"{base}/reserve/{slug}" if slug else ""
    pick_url = f"{base}/pick/{slug}" if slug else ""
    pickup_cfg = get_farm_pickup_settings(user["id"])

    reserved_phones = ", ".join(dict.fromkeys(p["customer_phone"] for p in reserved if p.get("customer_phone")))
    reserved_emails = ", ".join(dict.fromkeys(p["customer_email"] for p in reserved if p.get("customer_email")))
    stage_info = _batch_stage_info(b, s, user["id"])

    return _render(
        request, "preorders.html", user, "batches",
        b=b, s=s,
        stage_info=stage_info,
        batch_stage="preorders",
        reserved=reserved,
        waitlist=waitlist,
        cancelled=cancelled,
        cap=cap,
        reserved_qty=reserved_qty,
        waitlist_qty=waitlist_qty,
        spots_left=spots_left,
        dep_collected=dep_collected,
        dep_expected=dep_expected,
        dep_pending=dep_pending,
        reserve_url=reserve_url,
        pick_url=pick_url,
        pickup_cfg=pickup_cfg,
        customers=list_customers(user["id"]),
        reserved_phones=reserved_phones,
        reserved_emails=reserved_emails,
        msg=msg,
    )


@app.post("/batches/{batch_id}/preorders/settings")
def batch_preorders_settings_submit(
    batch_id: int,
    preorder_enabled: str = Form(""),
    preorder_cap: str = Form(""),
    deposit_per_bird: str = Form("0"),
    preorder_notes: str = Form(""),
    pickup_date: str = Form(""),
    pickup_window: str = Form(""),
    pickup_location: str = Form(""),
    user: dict = Depends(current_user),
):
    _get_owned_batch(batch_id, user)
    cap_val = int(preorder_cap) if preorder_cap.strip().isdigit() and int(preorder_cap) > 0 else None
    try:
        dep_val = max(0.0, float(deposit_per_bird or 0.0))
    except ValueError:
        dep_val = 0.0
    update_batch_preorder_settings(
        user["id"],
        batch_id,
        preorder_enabled=bool(preorder_enabled),
        preorder_cap=cap_val,
        deposit_per_bird=dep_val,
        preorder_notes=preorder_notes,
        pickup_date=pickup_date,
        pickup_window=pickup_window,
        pickup_location=pickup_location,
    )
    return RedirectResponse(
        f"/batches/{batch_id}/preorders?msg=" + quote_plus("Saved pre-order & pickup settings."),
        status_code=303,
    )


@app.post("/batches/{batch_id}/preorders/add")
def batch_preorders_add_submit(
    batch_id: int,
    customer_id: str = Form(""),
    new_name: str = Form(""),
    new_contact: str = Form(""),
    quantity: int = Form(1),
    status: str = Form("reserved"),
    deposit_paid: str = Form("0"),
    notes: str = Form(""),
    user: dict = Depends(current_user),
):
    b = _get_owned_batch(batch_id, user)
    cid = int(customer_id) if customer_id.strip().isdigit() else None
    if not cid:
        if not new_name.strip():
            return RedirectResponse(
                f"/batches/{batch_id}/preorders?msg=" + quote_plus("Select a customer or enter a new customer name."),
                status_code=303,
            )
        if new_contact.strip():
            cust = find_or_create_customer(user["id"], new_name.strip(), new_contact.strip())
            cid = int(cust["id"])
        else:
            cid = save_customer(user["id"], name=new_name.strip())
    elif not get_customer(user["id"], cid):
        raise HTTPException(status_code=404, detail="Customer not found.")

    dpb = float(b.get("deposit_per_bird") or 0.0)
    try:
        dp = max(0.0, float(deposit_paid or 0.0))
    except ValueError:
        dp = 0.0
    dst = "paid" if dp > 0 and (dpb <= 0 or dp >= round(dpb * max(1, int(quantity)), 2)) else "unpaid"
    create_batch_preorder(
        user["id"], batch_id, cid,
        quantity=max(1, int(quantity)),
        status=status,
        deposit_per_bird=dpb,
        deposit_paid=dp,
        deposit_status=dst,
        notes=notes,
    )
    return RedirectResponse(
        f"/batches/{batch_id}/preorders?msg=" + quote_plus("Pre-order added."),
        status_code=303,
    )


@app.post("/batches/{batch_id}/preorders/{preorder_id}/mark-paid")
def batch_preorder_mark_paid(
    batch_id: int,
    preorder_id: int,
    amount: str = Form(""),
    user: dict = Depends(current_user),
):
    b = _get_owned_batch(batch_id, user)
    po = next((p for p in list_batch_preorders(batch_id) if int(p["id"]) == int(preorder_id)), None)
    if not po:
        raise HTTPException(status_code=404, detail="Pre-order not found.")
    dpb = float(po.get("deposit_per_bird") or b.get("deposit_per_bird") or 0.0)
    default_amt = round(dpb * int(po["quantity"] or 1), 2)
    try:
        paid_val = max(0.0, float(amount)) if amount.strip() else default_amt
    except ValueError:
        paid_val = default_amt
    update_batch_preorder(
        user["id"], batch_id, preorder_id,
        deposit_paid=paid_val, deposit_status="paid",
    )
    return RedirectResponse(
        f"/batches/{batch_id}/preorders?msg=" + quote_plus(f"Marked deposit paid (${paid_val:.2f})."),
        status_code=303,
    )


@app.post("/batches/{batch_id}/preorders/{preorder_id}/promote")
def batch_preorder_promote(batch_id: int, preorder_id: int, user: dict = Depends(current_user)):
    _get_owned_batch(batch_id, user)
    update_batch_preorder(user["id"], batch_id, preorder_id, status="reserved")
    return RedirectResponse(
        f"/batches/{batch_id}/preorders?msg=" + quote_plus("Promoted from waitlist to reserved!"),
        status_code=303,
    )


@app.post("/batches/{batch_id}/preorders/{preorder_id}/cancel")
def batch_preorder_cancel(batch_id: int, preorder_id: int, user: dict = Depends(current_user)):
    _get_owned_batch(batch_id, user)
    update_batch_preorder(user["id"], batch_id, preorder_id, status="cancelled")
    return RedirectResponse(
        f"/batches/{batch_id}/preorders?msg=" + quote_plus("Reservation cancelled."),
        status_code=303,
    )


@app.post("/batches/{batch_id}/preorders/{preorder_id}/delete")
def batch_preorder_delete_submit(batch_id: int, preorder_id: int, user: dict = Depends(current_user)):
    _get_owned_batch(batch_id, user)
    delete_batch_preorder(user["id"], batch_id, preorder_id)
    return RedirectResponse(
        f"/batches/{batch_id}/preorders?msg=" + quote_plus("Pre-order removed."),
        status_code=303,
    )


@app.get("/reserve/{slug}")
def public_reserve_page(request: Request, slug: str):
    batch = get_batch_by_picking_slug(slug)
    if not batch:
        raise HTTPException(status_code=404, detail="Reservation page not found.")
    enabled = bool(batch.get("preorder_enabled"))
    bag_weight = get_bag_weight(user_id=int(batch["user_id"]))
    s = compute_batch_schedule(batch, bag_weight=bag_weight)
    cap = _effective_preorder_cap(batch, s)
    preorders = list_batch_preorders(int(batch["id"]))
    reserved_qty = sum(int(p["quantity"] or 0) for p in preorders if p["status"] == "reserved")
    spots_left = max(0, cap - reserved_qty)
    pickup_cfg = get_farm_pickup_settings(int(batch["user_id"]))
    return templates.TemplateResponse(
        request, "reserve.html",
        {
            "request": request,
            "slug": slug,
            "batch_name": batch["name"],
            "species": batch["species"],
            "sale_price_per_lb": float(batch.get("sale_price_per_lb") or 0.0),
            "avg_dressed_weight": float(batch.get("avg_dressed_weight") or 0.0),
            "deposit_per_bird": float(batch.get("deposit_per_bird") or 0.0),
            "preorder_notes": batch.get("preorder_notes") or "",
            "pickup_date": batch.get("pickup_date") or str(s.get("proc_date") or ""),
            "pickup_window": batch.get("pickup_window") or "",
            "pickup_location": batch.get("pickup_location") or pickup_cfg.get("pickup_address") or "",
            "farm_name": pickup_cfg.get("farm_name") or "Homestead Planner",
            "venmo_handle": pickup_cfg.get("venmo_handle") or "",
            "paypal_handle": pickup_cfg.get("paypal_handle") or "",
            "preorder_enabled": enabled,
            "cap": cap,
            "reserved_qty": reserved_qty,
            "spots_left": spots_left,
            "is_waitlist_mode": spots_left <= 0,
        },
    )


@app.post("/api/reserve/{slug}")
async def api_public_reserve(slug: str, request: Request):
    batch = get_batch_by_picking_slug(slug)
    if not batch or not batch.get("preorder_enabled"):
        raise HTTPException(status_code=404, detail="Pre-orders are not currently open for this batch.")
    payload = await request.json()
    name = str(payload.get("name") or "").strip()
    contact = str(payload.get("contact") or "").strip()
    notes = str(payload.get("notes") or "").strip()
    try:
        qty = max(1, min(25, int(payload.get("quantity") or 1)))
    except (TypeError, ValueError):
        qty = 1
    try:
        cust = find_or_create_customer(int(batch["user_id"]), name, contact)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    bag_weight = get_bag_weight(user_id=int(batch["user_id"]))
    s = compute_batch_schedule(batch, bag_weight=bag_weight)
    cap = _effective_preorder_cap(batch, s)
    dpb = round(float(batch.get("deposit_per_bird") or 0.0), 2)
    po = create_public_preorder_atomic(
        int(batch["id"]), int(batch["user_id"]), int(cust["id"]),
        quantity=qty, cap=cap, deposit_per_bird=dpb, notes=notes,
    )
    deposit_total = round(dpb * int(po["quantity"]), 2)
    is_waitlist = po["status"] == "waitlist"
    click_url = f"{str(request.base_url).rstrip('/')}/batches/{batch['id']}/preorders"
    if is_waitlist:
        send_ntfy_async(
            int(batch["user_id"]),
            f"Waitlist signup - {batch['name']}",
            f"{cust['name']} joined the waitlist for {po['quantity']}x {batch['species']}",
            click_url=click_url,
            tags="hourglass",
        )
    else:
        dep_txt = f" (Deposit due: ${deposit_total:.2f})" if deposit_total > 0 else ""
        send_ntfy_async(
            int(batch["user_id"]),
            f"New pre-order - {batch['name']}",
            f"{cust['name']} reserved {po['quantity']}x {batch['species']}{dep_txt}",
            click_url=click_url,
            tags="tada,turkey",
        )
    pickup_cfg = get_farm_pickup_settings(int(batch["user_id"]))
    return {
        "ok": True,
        "preorder_id": int(po["id"]),
        "status": po["status"],
        "customer_name": cust["name"],
        "quantity": int(po["quantity"]),
        "deposit_per_bird": dpb,
        "deposit_total": deposit_total,
        "venmo_handle": pickup_cfg.get("venmo_handle") or "",
        "paypal_handle": pickup_cfg.get("paypal_handle") or "",
    }


# ---------------------------------------------------------------------------
# Pickup Day Checkout Board (/batches/{id}/pickup)
# ---------------------------------------------------------------------------
@app.get("/batches/{batch_id}/pickup")
def batch_pickup_page(
    request: Request, batch_id: int, msg: str = "", user: dict = Depends(current_user)
):
    b = _get_owned_batch(batch_id, user)
    tally = _harvest_tally_payload(b, user["id"])
    pickup_cfg = get_farm_pickup_settings(user["id"])

    by_cust: dict[int, dict] = {}
    for e in tally["entries"]:
        if not e["for_sale"] or not e["customer_id"]:
            continue
        cid = int(e["customer_id"])
        grp = by_cust.setdefault(
            cid,
            {
                "customer_id": cid,
                "name": e["customer_name"],
                "phone": e["customer_phone"],
                "email": e["customer_email"],
                "pref": e["customer_pref"],
                "birds": [],
                "pending_birds": [],
                "completed_birds": [],
                "total_lbs": 0.0,
                "pending_lbs": 0.0,
                "calc_total": 0.0,
                "effective_total": 0.0,
                "deposits_paid": 0.0,
                "pending_deposits_paid": 0.0,
                "balance_due": 0.0,
                "all_paid": True,
                "all_picked_up": True,
            },
        )
        due_amt = (
            0.0
            if e["payment_status"] == "paid"
            else round(max(0.0, float(e["effective_price"]) - float(e["deposit_paid"])), 2)
        )
        bird_row = {**e, "due_amt": due_amt}
        is_bird_done = e["payment_status"] == "paid" and e["pickup_status"] == "picked_up"
        grp["birds"].append(bird_row)
        grp["total_lbs"] = round(grp["total_lbs"] + float(e["dressed_lbs"]), 2)
        grp["calc_total"] = round(grp["calc_total"] + float(e["est_price"]), 2)
        grp["effective_total"] = round(grp["effective_total"] + float(e["effective_price"]), 2)
        grp["deposits_paid"] = round(grp["deposits_paid"] + float(e["deposit_paid"]), 2)
        if is_bird_done:
            grp["completed_birds"].append(bird_row)
        else:
            grp["pending_birds"].append(bird_row)
            grp["pending_lbs"] = round(grp["pending_lbs"] + float(e["dressed_lbs"]), 2)
            grp["pending_deposits_paid"] = round(grp["pending_deposits_paid"] + float(e["deposit_paid"]), 2)
        if e["payment_status"] != "paid":
            grp["all_paid"] = False
            grp["balance_due"] = round(grp["balance_due"] + due_amt, 2)
        if e["pickup_status"] != "picked_up":
            grp["all_picked_up"] = False

    pending_groups = []
    completed_groups = []
    for g in by_cust.values():
        g["birds"].sort(key=lambda x: x["num"])
        g["pending_birds"].sort(key=lambda x: x["num"])
        g["completed_birds"].sort(key=lambda x: x["num"])
        g["bird_nums_label"] = ", ".join(f"#{x['num']}" for x in g["birds"])
        g["pending_nums_label"] = ", ".join(f"#{x['num']}" for x in g["pending_birds"])
        g["completed_nums_label"] = ", ".join(f"#{x['num']}" for x in g["completed_birds"])
        if g["all_paid"] and g["all_picked_up"]:
            completed_groups.append(g)
        else:
            pending_groups.append(g)

    pending_groups.sort(key=lambda x: x["name"].lower())
    completed_groups.sort(key=lambda x: x["name"].lower())

    total_balance_due = round(sum(g["balance_due"] for g in pending_groups), 2)
    total_deposits = round(sum(g["deposits_paid"] for g in by_cust.values()), 2)
    total_collected = round(
        sum(g["effective_total"] for g in completed_groups)
        + sum(sum(b["effective_price"] for b in g["completed_birds"]) for g in pending_groups)
        + sum(g["pending_deposits_paid"] for g in pending_groups),
        2,
    )

    pickup_location = (b.get("pickup_location") or "").strip() or pickup_cfg.get("pickup_address") or ""
    pickup_instructions = pickup_cfg.get("pickup_instructions") or ""
    bag_weight = get_bag_weight(user_id=user["id"])
    s = compute_batch_schedule(b, bag_weight=bag_weight)
    stage_info = _batch_stage_info(b, s, user["id"], tally=tally)

    return _render(
        request, "pickup_day.html", user, "batches",
        b=b, s=s,
        stage_info=stage_info,
        batch_stage="pickup",
        tally=tally,
        pending_groups=pending_groups,
        completed_groups=completed_groups,
        total_balance_due=total_balance_due,
        total_deposits=total_deposits,
        total_collected=total_collected,
        pickup_cfg=pickup_cfg,
        pickup_location=pickup_location,
        pickup_instructions=pickup_instructions,
        msg=msg,
    )


@app.post("/batches/{batch_id}/pickup/{customer_id}/settle")
def batch_pickup_settle_submit(
    batch_id: int,
    customer_id: int,
    collected_today: str = Form(""),
    bird_ids: list[str] = Form(default=[]),
    user: dict = Depends(current_user),
):
    _get_owned_batch(batch_id, user)
    collected_val: float | None = None
    if collected_today.strip() != "":
        try:
            collected_val = max(0.0, float(collected_today))
        except ValueError:
            collected_val = None
    sel_ids = [int(x) for x in bird_ids if str(x).strip().isdigit()]
    cnt = settle_customer_batch_pickup(
        user["id"], batch_id, customer_id,
        collected_today=collected_val, mark_paid=True, mark_picked_up=True,
        bird_ids=sel_ids or None,
    )
    cust = get_customer(user["id"], customer_id)
    cname = cust["name"] if cust else "Customer"
    return RedirectResponse(
        f"/batches/{batch_id}/pickup?msg=" + quote_plus(f"Marked {cname} ({cnt} bird{'s' if cnt != 1 else ''}) paid & picked up!"),
        status_code=303,
    )


@app.post("/batches/{batch_id}/pickup/{customer_id}/reopen")
def batch_pickup_reopen_submit(
    batch_id: int,
    customer_id: int,
    user: dict = Depends(current_user),
):
    _get_owned_batch(batch_id, user)
    for h in list_batch_harvests(batch_id):
        if h.get("customer_id") and int(h["customer_id"]) == int(customer_id) and bool(1 if h.get("for_sale") is None else h.get("for_sale")):
            dep = float(h.get("deposit_paid") or 0.0)
            update_harvest_assignment(
                batch_id,
                int(h["id"]),
                customer_id=customer_id,
                for_sale=True,
                deposit_paid=dep,
                actual_sale_price=float(h["actual_sale_price"]) if h.get("actual_sale_price") is not None else None,
                payment_status="deposit" if dep > 0 else "unpaid",
                pickup_status="assigned",
            )
    return RedirectResponse(
        f"/batches/{batch_id}/pickup?msg=" + quote_plus("Moved back to pending pickups."),
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Eggs & Laying Flock + Iron Ledger Bridge (/eggs)
# ---------------------------------------------------------------------------
@app.get("/eggs")
def eggs_page(
    request: Request,
    species: str = "",
    tab: str = "daily",
    msg: str = "",
    user: dict = Depends(current_user),
):
    bag_weight = get_bag_weight(user_id=user["id"])
    dash = compute_egg_dashboard(user["id"], bag_weight=bag_weight, species=species or None)
    clean_tab = tab if tab in ("daily", "inventory", "flock") else "daily"
    return _render(
        request, "eggs.html", user, "eggs",
        dash=dash,
        active_tab=clean_tab,
        bag_weight=bag_weight,
        gdrive_connected=bool(get_google_drive_connection(user["id"])),
        msg=msg,
    )


@app.post("/eggs/log")
def eggs_log_submit(
    species: str = Form("chicken"),
    log_date: str = Form(""),
    total_collected: int = Form(...),
    cracked_unusable: int = Form(0),
    home_used: int = Form(0),
    notes: str = Form(""),
    next_url: str = Form("/eggs"),
    user: dict = Depends(current_user),
):
    sp = normalize_egg_species(species)
    sp_label = EGG_SPECIES_DEFAULTS[sp]["label"].lower()
    target = next_url if next_url.startswith("/") else f"/eggs?species={sp}"
    if target == "/eggs":
        target = f"/eggs?species={sp}"
    sep = "&" if "?" in target else "?"
    try:
        add_egg_log(
            user["id"],
            log_date=log_date or date.today().isoformat(),
            total_collected=total_collected,
            cracked_unusable=cracked_unusable,
            home_used=home_used,
            notes=notes,
            created_by=user.get("actor_id", user["id"]),
            species=sp,
        )
        msg = f"Logged {total_collected} {sp_label} egg{'s' if int(total_collected) != 1 else ''}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"{target}{sep}msg={quote_plus(msg)}", status_code=303)


@app.post("/eggs/log/{log_id}/delete")
def eggs_log_delete_submit(
    log_id: int,
    species: str = Form(""),
    user: dict = Depends(current_user),
):
    delete_egg_log(user["id"], log_id)
    sp_q = f"species={normalize_egg_species(species)}&" if species else ""
    return RedirectResponse(f"/eggs?{sp_q}msg=" + quote_plus("Removed egg log entry."), status_code=303)


@app.post("/eggs/pack")
def eggs_pack_submit(
    species: str = Form("chicken"),
    pack_date: str = Form(""),
    dozens_packed: int = Form(1),
    sync_to_ledger: str = Form(""),
    notes: str = Form(""),
    user: dict = Depends(current_user),
):
    sp = normalize_egg_species(species)
    res = pack_egg_cartons(
        user["id"],
        pack_date=pack_date or date.today().isoformat(),
        dozens_packed=max(1, int(dozens_packed)),
        notes=notes,
        sync_to_ledger=bool(sync_to_ledger),
        created_by=user.get("actor_id", user["id"]),
        species=sp,
    )
    cnt = res["dozens_packed"]
    unit_lbl = "dozen" if res["pack_size"] == 12 else res["pack_unit"]
    sync_txt = " & restocked Iron Ledger!" if res["synced_to_ledger"] else "."
    return RedirectResponse(
        f"/eggs?species={sp}&tab=inventory&msg="
        + quote_plus(f"Packed {cnt} {unit_lbl} ({res['eggs_used']} eggs){sync_txt}"),
        status_code=303,
    )


@app.post("/eggs/pack/{pack_id}/delete")
def eggs_pack_delete_submit(
    pack_id: int,
    species: str = Form(""),
    user: dict = Depends(current_user),
):
    deleted_sp = delete_egg_pack_log(user["id"], pack_id)
    sp = normalize_egg_species(species or deleted_sp or "chicken")
    return RedirectResponse(
        f"/eggs?species={sp}&tab=inventory&msg="
        + quote_plus("Removed carton pack log (and reversed linked Iron Ledger restock)."),
        status_code=303,
    )


@app.post("/eggs/flock")
def eggs_flock_submit(
    species: str = Form("chicken"),
    event_date: str = Form(""),
    direction: str = Form("add"),
    count: int = Form(1),
    reason: str = Form("purchase"),
    notes: str = Form(""),
    user: dict = Depends(current_user),
):
    sp = normalize_egg_species(species)
    meta = EGG_SPECIES_DEFAULTS[sp]
    qty = max(1, int(count))
    delta = -qty if direction == "remove" or reason in ("loss", "cull") else qty
    add_layer_flock_event(
        user["id"],
        event_date=event_date or date.today().isoformat(),
        delta=delta,
        reason=reason,
        notes=notes,
        species=sp,
    )
    verb = f"Added +{qty}" if delta > 0 else f"Logged {delta}"
    noun = meta["bird_singular"] if qty == 1 else meta["bird_label"]
    return RedirectResponse(
        f"/eggs?species={sp}&tab=flock&msg=" + quote_plus(f"{verb} {noun}."),
        status_code=303,
    )


@app.post("/eggs/flock/{event_id}/delete")
def eggs_flock_delete_submit(
    event_id: int,
    species: str = Form(""),
    user: dict = Depends(current_user),
):
    delete_layer_flock_event(user["id"], event_id)
    sp_q = f"species={normalize_egg_species(species)}&" if species else ""
    return RedirectResponse(
        f"/eggs?{sp_q}tab=flock&msg=" + quote_plus("Removed flock headcount entry."),
        status_code=303,
    )


@app.post("/eggs/feed")
async def eggs_feed_submit(
    species: str = Form(""),
    feed_date: str = Form(""),
    feed_type: str = Form("layer"),
    species_tags: list[str] = Form([]),
    bags: float = Form(1.0),
    feed_lbs: float = Form(50.0),
    cost_total: float = Form(0.0),
    notes: str = Form(""),
    receipt_url: str = Form(""),
    receipt_file: UploadFile | None = File(None),
    user: dict = Depends(current_user),
):
    fdate = feed_date.strip() or date.today().isoformat()
    r_url, drive_id, warn = await _resolve_receipt_attachment(
        user["id"], receipt_file, receipt_url, fdate, f"Layer Feed ({feed_type})"
    )
    add_layer_feed_log(
        user["id"],
        feed_date=fdate,
        feed_type=feed_type,
        bags=bags,
        feed_lbs=feed_lbs,
        cost_total=cost_total,
        notes=notes,
        receipt_url=r_url,
        drive_file_id=drive_id,
        species_tags=species_tags or "all",
    )
    msg = "Logged layer feed purchase."
    if warn:
        msg += f" (Receipt not uploaded: {warn})"
    sp_q = f"species={normalize_egg_species(species)}&" if species else ""
    return RedirectResponse(f"/eggs?{sp_q}tab=flock&msg=" + quote_plus(msg), status_code=303)


@app.post("/eggs/feed/{feed_id}/delete")
def eggs_feed_delete_submit(
    feed_id: int,
    species: str = Form(""),
    user: dict = Depends(current_user),
):
    delete_layer_feed_log(user["id"], feed_id)
    sp_q = f"species={normalize_egg_species(species)}&" if species else ""
    return RedirectResponse(
        f"/eggs?{sp_q}tab=flock&msg=" + quote_plus("Removed layer feed log."),
        status_code=303,
    )


@app.post("/eggs/settings")
def eggs_settings_submit(
    species: str = Form("chicken"),
    feed_lbs_per_hen_day: float = Form(0.25),
    scratch_lbs_per_day: float = Form(0.0),
    bag_price: float = Form(22.00),
    scratch_bag_price: float = Form(16.00),
    ledger_sku_id: str = Form(""),
    goose_pack_size: str = Form(""),
    user: dict = Depends(current_user),
):
    sp = normalize_egg_species(species)
    sku_id = int(ledger_sku_id) if ledger_sku_id.strip().isdigit() and int(ledger_sku_id) > 0 else None
    gp = int(goose_pack_size) if goose_pack_size.strip().isdigit() else None
    save_layer_settings(
        user["id"],
        feed_lbs_per_hen_day=feed_lbs_per_hen_day,
        scratch_lbs_per_day=scratch_lbs_per_day,
        bag_price=bag_price,
        scratch_bag_price=scratch_bag_price,
        ledger_sku_id=sku_id,
        species=sp,
        goose_pack_size=gp,
    )
    return RedirectResponse(
        f"/eggs?species={sp}&tab=inventory&msg="
        + quote_plus("Saved laying flock & Iron Ledger SKU settings."),
        status_code=303,
    )


@app.post("/batches/{batch_id}/graduate-layers")
def batch_graduate_layers_submit(
    batch_id: int,
    head_count: str = Form(""),
    user: dict = Depends(current_user),
):
    b = _get_owned_batch(batch_id, user)
    bag_weight = get_bag_weight(user_id=user["id"])
    s = compute_batch_schedule(b, bag_weight=bag_weight)
    default_head = max(1, int(s.get("on_farm_head") or s.get("surviving_quantity") or b.get("initial_quantity") or 1))
    hc = int(head_count) if head_count.strip().isdigit() and int(head_count) > 0 else default_head
    sp = normalize_egg_species(b.get("species"))
    bird_lbl = EGG_SPECIES_DEFAULTS[sp]["bird_label"]
    try:
        graduate_batch_to_layers(user["id"], batch_id, head_count=hc, archive_batch=True)
        return RedirectResponse(
            f"/eggs?species={sp}&msg="
            + quote_plus(f"Graduated {hc} {bird_lbl} from {b['name']} into your active laying flock!"),
            status_code=303,
        )
    except ValueError as exc:
        return RedirectResponse(
            f"/batches/{batch_id}?msg=" + quote_plus(str(exc)),
            status_code=303,
        )


@app.get("/batches/{batch_id}/harvest.csv")
def batch_harvest_csv(batch_id: int, user: dict = Depends(current_user)):
    b = _get_owned_batch(batch_id, user)
    tally = _harvest_tally_payload(b, user["id"])
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["#", "Date", "Head", "Dressed (lb)", "Lb + Oz", "Notes"])
    for e in reversed(tally["entries"]):
        writer.writerow([
            e["num"], e["harvest_date"], e["head_count"],
            f"{e['dressed_lbs']:.2f}", e["lb_oz"], e["notes"],
        ])
    writer.writerow([
        "TOTAL", "", tally["harvested_head"],
        f"{tally['total_lbs']:.2f}", _fmt_lb_oz(tally["total_lbs"]),
        f"Avg {tally['avg_lbs']:.2f} lb/bird",
    ])
    slug = "".join(ch if ch.isalnum() else "-" for ch in b["name"].lower()).strip("-") or f"batch-{batch_id}"
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{slug}-harvest.csv"'},
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
    lifecycle_type: str = Form("direct_arrival"),
    days_to_birth: str = Form(""),
    starter_weeks: str = Form(""),
    grower_weeks: str = Form(""),
    starter_feed_lbs: str = Form(""),
    grower_feed_lbs: str = Form(""),
    starter_bag_price: str = Form(""),
    grower_bag_price: str = Form(""),
    notes: str = Form(""),
    next_url: str = Form("/batches"),
    user: dict = Depends(current_user),
):
    if (user.get("role") or "owner") == "staff":
        raise HTTPException(status_code=403, detail="Staff role cannot create or edit batches.")
    bid = int(batch_id) if batch_id.strip() else None
    if bid is not None:
        _get_owned_batch(bid, user)
    payload: dict = {
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
    for k, raw, cast in (
        ("days_to_birth", days_to_birth, int),
        ("starter_weeks", starter_weeks, int),
        ("grower_weeks", grower_weeks, int),
        ("starter_feed_lbs", starter_feed_lbs, float),
        ("grower_feed_lbs", grower_feed_lbs, float),
        ("starter_bag_price", starter_bag_price, float),
        ("grower_bag_price", grower_bag_price, float),
    ):
        if raw.strip():
            payload[k] = cast(raw)
    save_batch(payload, batch_id=bid, user_id=user["id"])
    target = next_url if next_url.startswith("/") else "/batches"
    return RedirectResponse(f"{target}?msg=" + quote_plus(f"Saved {name.strip()}."), status_code=303)


@app.post("/batches/{batch_id}/toggle-active")
def batch_toggle_active(batch_id: int, user: dict = Depends(current_user)):
    b = _get_owned_batch(batch_id, user)
    set_batch_active(batch_id, not bool(b.get("active", 1)))
    return RedirectResponse("/batches?msg=" + quote_plus("Updated batch status."), status_code=303)


@app.post("/batches/{batch_id}/start-now")
def batch_start_now(
    batch_id: int,
    next_url: str = Form("/"),
    user: dict = Depends(current_user),
):
    b = _get_owned_batch(batch_id, user)
    old_start = date.fromisoformat(b["start_date"])
    old_end = date.fromisoformat(b.get("end_date") or b["start_date"])
    win_days = max(0, (old_end - old_start).days)
    today = date.today()
    updated = dict(b)
    updated["start_date"] = today.isoformat()
    updated["end_date"] = (today + timedelta(days=win_days)).isoformat()
    updated["active"] = 1
    save_batch(updated, batch_id=batch_id, user_id=user["id"])
    target = next_url if next_url.startswith("/") else "/"
    return RedirectResponse(f"{target}?msg=" + quote_plus(f"Started {b['name']} today."), status_code=303)


@app.post("/batches/{batch_id}/delete")
def batch_delete_submit(batch_id: int, user: dict = Depends(current_user)):
    if (user.get("role") or "owner") != "owner":
        raise HTTPException(status_code=403, detail="Only farm owners can delete batches.")
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


_GDRIVE_OAUTH_STATES: dict[str, tuple[int, float]] = {}


async def _resolve_receipt_attachment(
    user_id: int,
    receipt_file: UploadFile | None,
    receipt_url: str,
    receipt_date: str,
    label: str,
) -> tuple[str, str, str | None]:
    manual_url = (receipt_url or "").strip()
    if receipt_file and receipt_file.filename:
        raw = await receipt_file.read()
        if raw:
            try:
                file_id, view_url = upload_receipt_to_drive(
                    user_id,
                    receipt_file.filename,
                    receipt_file.content_type or "application/octet-stream",
                    raw,
                    receipt_date=receipt_date,
                    label=label,
                )
                return view_url, file_id, None
            except GoogleDriveError as exc:
                return manual_url, "", str(exc)
    return manual_url, "", None


@app.post("/batches/log-harvest")
async def batch_log_harvest(
    batch_id: int = Form(...),
    harvest_date: str = Form(...),
    head_count: int = Form(...),
    dressed_lbs: float = Form(...),
    processing_cost_total: float = Form(0.0),
    notes: str = Form(""),
    receipt_url: str = Form(""),
    receipt_file: UploadFile | None = File(None),
    next_url: str = Form("/"),
    user: dict = Depends(current_user),
):
    b = _get_owned_batch(batch_id, user)
    r_url, drive_id, warn = await _resolve_receipt_attachment(
        user["id"], receipt_file, receipt_url, harvest_date, f"{b['name']}-processing"
    )
    add_batch_harvest(
        batch_id,
        harvest_date,
        max(1, head_count),
        dressed_lbs,
        processing_cost_total,
        notes.strip(),
        receipt_url=r_url,
        drive_file_id=drive_id,
    )
    target = next_url if next_url.startswith("/") else "/"
    msg = f"Logged {head_count} harvested."
    if warn:
        msg += f" (Receipt not uploaded: {warn})"
    return RedirectResponse(f"{target}?msg=" + quote_plus(msg), status_code=303)


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
async def batch_log_feed(
    batch_id: int = Form(...),
    feed_date: str = Form(""),
    feed_type: str = Form("grower"),
    bags: float = Form(0.0),
    feed_lbs: float = Form(0.0),
    cost_total: float = Form(0.0),
    notes: str = Form(""),
    receipt_url: str = Form(""),
    receipt_file: UploadFile | None = File(None),
    next_url: str = Form("/"),
    user: dict = Depends(current_user),
):
    b = _get_owned_batch(batch_id, user)
    fdate = feed_date.strip() or date.today().isoformat()
    label = f"{b['name']}-{feed_type}"
    r_url, drive_id, warn = await _resolve_receipt_attachment(
        user["id"], receipt_file, receipt_url, fdate, label
    )
    add_batch_feed_log(
        batch_id, fdate, feed_type, bags, feed_lbs, cost_total, notes,
        receipt_url=r_url, drive_file_id=drive_id,
    )
    target = next_url if next_url.startswith("/") else "/"
    msg = "Logged feed purchase."
    if warn:
        msg += f" (Receipt not uploaded: {warn})"
    return RedirectResponse(f"{target}?msg=" + quote_plus(msg), status_code=303)


@app.post("/batches/{batch_id}/feed-logs/{log_id}/delete")
def batch_feed_log_delete(batch_id: int, log_id: int, user: dict = Depends(current_user)):
    _get_owned_batch(batch_id, user)
    delete_batch_feed_log(log_id)
    return RedirectResponse(f"/batches/{batch_id}?msg=" + quote_plus("Feed entry removed."), status_code=303)


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
        egg_dash=compute_egg_dashboard(user["id"], bag_weight=bag_wt),
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
def financials_page(
    request: Request,
    msg: str = "",
    include_planned: int = 1,
    user: dict = Depends(current_user),
):
    all_schedules = _schedules_for_user(user["id"], include_archived=False)
    planned_count = sum(1 for s in all_schedules if s["is_planned"])
    schedules = all_schedules if include_planned else [s for s in all_schedules if not s["is_planned"]]
    unified_rows = compute_unified_financials_table(schedules)
    roi_rows = compute_roi_summary_table(schedules)
    expenses = list_farm_expenses(user["id"])
    totals = {
        "est_rev": sum(s["estimated_revenue"] for s in schedules),
        "total_cost": sum(s["total_budget_cost"] for s in schedules),
        "est_profit": sum(s["estimated_profit"] for s in schedules),
        "cash_sales": sum(s["actual_sales_revenue"] for s in schedules),
        "other_expenses": sum(float(e["amount"]) for e in expenses),
    }
    chart_peak = max(
        (max(s["total_budget_cost"], s["estimated_revenue"]) for s in schedules),
        default=1,
    ) or 1
    return _render(
        request, "financials.html", user, "financials",
        schedules=schedules,
        include_planned=bool(include_planned),
        planned_count=planned_count,
        unified_rows=unified_rows,
        roi_rows=roi_rows,
        expenses=expenses,
        expense_categories=EXPENSE_CATEGORIES,
        gdrive_connected=bool(get_google_drive_connection(user["id"])),
        totals=totals,
        chart_peak=chart_peak,
        msg=msg,
    )


@app.post("/expenses/add")
async def expense_add_submit(
    expense_date: str = Form(""),
    vendor: str = Form(""),
    category: str = Form("Other"),
    amount: float = Form(0.0),
    batch_id: str = Form(""),
    notes: str = Form(""),
    receipt_url: str = Form(""),
    receipt_file: UploadFile | None = File(None),
    user: dict = Depends(current_user),
):
    edate = expense_date.strip() or date.today().isoformat()
    bid = int(batch_id) if batch_id.strip().isdigit() else None
    if bid is not None:
        _get_owned_batch(bid, user)
    r_url, drive_id, warn = await _resolve_receipt_attachment(
        user["id"], receipt_file, receipt_url, edate, vendor or category
    )
    add_farm_expense(
        user["id"],
        edate,
        vendor,
        category,
        amount,
        batch_id=bid,
        notes=notes,
        receipt_url=r_url,
        drive_file_id=drive_id,
    )
    msg = "Saved farm expense."
    if warn:
        msg += f" (Receipt not uploaded: {warn})"
    return RedirectResponse("/financials?msg=" + quote_plus(msg), status_code=303)


@app.post("/expenses/{expense_id}/delete")
def expense_delete_submit(expense_id: int, user: dict = Depends(current_user)):
    delete_farm_expense(expense_id, user["id"])
    return RedirectResponse("/financials?msg=" + quote_plus("Expense removed."), status_code=303)


@app.get("/financials/expenses.csv")
def financials_expenses_csv(user: dict = Depends(current_user)):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date", "Source", "Category", "Vendor / Batch", "Amount ($)", "Notes", "Receipt URL"])
    for exp in list_farm_expenses(user["id"]):
        vendor_label = exp["vendor"] or exp["batch_name"] or ""
        writer.writerow([
            exp["expense_date"],
            "Farm Expense",
            exp["category"],
            vendor_label,
            f"{float(exp['amount']):.2f}",
            exp["notes"] or "",
            exp["receipt_url"] or "",
        ])
    for b in list_batches(user_id=user["id"]):
        for fl in list_batch_feed_logs(b["id"]):
            writer.writerow([
                fl["feed_date"],
                "Batch Feed",
                "Feed",
                f"{b['name']} ({fl['feed_type']})",
                f"{float(fl['cost_total']):.2f}",
                fl["notes"] or "",
                fl.get("receipt_url") or "",
            ])
        for hv in list_batch_harvests(b["id"]):
            if float(hv.get("processing_cost_total") or 0) > 0:
                writer.writerow([
                    hv["harvest_date"],
                    "Harvest Processing",
                    "Processing",
                    b["name"],
                    f"{float(hv['processing_cost_total']):.2f}",
                    hv["notes"] or "",
                    hv.get("receipt_url") or "",
                ])
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="homestead-expenses-schedule-f.csv"'},
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
        layer_cfg=get_layer_settings(user["id"]),
        egg_species_order=EGG_SPECIES_ORDER,
        reg_open=is_public_registration_enabled(),
        users=list_users() if user.get("is_admin") else [],
        members=list_homestead_merchant_members(user["merchant_id"]),
        roles=HOMESTEAD_ROLES,
        gdrive_configured=is_gdrive_configured(),
        gdrive_conn=get_google_drive_connection(user["id"]),
        ntfy_cfg=get_ntfy_config(user["id"]),
        pickup_cfg=get_farm_pickup_settings(user["id"]),
        msg=msg,
    )


@app.post("/settings/egg-species")
def settings_egg_species_save(
    enabled_species: list[str] = Form([]),
    goose_pack_size: str = Form("4"),
    user: dict = Depends(current_user),
):
    if (user.get("role") or "owner") != "owner":
        raise HTTPException(status_code=403, detail="Only farm owners can update farm settings.")
    gp = 1 if str(goose_pack_size or "4").strip() == "1" else 4
    save_egg_species_settings(user["id"], enabled_species, goose_pack_size=gp)
    return RedirectResponse(
        "/settings?msg=" + quote_plus("Saved active laying species & egg carton defaults."),
        status_code=303,
    )


@app.post("/settings/members")
def settings_members_save(
    username: str = Form(...),
    display_name: str = Form(""),
    password: str = Form(""),
    role: str = Form("manager"),
    user: dict = Depends(current_user),
):
    if (user.get("role") or "owner") != "owner":
        raise HTTPException(status_code=403, detail="Only farm owners can manage members.")
    try:
        clean_user = username.strip()
        existing = get_user_by_username(clean_user)
        if existing:
            uid = int(existing["id"])
        else:
            if not password:
                raise ValueError("Password is required when creating a new family/member account.")
            created = create_user(
                clean_user,
                password,
                display_name=display_name.strip() or clean_user,
                is_admin=False,
                claim_unowned_data=False,
            )
            uid = int(created["id"])
        add_homestead_merchant_member(user["merchant_id"], uid, role=role)
        msg = f"Saved {display_name.strip() or clean_user} ({role}) on {user['merchant_name']}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse("/settings?msg=" + quote_plus(msg), status_code=303)


@app.post("/settings/members/remove")
def settings_members_remove(
    user_id: int = Form(...),
    user: dict = Depends(current_user),
):
    if (user.get("role") or "owner") != "owner":
        raise HTTPException(status_code=403, detail="Only farm owners can manage members.")
    try:
        remove_homestead_merchant_member(user["merchant_id"], user_id)
        msg = "Removed member from farm workspace."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse("/settings?msg=" + quote_plus(msg), status_code=303)


@app.post("/settings/merchant/rename")
def settings_merchant_rename(
    name: str = Form(...),
    user: dict = Depends(current_user),
):
    if (user.get("role") or "owner") != "owner":
        raise HTTPException(status_code=403, detail="Only farm owners can rename the farm workspace.")
    try:
        rename_homestead_merchant(user["merchant_id"], name)
        msg = f"Renamed farm workspace to {name.strip()}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse("/settings?msg=" + quote_plus(msg), status_code=303)


@app.post("/settings/merchant/create")
def settings_merchant_create(
    request: Request,
    name: str = Form(...),
    user: dict = Depends(current_user),
):
    if (user.get("role") or "owner") != "owner":
        raise HTTPException(status_code=403)
    try:
        mid = create_homestead_merchant(name, owner_user_id=user.get("actor_id", user["id"]))
        resp = RedirectResponse("/settings?msg=" + quote_plus(f"Created farm workspace {name.strip()}."), status_code=303)
        resp.set_cookie(
            MERCHANT_COOKIE,
            str(mid),
            httponly=True,
            samesite="lax",
            secure=_cookie_secure(request),
            max_age=30 * 86400,
        )
        return resp
    except ValueError as exc:
        return RedirectResponse("/settings?msg=" + quote_plus(f"Error: {exc}"), status_code=303)


@app.post("/settings/pickup-payments")
def settings_pickup_payments_save(
    farm_name: str = Form(""),
    pickup_address: str = Form(""),
    pickup_instructions: str = Form(""),
    venmo_handle: str = Form(""),
    paypal_handle: str = Form(""),
    user: dict = Depends(current_user),
):
    save_farm_pickup_settings(
        user["id"],
        farm_name=farm_name,
        pickup_address=pickup_address,
        pickup_instructions=pickup_instructions,
        venmo_handle=venmo_handle,
        paypal_handle=paypal_handle,
    )
    return RedirectResponse(
        "/settings?msg=" + quote_plus("Saved farm pickup & digital payment settings."),
        status_code=303,
    )


@app.post("/settings/ntfy")
def settings_ntfy_save(
    ntfy_url: str = Form("https://ntfy.sh"),
    ntfy_topic: str = Form(""),
    ntfy_token: str = Form(""),
    clear_token: str = Form(""),
    user: dict = Depends(current_user),
):
    save_ntfy_config(
        user["id"],
        url=ntfy_url,
        topic=ntfy_topic,
        token=ntfy_token,
        clear_token=bool(clear_token),
    )
    return RedirectResponse(
        "/settings?msg=" + quote_plus("Saved ntfy push notification settings."),
        status_code=303,
    )


@app.post("/settings/ntfy/test")
def settings_ntfy_test(request: Request, user: dict = Depends(current_user)):
    ok, detail = send_ntfy_notification(
        user["id"],
        "Homestead Planner - Test Notification",
        "ntfy push notifications are connected! You'll get alerts when customers claim birds.",
        click_url=f"{str(request.base_url).rstrip('/')}/batches",
        tags="white_check_mark,turkey",
    )
    msg = "Test notification sent to ntfy!" if ok else f"ntfy test failed: {detail}"
    return RedirectResponse("/settings?msg=" + quote_plus(msg), status_code=303)


@app.get("/settings/google-drive/connect")
def gdrive_connect(request: Request, user: dict = Depends(current_user)):
    if not is_gdrive_configured():
        return RedirectResponse(
            "/settings?msg=" + quote_plus("Google Drive OAuth is not configured on this server."),
            status_code=303,
        )
    now = time.time()
    for k, (_, exp) in list(_GDRIVE_OAUTH_STATES.items()):
        if exp < now:
            _GDRIVE_OAUTH_STATES.pop(k, None)
    state = secrets.token_urlsafe(24)
    _GDRIVE_OAUTH_STATES[state] = (user["id"], now + 600)
    redirect_uri = get_redirect_uri(str(request.base_url))
    return RedirectResponse(build_auth_url(state, redirect_uri), status_code=302)


@app.get("/settings/google-drive/callback")
def gdrive_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    user: dict = Depends(current_user),
):
    if error:
        return RedirectResponse("/settings?msg=" + quote_plus(f"Google Drive error: {error}"), status_code=303)
    entry = _GDRIVE_OAUTH_STATES.pop(state, None)
    if not entry or entry[0] != user["id"] or entry[1] < time.time():
        return RedirectResponse("/settings?msg=" + quote_plus("Google Drive authorization expired; try again."), status_code=303)
    redirect_uri = get_redirect_uri(str(request.base_url))
    try:
        tokens = exchange_code_for_tokens(code, redirect_uri)
    except GoogleDriveError as exc:
        return RedirectResponse("/settings?msg=" + quote_plus(str(exc)), status_code=303)
    secs = max(60, int(tokens.get("expires_in") or 3600))
    exp_iso = (datetime.now(timezone.utc) + timedelta(seconds=secs)).isoformat()
    save_google_drive_connection(
        user_id=user["id"],
        google_email=str(tokens.get("email") or ""),
        access_token_enc=encrypt_token(tokens.get("access_token") or ""),
        refresh_token_enc=encrypt_token(tokens.get("refresh_token") or ""),
        token_expires_at=exp_iso,
    )
    return RedirectResponse("/settings?msg=" + quote_plus("Google Drive connected for receipts."), status_code=303)


@app.post("/settings/google-drive/folder")
def gdrive_folder_save(folder_name: str = Form(...), user: dict = Depends(current_user)):
    update_google_drive_folder(user["id"], folder_name)
    return RedirectResponse("/settings?msg=" + quote_plus("Updated Google Drive receipt folder."), status_code=303)


@app.post("/settings/google-drive/disconnect")
def gdrive_disconnect(user: dict = Depends(current_user)):
    disconnect_google_drive(user["id"])
    return RedirectResponse("/settings?msg=" + quote_plus("Disconnected Google Drive."), status_code=303)


@app.post("/settings/species")
def settings_species_save(
    species: str = Form(...),
    lifecycle_type: str = Form("direct_arrival"),
    days_to_birth: int = Form(0),
    starter_weeks: int = Form(3),
    grower_weeks: int = Form(5),
    starter_feed_lbs: float = Form(3.0),
    grower_feed_lbs: float = Form(12.0),
    unit_cost: float = Form(3.5),
    expected_yield_lbs: float = Form(4.5),
    sale_price_per_lb: float = Form(5.5),
    processing_cost: float = Form(5.0),
    starter_bag_price: float = Form(24.0),
    grower_bag_price: float = Form(22.0),
    apply_active: str = Form(""),
    user: dict = Depends(current_user),
):
    save_species_preset(
        species,
        {
            "lifecycle_type": lifecycle_type,
            "days_to_birth": days_to_birth,
            "starter_weeks": starter_weeks,
            "grower_weeks": grower_weeks,
            "starter_feed_lbs": starter_feed_lbs,
            "grower_feed_lbs": grower_feed_lbs,
            "unit_cost": unit_cost,
            "expected_yield_lbs": expected_yield_lbs,
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
        update_user_password(user.get("actor_id", user["id"]), new_password)
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
