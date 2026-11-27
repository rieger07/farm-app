"""Stock counts, SKU ledger adjustments, and stock history routes."""

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from inventory.stock import (
    add_ledger_entry,
    delete_manual_stock_entry,
    fmt_qty,
    list_sku_history,
    list_stock,
    set_low_stock_threshold,
    set_quick_sale_visibility,
    set_sell_mode,
    set_stock_count,
)
from inventory.web import Ctx, render, require, to_local

router = APIRouter()


_STOCK_PRIORITY = {
    "count_needed": 0,
    "out_of_stock": 1,
    "low": 2,
    "in_stock": 3,
}


@router.get("/inventory")
def inventory_page(
    request: Request, msg: str = "", ctx: Ctx = Depends(require("inventory"))
):
    rows = list_stock(ctx.merchant_id)
    rows.sort(key=lambda r: _STOCK_PRIORITY.get(r["status"], 9))
    return render(
        request, "inventory.html", ctx, "inventory",
        rows=rows,
        units=fmt_qty(sum(max(r["on_hand"], 0) for r in rows if r.get("sell_mode") != "lb_stock_lb")),
        lbs=fmt_qty(sum(max(r["on_hand"], 0) for r in rows if r.get("sell_mode") == "lb_stock_lb")),
        low=sum(1 for r in rows if r["status"] in ("low", "out_of_stock", "count_needed")),
        categories=sorted({r["category"] for r in rows if r["category"]}),
        msg=msg,
    )


@router.post("/inventory/stock")
def inventory_update_stock(
    sku_id: int = Form(...),
    mode: str = Form("set"),
    quantity: float = Form(...),
    low_stock_threshold: float = Form(10),
    sell_mode: str = Form(""),
    quick_sale_present: str = Form(""),
    show_in_quick_sale: bool = Form(False),
    note: str = Form(""),
    ctx: Ctx = Depends(require("inventory")),
):
    try:
        set_low_stock_threshold(ctx.merchant_id, sku_id, low_stock_threshold)
        if sell_mode:
            set_sell_mode(ctx.merchant_id, sku_id, sell_mode)
        if quick_sale_present:
            set_quick_sale_visibility(ctx.merchant_id, sku_id, show_in_quick_sale)
        qty = fmt_qty(quantity)
        unit_lbl = "lb" if sell_mode == "lb_stock_lb" else "units"
        if mode == "set":
            set_stock_count(
                ctx.merchant_id, sku_id, qty, note=note, created_by=ctx.user["id"]
            )
            msg = f"Set stock count to {qty} {unit_lbl}."
        elif mode == "restock":
            if qty != 0:
                add_ledger_entry(
                    ctx.merchant_id,
                    sku_id,
                    abs(qty),
                    "restock",
                    note=note or "Restock",
                    created_by=ctx.user["id"],
                )
            msg = f"Added {abs(qty)} {unit_lbl} to stock."
        else:
            if qty != 0:
                add_ledger_entry(
                    ctx.merchant_id,
                    sku_id,
                    qty,
                    "adjustment",
                    note=note or "Adjustment",
                    created_by=ctx.user["id"],
                )
            msg = f"Adjusted stock by {qty:+} {unit_lbl}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/inventory?msg={quote_plus(msg)}", status_code=303)


@router.get("/api/stock/{sku_id}/history")
def api_stock_history(sku_id: int, ctx: Ctx = Depends(require("inventory"))):
    rows = list_sku_history(ctx.merchant_id, sku_id, limit=8)
    for r in rows:
        r["local_time"] = (
            to_local(r["created_at"])
            .strftime("%b %-d · %-I:%M %p")
            .replace("AM", "am")
            .replace("PM", "pm")
        )
    return {"entries": rows}


@router.post("/inventory/stock/ledger/{entry_id}/delete")
def inventory_delete_stock_entry(
    entry_id: int,
    ctx: Ctx = Depends(require("inventory")),
):
    try:
        res = delete_manual_stock_entry(ctx.merchant_id, entry_id)
        msg = f"Removed stock ledger entry #{res['entry_id']}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/inventory?msg={quote_plus(msg)}", status_code=303)
