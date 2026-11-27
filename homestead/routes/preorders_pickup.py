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
    update_harvest_assignment,
)
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


@router.post("/batches/{batch_id}/preorders/add")
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
        ntfy.send_ntfy_async(
            int(batch["user_id"]),
            f"Waitlist signup - {batch['name']}",
            f"{cust['name']} joined the waitlist for {po['quantity']}x {batch['species']}",
            click_url=click_url,
            tags="hourglass",
        )
    else:
        dep_txt = f" (Deposit due: ${deposit_total:.2f})" if deposit_total > 0 else ""
        ntfy.send_ntfy_async(
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


@router.get("/batches/{batch_id}/pickup")
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


@router.post("/batches/{batch_id}/pickup/{customer_id}/settle")
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


@router.post("/batches/{batch_id}/pickup/{customer_id}/reopen")
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
