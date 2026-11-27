"""Batch pre-orders, public /reserve/<slug> reservations, and Pickup Day settlement."""

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from homestead import ntfy
from homestead.db.batches import list_batch_harvests
from homestead.db.core import get_bag_weight
from homestead.db.customers import (
    create_batch_preorder,
    create_public_preorder_atomic,
    delete_batch_preorder,
    find_or_create_customer,
    get_batch_by_picking_slug,
    get_customer,
    get_farm_pickup_settings,
    list_batch_preorders,
    list_customers,
    save_customer,
    settle_customer_batch_pickup,
    update_batch_preorder,
    update_batch_preorder_settings,
    update_customer_batch_payment_method,
    update_harvest_assignment,
)
from homestead.db.shares import (
    mark_shares_butcher_notified,
    normalize_sale_unit,
    normalize_share_size,
    reopen_customer_shares,
    settle_customer_shares,
    update_customer_share_payment_method,
)
from shared.bridge import HOMESTEAD_PAYMENT_METHODS
from homestead.scheduling import compute_batch_schedule
from homestead.web import (
    _batch_stage_info,
    _get_owned_batch,
    _harvest_tally_payload,
    _render,
    current_user,
    templates,
)

router = APIRouter()


def _effective_preorder_cap(batch: dict, schedule: dict | None = None) -> int:
    raw = batch.get("preorder_cap")
    if raw is not None and int(raw) > 0:
        return int(raw)
    if schedule is not None:
        return max(1, int(schedule.get("surviving_quantity") or batch.get("initial_quantity") or 1))
    return max(1, int(batch.get("initial_quantity") or 1))


@router.get("/batches/{batch_id}/preorders")
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

    is_share_mode = normalize_sale_unit(b.get("sale_unit")) == "shares"
    cap = _effective_preorder_cap(b, s)
    reserved_qty = sum(int(p["quantity"] or 0) for p in reserved)
    waitlist_qty = sum(int(p["quantity"] or 0) for p in waitlist)
    reserved_equiv = round(
        sum(int(p["quantity"] or 0) * float(p.get("share_fraction") or 1.0) for p in reserved), 2
    )
    waitlist_equiv = round(
        sum(int(p["quantity"] or 0) * float(p.get("share_fraction") or 1.0) for p in waitlist), 2
    )
    spots_left = max(0, cap - reserved_qty)
    spots_left_equiv = round(max(0.0, float(cap) - reserved_equiv), 2)
    dep_collected = round(sum(float(p["deposit_paid"] or 0.0) for p in reserved), 2)
    dep_expected = round(
        sum(
            int(p["quantity"] or 0)
            * float(p.get("share_fraction") or 1.0)
            * float(p["deposit_per_bird"] or 0.0)
            for p in reserved
        ),
        2,
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
        is_share_mode=is_share_mode,
        reserved=reserved,
        waitlist=waitlist,
        cancelled=cancelled,
        cap=cap,
        reserved_qty=reserved_qty,
        waitlist_qty=waitlist_qty,
        reserved_equiv=reserved_equiv,
        waitlist_equiv=waitlist_equiv,
        spots_left=spots_left,
        spots_left_equiv=spots_left_equiv,
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


@router.post("/batches/{batch_id}/preorders/settings")
def batch_preorders_settings_submit(
    batch_id: int,
    preorder_enabled: str = Form(""),
    preorder_cap: str = Form(""),
    deposit_per_bird: str = Form("0"),
    preorder_notes: str = Form(""),
    pickup_date: str = Form(""),
    pickup_window: str = Form(""),
    pickup_location: str = Form(""),
    sale_unit: str = Form("whole"),
    customer_pays_butcher: str = Form(""),
    butcher_name: str = Form(""),
    butcher_phone: str = Form(""),
    butcher_email: str = Form(""),
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
        sale_unit=sale_unit,
        customer_pays_butcher=bool(customer_pays_butcher),
        butcher_name=butcher_name,
        butcher_phone=butcher_phone,
        butcher_email=butcher_email,
    )
    return RedirectResponse(
        f"/batches/{batch_id}/preorders?msg=" + quote_plus("Saved pre-order & pickup settings."),
        status_code=303,
    )


@router.post("/batches/{batch_id}/preorders/add")
def batch_preorders_add_submit(
    batch_id: int,
    customer_id: str = Form(""),
    new_name: str = Form(""),
    new_contact: str = Form(""),
    quantity: int = Form(1),
    share_size: str = Form("whole"),
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
    is_share_mode = normalize_sale_unit(b.get("sale_unit")) == "shares"
    frac, s_label = normalize_share_size(share_size) if is_share_mode else (1.0, "")
    try:
        dp = max(0.0, float(deposit_paid or 0.0))
    except ValueError:
        dp = 0.0
    exp_dep = round(dpb * max(1, int(quantity)) * frac, 2)
    dst = "paid" if dp > 0 and (dpb <= 0 or dp >= exp_dep) else "unpaid"
    create_batch_preorder(
        user["id"], batch_id, cid,
        quantity=max(1, int(quantity)),
        status=status,
        deposit_per_bird=dpb,
        deposit_paid=dp,
        deposit_status=dst,
        notes=notes,
        share_fraction=frac,
        share_label=s_label,
    )
    return RedirectResponse(
        f"/batches/{batch_id}/preorders?msg=" + quote_plus("Pre-order added."),
        status_code=303,
    )


@router.post("/batches/{batch_id}/preorders/{preorder_id}/mark-paid")
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
    frac = float(po.get("share_fraction") or 1.0)
    default_amt = round(dpb * int(po["quantity"] or 1) * frac, 2)
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


@router.post("/batches/{batch_id}/preorders/{preorder_id}/promote")
def batch_preorder_promote(batch_id: int, preorder_id: int, user: dict = Depends(current_user)):
    _get_owned_batch(batch_id, user)
    update_batch_preorder(user["id"], batch_id, preorder_id, status="reserved")
    return RedirectResponse(
        f"/batches/{batch_id}/preorders?msg=" + quote_plus("Promoted from waitlist to reserved!"),
        status_code=303,
    )


@router.post("/batches/{batch_id}/preorders/{preorder_id}/cancel")
def batch_preorder_cancel(batch_id: int, preorder_id: int, user: dict = Depends(current_user)):
    _get_owned_batch(batch_id, user)
    update_batch_preorder(user["id"], batch_id, preorder_id, status="cancelled")
    return RedirectResponse(
        f"/batches/{batch_id}/preorders?msg=" + quote_plus("Reservation cancelled."),
        status_code=303,
    )


@router.post("/batches/{batch_id}/preorders/{preorder_id}/delete")
def batch_preorder_delete_submit(batch_id: int, preorder_id: int, user: dict = Depends(current_user)):
    _get_owned_batch(batch_id, user)
    delete_batch_preorder(user["id"], batch_id, preorder_id)
    return RedirectResponse(
        f"/batches/{batch_id}/preorders?msg=" + quote_plus("Pre-order removed."),
        status_code=303,
    )


@router.get("/reserve/{slug}")
def public_reserve_page(request: Request, slug: str):
    batch = get_batch_by_picking_slug(slug)
    if not batch:
        raise HTTPException(status_code=404, detail="Reservation page not found.")
    enabled = bool(batch.get("preorder_enabled"))
    bag_weight = get_bag_weight(user_id=int(batch["user_id"]))
    s = compute_batch_schedule(batch, bag_weight=bag_weight)
    cap = _effective_preorder_cap(batch, s)
    preorders = list_batch_preorders(int(batch["id"]))
    sale_unit = normalize_sale_unit(batch.get("sale_unit"))
    is_share_mode = sale_unit == "shares"
    reserved_qty = sum(int(p["quantity"] or 0) for p in preorders if p["status"] == "reserved")
    reserved_equiv = round(
        sum(int(p["quantity"] or 0) * float(p.get("share_fraction") or 1.0) for p in preorders if p["status"] == "reserved"),
        2,
    )
    spots_left = max(0, cap - reserved_qty)
    spots_left_equiv = round(max(0.0, float(cap) - reserved_equiv), 2)
    is_waitlist = spots_left_equiv <= 0 if is_share_mode else spots_left <= 0
    pickup_cfg = get_farm_pickup_settings(int(batch["user_id"]))
    return templates.TemplateResponse(
        request, "reserve.html",
        {
            "request": request,
            "slug": slug,
            "batch_name": batch["name"],
            "species": batch["species"],
            "sale_unit": sale_unit,
            "is_share_mode": is_share_mode,
            "customer_pays_butcher": bool(batch.get("customer_pays_butcher")),
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
            "reserved_equiv": reserved_equiv,
            "spots_left": spots_left,
            "spots_left_equiv": spots_left_equiv,
            "is_waitlist_mode": is_waitlist,
        },
    )


@router.post("/api/reserve/{slug}")
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

    is_share_mode = normalize_sale_unit(batch.get("sale_unit")) == "shares"
    if is_share_mode:
        frac, s_label = normalize_share_size(payload.get("share_size") or "half")
    else:
        frac, s_label = 1.0, ""

    bag_weight = get_bag_weight(user_id=int(batch["user_id"]))
    s = compute_batch_schedule(batch, bag_weight=bag_weight)
    cap = _effective_preorder_cap(batch, s)
    dpb = round(float(batch.get("deposit_per_bird") or 0.0), 2)
    po = create_public_preorder_atomic(
        int(batch["id"]), int(batch["user_id"]), int(cust["id"]),
        quantity=qty, cap=cap, deposit_per_bird=dpb, notes=notes,
        share_fraction=frac, share_label=s_label,
    )
    deposit_total = round(dpb * int(po["quantity"]) * frac, 2)
    is_waitlist = po["status"] == "waitlist"
    item_desc = f"{po['quantity']}x {s_label} {batch['species']}" if s_label else f"{po['quantity']}x {batch['species']}"
    click_url = f"{str(request.base_url).rstrip('/')}/batches/{batch['id']}/preorders"
    if is_waitlist:
        ntfy.send_ntfy_async(
            int(batch["user_id"]),
            f"Waitlist signup - {batch['name']}",
            f"{cust['name']} joined the waitlist for {item_desc}",
            click_url=click_url,
            tags="hourglass",
        )
    else:
        dep_txt = f" (Deposit due: ${deposit_total:.2f})" if deposit_total > 0 else ""
        ntfy.send_ntfy_async(
            int(batch["user_id"]),
            f"New pre-order - {batch['name']}",
            f"{cust['name']} reserved {item_desc}{dep_txt}",
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
        "share_fraction": frac,
        "share_label": s_label,
        "deposit_per_bird": dpb,
        "deposit_total": deposit_total,
        "venmo_handle": pickup_cfg.get("venmo_handle") or "",
        "paypal_handle": pickup_cfg.get("paypal_handle") or "",
    }


# ---------------------------------------------------------------------------


def _build_share_pickup_groups(b: dict, tally: dict) -> dict:
    """Group customer-assigned for-sale shares into pending/completed pickup & butcher release buckets."""
    by_cust: dict[int, dict] = {}
    for sh in tally.get("shares") or []:
        if not sh["for_sale"] or not sh["customer_id"]:
            continue
        cid = int(sh["customer_id"])
        grp = by_cust.setdefault(
            cid,
            {
                "customer_id": cid,
                "name": sh["customer_name"],
                "phone": sh["customer_phone"],
                "email": sh["customer_email"],
                "pref": sh["customer_pref"],
                "shares": [],
                "pending_shares": [],
                "completed_shares": [],
                "total_lbs": 0.0,
                "pending_lbs": 0.0,
                "calc_total": 0.0,
                "effective_total": 0.0,
                "deposits_paid": 0.0,
                "pending_deposits_paid": 0.0,
                "balance_due": 0.0,
                "customer_butcher_fees_total": 0.0,
                "pending_weight_count": 0,
                "all_paid": True,
                "all_butcher_notified": True,
                "all_picked_up": True,
            },
        )
        due_amt = (
            0.0
            if sh["payment_status"] == "paid"
            else round(max(0.0, float(sh["effective_price"]) - float(sh["deposit_paid"])), 2)
        )
        sh_row = {**sh, "due_amt": due_amt}
        is_done = sh["payment_status"] == "paid" and sh["pickup_status"] == "picked_up"
        grp["shares"].append(sh_row)
        grp["total_lbs"] = round(grp["total_lbs"] + float(sh["effective_lbs"]), 2)
        grp["calc_total"] = round(grp["calc_total"] + float(sh["est_price"]), 2)
        grp["effective_total"] = round(grp["effective_total"] + float(sh["effective_price"]), 2)
        grp["deposits_paid"] = round(grp["deposits_paid"] + float(sh["deposit_paid"]), 2)
        grp["customer_butcher_fees_total"] = round(
            grp["customer_butcher_fees_total"] + float(sh.get("customer_butcher_fee") or 0.0), 2
        )
        if is_done:
            grp["completed_shares"].append(sh_row)
        else:
            grp["pending_shares"].append(sh_row)
            grp["pending_lbs"] = round(grp["pending_lbs"] + float(sh["effective_lbs"]), 2)
            grp["pending_deposits_paid"] = round(grp["pending_deposits_paid"] + float(sh["deposit_paid"]), 2)
            if sh.get("weight_pending"):
                grp["pending_weight_count"] += 1
        if sh["payment_status"] != "paid":
            grp["all_paid"] = False
            grp["balance_due"] = round(grp["balance_due"] + due_amt, 2)
        if not sh.get("butcher_notified"):
            grp["all_butcher_notified"] = False
        if sh["pickup_status"] != "picked_up":
            grp["all_picked_up"] = False

    pending_groups: list[dict] = []
    completed_groups: list[dict] = []
    for g in by_cust.values():
        g["shares"].sort(key=lambda x: (x["carcass_num"], x["share_slot"]))
        g["pending_shares"].sort(key=lambda x: (x["carcass_num"], x["share_slot"]))
        g["completed_shares"].sort(key=lambda x: (x["carcass_num"], x["share_slot"]))
        g["share_labels"] = ", ".join(f"{s['carcass_label']} ({s['slot_label']})" for s in g["shares"])
        g["pending_share_labels"] = ", ".join(f"{s['carcass_label']} ({s['slot_label']})" for s in g["pending_shares"])
        g["ready_to_notify_butcher"] = g["all_paid"] and not g["all_butcher_notified"]
        paid_pms = {
            str(x.get("payment_method") or "").strip().lower()
            for x in g["shares"]
            if x.get("payment_status") == "paid" or x.get("pickup_status") == "picked_up"
        }
        g["payment_method"] = (
            next(iter(paid_pms))
            if len(paid_pms) == 1
            else ("mixed" if len(paid_pms) > 1 else "")
        )
        if g["all_paid"] and g["all_picked_up"]:
            completed_groups.append(g)
        else:
            pending_groups.append(g)

    pending_groups.sort(key=lambda x: x["name"].lower())
    completed_groups.sort(key=lambda x: x["name"].lower())

    awaiting_payment_count = sum(1 for g in pending_groups if not g["all_paid"])
    ready_for_butcher_count = sum(1 for g in pending_groups if g["ready_to_notify_butcher"])
    released_count = sum(1 for g in pending_groups if g["all_paid"] and g["all_butcher_notified"])

    bname = (b.get("butcher_name") or "").strip() or "Butcher"
    ready_lines = []
    for g in pending_groups:
        if g["ready_to_notify_butcher"]:
            contact_bit = f" ({g['phone'] or g['email']})" if (g["phone"] or g["email"]) else ""
            ready_lines.append(f"- {g['name']}{contact_bit}: {g['pending_share_labels']} — Paid farm in full")
    batch_butcher_release_text = (
        f"Hi {bname} — the following customers for {b['name']} have paid the farm in full and are cleared to pick up their meat / pay processing directly:\n"
        + "\n".join(ready_lines)
        if ready_lines
        else ""
    )

    total_balance_due = round(sum(g["balance_due"] for g in pending_groups), 2)
    total_deposits = round(sum(g["deposits_paid"] for g in by_cust.values()), 2)
    total_collected = round(
        sum(
            float(sh["effective_price"])
            if sh["payment_status"] == "paid"
            else float(sh["deposit_paid"])
            for g in by_cust.values()
            for sh in g["shares"]
        ),
        2,
    )
    return {
        "pending_groups": pending_groups,
        "completed_groups": completed_groups,
        "total_balance_due": total_balance_due,
        "total_deposits": total_deposits,
        "total_collected": total_collected,
        "awaiting_payment_count": awaiting_payment_count,
        "ready_for_butcher_count": ready_for_butcher_count,
        "released_count": released_count,
        "batch_butcher_release_text": batch_butcher_release_text,
    }


@router.get("/batches/{batch_id}/pickup")
def batch_pickup_page(
    request: Request, batch_id: int, msg: str = "", user: dict = Depends(current_user)
):
    b = _get_owned_batch(batch_id, user)
    tally = _harvest_tally_payload(b, user["id"])
    pickup_cfg = get_farm_pickup_settings(user["id"])

    if tally.get("is_share_mode"):
        share_ctx = _build_share_pickup_groups(b, tally)
        pending_groups = share_ctx["pending_groups"]
        completed_groups = share_ctx["completed_groups"]
        total_balance_due = share_ctx["total_balance_due"]
        total_deposits = share_ctx["total_deposits"]
        total_collected = share_ctx["total_collected"]
        awaiting_payment_count = share_ctx["awaiting_payment_count"]
        ready_for_butcher_count = share_ctx["ready_for_butcher_count"]
        released_count = share_ctx["released_count"]
        batch_butcher_release_text = share_ctx["batch_butcher_release_text"]
    else:
        awaiting_payment_count = 0
        ready_for_butcher_count = 0
        released_count = 0
        batch_butcher_release_text = ""
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
            comp_pms = {str(x.get("payment_method") or "").strip().lower() for x in g["completed_birds"]}
            if len(comp_pms) == 1:
                g["payment_method"] = next(iter(comp_pms))
            elif len(comp_pms) > 1:
                g["payment_method"] = "mixed"
            else:
                g["payment_method"] = ""
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
        is_share_mode=tally.get("is_share_mode"),
        pending_groups=pending_groups,
        completed_groups=completed_groups,
        total_balance_due=total_balance_due,
        total_deposits=total_deposits,
        total_collected=total_collected,
        awaiting_payment_count=awaiting_payment_count,
        ready_for_butcher_count=ready_for_butcher_count,
        released_count=released_count,
        batch_butcher_release_text=batch_butcher_release_text,
        pickup_cfg=pickup_cfg,
        pickup_location=pickup_location,
        pickup_instructions=pickup_instructions,
        payment_methods=HOMESTEAD_PAYMENT_METHODS,
        msg=msg,
    )


@router.post("/batches/{batch_id}/pickup/{customer_id}/settle-shares")
def batch_pickup_settle_shares_submit(
    batch_id: int,
    customer_id: int,
    collected_today: str = Form(""),
    payment_method: str = Form("cash"),
    action: str = Form("pay_and_release"),
    share_ids: list[str] = Form(default=[]),
    user: dict = Depends(current_user),
):
    _get_owned_batch(batch_id, user)
    collected_val: float | None = None
    if collected_today.strip() != "":
        try:
            collected_val = max(0.0, float(collected_today))
        except ValueError:
            collected_val = None
    sel_ids = [int(x) for x in share_ids if str(x).strip().isdigit()]
    clean_action = action.strip().lower()
    mark_butcher = clean_action in ("pay_and_release", "mark_picked_up")
    mark_picked = clean_action == "mark_picked_up"
    try:
        cnt = settle_customer_shares(
            user["id"],
            batch_id,
            customer_id,
            collected_today=collected_val,
            mark_paid=True,
            mark_butcher_notified=mark_butcher,
            mark_picked_up=mark_picked,
            share_ids=sel_ids or None,
            payment_method=None if mark_picked and collected_val is None else payment_method,
        )
    except ValueError as exc:
        return RedirectResponse(
            f"/batches/{batch_id}/pickup?msg=" + quote_plus(f"Error: {exc}"),
            status_code=303,
        )
    cust = get_customer(user["id"], customer_id)
    cname = cust["name"] if cust else "Customer"
    if mark_picked:
        msg_txt = f"Marked {cname} ({cnt} share{'s' if cnt != 1 else ''}) picked up!"
    elif mark_butcher:
        msg_txt = f"Marked {cname} ({cnt} share{'s' if cnt != 1 else ''}) paid ({payment_method}) & butcher released!"
    else:
        msg_txt = f"Marked {cname} ({cnt} share{'s' if cnt != 1 else ''}) paid to farm ({payment_method})."
    return RedirectResponse(
        f"/batches/{batch_id}/pickup?msg=" + quote_plus(msg_txt),
        status_code=303,
    )


@router.post("/batches/{batch_id}/pickup/{customer_id}/butcher-release")
def batch_pickup_butcher_release_submit(
    batch_id: int,
    customer_id: int,
    notified: str = Form("1"),
    user: dict = Depends(current_user),
):
    _get_owned_batch(batch_id, user)
    want_notified = notified.strip() != "0"
    cnt = mark_shares_butcher_notified(
        user["id"], batch_id, customer_id=customer_id, notified=want_notified
    )
    cust = get_customer(user["id"], customer_id)
    cname = cust["name"] if cust else "Customer"
    msg_txt = (
        f"Marked butcher released for {cname} ({cnt} share{'s' if cnt != 1 else ''})."
        if want_notified
        else f"Cleared butcher release flag for {cname}."
    )
    return RedirectResponse(
        f"/batches/{batch_id}/pickup?msg=" + quote_plus(msg_txt),
        status_code=303,
    )


@router.post("/batches/{batch_id}/pickup/butcher-release-all")
def batch_pickup_butcher_release_all_submit(
    batch_id: int,
    user: dict = Depends(current_user),
):
    _get_owned_batch(batch_id, user)
    cnt = mark_shares_butcher_notified(
        user["id"], batch_id, notified=True, only_paid=True
    )
    msg_txt = (
        f"Marked butcher released for {cnt} paid share{'s' if cnt != 1 else ''}."
        if cnt
        else "No paid shares awaiting butcher release."
    )
    return RedirectResponse(
        f"/batches/{batch_id}/pickup?msg=" + quote_plus(msg_txt),
        status_code=303,
    )


@router.post("/batches/{batch_id}/pickup/{customer_id}/settle")
def batch_pickup_settle_submit(
    batch_id: int,
    customer_id: int,
    collected_today: str = Form(""),
    payment_method: str = Form("cash"),
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
    try:
        cnt = settle_customer_batch_pickup(
            user["id"], batch_id, customer_id,
            collected_today=collected_val, mark_paid=True, mark_picked_up=True,
            bird_ids=sel_ids or None, payment_method=payment_method,
        )
    except ValueError as exc:
        return RedirectResponse(
            f"/batches/{batch_id}/pickup?msg=" + quote_plus(f"Error: {exc}"),
            status_code=303,
        )
    cust = get_customer(user["id"], customer_id)
    cname = cust["name"] if cust else "Customer"
    return RedirectResponse(
        f"/batches/{batch_id}/pickup?msg=" + quote_plus(f"Marked {cname} ({cnt} bird{'s' if cnt != 1 else ''}) paid & picked up ({payment_method})!"),
        status_code=303,
    )


@router.post("/batches/{batch_id}/pickup/{customer_id}/payment-method")
def batch_pickup_payment_method_submit(
    batch_id: int,
    customer_id: int,
    payment_method: str = Form(""),
    user: dict = Depends(current_user),
):
    b = _get_owned_batch(batch_id, user)
    try:
        if normalize_sale_unit(b.get("sale_unit")) == "shares":
            cnt = update_customer_share_payment_method(user["id"], batch_id, customer_id, payment_method)
            unit_lbl = "share"
        else:
            cnt = update_customer_batch_payment_method(user["id"], batch_id, customer_id, payment_method)
            unit_lbl = "bird"
        lbl = payment_method.strip().capitalize() if payment_method.strip() else "Untagged"
        msg = f"Updated payment method to {lbl} ({cnt} {unit_lbl}{'s' if cnt != 1 else ''})."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(
        f"/batches/{batch_id}/pickup?msg=" + quote_plus(msg),
        status_code=303,
    )


@router.post("/batches/{batch_id}/pickup/{customer_id}/reopen")
def batch_pickup_reopen_submit(
    batch_id: int,
    customer_id: int,
    user: dict = Depends(current_user),
):
    b = _get_owned_batch(batch_id, user)
    if normalize_sale_unit(b.get("sale_unit")) == "shares":
        reopen_customer_shares(user["id"], batch_id, customer_id)
    else:
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
