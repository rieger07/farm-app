"""Harvest tally API, customer bird assignment, and public live /pick/<slug> SSE claiming."""

import asyncio
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from homestead import ntfy
from homestead.db.batches import (
    add_batch_harvest,
    delete_batch_harvest,
    list_batch_harvests,
)
from homestead.db.core import get_bag_weight
from homestead.db.customers import (
    claim_bird_atomically,
    find_or_create_customer,
    get_batch_by_picking_slug,
    get_customer,
    get_customer_batch_preorder_summary,
    list_customers,
    update_batch_picking_settings,
    update_harvest_assignment,
)
from homestead.scheduling import compute_batch_schedule
from homestead.web import (
    _PICK_SUBSCRIBERS,
    _batch_stage_info,
    _broadcast_pick_event,
    _fmt_lb_oz,
    _get_owned_batch,
    _harvest_tally_payload,
    _render,
    current_user,
    templates,
)

router = APIRouter()


@router.get("/batches/{batch_id}/harvest")
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


@router.post("/api/batches/{batch_id}/harvest")
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


@router.post("/api/batches/{batch_id}/harvest/{harvest_id}/assign")
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


@router.delete("/api/batches/{batch_id}/harvest/{harvest_id}")
def api_batch_harvest_delete(
    batch_id: int, harvest_id: int, user: dict = Depends(current_user)
):
    b = _get_owned_batch(batch_id, user)
    delete_batch_harvest(harvest_id)
    _broadcast_pick_event(
        b.get("picking_slug"), {"bird_id": int(harvest_id), "status": "hidden"}
    )
    return _harvest_tally_payload(b, user["id"])


@router.post("/api/batches/{batch_id}/picking")
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


@router.get("/pick/{slug}")
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


@router.post("/api/pick/{slug}/identify")
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


@router.post("/api/pick/{slug}/claim/{bird_id}")
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
        ntfy.send_ntfy_async(
            int(batch["user_id"]),
            f"Bird claimed - {batch['name']}",
            f"{buyer_name} claimed Bird #{claimed_info['num']} "
            f"({claimed_info['dressed_lbs']:.2f} lb / {claimed_info['lb_oz']} - "
            f"${claimed_info['est_price']:.2f}){dep_note}",
            click_url=click_url,
        )
    return {"ok": True, "bird": claimed_info}


@router.get("/api/pick/{slug}/events")
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
