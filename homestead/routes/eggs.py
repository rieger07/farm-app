"""Multi-species eggs tracker, carton packing + Iron Ledger sync, flock & layer feed."""

from datetime import date
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse

from homestead.db.core import get_bag_weight
from homestead.db.eggs import (
    EGG_SPECIES_DEFAULTS,
    add_egg_log,
    add_layer_feed_log,
    add_layer_flock_event,
    compute_egg_dashboard,
    delete_egg_log,
    delete_egg_pack_log,
    delete_layer_feed_log,
    delete_layer_flock_event,
    normalize_egg_species,
    pack_egg_cartons,
    save_layer_settings,
)
from homestead.db.expenses import get_google_drive_connection
from homestead.web import (
    _render,
    _resolve_receipt_attachment,
    current_user,
)

router = APIRouter()


@router.get("/eggs")
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


@router.post("/eggs/log")
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


@router.post("/eggs/log/{log_id}/delete")
def eggs_log_delete_submit(
    log_id: int,
    species: str = Form(""),
    user: dict = Depends(current_user),
):
    delete_egg_log(user["id"], log_id)
    sp_q = f"species={normalize_egg_species(species)}&" if species else ""
    return RedirectResponse(f"/eggs?{sp_q}msg=" + quote_plus("Removed egg log entry."), status_code=303)


@router.post("/eggs/pack")
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


@router.post("/eggs/pack/{pack_id}/delete")
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


@router.post("/eggs/flock")
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


@router.post("/eggs/flock/{event_id}/delete")
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


@router.post("/eggs/feed")
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


@router.post("/eggs/feed/{feed_id}/delete")
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


@router.post("/eggs/settings")
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
