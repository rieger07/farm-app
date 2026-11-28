"""Feed planner, unified financials, Schedule F tax books, and JSON backup download."""

import csv
from datetime import date
import io
import json
import secrets
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response

from homestead.db.backup import export_backup_json
from homestead.db.batches import (
    add_batch_feed_log,
    attach_batch_feed_receipt,
    attach_batch_harvest_receipt,
    delete_batch_feed_log_for_user,
    list_batches,
)
from homestead.db.core import (
    get_bag_weight,
    get_barrel_capacity,
    set_bag_weight,
    set_barrel_capacity,
)
from homestead.db.eggs import (
    add_layer_feed_log,
    attach_layer_feed_receipt,
    compute_egg_dashboard,
    delete_layer_feed_log,
    get_layer_settings,
)
from homestead.db.expenses import (
    EXPENSE_CATEGORIES,
    EXPENSE_CATEGORY_OPTIONS,
    add_farm_expense,
    attach_farm_expense_receipt,
    delete_farm_expense,
    delete_receipt_group,
    get_google_drive_connection,
    get_receipt_for_edit,
    list_recent_vendors,
    replace_receipt_entries,
)
from homestead.db.fiber import attach_fiber_mill_lot_receipt
from homestead.db.herds import (
    add_herd_feed_log,
    attach_herd_feed_receipt,
    delete_herd_feed_log,
    list_herds,
)
from homestead.db.workspaces import can_access_homestead_page
from homestead.financials import (
    compute_purchasing_schedule,
    compute_roi_summary_table,
    compute_unified_financials_table,
)
from homestead.receipt_allocation import (
    BatchOwnershipError,
    build_receipt_batch_options,
    expand_receipt_lines,
)
from homestead.receipt_ocr import (
    MAX_OCR_FILE_BYTES,
    ReceiptOCRError,
    detect_receipt_mime_type,
    is_receipt_ocr_enabled,
    parse_receipt_with_gemini,
)
from homestead.scheduling import compute_combined_feed_schedule
from homestead.tax_books import build_unified_tax_books
from homestead.web import (
    _get_owned_batch,
    _render,
    _resolve_receipt_attachment,
    _schedules_for_user,
    current_user,
)

router = APIRouter()


def _csv_safe(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _tax_redirect(year: str, msg: str, tab: str = "taxes") -> RedirectResponse:
    clean_yr = str(year or "").strip()
    yq = f"&year={quote_plus(clean_yr)}" if clean_yr else ""
    return RedirectResponse(
        f"/financials?tab={quote_plus(tab)}{yq}&msg={quote_plus(msg)}",
        status_code=303,
    )


def _require_financials(user: dict) -> None:
    if not can_access_homestead_page(user.get("role") or "owner", "financials"):
        raise HTTPException(
            status_code=403, detail="Your role does not have access to Financials."
        )


@router.get("/feed")
def feed_page(request: Request, msg: str = "", user: dict = Depends(current_user)):
    barrel_cap = get_barrel_capacity(user_id=user["id"])
    bag_wt = get_bag_weight(user_id=user["id"])
    schedules = _schedules_for_user(user["id"], include_archived=False)
    weekly_feed, monthly_feed, feed_stats = compute_combined_feed_schedule(
        schedules, barrel_capacity=barrel_cap
    )
    purchase_events, _ = compute_purchasing_schedule(schedules, bag_weight=bag_wt)
    today_iso = date.today().isoformat()
    next_feed = next(
        (e for e in purchase_events if e["Feed Bags"] > 0 and e["Date"] >= today_iso),
        next((e for e in purchase_events if e["Feed Bags"] > 0), None),
    )
    return _render(
        request,
        "feed.html",
        user,
        "feed",
        barrel_capacity=barrel_cap,
        bag_weight=bag_wt,
        feed_stats=feed_stats,
        next_feed=next_feed,
        purchase_events=purchase_events,
        monthly_feed=monthly_feed,
        weekly_feed=weekly_feed,
        egg_dash=compute_egg_dashboard(user["id"], bag_weight=bag_wt),
        msg=msg,
    )


@router.post("/feed/settings")
def feed_settings_submit(
    barrel_capacity: int = Form(...),
    bag_weight: int = Form(...),
    user: dict = Depends(current_user),
):
    set_barrel_capacity(max(1, barrel_capacity), user_id=user["id"])
    set_bag_weight(max(1, bag_weight), user_id=user["id"])
    return RedirectResponse(
        "/feed?msg=" + quote_plus("Updated feed container settings."),
        status_code=303,
    )


@router.get("/financials")
def financials_page(
    request: Request,
    msg: str = "",
    tab: str = "",
    year: str = "",
    include_planned: str = "",
    new: int = 0,
    preset: str = "",
    batch_id: str = "",
    herd_id: str = "",
    species: str = "",
    user: dict = Depends(current_user),
):
    _require_financials(user)
    if tab in ("taxes", "economics"):
        active_tab = tab
    elif include_planned != "":
        active_tab = "economics"
    else:
        active_tab = "taxes"
    include_planned_flag = False if str(include_planned).strip() == "0" else True

    all_user_schedules = _schedules_for_user(user["id"], include_archived=True)
    all_schedules = [s for s in all_user_schedules if s["batch"].get("active", 1)]
    receipt_batches = build_receipt_batch_options(
        all_user_schedules
        if include_planned_flag
        else [s for s in all_user_schedules if not s["is_planned"]]
    )
    planned_count = sum(1 for s in all_schedules if s["is_planned"])
    schedules = (
        all_schedules
        if include_planned_flag
        else [s for s in all_schedules if not s["is_planned"]]
    )
    unified_rows = compute_unified_financials_table(schedules)
    roi_rows = compute_roi_summary_table(schedules)

    merchant_id = user.get("merchant_id")
    all_books = build_unified_tax_books(
        user["id"], merchant_id=merchant_id, tax_year=None
    )
    yr_raw = year.strip()
    if yr_raw.lower() == "all":
        selected_year = None
        selected_year_str = "all"
    elif yr_raw.isdigit() and len(yr_raw) == 4:
        selected_year = int(yr_raw)
        selected_year_str = yr_raw
    else:
        cur_yr = date.today().year
        entry_years = [
            int(e["date"][:4])
            for e in all_books["entries"]
            if len(e["date"]) >= 4 and e["date"][:4].isdigit()
        ]
        if not entry_years or cur_yr in entry_years:
            selected_year = cur_yr
        else:
            selected_year = max(entry_years)
        selected_year_str = str(selected_year)

    tax_books = (
        all_books
        if selected_year is None
        else build_unified_tax_books(
            user["id"], merchant_id=merchant_id, tax_year=selected_year
        )
    )

    totals = {
        "est_rev": sum(s["estimated_revenue"] for s in schedules),
        "total_cost": sum(s["total_budget_cost"] for s in schedules),
        "est_profit": sum(s["estimated_profit"] for s in schedules),
        "cash_sales": sum(s["actual_sales_revenue"] for s in schedules),
        "other_expenses": tax_books["totals"]["total_expenses"],
    }
    chart_peak = (
        max(
            (max(s["total_budget_cost"], s["estimated_revenue"]) for s in schedules),
            default=1,
        )
        or 1
    )
    return _render(
        request,
        "financials.html",
        user,
        "financials",
        active_tab=active_tab,
        selected_year_str=selected_year_str,
        tax_books=tax_books,
        all_batches=(
            list_batches(user_id=user["id"])
            if include_planned_flag
            else [s["batch"] for s in schedules]
        ),
        receipt_batches=receipt_batches,
        all_herds=list_herds(user["id"], include_inactive=True),
        bag_weight=get_bag_weight(user_id=user["id"]),
        layer_cfg=get_layer_settings(user["id"]),
        recent_vendors=list_recent_vendors(user["id"]),
        auto_open_receipt=bool(new),
        preset_mode=preset,
        preset_batch_id=batch_id,
        preset_herd_id=herd_id,
        preset_species=species,
        schedules=schedules,
        include_planned=include_planned_flag,
        planned_count=planned_count,
        unified_rows=unified_rows,
        roi_rows=roi_rows,
        expense_categories=EXPENSE_CATEGORIES,
        expense_category_options=EXPENSE_CATEGORY_OPTIONS,
        ocr_enabled=is_receipt_ocr_enabled(),
        gdrive_connected=bool(get_google_drive_connection(user["id"])),
        totals=totals,
        chart_peak=chart_peak,
        msg=msg,
    )


@router.post("/api/receipts/parse")
async def api_receipt_parse(
    file: UploadFile = File(...),
    user: dict = Depends(current_user),
):
    _require_financials(user)
    if not is_receipt_ocr_enabled():
        return JSONResponse(
            {"error": "Receipt OCR is not configured (set GEMINI_API_KEY)."},
            status_code=503,
        )
    try:
        mime_type = detect_receipt_mime_type(file.content_type, file.filename)
    except ValueError:
        return JSONResponse(
            {"error": "Unsupported file format. Upload a JPEG/PNG/WebP image or PDF receipt."},
            status_code=400,
        )

    raw_bytes = await file.read()
    if not raw_bytes:
        return JSONResponse({"error": "Uploaded receipt file is empty."}, status_code=400)
    if len(raw_bytes) > MAX_OCR_FILE_BYTES:
        return JSONResponse(
            {"error": "Receipt file exceeds the 12 MB size limit."},
            status_code=413,
        )

    try:
        parsed = await parse_receipt_with_gemini(raw_bytes, mime_type)
        return JSONResponse(parsed, status_code=200)
    except ReceiptOCRError as exc:
        return JSONResponse({"error": exc.message}, status_code=exc.status_code)


@router.get("/api/receipts/entry")
def api_receipt_entry_get(
    source_key: str = "",
    entry_id: str = "",
    receipt_group_id: str = "",
    user: dict = Depends(current_user),
):
    _require_financials(user)
    parsed_eid = int(entry_id) if str(entry_id).strip().isdigit() else None
    payload = get_receipt_for_edit(
        user["id"],
        receipt_group_id=receipt_group_id,
        source_key=source_key,
        entry_id=parsed_eid,
    )
    if not payload:
        return JSONResponse({"error": "Receipt entry not found."}, status_code=404)
    return JSONResponse(payload, status_code=200)


@router.post("/expenses/receipt")
async def expense_receipt_submit(
    receipt_date: str = Form(""),
    vendor: str = Form(""),
    receipt_url: str = Form(""),
    receipt_file: UploadFile | None = File(None),
    lines_json: str = Form("[]"),
    edit_mode: str = Form(""),
    orig_group_id: str = Form(""),
    orig_source_key: str = Form(""),
    orig_entry_id: str = Form(""),
    keep_existing_receipt: str = Form("1"),
    user: dict = Depends(current_user),
):
    _require_financials(user)
    rdate = receipt_date.strip()[:10] or date.today().isoformat()
    yr = rdate[:4] if len(rdate) >= 4 and rdate[:4].isdigit() else ""
    try:
        raw_lines = json.loads(lines_json or "[]")
    except (ValueError, TypeError):
        return _tax_redirect(yr, "Error: Invalid receipt line data.")

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
        return _tax_redirect(yr, f"Error: {exc}")

    clean_vendor = vendor.strip()
    clean_edit_mode = edit_mode.strip()
    parsed_orig_eid = int(orig_entry_id) if str(orig_entry_id).strip().isdigit() else None
    orig_payload = (
        get_receipt_for_edit(
            user["id"],
            receipt_group_id=orig_group_id,
            source_key=orig_source_key,
            entry_id=parsed_orig_eid,
        )
        if clean_edit_mode in ("single", "group")
        else None
    )

    if (receipt_file and receipt_file.filename) or receipt_url.strip():
        r_url, drive_id, warn = await _resolve_receipt_attachment(
            user["id"],
            receipt_file,
            receipt_url,
            rdate,
            clean_vendor or "Receipt",
        )
        if (
            not r_url
            and orig_payload
            and str(keep_existing_receipt).strip() != "0"
        ):
            r_url = orig_payload["receipt_url"]
            drive_id = orig_payload["drive_file_id"]
    elif orig_payload and str(keep_existing_receipt).strip() != "0":
        r_url = orig_payload["receipt_url"]
        drive_id = orig_payload["drive_file_id"]
        warn = None
    else:
        r_url, drive_id, warn = "", "", None

    if clean_edit_mode in ("single", "group"):
        if not orig_payload:
            return _tax_redirect(yr, "Error: Original receipt entry not found.")
        try:
            saved_count, total_amt = replace_receipt_entries(
                user["id"],
                edit_mode=clean_edit_mode,
                orig_group_id=orig_group_id,
                orig_source_key=orig_source_key,
                orig_entry_id=parsed_orig_eid,
                receipt_date=rdate,
                vendor=clean_vendor,
                receipt_url=r_url,
                drive_file_id=drive_id,
                expanded_lines=expanded_lines,
            )
        except ValueError as exc:
            return _tax_redirect(yr, f"Error: {exc}")
        action_verb = "Updated"
    else:
        group_id = secrets.token_hex(6) if len(expanded_lines) > 1 else ""
        for line in expanded_lines:
            kind = line["kind"]
            notes = str(line.get("notes") or "").strip()
            if kind == "batch_feed":
                add_batch_feed_log(
                    line["batch_id"],
                    rdate,
                    str(line.get("feed_type") or "grower"),
                    line["bags"],
                    line["feed_lbs"],
                    line["amount"],
                    notes=notes,
                    receipt_url=r_url,
                    drive_file_id=drive_id,
                    vendor=clean_vendor,
                    receipt_group_id=group_id,
                )
            elif kind == "layer_feed":
                add_layer_feed_log(
                    user["id"],
                    feed_date=rdate,
                    bags=line["bags"],
                    feed_lbs=line["feed_lbs"],
                    cost_total=line["amount"],
                    notes=notes,
                    receipt_url=r_url,
                    drive_file_id=drive_id,
                    feed_type=str(line.get("feed_type") or "layer"),
                    species_tags=line.get("species_tags") or "all",
                    vendor=clean_vendor,
                    receipt_group_id=group_id,
                )
            elif kind == "herd_feed":
                add_herd_feed_log(
                    user["id"],
                    herd_id=line["herd_id"],
                    feed_date=rdate,
                    feed_category=line["feed_category"],
                    quantity=line["quantity"],
                    unit=line["unit"],
                    weight_lbs=line["weight_lbs"],
                    cost_total=line["amount"],
                    vendor=clean_vendor,
                    notes=notes,
                    receipt_url=r_url,
                    drive_file_id=drive_id,
                    receipt_group_id=group_id,
                )
            else:
                add_farm_expense(
                    user["id"],
                    rdate,
                    clean_vendor,
                    str(line.get("category") or "Other"),
                    line["amount"],
                    batch_id=line["batch_id"],
                    notes=notes,
                    receipt_url=r_url,
                    drive_file_id=drive_id,
                    receipt_group_id=group_id,
                )
        saved_count = len(expanded_lines)
        total_amt = round(sum(item["amount"] for item in expanded_lines), 2)
        action_verb = "Saved"

    total_lbl = f"-${abs(total_amt):,.2f}" if total_amt < 0 else f"${total_amt:,.2f}"
    if saved_count == 1:
        msg = f"{action_verb} receipt ({total_lbl})."
    else:
        msg = f"{action_verb} split receipt ({saved_count} items, {total_lbl})."
    if warn:
        msg += f" (Receipt not uploaded: {warn})"
    return _tax_redirect(yr, msg)


@router.post("/expenses/add")
async def expense_add_submit(
    expense_date: str = Form(""),
    vendor: str = Form(""),
    category: str = Form("Other"),
    amount: float = Form(0.0),
    batch_id: str = Form(""),
    notes: str = Form(""),
    receipt_url: str = Form(""),
    receipt_file: UploadFile | None = File(None),
    user: dict = Depends(current_user),
):
    _require_financials(user)
    edate = expense_date.strip() or date.today().isoformat()
    bid = int(batch_id) if batch_id.strip().isdigit() else None
    if bid is not None:
        _get_owned_batch(bid, user)
    r_url, drive_id, warn = await _resolve_receipt_attachment(
        user["id"], receipt_file, receipt_url, edate, vendor or category
    )
    add_farm_expense(
        user["id"],
        edate,
        vendor,
        category,
        amount,
        batch_id=bid,
        notes=notes,
        receipt_url=r_url,
        drive_file_id=drive_id,
    )
    msg = "Saved farm expense."
    if warn:
        msg += f" (Receipt not uploaded: {warn})"
    yr = edate[:4] if len(edate) >= 4 and edate[:4].isdigit() else ""
    return _tax_redirect(yr, msg)


@router.post("/expenses/entry/delete")
def expense_entry_delete_submit(
    source_key: str = Form(...),
    entry_id: int = Form(...),
    year: str = Form(""),
    user: dict = Depends(current_user),
):
    _require_financials(user)
    key = source_key.strip()
    if key == "general":
        delete_farm_expense(entry_id, user["id"])
    elif key == "batch_feed":
        delete_batch_feed_log_for_user(entry_id, user["id"])
    elif key == "layer_feed":
        delete_layer_feed_log(user["id"], entry_id)
    elif key == "herd_feed":
        delete_herd_feed_log(user["id"], entry_id)
    else:
        return _tax_redirect(year, "Error: This entry cannot be deleted from Tax Books.")
    return _tax_redirect(year, "Removed expense entry.")


@router.post("/expenses/group/delete")
def expense_group_delete_submit(
    receipt_group_id: str = Form(...),
    year: str = Form(""),
    user: dict = Depends(current_user),
):
    _require_financials(user)
    removed = delete_receipt_group(user["id"], receipt_group_id)
    msg = f"Removed split receipt ({removed} line{'s' if removed != 1 else ''})."
    return _tax_redirect(year, msg)


@router.post("/expenses/{expense_id}/delete")
def expense_delete_submit(expense_id: int, user: dict = Depends(current_user)):
    _require_financials(user)
    delete_farm_expense(expense_id, user["id"])
    return RedirectResponse(
        "/financials?tab=taxes&msg=" + quote_plus("Expense removed."), status_code=303
    )


@router.post("/expenses/attach-receipt")
async def expense_attach_receipt_submit(
    source_key: str = Form(...),
    entry_id: int = Form(...),
    entry_date: str = Form(""),
    label: str = Form("Receipt"),
    receipt_url: str = Form(""),
    receipt_file: UploadFile | None = File(None),
    year: str = Form(""),
    user: dict = Depends(current_user),
):
    _require_financials(user)
    edate = entry_date.strip()[:10] or date.today().isoformat()
    r_url, drive_id, warn = await _resolve_receipt_attachment(
        user["id"], receipt_file, receipt_url, edate, label or "Receipt"
    )
    if not r_url:
        detail = f"Error: Receipt not attached ({warn})" if warn else "Error: Choose a receipt photo/PDF or enter a receipt URL."
        return _tax_redirect(year, detail)

    key = source_key.strip()
    updated = False
    if key == "general":
        updated = attach_farm_expense_receipt(entry_id, user["id"], r_url, drive_id)
    elif key == "batch_feed":
        updated = attach_batch_feed_receipt(entry_id, user["id"], r_url, drive_id)
    elif key == "layer_feed":
        updated = attach_layer_feed_receipt(entry_id, user["id"], r_url, drive_id)
    elif key == "herd_feed":
        updated = attach_herd_feed_receipt(entry_id, user["id"], r_url, drive_id)
    elif key == "processing":
        updated = attach_batch_harvest_receipt(entry_id, user["id"], r_url, drive_id)
    elif key == "fiber_mill":
        updated = attach_fiber_mill_lot_receipt(entry_id, user["id"], r_url, drive_id)
    if not updated:
        return _tax_redirect(year, "Error: Expense entry not found.")
    return _tax_redirect(year, "Attached receipt.")


@router.get("/financials/expenses.csv")
def financials_expenses_csv(year: str = "", user: dict = Depends(current_user)):
    _require_financials(user)
    yr_clean = year.strip()
    tax_year = int(yr_clean) if yr_clean.isdigit() and len(yr_clean) == 4 else None
    books = build_unified_tax_books(
        user["id"], merchant_id=user.get("merchant_id"), tax_year=tax_year
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "Date",
            "Schedule F Line",
            "Category",
            "Source",
            "Vendor",
            "Allocation / Batch",
            "Amount ($)",
            "Notes",
            "Receipt URL",
        ]
    )
    for entry in books["entries"]:
        writer.writerow(
            [
                _csv_safe(entry["date"]),
                _csv_safe(entry["schedule_f_code"]),
                _csv_safe(entry["category"]),
                _csv_safe(entry["source_label"]),
                _csv_safe(entry["vendor"]),
                _csv_safe(entry["allocation_label"]),
                f"{entry['amount']:.2f}",
                _csv_safe(entry["notes"]),
                _csv_safe(entry["receipt_url"]),
            ]
        )
    fname = (
        f"homestead-schedule-f-{tax_year}.csv"
        if tax_year
        else "homestead-expenses-schedule-f.csv"
    )
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/backup.json")
def backup_download(user: dict = Depends(current_user)):
    data = export_backup_json(user_id=user["id"])
    return Response(
        data,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="homestead-backup.json"'},
    )
