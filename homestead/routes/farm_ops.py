"""Feed planner, unified financials, farm expenses ledger, and JSON backup download."""

import csv
from datetime import date
import io
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from homestead.db.backup import export_backup_json
from homestead.db.batches import (
    list_batch_feed_logs,
    list_batch_harvests,
    list_batches,
)
from homestead.db.core import (
    get_bag_weight,
    get_barrel_capacity,
    set_bag_weight,
    set_barrel_capacity,
)
from homestead.db.eggs import compute_egg_dashboard
from homestead.db.expenses import (
    EXPENSE_CATEGORIES,
    add_farm_expense,
    delete_farm_expense,
    get_google_drive_connection,
    list_farm_expenses,
)
from homestead.financials import (
    compute_purchasing_schedule,
    compute_roi_summary_table,
    compute_unified_financials_table,
)
from homestead.scheduling import compute_combined_feed_schedule
from homestead.web import (
    _get_owned_batch,
    _render,
    _resolve_receipt_attachment,
    _schedules_for_user,
    current_user,
)

router = APIRouter()


@router.get("/feed")
def feed_page(request: Request, msg: str = "", user: dict = Depends(current_user)):
    barrel_cap = get_barrel_capacity(user_id=user["id"])
    bag_wt = get_bag_weight(user_id=user["id"])
    schedules = _schedules_for_user(user["id"], include_archived=False)
    weekly_feed, monthly_feed, feed_stats = compute_combined_feed_schedule(schedules, barrel_capacity=barrel_cap)
    purchase_events, _ = compute_purchasing_schedule(schedules, bag_weight=bag_wt)
    today_iso = date.today().isoformat()
    next_feed = next(
        (e for e in purchase_events if e["Feed Bags"] > 0 and e["Date"] >= today_iso),
        next((e for e in purchase_events if e["Feed Bags"] > 0), None),
    )
    return _render(
        request, "feed.html", user, "feed",
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
    return RedirectResponse("/feed?msg=" + quote_plus("Updated feed container settings."), status_code=303)


@router.get("/financials")
def financials_page(
    request: Request,
    msg: str = "",
    include_planned: int = 1,
    user: dict = Depends(current_user),
):
    all_schedules = _schedules_for_user(user["id"], include_archived=False)
    planned_count = sum(1 for s in all_schedules if s["is_planned"])
    schedules = all_schedules if include_planned else [s for s in all_schedules if not s["is_planned"]]
    unified_rows = compute_unified_financials_table(schedules)
    roi_rows = compute_roi_summary_table(schedules)
    expenses = list_farm_expenses(user["id"])
    totals = {
        "est_rev": sum(s["estimated_revenue"] for s in schedules),
        "total_cost": sum(s["total_budget_cost"] for s in schedules),
        "est_profit": sum(s["estimated_profit"] for s in schedules),
        "cash_sales": sum(s["actual_sales_revenue"] for s in schedules),
        "other_expenses": sum(float(e["amount"]) for e in expenses),
    }
    chart_peak = max(
        (max(s["total_budget_cost"], s["estimated_revenue"]) for s in schedules),
        default=1,
    ) or 1
    return _render(
        request, "financials.html", user, "financials",
        schedules=schedules,
        include_planned=bool(include_planned),
        planned_count=planned_count,
        unified_rows=unified_rows,
        roi_rows=roi_rows,
        expenses=expenses,
        expense_categories=EXPENSE_CATEGORIES,
        gdrive_connected=bool(get_google_drive_connection(user["id"])),
        totals=totals,
        chart_peak=chart_peak,
        msg=msg,
    )


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
    return RedirectResponse("/financials?msg=" + quote_plus(msg), status_code=303)


@router.post("/expenses/{expense_id}/delete")
def expense_delete_submit(expense_id: int, user: dict = Depends(current_user)):
    delete_farm_expense(expense_id, user["id"])
    return RedirectResponse("/financials?msg=" + quote_plus("Expense removed."), status_code=303)


@router.get("/financials/expenses.csv")
def financials_expenses_csv(user: dict = Depends(current_user)):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date", "Source", "Category", "Vendor / Batch", "Amount ($)", "Notes", "Receipt URL"])
    for exp in list_farm_expenses(user["id"]):
        vendor_label = exp["vendor"] or exp["batch_name"] or ""
        writer.writerow([
            exp["expense_date"],
            "Farm Expense",
            exp["category"],
            vendor_label,
            f"{float(exp['amount']):.2f}",
            exp["notes"] or "",
            exp["receipt_url"] or "",
        ])
    for b in list_batches(user_id=user["id"]):
        for fl in list_batch_feed_logs(b["id"]):
            writer.writerow([
                fl["feed_date"],
                "Batch Feed",
                "Feed",
                f"{b['name']} ({fl['feed_type']})",
                f"{float(fl['cost_total']):.2f}",
                fl["notes"] or "",
                fl.get("receipt_url") or "",
            ])
        for hv in list_batch_harvests(b["id"]):
            if float(hv.get("processing_cost_total") or 0) > 0:
                writer.writerow([
                    hv["harvest_date"],
                    "Harvest Processing",
                    "Processing",
                    b["name"],
                    f"{float(hv['processing_cost_total']):.2f}",
                    hv["notes"] or "",
                    hv.get("receipt_url") or "",
                ])
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="homestead-expenses-schedule-f.csv"'},
    )


@router.get("/backup.json")
def backup_download(user: dict = Depends(current_user)):
    data = export_backup_json(user_id=user["id"])
    return Response(
        data, media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="homestead-backup.json"'},
    )
