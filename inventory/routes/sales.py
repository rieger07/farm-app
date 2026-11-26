"""Sales ledger, Insights analytics, marketplace day tags, void/retag, and CSV export."""

import csv
from datetime import datetime, timedelta
import io
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse, Response

from inventory.insights import (
    list_sale_dates,
    marketplace_analytics,
    recommend_cash_drawer,
    suggest_pack_list,
)
from inventory.marketplaces import (
    WEATHER_OPTIONS,
    delete_market_day,
    get_market_day,
    list_marketplaces,
    save_market_day,
)
from inventory.sales import (
    count_sales,
    export_rows,
    market_insights,
    recent_sales,
    sales_summary,
    update_sale_payment_type,
    void_quick_sale,
)
from inventory.stock import fmt_qty
from inventory.web import (
    Ctx,
    _csv_safe,
    _parse_dollars_to_cents,
    _resolve_marketplace_windows,
    _sales_query,
    chart_buckets,
    local_tz,
    render,
    require,
    to_local,
    utc_bounds,
)

router = APIRouter()


@router.get("/sales")
def sales_page(
    request: Request,
    period: str = "today",
    source: str = "all",
    date_from: str = Query("", alias="from"),
    date_to: str = Query("", alias="to"),
    market_date: str = Query("", alias="date"),
    marketplace: str = Query(""),
    limit: int = 25,
    msg: str = "",
    ctx: Ctx = Depends(require("sales")),
):
    rng = _sales_query(period, date_from, date_to, market_date)
    src = None if source == "all" else source
    limit = max(25, min(int(limit), 500))
    mp_id, utc_windows = _resolve_marketplace_windows(ctx.merchant_id, marketplace, rng)
    summary = sales_summary(ctx.merchant_id, rng["since"], rng["until"], utc_windows=utc_windows)

    days = []
    for b in chart_buckets(rng["chart_start"], rng["chart_end"]):
        s0, s1 = utc_bounds(b["d0"], b["d1"])
        d = sales_summary(ctx.merchant_id, s0, s1, utc_windows=utc_windows)
        days.append({
            "label": b["label"],
            "title": b["title"],
            "card_cents": d["card_cents"],
            "digital_cents": d["digital_cents"],
            "cash_cents": d["cash_cents"],
            "zettle_card_cents": d["zettle_card_cents"],
            "zettle_cash_cents": d["zettle_cash_cents"],
            "quick_cash_cents": d["quick_cash_cents"],
        })
    peak = max((d["card_cents"] + d["digital_cents"] + d["cash_cents"] for d in days), default=0) or 1
    best = max(days, key=lambda d: d["card_cents"] + d["digital_cents"] + d["cash_cents"])

    txns = recent_sales(
        ctx.merchant_id, rng["since"], rng["until"], src, limit=limit, utc_windows=utc_windows
    )
    total_count = count_sales(
        ctx.merchant_id, rng["since"], rng["until"], src, utc_windows=utc_windows
    )
    today = datetime.now(local_tz()).date()
    for t in txns:
        local = to_local(t["occurred_at"])
        t["date_label"] = (
            "Today" if local.date() == today
            else "Yesterday" if local.date() == today - timedelta(days=1)
            else local.strftime("%b %-d")
        )
        t["time_label"] = local.strftime("%-I:%M %p").lower()
        t["full_label"] = local.strftime("%a %b %-d, %Y · %-I:%M %p").replace("AM", "am").replace("PM", "pm")

    active_day_str = rng["date_from"] if rng["period"] in ("today", "yesterday", "day") else ""
    active_market_day = get_market_day(ctx.merchant_id, active_day_str) if active_day_str else None
    marketplaces = list_marketplaces(ctx.merchant_id)

    export_qs = f"period={rng['period']}&source={source}"
    if rng["period"] == "custom":
        export_qs += f"&from={rng['date_from']}&to={rng['date_to']}"
    elif rng["period"] == "day":
        export_qs += f"&date={rng['market_date']}"
    if mp_id:
        export_qs += f"&marketplace={mp_id}"
    return render(
        request, "sales.html", ctx, "sales",
        summary=summary, days=days, peak=peak, best=best, txns=txns,
        period=rng["period"], period_label=rng["label"], source=source,
        date_from=rng["date_from"], date_to=rng["date_to"],
        market_date=rng["market_date"] or today.isoformat(),
        sale_dates=list_sale_dates(ctx.merchant_id, local_tz()),
        marketplaces=marketplaces,
        selected_marketplace=str(mp_id or ""),
        active_day_str=active_day_str,
        active_market_day=active_market_day,
        weather_options=WEATHER_OPTIONS,
        today_label=datetime.now(local_tz()).strftime("%-d %b %Y"),
        total_count=total_count, has_more=len(txns) < total_count, next_limit=limit + 25,
        export_qs=export_qs, weekly=(rng["chart_end"] - rng["chart_start"]).days > 31,
        msg=msg,
    )


@router.get("/insights")
def insights_page(
    request: Request,
    period: str = "quarter",
    date_from: str = Query("", alias="from"),
    date_to: str = Query("", alias="to"),
    market_date: str = Query("", alias="date"),
    marketplace: str = Query(""),
    msg: str = "",
    ctx: Ctx = Depends(require("insights")),
):
    rng = _sales_query(period, date_from, date_to, market_date)
    mp_id, utc_windows = _resolve_marketplace_windows(ctx.merchant_id, marketplace, rng)
    data = market_insights(ctx.merchant_id, rng["since"], rng["until"], utc_windows=utc_windows)
    summary = sales_summary(ctx.merchant_id, rng["since"], rng["until"], utc_windows=utc_windows)
    mp_analytics = marketplace_analytics(
        ctx.merchant_id, rng["date_from"], rng["date_to"], rng["since"], rng["until"], local_tz()
    )
    drawer = recommend_cash_drawer(
        ctx.merchant_id, rng["since"], rng["until"], local_tz(), utc_windows=utc_windows
    )
    pack_list = suggest_pack_list(
        ctx.merchant_id, rng["since"], rng["until"], local_tz(), utc_windows=utc_windows
    )
    marketplaces = list_marketplaces(ctx.merchant_id)
    selected_mp_name = next((m["name"] for m in marketplaces if str(m["id"]) == str(mp_id or "")), "")
    for so in data["sellouts"]:
        local = to_local(so["last_sold_at"])
        so["last_sold_label"] = (
            local.strftime("%b %-d · %-I:%M %p")
            .replace("AM", "am")
            .replace("PM", "pm")
        )
    active_day_str = rng["date_from"] if rng["period"] in ("today", "yesterday", "day") else ""
    active_market_day = get_market_day(ctx.merchant_id, active_day_str) if active_day_str else None
    return render(
        request, "insights.html", ctx, "insights",
        insights=data,
        summary=summary,
        mp_analytics=mp_analytics,
        drawer=drawer,
        pack_list=pack_list,
        marketplaces=marketplaces,
        selected_marketplace=str(mp_id or ""),
        selected_mp_name=selected_mp_name,
        active_day_str=active_day_str,
        active_market_day=active_market_day,
        weather_options=WEATHER_OPTIONS,
        period=rng["period"],
        period_label=rng["label"],
        date_from=rng["date_from"],
        date_to=rng["date_to"],
        market_date=rng["market_date"] or datetime.now(local_tz()).date().isoformat(),
        sale_dates=list_sale_dates(ctx.merchant_id, local_tz()),
        msg=msg,
    )


@router.post("/market-days/save")
def market_day_save(
    market_date: str = Form(...),
    marketplace_id: int = Form(...),
    booth_fee: str = Form(""),
    travel_cost: str = Form(""),
    weather: str = Form(""),
    notes: str = Form(""),
    next_url: str = Form("/insights"),
    ctx: Ctx = Depends(require("sales")),
):
    try:
        save_market_day(
            ctx.merchant_id,
            market_date,
            marketplace_id,
            booth_fee_cents=_parse_dollars_to_cents(booth_fee),
            travel_cost_cents=_parse_dollars_to_cents(travel_cost),
            weather=weather,
            notes=notes,
        )
        msg = f"Tagged {market_date} market day."
    except ValueError as exc:
        msg = f"Error: {exc}"
    target = next_url if next_url.startswith("/") else "/insights"
    sep = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{sep}msg={quote_plus(msg)}", status_code=303)


@router.post("/market-days/delete")
def market_day_delete(
    market_date: str = Form(...),
    next_url: str = Form("/insights"),
    ctx: Ctx = Depends(require("sales")),
):
    delete_market_day(ctx.merchant_id, market_date)
    target = next_url if next_url.startswith("/") else "/insights"
    sep = "&" if "?" in target else "?"
    return RedirectResponse(
        f"{target}{sep}msg={quote_plus(f'Removed market tag for {market_date}.')}",
        status_code=303,
    )


@router.post("/sales/{sale_id}/payment-type")
def sales_retag_payment_type(
    sale_id: int,
    payment_type: str = Form(...),
    next_url: str = Form("/sales"),
    ctx: Ctx = Depends(require("sales")),
):
    try:
        update_sale_payment_type(ctx.merchant_id, sale_id, payment_type)
        msg = f"Updated sale #{sale_id} to {payment_type.capitalize()}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    target = next_url if next_url.startswith("/") else "/sales"
    sep = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{sep}msg={quote_plus(msg)}", status_code=303)


@router.post("/sales/{sale_id}/void")
def sales_void_quick_sale(
    sale_id: int,
    restore_stock: str = Form(""),
    next_url: str = Form("/sales"),
    ctx: Ctx = Depends(require("sales")),
):
    try:
        res = void_quick_sale(
            ctx.merchant_id,
            sale_id,
            created_by=ctx.user["id"],
            restore_stock=bool(restore_stock),
        )
        stock_note = (
            f" and restored stock for {res['restored_items']} item(s)"
            if restore_stock and res["restored_items"]
            else " (stock unchanged)"
        )
        msg = f"Voided Quick sale #{sale_id}{stock_note}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    target = next_url if next_url.startswith("/") else "/sales"
    sep = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{sep}msg={quote_plus(msg)}", status_code=303)


@router.get("/sales/export.csv")
def sales_export(
    period: str = "today",
    source: str = "all",
    date_from: str = Query("", alias="from"),
    date_to: str = Query("", alias="to"),
    market_date: str = Query("", alias="date"),
    marketplace: str = Query(""),
    ctx: Ctx = Depends(require("sales")),
):
    rng = _sales_query(period, date_from, date_to, market_date)
    _, utc_windows = _resolve_marketplace_windows(ctx.merchant_id, marketplace, rng)
    rows = export_rows(
        ctx.merchant_id,
        rng["since"],
        rng["until"],
        None if source == "all" else source,
        utc_windows=utc_windows,
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["sale_id", "date", "time", "source", "payment_type", "status", "cashier",
                "item", "quantity", "unit_price", "line_subtotal", "sale_total"])
    dollars = lambda c: f"{c / 100:.2f}"
    for r in rows:
        local = to_local(r["occurred_at"])
        sign = -1 if r["status"] == "refunded" else 1
        qty, unit = r["quantity"], r["unit_price_cents"]
        w.writerow([
            r["sale_id"], local.strftime("%Y-%m-%d"), local.strftime("%H:%M:%S"),
            r["source"], r["payment_type"] or ("cash" if r["source"] == "cash" else ""),
            r["status"], r["cashier"] or "",
            _csv_safe(r["item"]), "" if qty is None else fmt_qty(qty),
            "" if unit is None else dollars(unit),
            "" if qty is None else dollars(round(sign * qty * unit)), dollars(r["total_cents"]),
        ])
    name = f"ironledger-sales-{rng['date_from']}_to_{rng['date_to']}.csv"
    return Response(
        buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
