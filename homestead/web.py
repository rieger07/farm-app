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
from homestead.config import (
    format_species_display,
    format_species_preset_label,
    get_species_terminology,
)
from homestead.db.batches import list_batch_harvests, list_batches
from homestead.db.core import (
    create_user_session,
    get_bag_weight,
    get_user_by_session_token,
)
from homestead.db.customers import batch_preorder_counts, list_customers
from homestead.db.shares import (
    effective_share_weight,
    list_batch_harvest_shares,
    normalize_sale_unit,
    share_slot_label,
)
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
    ("flocks", "Flocks & Herds", "/eggs"),
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
templates.env.globals["species_terms"] = get_species_terminology
templates.env.filters["money"] = _fmt_money
templates.env.filters["money0"] = _fmt_money0
templates.env.filters["species_name"] = format_species_display
templates.env.filters["species_plural"] = lambda sp: format_species_display(sp, plural=True)
templates.env.filters["species_preset"] = format_species_preset_label


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
    can_flocks = any(can_access_homestead_page(role, p) for p in ("eggs", "herds"))
    can_farm = any(can_access_homestead_page(role, p) for p in ("notes", "feed", "financials", "settings"))
    farm_path = "/financials" if can_access_homestead_page(role, "financials") else "/notes"
    visible_nav = []
    for key, label, path in NAV:
        if key == "flocks":
            if can_flocks:
                visible_nav.append((key, label, path))
        elif key == "farm":
            if can_farm:
                visible_nav.append((key, label, farm_path))
        elif can_access_homestead_page(role, key):
            visible_nav.append((key, label, path))
    if active in ("eggs", "herds"):
        nav_active = "flocks"
    elif active in ("notes", "feed", "financials", "settings"):
        nav_active = "farm"
    else:
        nav_active = active
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
        int(b["id"]),
        {"reserved_qty": 0, "waitlist_qty": 0, "reserved_equiv": 0.0, "waitlist_equiv": 0.0, "deposits_paid": 0.0},
    )
    items_for_pickup = tally["shares"] if tally.get("is_share_mode") else tally["entries"]
    by_cust_stat: dict[int, bool] = {}
    for e in items_for_pickup:
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
    sale_unit = normalize_sale_unit(batch.get("sale_unit"))
    is_share_mode = sale_unit == "shares"
    harvests = sorted(
        list_batch_harvests(batch["id"]),
        key=lambda h: int(h.get("id") or 0),
        reverse=True,
    )
    shares_by_harvest: dict[int, list[dict]] = {}
    if is_share_mode or harvests:
        for sh in list_batch_harvest_shares(batch["id"]):
            shares_by_harvest.setdefault(int(sh["harvest_id"]), []).append(sh)
    customers = list_customers(user_id)
    cust_by_id = {int(c["id"]): c for c in customers}

    harvested_head = sum(int(h.get("head_count") or 0) for h in harvests)
    surviving = int(s["surviving_quantity"])
    live_sold_head = int(s.get("live_sold_head") or 0)
    harvest_target = max(0, surviving - live_sold_head)
    remaining = max(0, harvest_target - harvested_head)
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
    est_total_lbs = round(harvest_target * float(batch.get("avg_dressed_weight") or 0.0), 1)
    progress_pct = min(100, round(harvested_head / harvest_target * 100)) if harvest_target > 0 else 0

    for_sale_lbs = 0.0
    for_sale_value = 0.0
    available_count = 0
    assigned_count = 0
    picked_up_count = 0
    not_for_sale_count = 0
    paid_unnotified_count = 0
    released_count = 0
    pending_weight_count = 0
    all_shares_flat: list[dict] = []
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

        carcass_num = len(harvests) - idx
        carcass_label = str(h.get("animal_label") or "").strip() or f"Carcass #{carcass_num}"
        weight_basis = str(h.get("weight_basis") or ("hanging" if is_share_mode else "dressed")).strip().lower()
        live_wt = round(float(h["live_weight_lbs"]), 2) if h.get("live_weight_lbs") is not None else None
        proc_cost = round(float(h.get("processing_cost_total") or 0.0), 2)

        entry_shares: list[dict] = []
        raw_entry_shares = shares_by_harvest.get(int(h["id"]), [])
        for sh in raw_entry_shares:
            sh_eff_lbs = effective_share_weight(sh, dl)
            sh_wt_pending = sh_eff_lbs <= 0.0
            sh_for_sale = bool(1 if sh.get("for_sale") is None else sh.get("for_sale"))
            sh_cid = int(sh["customer_id"]) if sh.get("customer_id") else None
            sh_cust = cust_by_id.get(sh_cid) if sh_cid else None
            sh_dep = round(float(sh.get("deposit_paid") or 0.0), 2)
            sh_pay = str(sh.get("payment_status") or "unpaid")
            sh_pick = str(sh.get("pickup_status") or ("assigned" if sh_cid else "unassigned"))
            sh_est = round(sh_eff_lbs * sale_price, 2) if sh_for_sale else 0.0
            sh_act = (
                round(float(sh["actual_sale_price"]), 2)
                if sh.get("actual_sale_price") is not None and sh_for_sale
                else None
            )
            sh_eff_price = sh_act if sh_act is not None else sh_est
            sh_tip = round(sh_eff_price - sh_est, 2) if sh_for_sale else 0.0
            sh_bal = round(max(0.0, sh_eff_price - sh_dep), 2) if sh_for_sale else 0.0
            sh_bnot_at = str(sh.get("butcher_notified_at") or "").strip()
            sh_dict = {
                "id": int(sh["id"]),
                "harvest_id": int(h["id"]),
                "batch_id": int(batch["id"]),
                "carcass_num": carcass_num,
                "carcass_label": carcass_label,
                "harvest_date": h["harvest_date"],
                "weight_basis": weight_basis,
                "share_slot": str(sh.get("share_slot") or "whole"),
                "slot_label": share_slot_label(sh.get("share_slot")),
                "share_fraction": float(sh.get("share_fraction") or 1.0),
                "share_weight_lbs": round(float(sh["share_weight_lbs"]), 2)
                if sh.get("share_weight_lbs") is not None
                else None,
                "effective_lbs": sh_eff_lbs,
                "weight_pending": sh_wt_pending,
                "for_sale": sh_for_sale,
                "customer_id": sh_cid,
                "customer_name": sh_cust["name"] if sh_cust else "",
                "customer_phone": sh_cust["phone"] if sh_cust else "",
                "customer_email": sh_cust["email"] if sh_cust else "",
                "customer_pref": sh_cust["preferred_contact"] if sh_cust else "sms",
                "deposit_paid": sh_dep,
                "est_price": sh_est,
                "actual_sale_price": sh_act,
                "effective_price": sh_eff_price,
                "tip_diff": sh_tip,
                "balance_due": sh_bal,
                "customer_butcher_fee": round(float(sh.get("customer_butcher_fee") or 0.0), 2),
                "payment_status": sh_pay,
                "payment_method": str(sh.get("payment_method") or ""),
                "paid_at": str(sh.get("paid_at") or ""),
                "butcher_notified_at": sh_bnot_at,
                "butcher_notified": bool(sh_bnot_at),
                "pickup_status": sh_pick,
                "cut_sheet_notes": str(sh.get("cut_sheet_notes") or ""),
                "claimed_at": str(sh.get("claimed_at") or ""),
            }
            entry_shares.append(sh_dict)
            all_shares_flat.append(sh_dict)

        if is_share_mode and entry_shares:
            if dl <= 0.0:
                pending_weight_count += 1
            for sh_dict in entry_shares:
                if sh_dict["for_sale"]:
                    for_sale_lbs += sh_dict["effective_lbs"]
                    for_sale_value += sh_dict["effective_price"]
                    if not sh_dict["customer_id"]:
                        available_count += 1
                    elif sh_dict["pickup_status"] == "picked_up":
                        picked_up_count += 1
                    else:
                        assigned_count += 1
                    if sh_dict["payment_status"] == "paid" and not sh_dict["butcher_notified"]:
                        paid_unnotified_count += 1
                    if sh_dict["butcher_notified"]:
                        released_count += 1
                else:
                    not_for_sale_count += 1
        elif for_sale:
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
            "num": carcass_num,
            "animal_label": carcass_label,
            "live_weight_lbs": live_wt,
            "weight_basis": weight_basis,
            "processing_cost_total": proc_cost,
            "shares": entry_shares,
            "split_mode": "halves" if len(entry_shares) >= 2 else "whole",
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
    terms = get_species_terminology(batch.get("species", ""), batch.get("lifecycle_type"))
    return {
        "batch_id": int(batch["id"]),
        "batch_name": batch["name"],
        "species_display": terms["display_singular"],
        "species_terms": terms,
        "sale_unit": sale_unit,
        "is_share_mode": is_share_mode,
        "customer_pays_butcher": bool(batch.get("customer_pays_butcher")),
        "butcher_name": str(batch.get("butcher_name") or ""),
        "butcher_phone": str(batch.get("butcher_phone") or ""),
        "butcher_email": str(batch.get("butcher_email") or ""),
        "harvested_head": harvested_head,
        "surviving_quantity": surviving,
        "live_sold_head": live_sold_head,
        "harvest_target_head": harvest_target,
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
        "paid_unnotified_count": paid_unnotified_count,
        "released_count": released_count,
        "pending_weight_count": pending_weight_count,
        "picking_enabled": bool(batch.get("picking_enabled")),
        "picking_preorder_only": bool(batch.get("picking_preorder_only")),
        "picking_slug": batch.get("picking_slug") or "",
        "max_birds_per_customer": batch.get("max_birds_per_customer"),
        "entries": entries,
        "shares": all_shares_flat,
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
