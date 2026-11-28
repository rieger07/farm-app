"""Livestock batch management, losses, harvests, sales, feed logs, and layer graduation."""

import csv
from datetime import date, timedelta
import io
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from homestead.calendar_export import create_gcal_link
from homestead.config import (
    format_species_display,
    format_species_preset_label,
    get_species_terminology,
)
from homestead.db.batches import (
    add_batch_feed_log,
    add_batch_harvest,
    add_batch_loss,
    add_batch_sale,
    delete_batch,
    delete_batch_feed_log,
    delete_batch_harvest,
    delete_batch_loss,
    delete_batch_sale,
    list_batch_feed_logs,
    list_batch_harvests,
    list_batch_losses,
    list_batch_sales,
    save_batch,
    set_batch_active,
    update_batch_sale_payment_method,
)
from homestead.db.core import get_bag_weight, get_species_catalog
from shared.bridge import HOMESTEAD_PAYMENT_METHODS
from homestead.db.customers import batch_preorder_counts
from homestead.db.eggs import (
    EGG_SPECIES_DEFAULTS,
    graduate_batch_to_layers,
    is_batch_graduated_to_layers,
    normalize_egg_species,
)
from homestead.db.expenses import get_google_drive_connection
from homestead.db.herds import get_herd
from homestead.scheduling import compute_batch_schedule, get_batch_status_badge
from homestead.web import (
    _batch_stage_info,
    _fmt_lb_oz,
    _get_owned_batch,
    _harvest_tally_payload,
    _render,
    _resolve_receipt_attachment,
    _schedules_for_user,
    current_user,
)

router = APIRouter()


@router.get("/batches")
def batches_page(
    request: Request,
    msg: str = "",
    from_herd: str = "",
    user: dict = Depends(current_user),
):
    schedules = _schedules_for_user(user["id"], include_archived=True)
    po_counts = batch_preorder_counts(user["id"])
    species_catalog = get_species_catalog(user_id=user["id"])
    litter_preset = None
    if from_herd.strip().isdigit():
        herd = get_herd(user["id"], int(from_herd.strip()))
        if herd:
            default_qty = 6
            sp_key = "kunekune pigs" if (herd["species"] == "pig" and "kunekune pigs" in species_catalog) else next(iter(species_catalog), "chicken chicks")
            t12 = float(herd.get("trailing_12m_cost") or 0.0)
            suggested_unit = round(t12 / default_qty, 2) if t12 > 0 else float(species_catalog.get(sp_key, {}).get("unit_cost") or 0.0)
            litter_preset = {
                "name": f"{herd['name']} Litter ({date.today().strftime('%b %Y')})",
                "species": sp_key,
                "lifecycle_type": "mammal_breeding" if herd["species"] == "pig" else "direct_arrival",
                "quantity": default_qty,
                "unit_cost": suggested_unit,
                "sale_unit": "shares" if herd["species"] == "pig" else "whole",
                "customer_pays_butcher": herd["species"] == "pig",
                "notes": f"From {herd['name']} (trailing 12m upkeep: ${t12:,.2f})",
            }
    return _render(
        request, "batches.html", user, "batches",
        schedules=schedules,
        po_counts=po_counts,
        species_catalog=species_catalog,
        litter_preset=litter_preset,
        msg=msg,
    )


@router.get("/batches/{batch_id}")
def batch_detail_page(request: Request, batch_id: int, msg: str = "", user: dict = Depends(current_user)):
    b = _get_owned_batch(batch_id, user)
    bag_weight = get_bag_weight(user_id=user["id"])
    s = compute_batch_schedule(b, bag_weight=bag_weight)
    s["status_badge"] = get_batch_status_badge(s)
    gcal_arrival_url = create_gcal_link(
        f"{b['name']} - {s['initial_label']}",
        s["start_date"],
        s["end_date"],
        f"{s['initial_quantity']} {format_species_preset_label(b['species'])}",
    )
    gcal_url = create_gcal_link(
        f"Process {b['name']}",
        s["proc_date"],
        s["proc_date"],
        f"{s['surviving_quantity']} {format_species_display(b['species'], plural=s['surviving_quantity'] != 1)}",
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
    alloc_items = tally["shares"] if tally.get("is_share_mode") else tally["entries"]
    for e in alloc_items:
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
        payment_methods=HOMESTEAD_PAYMENT_METHODS,
        msg=msg,
    )


@router.post("/batches/{batch_id}/graduate-layers")
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


@router.get("/batches/{batch_id}/harvest.csv")
def batch_harvest_csv(batch_id: int, user: dict = Depends(current_user)):
    b = _get_owned_batch(batch_id, user)
    tally = _harvest_tally_payload(b, user["id"])
    buf = io.StringIO()
    writer = csv.writer(buf)

    def _safe(val: object) -> str:
        s = str(val if val is not None else "")
        return "'" + s if s and s[0] in ("=", "+", "-", "@", "\t", "\r") else s

    if tally.get("is_share_mode"):
        writer.writerow([
            "Carcass", "Date", "Share", "Weight (lb)", "Customer",
            "Price ($)", "Deposit ($)", "Payment", "Butcher Released",
            "Pickup", "Farm Proc Fee ($)", "Cut Sheet / Notes",
        ])
        for entry in reversed(tally["entries"]):
            shares = entry.get("shares") or []
            for idx_s, sh in enumerate(shares):
                wt_str = f"{sh['effective_lbs']:.2f}" if sh["effective_lbs"] > 0 else "Pending"
                writer.writerow([
                    _safe(entry["animal_label"]),
                    sh["harvest_date"],
                    sh["slot_label"],
                    wt_str,
                    _safe("Freezer / Keep" if not sh["for_sale"] else (sh["customer_name"] or "Available")),
                    f"{sh['effective_price']:.2f}" if sh["for_sale"] else "0.00",
                    f"{sh['deposit_paid']:.2f}",
                    sh["payment_status"],
                    sh["butcher_notified_at"] or "No",
                    sh["pickup_status"],
                    f"{entry['processing_cost_total']:.2f}" if idx_s == 0 else "0.00",
                    _safe(sh["cut_sheet_notes"] or entry["notes"]),
                ])
        writer.writerow([
            "TOTAL", "", f"{tally['harvested_head']} carcasses",
            f"{tally['total_lbs']:.2f}", "", f"{tally['est_value']:.2f}",
            "", "", "", "", "", "",
        ])
    else:
        writer.writerow(["#", "Date", "Head", "Dressed (lb)", "Lb + Oz", "Notes"])
        for e in reversed(tally["entries"]):
            writer.writerow([
                e["num"], e["harvest_date"], e["head_count"],
                f"{e['dressed_lbs']:.2f}", e["lb_oz"], _safe(e["notes"]),
            ])
        terms = get_species_terminology(b.get("species", ""), b.get("lifecycle_type"))
        writer.writerow([
            "TOTAL", "", tally["harvested_head"],
            f"{tally['total_lbs']:.2f}", _fmt_lb_oz(tally["total_lbs"]),
            f"Avg {tally['avg_lbs']:.2f} lb/{terms['singular']}",
        ])
    slug = "".join(ch if ch.isalnum() else "-" for ch in b["name"].lower()).strip("-") or f"batch-{batch_id}"
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{slug}-harvest.csv"'},
    )


@router.post("/batches/save")
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
    sale_unit: str = Form(""),
    customer_pays_butcher: str = Form(""),
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
    if sale_unit.strip():
        payload["sale_unit"] = sale_unit.strip()
        payload["customer_pays_butcher"] = customer_pays_butcher.strip() in ("1", "true", "on", "yes")
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


@router.post("/batches/{batch_id}/toggle-active")
def batch_toggle_active(batch_id: int, user: dict = Depends(current_user)):
    b = _get_owned_batch(batch_id, user)
    set_batch_active(batch_id, not bool(b.get("active", 1)))
    return RedirectResponse("/batches?msg=" + quote_plus("Updated batch status."), status_code=303)


@router.post("/batches/{batch_id}/start-now")
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


@router.post("/batches/{batch_id}/delete")
def batch_delete_submit(batch_id: int, user: dict = Depends(current_user)):
    if (user.get("role") or "owner") != "owner":
        raise HTTPException(status_code=403, detail="Only farm owners can delete batches.")
    _get_owned_batch(batch_id, user)
    delete_batch(batch_id)
    return RedirectResponse("/batches?msg=" + quote_plus("Batch deleted."), status_code=303)


@router.post("/batches/log-loss")
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


@router.post("/batches/{batch_id}/losses/{loss_id}/delete")
def batch_loss_delete(batch_id: int, loss_id: int, user: dict = Depends(current_user)):
    _get_owned_batch(batch_id, user)
    delete_batch_loss(loss_id)
    return RedirectResponse(f"/batches/{batch_id}?msg=" + quote_plus("Loss removed."), status_code=303)


@router.post("/batches/log-harvest")
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


@router.post("/batches/{batch_id}/harvests/{harvest_id}/delete")
def batch_harvest_delete(batch_id: int, harvest_id: int, user: dict = Depends(current_user)):
    _get_owned_batch(batch_id, user)
    delete_batch_harvest(harvest_id)
    return RedirectResponse(f"/batches/{batch_id}?msg=" + quote_plus("Harvest removed."), status_code=303)


@router.post("/batches/log-sale")
def batch_log_sale(
    batch_id: int = Form(...),
    entry_date: str = Form(...),
    allocation_type: str = Form("sale"),
    weight_lbs: float = Form(...),
    revenue: float = Form(0.0),
    payment_method: str = Form("cash"),
    notes: str = Form(""),
    next_url: str = Form("/"),
    user: dict = Depends(current_user),
):
    _get_owned_batch(batch_id, user)
    target = next_url if next_url.startswith("/") else "/"
    try:
        add_batch_sale(
            batch_id, entry_date, allocation_type, weight_lbs, revenue,
            notes.strip(), payment_method=payment_method,
        )
        msg = "Saved sale/freezer entry."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"{target}?msg=" + quote_plus(msg), status_code=303)


@router.post("/batches/{batch_id}/sales/{sale_id}/payment-method")
def batch_sale_payment_method_submit(
    batch_id: int,
    sale_id: int,
    payment_method: str = Form(""),
    user: dict = Depends(current_user),
):
    _get_owned_batch(batch_id, user)
    try:
        updated = update_batch_sale_payment_method(batch_id, sale_id, payment_method)
        lbl = payment_method.strip().capitalize() if payment_method.strip() else "Untagged"
        msg = f"Updated sale payment method to {lbl}." if updated else "Customer sale not found."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/batches/{batch_id}?msg=" + quote_plus(msg), status_code=303)


@router.post("/batches/{batch_id}/sales/{sale_id}/delete")
def batch_sale_delete(batch_id: int, sale_id: int, user: dict = Depends(current_user)):
    _get_owned_batch(batch_id, user)
    delete_batch_sale(sale_id)
    return RedirectResponse(f"/batches/{batch_id}?msg=" + quote_plus("Entry removed."), status_code=303)


@router.post("/batches/log-feed")
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


@router.post("/batches/{batch_id}/feed-logs/{log_id}/delete")
def batch_feed_log_delete(batch_id: int, log_id: int, user: dict = Depends(current_user)):
    _get_owned_batch(batch_id, user)
    delete_batch_feed_log(log_id)
    return RedirectResponse(f"/batches/{batch_id}?msg=" + quote_plus("Feed entry removed."), status_code=303)
