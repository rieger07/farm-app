"""Offline-first Quick Sale register, cash/digital checkout API, and PWA manifest/SW."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from inventory.market_day import compute_market_day_status
from inventory.merchants import get_payment_handles
from inventory.sales import record_cash_sale
from inventory.stock import list_stock
from inventory.web import BASE, Ctx, local_tz, render, require

router = APIRouter()


@router.get("/quick-sale")
def quick_sale_page(request: Request, ctx: Ctx = Depends(require("quick_sale"))):
    tz = local_tz()
    today_iso = datetime.now(tz).date().isoformat()
    md = compute_market_day_status(ctx.merchant_id, today_iso, tz)
    live_booth = md["state"] == "live" and md["packed_sku_count"] > 0
    booth_by_sku = {
        it["sku_id"]: it for it in md["skus"]
    } if live_booth else {}

    skus = []
    for r in list_stock(ctx.merchant_id):
        if not r.get("show_in_quick_sale", 1):
            continue
        b_it = booth_by_sku.get(r["id"])
        skus.append(
            {
                "id": r["id"],
                "name": r["name"],
                "variant": r["variant_name"],
                "category": r["category"],
                "price_cents": r["price_cents"],
                "on_hand": r["on_hand"],
                "sell_mode": r.get("sell_mode", "unit"),
                "booth_packed": bool(b_it and b_it["is_packed"]),
                "booth_brought": b_it["brought_qty"] if b_it else None,
                "booth_left": b_it["left_at_booth"] if (b_it and b_it["is_packed"]) else None,
            }
        )

    active_booth_banner = (
        {
            "active": True,
            "market_date": today_iso,
            "marketplace_name": md["market_day"]["marketplace_name"],
            "packed_sku_count": md["packed_sku_count"],
            "gross_sales_cents": md["gross_sales_cents"],
            "booth_cost_cents": md["booth_cost_cents"],
            "breakeven_pct": md["breakeven_pct"],
            "overall_sell_through_pct": md["overall_sell_through_pct"],
        }
        if live_booth
        else {"active": False}
    )

    return render(
        request, "quick_sale.html", ctx, "quick_sale",
        skus=skus, categories=sorted({s["category"] for s in skus if s["category"]}),
        payment_handles=get_payment_handles(ctx.merchant_id),
        active_booth=active_booth_banner,
    )


@router.post("/api/cash-sale")
async def api_cash_sale(request: Request, ctx: Ctx = Depends(require("quick_sale"))):
    body = await request.json()
    try:
        result = record_cash_sale(
            ctx.merchant_id,
            str(body.get("client_uuid", "")),
            body.get("items", []),
            cash_received_cents=body.get("cash_received_cents"),
            created_by=ctx.user["id"],
            occurred_at=body.get("occurred_at"),
            payment_type=str(body.get("payment_type") or "cash"),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result


@router.get("/sw.js")
def service_worker():
    return FileResponse(BASE / "static" / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@router.get("/manifest.webmanifest")
def manifest():
    return FileResponse(BASE / "static" / "manifest.webmanifest",
                        media_type="application/manifest+json")
