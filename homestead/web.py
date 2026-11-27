"""Shared FastAPI request context, templates, auth cookies, and cross-route helpers."""

import asyncio
from datetime import date
import json
import os
from pathlib import Path

from fastapi import Depends, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from homestead import gdrive
from homestead.db.batches import list_batch_harvests, list_batches
from homestead.db.core import (
    create_user_session,
    get_bag_weight,
    get_user_by_session_token,
)
from homestead.db.customers import batch_preorder_counts, list_customers
from homestead.db.workspaces import (
    can_access_homestead_page,
    resolve_homestead_workspace,
)
from homestead.gdrive import GoogleDriveError
from homestead.scheduling import compute_batch_schedule, get_batch_status_badge

BASE = Path(__file__).resolve().parent
COOKIE = "homestead_session"
MERCHANT_COOKIE = "homestead_merchant"
NAV = [
    ("radar", "Today", "/"),
    ("batches", "Batches", "/batches"),
    ("eggs", "Eggs", "/eggs"),
    ("customers", "Customers", "/customers"),
    ("farm", "Farm", "/financials"),
]

templates = Jinja2Templates(directory=str(BASE / "templates"))


def _asset(name: str) -> str:
    p = BASE / "static" / name
    return f"/static/{name}?v={int(p.stat().st_mtime)}" if p.exists() else f"/static/{name}"


def _fmt_money(v) -> str:
    n = round(float(v or 0), 2)
    return f"-${abs(n):,.2f}" if n < 0 else f"${n:,.2f}"


def _fmt_money0(v) -> str:
    n = round(float(v or 0))
    return f"-${abs(n):,.0f}" if n < 0 else f"${n:,.0f}"


templates.env.globals["asset"] = _asset
templates.env.filters["money"] = _fmt_money
templates.env.filters["money0"] = _fmt_money0


class _LoginRedirect(Exception):
    pass


def current_user(request: Request) -> dict:
    user = optional_user(request)
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
    user = get_user_by_session_token(tok) if tok else None
    if not user:
        return None
    raw_mid = (request.cookies.get(MERCHANT_COOKIE) or "").strip()
    pref_mid = int(raw_mid) if raw_mid.isdigit() else None
    return resolve_homestead_workspace(user, preferred_merchant_id=pref_mid)


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
            "payment_method": h.get("payment_method") or "",
            "paid_at": h.get("paid_at") or "",
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
                file_id, view_url = gdrive.upload_receipt_to_drive(
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
