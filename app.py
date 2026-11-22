"""Homestead Planner — FastAPI web app."""

from contextlib import asynccontextmanager
import csv
from datetime import date, datetime, timedelta, timezone
import io
import os
from pathlib import Path
import secrets
import time
from urllib.parse import quote_plus

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from homestead.calendar_export import create_gcal_link, generate_ics_calendar
from homestead.database import (
    EXPENSE_CATEGORIES,
    acknowledge_milestone,
    add_batch_feed_log,
    add_batch_harvest,
    add_batch_loss,
    add_batch_sale,
    add_farm_expense,
    authenticate_user,
    count_users,
    create_user,
    create_user_session,
    delete_batch,
    delete_batch_feed_log,
    delete_batch_harvest,
    delete_batch_loss,
    delete_batch_sale,
    delete_farm_expense,
    delete_species_preset,
    delete_user,
    delete_user_session,
    disconnect_google_drive,
    export_backup_json,
    get_bag_weight,
    get_barrel_capacity,
    get_google_drive_connection,
    get_species_catalog,
    get_user_by_session_token,
    import_backup_json,
    init_db,
    is_public_registration_enabled,
    list_acknowledged_milestones,
    list_batch_feed_logs,
    list_batch_harvests,
    list_batch_losses,
    list_batch_sales,
    list_batches,
    list_farm_expenses,
    list_users,
    save_batch,
    save_google_drive_connection,
    save_species_preset,
    set_bag_weight,
    set_barrel_capacity,
    set_batch_active,
    set_batch_actual_feed,
    set_public_registration_enabled,
    update_google_drive_folder,
    update_user_password,
    verify_password,
)
from homestead.gdrive import (
    GoogleDriveError,
    build_auth_url,
    exchange_code_for_tokens,
    get_redirect_uri,
    is_gdrive_configured,
    upload_receipt_to_drive,
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
    return _render(
        request, "batch_detail.html", user, "batches",
        b=b, s=s, gcal_url=gcal_url, gcal_arrival_url=gcal_arrival_url,
        losses=list_batch_losses(batch_id),
        harvests=list_batch_harvests(batch_id),
        sales=list_batch_sales(batch_id),
        feed_logs=list_batch_feed_logs(batch_id),
        bag_weight=bag_weight,
        gdrive_connected=bool(get_google_drive_connection(user["id"])),
        species_catalog=get_species_catalog(user_id=user["id"]),
        msg=msg,
    )


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
    est_value = round(total_lbs * sale_price, 2)
    est_total_lbs = round(surviving * float(batch.get("avg_dressed_weight") or 0.0), 1)
    progress_pct = min(100, round(harvested_head / surviving * 100)) if surviving > 0 else 0
    entries = []
    for idx, h in enumerate(harvests):
        hc = max(1, int(h.get("head_count") or 1))
        dl = round(float(h.get("dressed_lbs") or 0), 2)
        entries.append({
            "id": int(h["id"]),
            "num": len(harvests) - idx,
            "harvest_date": h["harvest_date"],
            "head_count": hc,
            "dressed_lbs": dl,
            "lb_oz": _fmt_lb_oz(dl),
            "per_bird_lbs": round(dl / hc, 2),
            "notes": h.get("notes") or "",
        })
    return {
        "batch_id": int(batch["id"]),
        "batch_name": batch["name"],
        "harvested_head": harvested_head,
        "surviving_quantity": surviving,
        "remaining_head": remaining,
        "progress_pct": progress_pct,
        "total_lbs": total_lbs,
        "est_total_lbs": est_total_lbs,
        "avg_lbs": avg_lbs,
        "min_lbs": min_lbs,
        "max_lbs": max_lbs,
        "sale_price_per_lb": sale_price,
        "est_value": est_value,
        "entries": entries,
    }


@app.get("/batches/{batch_id}/harvest")
def batch_harvest_tally_page(
    request: Request, batch_id: int, user: dict = Depends(current_user)
):
    b = _get_owned_batch(batch_id, user)
    tally = _harvest_tally_payload(b, user["id"])
    return _render(
        request, "harvest_tally.html", user, "batches",
        b=b, tally=tally,
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
    add_batch_harvest(batch_id, hdate, head_count, round(dressed_lbs, 2), proc_cost, notes)
    return _harvest_tally_payload(b, user["id"])


@app.delete("/api/batches/{batch_id}/harvest/{harvest_id}")
def api_batch_harvest_delete(
    batch_id: int, harvest_id: int, user: dict = Depends(current_user)
):
    b = _get_owned_batch(batch_id, user)
    delete_batch_harvest(harvest_id)
    return _harvest_tally_payload(b, user["id"])


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
        reg_open=is_public_registration_enabled(),
        users=list_users() if user.get("is_admin") else [],
        gdrive_configured=is_gdrive_configured(),
        gdrive_conn=get_google_drive_connection(user["id"]),
        msg=msg,
    )


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
