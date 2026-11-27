"""Feed planner, unified financials, Schedule F tax books, and JSON backup download."""

import csv
from datetime import date
import io
import json
import secrets
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

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
    add_farm_expense,
    attach_farm_expense_receipt,
    delete_farm_expense,
    delete_receipt_group,
    get_google_drive_connection,
    list_recent_vendors,
)
from homestead.db.workspaces import can_access_homestead_page
from homestead.financials import (
    compute_purchasing_schedule,
    compute_roi_summary_table,
    compute_unified_financials_table,
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

    all_schedules = _schedules_for_user(user["id"], include_archived=False)
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
        bag_weight=get_bag_weight(user_id=user["id"]),
        layer_cfg=get_layer_settings(user["id"]),
        recent_vendors=list_recent_vendors(user["id"]),
        auto_open_receipt=bool(new),
        preset_mode=preset,
        preset_batch_id=batch_id,
        preset_species=species,
        schedules=schedules,
        include_planned=include_planned_flag,
        planned_count=planned_count,
        unified_rows=unified_rows,
        roi_rows=roi_rows,
        expense_categories=EXPENSE_CATEGORIES,
        gdrive_connected=bool(get_google_drive_connection(user["id"])),
        totals=totals,
        chart_peak=chart_peak,
        msg=msg,
    )


@router.post("/expenses/receipt")
async def expense_receipt_submit(
    receipt_date: str = Form(""),
    vendor: str = Form(""),
    receipt_url: str = Form(""),
    receipt_file: UploadFile | None = File(None),
    lines_json: str = Form("[]"),
    user: dict = Depends(current_user),
):
    _require_financials(user)
    rdate = receipt_date.strip()[:10] or date.today().isoformat()
    yr = rdate[:4] if len(rdate) >= 4 and rdate[:4].isdigit() else ""
    try:
        raw_lines = json.loads(lines_json or "[]")
    except (ValueError, TypeError):
        return _tax_redirect(yr, "Error: Invalid receipt line data.")
    if not isinstance(raw_lines, list):
        return _tax_redirect(yr, "Error: Invalid receipt line data.")

    valid_lines: list[dict] = []
    for item in raw_lines:
        if not isinstance(item, dict):
            continue
        try:
            amt = round(float(item.get("amount") or 0.0), 2)
        except (TypeError, ValueError):
            amt = 0.0
        if amt <= 0:
            continue
        kind = str(item.get("kind") or "general").strip()
        if kind not in ("batch_feed", "layer_feed", "general"):
            kind = "general"
        line = dict(item)
        line["kind"] = kind
        line["amount"] = amt
        if kind == "batch_feed":
            raw_bid = str(line.get("batch_id") or "").strip()
            if not raw_bid.isdigit():
                return _tax_redirect(
                    yr, "Error: Choose a batch for each Meat Batch Feed line."
                )
            _get_owned_batch(int(raw_bid), user)
            line["batch_id"] = int(raw_bid)
        elif kind == "general":
            raw_bid = str(line.get("batch_id") or "").strip()
            if raw_bid.isdigit():
                _get_owned_batch(int(raw_bid), user)
                line["batch_id"] = int(raw_bid)
            else:
                line["batch_id"] = None
        valid_lines.append(line)

    if not valid_lines:
        return _tax_redirect(
            yr, "Error: Add at least one line item with an amount greater than $0."
        )

    clean_vendor = vendor.strip()
    r_url, drive_id, warn = await _resolve_receipt_attachment(
        user["id"],
        receipt_file,
        receipt_url,
        rdate,
        clean_vendor or "Receipt",
    )
    group_id = secrets.token_hex(6) if len(valid_lines) > 1 else ""

    for line in valid_lines:
        kind = line["kind"]
        notes = str(line.get("notes") or "").strip()
        if kind == "batch_feed":
            add_batch_feed_log(
                line["batch_id"],
                rdate,
                str(line.get("feed_type") or "grower"),
                float(line.get("bags") or 0.0),
                float(line.get("feed_lbs") or 0.0),
                line["amount"],
                notes=notes,
                receipt_url=r_url,
                drive_file_id=drive_id,
                vendor=clean_vendor,
                receipt_group_id=group_id,
            )
        elif kind == "layer_feed":
            tags = line.get("species_tags") or "all"
            add_layer_feed_log(
                user["id"],
                feed_date=rdate,
                bags=float(line.get("bags") or 0.0),
                feed_lbs=float(line.get("feed_lbs") or 0.0),
                cost_total=line["amount"],
                notes=notes,
                receipt_url=r_url,
                drive_file_id=drive_id,
                feed_type=str(line.get("feed_type") or "layer"),
                species_tags=tags,
                vendor=clean_vendor,
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

    total_amt = sum(item["amount"] for item in valid_lines)
    if len(valid_lines) == 1:
        msg = f"Saved receipt (${total_amt:,.2f})."
    else:
        msg = f"Saved split receipt ({len(valid_lines)} items, ${total_amt:,.2f})."
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
    elif key == "processing":
        updated = attach_batch_harvest_receipt(entry_id, user["id"], r_url, drive_id)
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
