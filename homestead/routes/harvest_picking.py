"""Harvest tally API, customer bird assignment, and public live /pick/<slug> SSE claiming."""

import asyncio
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from homestead import ntfy
from homestead.config import get_species_terminology
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
from homestead.db.shares import (
    claim_share_atomically,
    create_harvest_with_shares,
    effective_share_weight,
    list_batch_harvest_shares,
    normalize_sale_unit,
    set_carcass_split_mode,
    share_slot_label,
    update_batch_share_settings,
    update_carcass_details,
    update_share_assignment,
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
    dressed_lbs = float(payload.get("dressed_lbs") or 0)
    notes = str(payload.get("notes") or "").strip()
    proc_cost = float(payload.get("processing_cost_total") or 0.0)
    for_sale = bool(payload.get("for_sale", True))
    is_share_mode = normalize_sale_unit(b.get("sale_unit")) == "shares"
    if is_share_mode:
        if dressed_lbs < 0:
            raise HTTPException(status_code=400, detail="Weight cannot be negative.")
        raw_live = payload.get("live_weight_lbs")
        live_wt = float(raw_live) if raw_live not in (None, "") else None
        bird_id = create_harvest_with_shares(
            batch_id,
            hdate,
            dressed_lbs=round(dressed_lbs, 2),
            processing_cost_total=proc_cost,
            notes=notes,
            animal_label=str(payload.get("animal_label") or "").strip(),
            live_weight_lbs=live_wt,
            split_mode=str(payload.get("split_mode") or "halves"),
            for_sale=for_sale,
            weight_basis=str(payload.get("weight_basis") or "hanging"),
        )
    else:
        head_count = max(1, int(payload.get("head_count") or 1))
        if dressed_lbs <= 0:
            raise HTTPException(status_code=400, detail="Weight must be greater than 0.")
        bird_id = add_batch_harvest(
            batch_id, hdate, head_count, round(dressed_lbs, 2), proc_cost, notes, for_sale=for_sale
        )
    _broadcast_pick_event(
        b.get("picking_slug"),
        {"bird_id": bird_id, "status": "available" if for_sale else "hidden", "reload": True},
    )
    return _harvest_tally_payload(b, user["id"])


@router.post("/api/batches/{batch_id}/harvest/{harvest_id}/update")
async def api_batch_harvest_update(
    batch_id: int, harvest_id: int, request: Request, user: dict = Depends(current_user)
):
    b = _get_owned_batch(batch_id, user)
    payload = await request.json()
    raw_dressed = payload.get("dressed_lbs")
    dressed_val = float(raw_dressed) if raw_dressed not in (None, "") else None
    if dressed_val is not None and dressed_val < 0:
        raise HTTPException(status_code=400, detail="Weight cannot be negative.")
    raw_live = payload.get("live_weight_lbs")
    clear_live = "live_weight_lbs" in payload and raw_live in (None, "", 0, "0")
    live_val = float(raw_live) if raw_live not in (None, "") and not clear_live else None
    raw_proc = payload.get("processing_cost_total")
    proc_val = float(raw_proc) if raw_proc not in (None, "") else None
    ok = update_carcass_details(
        user["id"],
        batch_id,
        harvest_id,
        dressed_lbs=dressed_val,
        live_weight_lbs=live_val,
        clear_live_weight=clear_live,
        processing_cost_total=proc_val,
        animal_label=str(payload["animal_label"]) if "animal_label" in payload else None,
        harvest_date=str(payload["harvest_date"]) if "harvest_date" in payload else None,
        notes=str(payload["notes"]) if "notes" in payload else None,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Harvest entry not found.")
    _broadcast_pick_event(b.get("picking_slug"), {"reload": True})
    return _harvest_tally_payload(b, user["id"])


@router.post("/api/batches/{batch_id}/harvest/{harvest_id}/split-mode")
async def api_batch_harvest_split_mode(
    batch_id: int, harvest_id: int, request: Request, user: dict = Depends(current_user)
):
    b = _get_owned_batch(batch_id, user)
    payload = await request.json()
    mode = str(payload.get("split_mode") or "")
    try:
        ok = set_carcass_split_mode(user["id"], batch_id, harvest_id, mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="Harvest entry not found.")
    _broadcast_pick_event(b.get("picking_slug"), {"reload": True})
    return _harvest_tally_payload(b, user["id"])


@router.post("/api/batches/{batch_id}/shares/{share_id}/assign")
async def api_batch_share_assign(
    batch_id: int, share_id: int, request: Request, user: dict = Depends(current_user)
):
    b = _get_owned_batch(batch_id, user)
    payload = await request.json()
    raw_cid = payload.get("customer_id")
    cid = int(raw_cid) if raw_cid not in (None, "", 0, "0") else None
    if cid is not None and not get_customer(user["id"], cid):
        raise HTTPException(status_code=404, detail="Customer not found.")
    for_sale = bool(payload.get("for_sale", True))
    raw_sw = payload.get("share_weight_lbs")
    share_weight_lbs = float(raw_sw) if raw_sw not in (None, "") else None
    deposit_paid = float(payload.get("deposit_paid") or 0.0)
    raw_actual = payload.get("actual_sale_price")
    actual_sale_price = float(raw_actual) if raw_actual not in (None, "") else None
    customer_butcher_fee = float(payload.get("customer_butcher_fee") or 0.0)
    payment_status = str(payload.get("payment_status") or "unpaid")
    pickup_status = str(payload.get("pickup_status") or ("assigned" if cid else "unassigned"))
    raw_pm = payload.get("payment_method")
    payment_method = str(raw_pm) if raw_pm is not None else None
    butcher_notified = bool(payload["butcher_notified"]) if "butcher_notified" in payload else None
    cut_sheet_notes = payload.get("cut_sheet_notes")

    # Auto-apply unapplied paid pre-order deposit proportionally when newly assigning a customer
    if cid is not None and for_sale and deposit_paid == 0.0 and payment_status == "unpaid":
        existing_sh = next(
            (s for s in list_batch_harvest_shares(batch_id) if int(s["id"]) == int(share_id)),
            None,
        )
        prev_cid = int(existing_sh["customer_id"]) if existing_sh and existing_sh.get("customer_id") else None
        share_frac = float(existing_sh.get("share_fraction") or 1.0) if existing_sh else 1.0
        if prev_cid != cid:
            po_sum = get_customer_batch_preorder_summary(batch_id, cid)
            rem = float(po_sum.get("remaining_unapplied_deposit") or 0.0)
            if rem > 0.0:
                per_whole = float(po_sum.get("deposit_per_bird") or 0.0)
                po_equiv = float(po_sum.get("reserved_equiv") or po_sum.get("reserved_qty") or 0.0)
                claimed_equiv = float(po_sum.get("claimed_equiv") or po_sum.get("claimed_count") or 0.0)
                if po_equiv > 0 and (claimed_equiv + share_frac) >= (po_equiv - 1e-6):
                    deposit_paid = rem
                elif per_whole > 0.0:
                    deposit_paid = round(min(rem, per_whole * share_frac), 2)
                else:
                    deposit_paid = rem
                payment_status = "deposit"

    try:
        ok = update_share_assignment(
            user["id"],
            batch_id,
            share_id,
            customer_id=cid,
            for_sale=for_sale,
            share_weight_lbs=share_weight_lbs,
            deposit_paid=deposit_paid,
            actual_sale_price=actual_sale_price,
            customer_butcher_fee=customer_butcher_fee,
            payment_status=payment_status,
            payment_method=payment_method,
            pickup_status=pickup_status,
            butcher_notified=butcher_notified,
            cut_sheet_notes=str(cut_sheet_notes) if cut_sheet_notes is not None else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="Share not found.")
    pub_status = "hidden" if not for_sale else ("claimed" if cid else "available")
    _broadcast_pick_event(
        b.get("picking_slug"), {"bird_id": int(share_id), "status": pub_status, "reload": True}
    )
    return _harvest_tally_payload(b, user["id"])


@router.post("/api/batches/{batch_id}/share-mode")
async def api_batch_share_mode(
    batch_id: int, request: Request, user: dict = Depends(current_user)
):
    b = _get_owned_batch(batch_id, user)
    payload = await request.json()
    sale_unit = str(payload.get("sale_unit") or "shares")
    cpb = bool(payload.get("customer_pays_butcher", b.get("customer_pays_butcher")))
    bname = str(payload.get("butcher_name") if "butcher_name" in payload else (b.get("butcher_name") or ""))
    bphone = str(payload.get("butcher_phone") if "butcher_phone" in payload else (b.get("butcher_phone") or ""))
    bemail = str(payload.get("butcher_email") if "butcher_email" in payload else (b.get("butcher_email") or ""))
    updated = update_batch_share_settings(
        user["id"],
        batch_id,
        sale_unit=sale_unit,
        customer_pays_butcher=cpb,
        butcher_name=bname,
        butcher_phone=bphone,
        butcher_email=bemail,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Batch not found.")
    return _harvest_tally_payload(updated, user["id"])


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
    raw_pm = payload.get("payment_method")
    payment_method = str(raw_pm) if raw_pm is not None else None
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
    try:
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
            payment_method=payment_method,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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


def _public_picking_shares(batch: dict) -> list[dict]:
    """Return ONLY for_sale carcass shares with ZERO buyer PII."""
    shares = list_batch_harvest_shares(batch["id"])
    price = float(batch.get("sale_price_per_lb") or 0.0)
    harvest_nums: dict[int, int] = {}
    out = []
    for idx, sh in enumerate(shares, start=1):
        hid = int(sh["harvest_id"])
        if hid not in harvest_nums:
            harvest_nums[hid] = len(harvest_nums) + 1
        cnum = harvest_nums[hid]
        if not bool(1 if sh.get("for_sale") is None else sh.get("for_sale")):
            continue
        carcass_lbl = str(sh.get("animal_label") or "").strip() or f"Carcass #{cnum}"
        slot_lbl = share_slot_label(sh.get("share_slot"))
        dl = effective_share_weight(sh, float(sh.get("carcass_lbs") or 0.0))
        claimed = bool(sh.get("customer_id"))
        out.append({
            "id": int(sh["id"]),
            "num": idx,
            "label": f"{carcass_lbl} · {slot_lbl}",
            "dressed_lbs": dl,
            "lb_oz": _fmt_lb_oz(dl),
            "weight_pending": dl <= 0.0,
            "share_fraction": float(sh.get("share_fraction") or 1.0),
            "est_price": round(dl * price, 2),
            "status": "claimed" if claimed else "available",
        })
    out.sort(key=lambda x: (1 if x["status"] == "claimed" else 0, x["num"]))
    return out


def _resolve_pick_effective_max(batch: dict, po_summary: dict, is_share_mode: bool) -> float | int | None:
    po_only = bool(batch.get("picking_preorder_only"))
    base_max = batch.get("max_birds_per_customer")
    reserved_amt = po_summary["reserved_equiv"] if is_share_mode else po_summary["reserved_qty"]
    if po_only:
        return reserved_amt
    if po_summary["has_reservation"] and base_max and reserved_amt > float(base_max):
        return reserved_amt
    return base_max


@router.get("/pick/{slug}")
def public_picking_page(request: Request, slug: str):
    batch = get_batch_by_picking_slug(slug)
    if not batch:
        raise HTTPException(status_code=404, detail="Picking page not found.")
    enabled = bool(batch.get("picking_enabled"))
    sale_unit = normalize_sale_unit(batch.get("sale_unit"))
    is_share_mode = sale_unit == "shares"
    birds = (
        (_public_picking_shares(batch) if is_share_mode else _public_picking_birds(batch))
        if enabled
        else []
    )
    return templates.TemplateResponse(
        request, "pick.html",
        {
            "request": request,
            "batch_name": batch["name"],
            "species": batch["species"],
            "species_terms": get_species_terminology(batch.get("species", ""), batch.get("lifecycle_type")),
            "sale_unit": sale_unit,
            "is_share_mode": is_share_mode,
            "customer_pays_butcher": bool(batch.get("customer_pays_butcher")),
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
            detail="Early picking is currently open to pre-order customers only. Please use the phone number or email from your reservation.",
        )

    is_share_mode = normalize_sale_unit(batch.get("sale_unit")) == "shares"
    effective_max = _resolve_pick_effective_max(batch, po_summary, is_share_mode)

    price = float(batch.get("sale_price_per_lb") or 0.0)
    mine = []
    if is_share_mode:
        harvest_nums: dict[int, int] = {}
        for idx, sh in enumerate(list_batch_harvest_shares(batch["id"]), start=1):
            hid = int(sh["harvest_id"])
            if hid not in harvest_nums:
                harvest_nums[hid] = len(harvest_nums) + 1
            cnum = harvest_nums[hid]
            if sh.get("customer_id") and int(sh["customer_id"]) == int(cust["id"]) and bool(1 if sh.get("for_sale") is None else sh.get("for_sale")):
                dl = effective_share_weight(sh, float(sh.get("carcass_lbs") or 0.0))
                carcass_lbl = str(sh.get("animal_label") or "").strip() or f"Carcass #{cnum}"
                slot_lbl = share_slot_label(sh.get("share_slot"))
                mine.append({
                    "id": int(sh["id"]),
                    "num": idx,
                    "label": f"{carcass_lbl} · {slot_lbl}",
                    "dressed_lbs": dl,
                    "lb_oz": _fmt_lb_oz(dl),
                    "weight_pending": dl <= 0.0,
                    "share_fraction": float(sh.get("share_fraction") or 1.0),
                    "est_price": round(dl * price, 2),
                    "deposit_paid": round(float(sh.get("deposit_paid") or 0.0), 2),
                })
    else:
        harvests = sorted(list_batch_harvests(batch["id"]), key=lambda h: int(h.get("id") or 0))
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
        "reserved_qty": po_summary["reserved_equiv"] if is_share_mode else po_summary["reserved_qty"],
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
            detail="Early picking is currently open to pre-order customers only.",
        )

    is_share_mode = normalize_sale_unit(batch.get("sale_unit")) == "shares"
    effective_max = _resolve_pick_effective_max(batch, po_summary, is_share_mode)

    if is_share_mode:
        ok, reason = claim_share_atomically(
            int(batch["id"]),
            int(bird_id),
            int(cid),
            max_equiv=float(effective_max) if effective_max else None,
            apply_preorder_deposit=True,
        )
    else:
        ok, reason = claim_bird_atomically(
            int(batch["id"]),
            int(bird_id),
            int(cid),
            max_birds_per_customer=int(effective_max) if effective_max else None,
            apply_preorder_deposit=True,
        )
    terms = get_species_terminology(batch.get("species", ""), batch.get("lifecycle_type"))
    if not ok:
        unit_word = "share" if is_share_mode else str(terms["singular"])
        if reason == "limit_reached":
            raise HTTPException(
                status_code=409,
                detail=f"Limit of {effective_max} {unit_word}(s) per customer reached.",
            )
        raise HTTPException(
            status_code=409,
            detail=f"Someone just grabbed that {unit_word}—please pick another!",
        )
    _broadcast_pick_event(slug, {"bird_id": int(bird_id), "status": "claimed"})
    price = float(batch.get("sale_price_per_lb") or 0.0)
    claimed_info = None
    if is_share_mode:
        harvest_nums: dict[int, int] = {}
        for idx, sh in enumerate(list_batch_harvest_shares(batch["id"]), start=1):
            hid = int(sh["harvest_id"])
            if hid not in harvest_nums:
                harvest_nums[hid] = len(harvest_nums) + 1
            cnum = harvest_nums[hid]
            if int(sh["id"]) == int(bird_id):
                dl = effective_share_weight(sh, float(sh.get("carcass_lbs") or 0.0))
                carcass_lbl = str(sh.get("animal_label") or "").strip() or f"Carcass #{cnum}"
                slot_lbl = share_slot_label(sh.get("share_slot"))
                claimed_info = {
                    "id": int(sh["id"]),
                    "num": idx,
                    "label": f"{carcass_lbl} · {slot_lbl}",
                    "dressed_lbs": dl,
                    "lb_oz": _fmt_lb_oz(dl),
                    "weight_pending": dl <= 0.0,
                    "share_fraction": float(sh.get("share_fraction") or 1.0),
                    "est_price": round(dl * price, 2),
                    "deposit_paid": round(float(sh.get("deposit_paid") or 0.0), 2),
                }
                break
    else:
        harvests = sorted(list_batch_harvests(batch["id"]), key=lambda h: int(h.get("id") or 0))
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
        if is_share_mode:
            wt_note = (
                f"{claimed_info['dressed_lbs']:.2f} lb hanging - ${claimed_info['est_price']:.2f}"
                if claimed_info["dressed_lbs"] > 0
                else "Pending hanging wt"
            )
            ntfy.send_ntfy_async(
                int(batch["user_id"]),
                f"Share claimed - {batch['name']}",
                f"{buyer_name} claimed {claimed_info['label']} ({wt_note}){dep_note}",
                click_url=click_url,
            )
        else:
            ntfy.send_ntfy_async(
                int(batch["user_id"]),
                f"{terms['singular_title']} claimed - {batch['name']}",
                f"{buyer_name} claimed {terms['singular_title']} #{claimed_info['num']} "
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
