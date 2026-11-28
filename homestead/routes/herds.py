"""Perennial herds management (breeding pigs, fiber alpacas), named animals, and feed logs."""

from datetime import date
import json
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from homestead.db.core import get_bag_weight
from homestead.db.expenses import get_google_drive_connection
from homestead.db.fiber import (
    ALPACA_ANIMAL_FACTORS,
    ALPACA_FIBER_GRADES,
    ALPACA_FLEECE_COLORS,
    ALPACA_SHEARING_FACTORS,
    FIBER_OUTPUT_UNITS,
    FIBER_PRODUCT_TYPES,
    MILL_LOT_STATUSES,
    add_fiber_lot_output,
    add_fiber_shearing,
    compute_fiber_dashboard,
    delete_fiber_lot_output,
    delete_fiber_mill_lot,
    delete_fiber_shearing,
    save_fiber_mill_lot,
    sync_fiber_output_to_ledger,
)
from homestead.db.herds import (
    HERD_FEED_CATEGORIES,
    HERD_FEED_UNITS,
    HERD_SPECIES,
    HERD_TYPES,
    delete_herd,
    delete_herd_animal,
    delete_herd_feed_log,
    get_herd,
    list_herd_animals,
    list_herd_feed_logs,
    list_herds,
    save_herd,
    save_herd_animal,
    toggle_herd_animal_active,
)
from homestead.web import _render, _resolve_receipt_attachment, current_user

router = APIRouter()


def _parse_float(raw: object, default: float = 0.0) -> float:
    try:
        val = str(raw if raw is not None else "").strip()
        return float(val) if val else default
    except (TypeError, ValueError):
        return default


def _require_herd_manage(user: dict) -> None:
    if (user.get("role") or "owner") not in ("owner", "manager"):
        raise HTTPException(
            status_code=403, detail="Only owners and managers can modify herds."
        )


def _herd_redirect(
    herd_id: int | str | None, msg: str, tab: str = ""
) -> RedirectResponse:
    hid = str(herd_id or "").strip()
    hq = f"herd_id={quote_plus(hid)}&" if hid else ""
    tq = f"tab={quote_plus(tab)}&" if tab else ""
    return RedirectResponse(f"/herds?{hq}{tq}msg={quote_plus(msg)}", status_code=303)


@router.get("/herds")
def herds_page(
    request: Request,
    herd_id: str = "",
    tab: str = "overview",
    msg: str = "",
    user: dict = Depends(current_user),
):
    uid = user["id"]
    herds = list_herds(uid, include_inactive=True)
    selected_herd = None
    if herd_id.strip().isdigit():
        wanted = int(herd_id.strip())
        selected_herd = next((h for h in herds if int(h["id"]) == wanted), None)
    if selected_herd is None and herds:
        selected_herd = next((h for h in herds if h["active"]), herds[0])

    is_fiber_herd = bool(
        selected_herd
        and (
            selected_herd.get("herd_type") == "fiber"
            or selected_herd.get("species") in ("alpaca", "sheep")
        )
    )
    is_alpaca_herd = bool(selected_herd and selected_herd.get("species") == "alpaca")
    clean_tab = tab if (is_fiber_herd and tab in ("overview", "shearing", "mill")) else "overview"

    animals = (
        list_herd_animals(uid, herd_id=int(selected_herd["id"]), include_inactive=True)
        if selected_herd
        else []
    )
    feed_logs = (
        list_herd_feed_logs(uid, herd_id=int(selected_herd["id"]), limit=100)
        if selected_herd
        else []
    )
    fiber_dash = (
        compute_fiber_dashboard(
            uid,
            int(selected_herd["id"]),
            merchant_id=user.get("merchant_id"),
        )
        if is_fiber_herd and selected_herd
        else None
    )
    can_manage = (user.get("role") or "owner") in ("owner", "manager")

    return _render(
        request,
        "herds.html",
        user,
        "herds",
        herds=herds,
        selected_herd=selected_herd,
        is_fiber_herd=is_fiber_herd,
        is_alpaca_herd=is_alpaca_herd,
        active_tab=clean_tab,
        animals=animals,
        feed_logs=feed_logs,
        fiber_dash=fiber_dash,
        bag_weight=get_bag_weight(user_id=uid),
        herd_species_options=HERD_SPECIES,
        herd_type_options=HERD_TYPES,
        feed_category_options=HERD_FEED_CATEGORIES,
        feed_unit_options=HERD_FEED_UNITS,
        product_types=FIBER_PRODUCT_TYPES,
        output_units=FIBER_OUTPUT_UNITS,
        mill_statuses=MILL_LOT_STATUSES,
        alpaca_colors=ALPACA_FLEECE_COLORS,
        alpaca_fiber_grades=ALPACA_FIBER_GRADES,
        alpaca_animal_factors=ALPACA_ANIMAL_FACTORS,
        alpaca_shearing_factors=ALPACA_SHEARING_FACTORS,
        gdrive_connected=bool(get_google_drive_connection(uid)),
        can_manage=can_manage,
        msg=msg,
    )


@router.post("/herds/save")
def herd_save_submit(
    herd_id: str = Form(""),
    name: str = Form(...),
    species: str = Form("pig"),
    herd_type: str = Form("breeding"),
    head_count: int = Form(0),
    notes: str = Form(""),
    active: str = Form("1"),
    user: dict = Depends(current_user),
):
    _require_herd_manage(user)
    hid = int(herd_id) if herd_id.strip().isdigit() else None
    if hid is not None and not get_herd(user["id"], hid):
        raise HTTPException(status_code=404, detail="Herd not found.")
    try:
        saved_id = save_herd(
            user["id"],
            name=name,
            species=species,
            herd_type=herd_type,
            head_count=head_count,
            notes=notes,
            herd_id=hid,
            active=active != "0",
        )
    except ValueError as exc:
        return _herd_redirect(hid, f"Error: {exc}")
    verb = "Updated" if hid else "Added"
    return _herd_redirect(saved_id, f"{verb} herd '{name.strip()}'.")


@router.post("/herds/{herd_id}/delete")
def herd_delete_submit(herd_id: int, user: dict = Depends(current_user)):
    _require_herd_manage(user)
    if not delete_herd(user["id"], herd_id):
        raise HTTPException(status_code=404, detail="Herd not found.")
    return _herd_redirect("", "Removed herd.")


@router.post("/herds/{herd_id}/animals/save")
def herd_animal_save_submit(
    herd_id: int,
    animal_id: str = Form(""),
    name: str = Form(...),
    sex_role: str = Form(""),
    color: str = Form(""),
    fiber_grade: str = Form(""),
    fleece_factors: list[str] = Form([]),
    birth_year: str = Form(""),
    notes: str = Form(""),
    user: dict = Depends(current_user),
):
    _require_herd_manage(user)
    if not get_herd(user["id"], herd_id):
        raise HTTPException(status_code=404, detail="Herd not found.")
    aid = int(animal_id) if animal_id.strip().isdigit() else None
    byr = int(birth_year.strip()) if birth_year.strip().isdigit() else None
    try:
        save_herd_animal(
            user["id"],
            herd_id=herd_id,
            name=name,
            sex_role=sex_role,
            color=color,
            fiber_grade=fiber_grade,
            fleece_factors=fleece_factors,
            birth_year=byr,
            notes=notes,
            animal_id=aid,
        )
    except ValueError as exc:
        return _herd_redirect(herd_id, f"Error: {exc}")
    verb = "Updated" if aid else "Added"
    return _herd_redirect(herd_id, f"{verb} {name.strip()}.")


@router.post("/herds/{herd_id}/animals/{animal_id}/toggle")
def herd_animal_toggle_submit(
    herd_id: int,
    animal_id: int,
    active: int = Form(...),
    user: dict = Depends(current_user),
):
    _require_herd_manage(user)
    if not get_herd(user["id"], herd_id):
        raise HTTPException(status_code=404, detail="Herd not found.")
    toggle_herd_animal_active(user["id"], animal_id, bool(active))
    return _herd_redirect(
        herd_id, "Marked animal active." if active else "Retired animal from active count."
    )


@router.post("/herds/{herd_id}/animals/{animal_id}/delete")
def herd_animal_delete_submit(
    herd_id: int, animal_id: int, user: dict = Depends(current_user)
):
    _require_herd_manage(user)
    if not get_herd(user["id"], herd_id):
        raise HTTPException(status_code=404, detail="Herd not found.")
    delete_herd_animal(user["id"], animal_id)
    return _herd_redirect(herd_id, "Removed animal from roster.")


@router.post("/herds/{herd_id}/feed/{log_id}/delete")
def herd_feed_delete_submit(
    herd_id: int, log_id: int, user: dict = Depends(current_user)
):
    _require_herd_manage(user)
    if not get_herd(user["id"], herd_id):
        raise HTTPException(status_code=404, detail="Herd not found.")
    delete_herd_feed_log(user["id"], log_id)
    return _herd_redirect(herd_id, "Removed herd feed entry.")


@router.post("/herds/{herd_id}/shearings/add")
def herd_shearing_add_submit(
    herd_id: int,
    animal_id: str = Form(""),
    shear_date: str = Form(""),
    fleece_color: str = Form(""),
    fiber_grade: str = Form(""),
    fleece_factors: list[str] = Form([]),
    fleece_factors_submitted: str = Form(""),
    prime_lbs: str = Form("0"),
    seconds_lbs: str = Form("0"),
    total_lbs: str = Form("0"),
    shearing_fee: str = Form("0"),
    notes: str = Form(""),
    user: dict = Depends(current_user),
):
    _require_herd_manage(user)
    if not get_herd(user["id"], herd_id):
        raise HTTPException(status_code=404, detail="Herd not found.")
    aid = int(animal_id) if animal_id.strip().isdigit() else None
    try:
        add_fiber_shearing(
            user["id"],
            herd_id,
            animal_id=aid,
            shear_date=shear_date or date.today().isoformat(),
            fleece_color=fleece_color,
            fiber_grade=fiber_grade,
            fleece_factors=(
                fleece_factors if fleece_factors_submitted else (fleece_factors or None)
            ),
            prime_lbs=_parse_float(prime_lbs),
            seconds_lbs=_parse_float(seconds_lbs),
            total_lbs=_parse_float(total_lbs),
            shearing_fee=_parse_float(shearing_fee),
            notes=notes,
        )
        msg = "Logged shearing harvest."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return _herd_redirect(herd_id, msg, tab="shearing")


@router.post("/herds/{herd_id}/shearings/{shearing_id}/delete")
def herd_shearing_delete_submit(
    herd_id: int, shearing_id: int, user: dict = Depends(current_user)
):
    _require_herd_manage(user)
    if not get_herd(user["id"], herd_id):
        raise HTTPException(status_code=404, detail="Herd not found.")
    delete_fiber_shearing(user["id"], shearing_id)
    return _herd_redirect(herd_id, "Removed shearing record.", tab="shearing")


@router.post("/herds/{herd_id}/mill-lots/save")
async def herd_mill_lot_save_submit(
    herd_id: int, request: Request, user: dict = Depends(current_user)
):
    _require_herd_manage(user)
    if not get_herd(user["id"], herd_id):
        raise HTTPException(status_code=404, detail="Herd not found.")
    form = await request.form()
    raw_lid = str(form.get("lot_id") or "").strip()
    lid = int(raw_lid) if raw_lid.isdigit() else None
    lot_name = str(form.get("lot_name") or "").strip()
    mill_name = str(form.get("mill_name") or "").strip()
    color_group = str(form.get("color_group") or "").strip()
    status = str(form.get("status") or "sent_to_mill").strip()
    sent_date = str(form.get("sent_date") or "").strip()
    received_date = str(form.get("received_date") or "").strip()
    ship_out = _parse_float(form.get("shipping_out_cost"))
    mill_fee = _parse_float(form.get("milling_fee"))
    ship_back = _parse_float(form.get("shipping_back_cost"))
    other_fee = _parse_float(form.get("other_handling_fee"))
    include_tax = str(form.get("include_in_tax_books") or "") == "1"
    notes = str(form.get("notes") or "").strip()
    manual_url = str(form.get("receipt_url") or "").strip()
    receipt_file = form.get("receipt_file")
    upload = receipt_file if (receipt_file is not None and getattr(receipt_file, "filename", "")) else None
    r_url, drive_id, warn = await _resolve_receipt_attachment(
        user["id"],
        upload,
        manual_url,
        received_date or sent_date or date.today().isoformat(),
        f"Fiber Mill ({lot_name or 'Lot'})",
    )

    inputs_payload: list[dict] | None = None
    raw_inputs_json = str(form.get("inputs_json") or "").strip()
    if raw_inputs_json:
        try:
            parsed_in = json.loads(raw_inputs_json)
            if isinstance(parsed_in, list):
                inputs_payload = parsed_in
        except json.JSONDecodeError:
            return _herd_redirect(herd_id, "Error: Invalid raw fleece allocation list.", tab="mill")
    elif form.get("inputs_submitted") or form.getlist("shearing_ids"):
        inputs_payload = []
        for sid_str in form.getlist("shearing_ids"):
            if str(sid_str).strip().isdigit():
                wt = _parse_float(form.get(f"raw_lbs_{sid_str}"))
                inputs_payload.append(
                    {"shearing_id": int(sid_str), "raw_lbs_used": wt}
                )

    try:
        save_fiber_mill_lot(
            user["id"],
            herd_id,
            lot_name=lot_name,
            mill_name=mill_name,
            color_group=color_group,
            status=status,
            sent_date=sent_date,
            received_date=received_date,
            shipping_out_cost=ship_out,
            milling_fee=mill_fee,
            shipping_back_cost=ship_back,
            other_handling_fee=other_fee,
            include_in_tax_books=include_tax,
            receipt_url=r_url,
            drive_file_id=drive_id,
            notes=notes,
            lot_id=lid,
            inputs=inputs_payload,
        )
        msg = ("Updated mill lot." if lid else f"Created mill lot '{lot_name}'.")
        if warn:
            msg += f" (Receipt upload note: {warn})"
    except ValueError as exc:
        msg = f"Error: {exc}"
    return _herd_redirect(herd_id, msg, tab="mill")


@router.post("/herds/{herd_id}/mill-lots/{lot_id}/delete")
def herd_mill_lot_delete_submit(
    herd_id: int, lot_id: int, user: dict = Depends(current_user)
):
    _require_herd_manage(user)
    if not get_herd(user["id"], herd_id):
        raise HTTPException(status_code=404, detail="Herd not found.")
    delete_fiber_mill_lot(user["id"], lot_id)
    return _herd_redirect(herd_id, "Removed mill lot (and reversed linked Iron Ledger restocks).", tab="mill")


@router.post("/herds/{herd_id}/mill-lots/{lot_id}/outputs/add")
def herd_mill_output_add_submit(
    herd_id: int,
    lot_id: int,
    product_type: str = Form("yarn_skein"),
    product_name: str = Form(...),
    unit: str = Form("skeins"),
    quantity: str = Form("1"),
    finished_weight_oz: str = Form("0"),
    ledger_sku_id: str = Form(""),
    sync_to_ledger: str = Form(""),
    notes: str = Form(""),
    user: dict = Depends(current_user),
):
    _require_herd_manage(user)
    if not get_herd(user["id"], herd_id):
        raise HTTPException(status_code=404, detail="Herd not found.")
    sku_val = int(ledger_sku_id) if ledger_sku_id.strip().isdigit() else None
    try:
        res = add_fiber_lot_output(
            user["id"],
            lot_id,
            product_type=product_type,
            product_name=product_name,
            unit=unit,
            quantity=_parse_float(quantity, 0.0),
            finished_weight_oz=_parse_float(finished_weight_oz, 0.0),
            ledger_sku_id=sku_val,
            sync_to_ledger=bool(sync_to_ledger) and bool(sku_val),
            notes=notes,
            created_by=user.get("actor_id", user["id"]),
        )
        sync_note = " & restocked Iron Ledger!" if res["synced_to_ledger"] else "."
        msg = f"Logged {res['quantity']:g} {unit} {product_name.strip()}{sync_note}"
    except ValueError as exc:
        msg = f"Error: {exc}"
    return _herd_redirect(herd_id, msg, tab="mill")


@router.post("/herds/{herd_id}/mill-lots/{lot_id}/outputs/{output_id}/sync-ledger")
def herd_mill_output_sync_submit(
    herd_id: int,
    lot_id: int,
    output_id: int,
    ledger_sku_id: str = Form(""),
    user: dict = Depends(current_user),
):
    _require_herd_manage(user)
    if not get_herd(user["id"], herd_id):
        raise HTTPException(status_code=404, detail="Herd not found.")
    if not ledger_sku_id.strip().isdigit():
        return _herd_redirect(herd_id, "Error: Select an Iron Ledger product first.", tab="mill")
    try:
        res = sync_fiber_output_to_ledger(
            user["id"],
            output_id,
            int(ledger_sku_id),
            created_by=user.get("actor_id", user["id"]),
        )
        msg = f"Restocked +{res['quantity']:g} units into Iron Ledger!"
    except ValueError as exc:
        msg = f"Error: {exc}"
    return _herd_redirect(herd_id, msg, tab="mill")


@router.post("/herds/{herd_id}/mill-lots/{lot_id}/outputs/{output_id}/delete")
def herd_mill_output_delete_submit(
    herd_id: int,
    lot_id: int,
    output_id: int,
    user: dict = Depends(current_user),
):
    _require_herd_manage(user)
    if not get_herd(user["id"], herd_id):
        raise HTTPException(status_code=404, detail="Herd not found.")
    delete_fiber_lot_output(user["id"], output_id)
    return _herd_redirect(herd_id, "Removed finished output (and reversed Iron Ledger restock).", tab="mill")
