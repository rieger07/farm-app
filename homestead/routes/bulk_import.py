"""Bulk receipt uploads (multi-file queue + CSV) and historical batch onboarding routes."""

from datetime import date
import json
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response

from homestead.bulk_import import (
    create_historical_batch_record,
    find_duplicate_receipt_indices,
    generate_batch_csv_template,
    generate_receipt_csv_template,
    parse_batch_csv,
    parse_receipt_csv,
)
from homestead.db.batches import list_batches
from homestead.db.core import get_bag_weight, get_species_catalog
from homestead.db.eggs import get_layer_settings
from homestead.db.expenses import (
    EXPENSE_CATEGORIES,
    EXPENSE_CATEGORY_OPTIONS,
    get_google_drive_connection,
    list_recent_vendors,
)
from homestead.db.herds import list_herds
from homestead.db.workspaces import can_access_homestead_page
from homestead.receipt_allocation import (
    BatchOwnershipError,
    build_receipt_batch_options,
    expand_receipt_lines,
)
from homestead.receipt_ocr import is_receipt_ocr_enabled
from homestead.routes.farm_ops import persist_new_receipt_lines
from homestead.web import (
    _render,
    _resolve_receipt_attachment,
    _schedules_for_user,
    current_user,
)
from shared.bridge import HOMESTEAD_PAYMENT_METHODS

router = APIRouter()


def _require_financials(user: dict) -> None:
    if not can_access_homestead_page(user.get("role") or "owner", "financials"):
        raise HTTPException(
            status_code=403, detail="Your role does not have access to Financials."
        )


def _require_batch_editor(user: dict) -> None:
    if (user.get("role") or "owner") == "staff":
        raise HTTPException(
            status_code=403, detail="Staff role cannot import or create batches."
        )


def _bulk_receipts_context(
    user: dict,
    *,
    active_tab: str = "files",
    csv_preview: dict | None = None,
    csv_error: str = "",
    msg: str = "",
) -> dict:
    uid = user["id"]
    all_schedules = _schedules_for_user(uid, include_archived=True)
    receipt_batches = build_receipt_batch_options(all_schedules)
    return {
        "active_tab": active_tab if active_tab in ("files", "csv") else "files",
        "receipt_batches": receipt_batches,
        "all_herds": list_herds(uid, include_inactive=True),
        "expense_categories": EXPENSE_CATEGORIES,
        "expense_category_options": EXPENSE_CATEGORY_OPTIONS,
        "layer_cfg": get_layer_settings(uid),
        "bag_weight": get_bag_weight(user_id=uid),
        "ocr_enabled": is_receipt_ocr_enabled(),
        "gdrive_connected": bool(get_google_drive_connection(uid)),
        "recent_vendors": list_recent_vendors(uid),
        "csv_preview": csv_preview,
        "csv_error": csv_error,
        "msg": msg,
    }


def _bulk_batches_context(
    user: dict,
    *,
    active_tab: str = "quick",
    csv_preview: dict | None = None,
    csv_error: str = "",
    msg: str = "",
) -> dict:
    uid = user["id"]
    return {
        "active_tab": active_tab if active_tab in ("quick", "csv") else "quick",
        "species_catalog": get_species_catalog(user_id=uid),
        "payment_methods": HOMESTEAD_PAYMENT_METHODS,
        "csv_preview": csv_preview,
        "csv_error": csv_error,
        "msg": msg,
    }


@router.get("/financials/bulk-receipts")
def bulk_receipts_page(
    request: Request,
    tab: str = "files",
    msg: str = "",
    user: dict = Depends(current_user),
):
    _require_financials(user)
    ctx = _bulk_receipts_context(user, active_tab=tab, msg=msg)
    return _render(request, "bulk_receipts.html", user, "financials", **ctx)


@router.post("/api/receipts/save")
async def api_receipt_save(
    receipt_date: str = Form(""),
    vendor: str = Form(""),
    receipt_url: str = Form(""),
    receipt_file: UploadFile | None = File(None),
    lines_json: str = Form("[]"),
    user: dict = Depends(current_user),
):
    _require_financials(user)
    rdate = receipt_date.strip()[:10] or date.today().isoformat()
    clean_vendor = vendor.strip()
    try:
        raw_lines = json.loads(lines_json or "[]")
    except (ValueError, TypeError):
        return JSONResponse({"error": "Invalid receipt line data."}, status_code=400)

    owned_batch_ids = {int(b["id"]) for b in list_batches(user_id=user["id"])}
    owned_herd_ids = {int(h["id"]) for h in list_herds(user["id"], include_inactive=True)}
    try:
        expanded_lines = expand_receipt_lines(
            raw_lines,
            owned_batch_ids=owned_batch_ids,
            owned_herd_ids=owned_herd_ids,
        )
    except BatchOwnershipError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    r_url, drive_id, warn = await _resolve_receipt_attachment(
        user["id"],
        receipt_file,
        receipt_url,
        rdate,
        clean_vendor or "Receipt",
    )
    saved_count, total_amt = persist_new_receipt_lines(
        user["id"],
        receipt_date=rdate,
        vendor=clean_vendor,
        receipt_url=r_url,
        drive_file_id=drive_id,
        expanded_lines=expanded_lines,
    )
    return JSONResponse(
        {
            "ok": True,
            "saved_count": saved_count,
            "total_amount": total_amt,
            "receipt_url": r_url,
            "warning": warn or "",
        },
        status_code=200,
    )


@router.post("/api/receipts/check-duplicates")
async def api_receipts_check_duplicates(
    request: Request, user: dict = Depends(current_user)
):
    _require_financials(user)
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return JSONResponse({"duplicates": []}, status_code=200)
    items = payload.get("items") if isinstance(payload, dict) else []
    if not isinstance(items, list):
        items = []
    dups = sorted(find_duplicate_receipt_indices(user["id"], items[:200]))
    return JSONResponse({"duplicates": dups}, status_code=200)


@router.get("/financials/bulk-receipts/template.csv")
def bulk_receipts_template_csv(user: dict = Depends(current_user)):
    _require_financials(user)
    return Response(
        generate_receipt_csv_template(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="homestead-receipts-template.csv"'},
    )


@router.post("/financials/bulk-receipts/csv-preview")
async def bulk_receipts_csv_preview(
    request: Request,
    csv_file: UploadFile = File(...),
    user: dict = Depends(current_user),
):
    _require_financials(user)
    raw = await csv_file.read()
    uid = user["id"]
    batches = list_batches(user_id=uid)
    herds = list_herds(uid, include_inactive=True)
    bag_wt = get_bag_weight(user_id=uid)
    try:
        preview = parse_receipt_csv(
            raw,
            batches=batches,
            herds=herds,
            bag_weight=bag_wt,
            existing_user_id=uid,
        )
        ctx = _bulk_receipts_context(user, active_tab="csv", csv_preview=preview)
    except ValueError as exc:
        ctx = _bulk_receipts_context(user, active_tab="csv", csv_error=str(exc))
    return _render(request, "bulk_receipts.html", user, "financials", **ctx)


@router.post("/financials/bulk-receipts/csv-commit")
def bulk_receipts_csv_commit(
    rows_json: str = Form("[]"),
    user: dict = Depends(current_user),
):
    _require_financials(user)
    try:
        rows = json.loads(rows_json or "[]")
    except (ValueError, TypeError):
        return RedirectResponse(
            "/financials/bulk-receipts?tab=csv&msg=" + quote_plus("Error: Invalid import selection."),
            status_code=303,
        )
    if not isinstance(rows, list) or not rows:
        return RedirectResponse(
            "/financials/bulk-receipts?tab=csv&msg=" + quote_plus("Select at least one valid row to import."),
            status_code=303,
        )

    uid = user["id"]
    owned_batch_ids = {int(b["id"]) for b in list_batches(user_id=uid)}
    owned_herd_ids = {int(h["id"]) for h in list_herds(uid, include_inactive=True)}
    imported = 0
    total_amt = 0.0
    for item in rows:
        if not isinstance(item, dict):
            continue
        rdate = str(item.get("date") or "").strip()[:10]
        vendor = str(item.get("vendor") or "").strip()
        r_url = str(item.get("receipt_url") or "").strip()
        if r_url and not r_url.lower().startswith(("http://", "https://")):
            r_url = ""
        line = item.get("line")
        if not rdate or not vendor or not isinstance(line, dict):
            continue
        try:
            expanded = expand_receipt_lines(
                [line],
                owned_batch_ids=owned_batch_ids,
                owned_herd_ids=owned_herd_ids,
            )
        except (BatchOwnershipError, ValueError):
            continue
        cnt, amt = persist_new_receipt_lines(
            uid,
            receipt_date=rdate,
            vendor=vendor,
            receipt_url=r_url,
            drive_file_id="",
            expanded_lines=expanded,
        )
        imported += cnt
        total_amt = round(total_amt + amt, 2)

    msg = f"Imported {imported} receipt entr{'y' if imported == 1 else 'ies'} (${total_amt:,.2f})."
    return RedirectResponse(
        "/financials?tab=taxes&year=all&msg=" + quote_plus(msg),
        status_code=303,
    )


@router.get("/batches/import")
def bulk_batches_page(
    request: Request,
    tab: str = "quick",
    msg: str = "",
    user: dict = Depends(current_user),
):
    _require_batch_editor(user)
    ctx = _bulk_batches_context(user, active_tab=tab, msg=msg)
    return _render(request, "bulk_batches.html", user, "batches", **ctx)


@router.post("/batches/import/quick")
def bulk_batches_quick_submit(
    name: str = Form(...),
    species: str = Form(...),
    start_date: str = Form(...),
    initial_quantity: int = Form(...),
    unit_cost: str = Form("0"),
    sale_unit: str = Form("whole"),
    harvest_date: str = Form(""),
    harvested_head: str = Form("0"),
    dressed_lbs: str = Form("0"),
    processing_cost: str = Form("0"),
    feed_lbs: str = Form("0"),
    feed_cost: str = Form("0"),
    losses: str = Form("0"),
    live_sold_head: str = Form("0"),
    live_sale_date: str = Form(""),
    live_sale_revenue: str = Form("0"),
    meat_sale_date: str = Form(""),
    meat_sale_revenue: str = Form("0"),
    freezer_lbs: str = Form("0"),
    payment_method: str = Form("cash"),
    notes: str = Form(""),
    user: dict = Depends(current_user),
):
    _require_batch_editor(user)
    try:
        bid = create_historical_batch_record(
            user["id"],
            {
                "name": name,
                "species": species,
                "start_date": start_date,
                "initial_quantity": initial_quantity,
                "unit_cost": unit_cost,
                "sale_unit": sale_unit,
                "harvest_date": harvest_date,
                "harvested_head": harvested_head,
                "dressed_lbs": dressed_lbs,
                "processing_cost": processing_cost,
                "feed_lbs": feed_lbs,
                "feed_cost": feed_cost,
                "losses": losses,
                "live_sold_head": live_sold_head,
                "live_sale_date": live_sale_date,
                "live_sale_revenue": live_sale_revenue,
                "meat_sale_date": meat_sale_date,
                "meat_sale_revenue": meat_sale_revenue,
                "freezer_lbs": freezer_lbs,
                "payment_method": payment_method,
                "notes": notes,
            },
        )
    except ValueError as exc:
        return RedirectResponse(
            "/batches/import?tab=quick&msg=" + quote_plus(f"Error: {exc}"),
            status_code=303,
        )
    return RedirectResponse(
        f"/batches/{int(bid)}?msg=" + quote_plus(f"Logged historical batch {name.strip()}."),
        status_code=303,
    )


@router.get("/batches/import/template.csv")
def bulk_batches_template_csv(user: dict = Depends(current_user)):
    _require_batch_editor(user)
    return Response(
        generate_batch_csv_template(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="homestead-historical-batches-template.csv"'},
    )


@router.post("/batches/import/csv-preview")
async def bulk_batches_csv_preview(
    request: Request,
    csv_file: UploadFile = File(...),
    user: dict = Depends(current_user),
):
    _require_batch_editor(user)
    raw = await csv_file.read()
    catalog = get_species_catalog(user_id=user["id"])
    try:
        preview = parse_batch_csv(raw, species_catalog=catalog)
        ctx = _bulk_batches_context(user, active_tab="csv", csv_preview=preview)
    except ValueError as exc:
        ctx = _bulk_batches_context(user, active_tab="csv", csv_error=str(exc))
    return _render(request, "bulk_batches.html", user, "batches", **ctx)


@router.post("/batches/import/csv-commit")
def bulk_batches_csv_commit(
    rows_json: str = Form("[]"),
    user: dict = Depends(current_user),
):
    _require_batch_editor(user)
    try:
        rows = json.loads(rows_json or "[]")
    except (ValueError, TypeError):
        return RedirectResponse(
            "/batches/import?tab=csv&msg=" + quote_plus("Error: Invalid batch import selection."),
            status_code=303,
        )
    if not isinstance(rows, list) or not rows:
        return RedirectResponse(
            "/batches/import?tab=csv&msg=" + quote_plus("Select at least one valid batch row to import."),
            status_code=303,
        )

    imported = 0
    for item in rows:
        if not isinstance(item, dict):
            continue
        try:
            create_historical_batch_record(user["id"], item)
            imported += 1
        except ValueError:
            continue

    unit_lbl = "batch" if imported == 1 else "batches"
    msg = f"Imported {imported} historical {unit_lbl}."
    return RedirectResponse("/batches?msg=" + quote_plus(msg), status_code=303)
