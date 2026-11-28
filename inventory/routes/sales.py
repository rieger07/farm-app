"""Sales ledger, Insights analytics, marketplace day tags, void/retag, and CSV export."""

import csv
from datetime import date, datetime, timedelta
import io
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse, Response

from inventory.insights import (
    list_sale_dates,
    marketplace_analytics,
    recommend_cash_drawer,
    suggest_pack_list,
)
from inventory.market_day import delete_market_day_safely
from inventory.marketplaces import (
    WEATHER_OPTIONS,
    get_market_day,
    list_marketplaces,
    save_market_day,
)
from inventory.sales import (
    count_sales,
    delete_quick_sale,
    exclude_zettle_sale,
    export_rows,
    market_insights,
    recent_sales,
    restore_sale,
    sales_summary,
    update_quick_sale,
    update_sale_payment_type,
    void_quick_sale,
)
from inventory.web import local_input_to_utc_iso
from shared.bridge import (
    list_homestead_paid_transactions,
    update_homestead_transaction_payment_method,
)
from shared.web import safe_redirect
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


def _filter_homestead_txns(hs_txns: list[dict], source: str) -> list[dict]:
    if source in ("all", "homestead"):
        return list(hs_txns)
    if source in ("cash", "venmo", "paypal", "card", "check"):
        return [t for t in hs_txns if t["pay_method"] == source]
    if source == "digital":
        return [t for t in hs_txns if t["pay_method"] in ("venmo", "paypal")]
    if source == "unset":
        return [t for t in hs_txns if not t["pay_method"]]
    return []


def _merge_homestead_into_summary(summary: dict, hs_txns: list[dict]) -> None:
    if not hs_txns:
        return
    hs_cents = sum(int(t["total_cents"] or 0) for t in hs_txns)
    hs_count = len(hs_txns)
    hs_units = sum(int(t["units"] or 0) for t in hs_txns)
    summary["homestead_count"] = summary.get("homestead_count", 0) + hs_count
    summary["homestead_cents"] = summary.get("homestead_cents", 0) + hs_cents
    summary["count"] += hs_count
    summary["total_cents"] += hs_cents
    summary["units"] = fmt_qty(float(summary["units"] or 0) + hs_units)
    summary["avg_cents"] = round(summary["total_cents"] / summary["count"]) if summary["count"] else 0

    for t in hs_txns:
        c = int(t["total_cents"] or 0)
        pm = t["pay_method"]
        if pm == "card":
            summary["card_count"] += 1
            summary["card_cents"] += c
        elif pm == "venmo":
            summary["venmo_count"] += 1
            summary["venmo_cents"] += c
            summary["digital_count"] += 1
            summary["digital_cents"] += c
        elif pm == "paypal":
            summary["paypal_count"] += 1
            summary["paypal_cents"] += c
            summary["digital_count"] += 1
            summary["digital_cents"] += c
        elif pm == "cash":
            summary["cash_count"] += 1
            summary["cash_cents"] += c
        elif pm == "check":
            summary["check_count"] += 1
            summary["check_cents"] += c
        else:
            summary["unset_count"] += 1
            summary["unset_cents"] += c


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

    tz = local_tz()
    hs_all = [] if mp_id else list_homestead_paid_transactions(ctx.merchant_id, tz=tz)
    hs_in_range = [t for t in hs_all if rng["date_from"] <= t["date_str"] <= rng["date_to"]]
    _merge_homestead_into_summary(summary, hs_in_range)

    days = []
    for b in chart_buckets(rng["chart_start"], rng["chart_end"]):
        s0, s1 = utc_bounds(b["d0"], b["d1"])
        d = sales_summary(ctx.merchant_id, s0, s1, utc_windows=utc_windows)
        card_c = int(d["card_cents"])
        dig_c = int(d["digital_cents"])
        cash_c = int(d["cash_cents"])
        other_c = int(d["check_cents"])
        if hs_all:
            b0, b1 = b["d0"].isoformat(), b["d1"].isoformat()
            for ht in hs_all:
                if b0 <= ht["date_str"] < b1:
                    amt = int(ht["total_cents"] or 0)
                    pm = ht["pay_method"]
                    if pm == "card":
                        card_c += amt
                    elif pm in ("venmo", "paypal"):
                        dig_c += amt
                    elif pm == "cash":
                        cash_c += amt
                    else:
                        other_c += amt
        days.append({
            "label": b["label"],
            "title": b["title"],
            "card_cents": card_c,
            "digital_cents": dig_c,
            "cash_cents": cash_c,
            "other_cents": other_c,
            "zettle_card_cents": d["zettle_card_cents"],
            "zettle_cash_cents": d["zettle_cash_cents"],
            "quick_cash_cents": d["quick_cash_cents"],
        })
    peak = max((d["card_cents"] + d["digital_cents"] + d["cash_cents"] + d["other_cents"] for d in days), default=0) or 1
    best = max(days, key=lambda d: d["card_cents"] + d["digital_cents"] + d["cash_cents"] + d["other_cents"])

    pos_txns = recent_sales(
        ctx.merchant_id, rng["since"], rng["until"], src, limit=limit, utc_windows=utc_windows
    )
    pos_total_count = count_sales(
        ctx.merchant_id, rng["since"], rng["until"], src, utc_windows=utc_windows
    )
    today = datetime.now(tz).date()
    for t in pos_txns:
        local = to_local(t["occurred_at"])
        t["date_label"] = (
            "Today" if local.date() == today
            else "Yesterday" if local.date() == today - timedelta(days=1)
            else local.strftime("%b %-d")
        )
        t["time_label"] = local.strftime("%-I:%M %p").lower()
        t["full_label"] = local.strftime("%a %b %-d, %Y · %-I:%M %p").replace("AM", "am").replace("PM", "pm")
        t["local_dt_input"] = local.strftime("%Y-%m-%dT%H:%M")
        t["sort_ts"] = local.strftime("%Y-%m-%dT%H:%M:%S")
        t["sort_id"] = int(t["id"])

    hs_filtered = _filter_homestead_txns(hs_in_range, source)
    hs_page = hs_filtered[:limit]
    for ht in hs_page:
        try:
            d_obj = date.fromisoformat(ht["date_str"])
            ht["date_label"] = (
                "Today" if d_obj == today
                else "Yesterday" if d_obj == today - timedelta(days=1)
                else d_obj.strftime("%b %-d")
            )
            ht["full_label"] = f"{d_obj.strftime('%a %b %-d, %Y')} · {ht['time_label']}"
        except ValueError:
            ht["date_label"] = ht["date_str"]
            ht["full_label"] = ht["date_str"]
        ht["sort_id"] = int(ht["record_ids"][0])

    txns = sorted([*pos_txns, *hs_page], key=lambda x: (x["sort_ts"], x["sort_id"]), reverse=True)[:limit]
    total_count = pos_total_count + len(hs_filtered)

    sale_dates = list_sale_dates(ctx.merchant_id, tz)
    for ht in hs_all:
        cur = sale_dates.setdefault(ht["date_str"], {"cents": 0, "txns": 0, "marketplace": ""})
        cur["cents"] += int(ht["total_cents"] or 0)
        cur["txns"] += 1

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
        sale_dates=sale_dates,
        marketplaces=marketplaces,
        selected_marketplace=str(mp_id or ""),
        active_day_str=active_day_str,
        active_market_day=active_market_day,
        weather_options=WEATHER_OPTIONS,
        today_label=datetime.now(tz).strftime("%-d %b %Y"),
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
    return safe_redirect(next_url, fallback="/insights", msg=msg)


@router.post("/market-days/delete")
def market_day_delete(
    market_date: str = Form(...),
    next_url: str = Form("/insights"),
    ctx: Ctx = Depends(require("sales")),
):
    delete_market_day_safely(ctx.merchant_id, market_date)
    return safe_redirect(
        next_url,
        fallback="/insights",
        msg=f"Removed market tag for {market_date}.",
    )


def _redirect_sales_msg(next_url: str, msg: str, fallback: str = "/sales") -> RedirectResponse:
    return safe_redirect(next_url, fallback=fallback, msg=msg)


@router.post("/sales/homestead/payment-type")
def sales_retag_homestead_payment_type(
    record_type: str = Form(...),
    record_ids: str = Form(...),
    payment_type: str = Form(""),
    next_url: str = Form("/sales"),
    ctx: Ctx = Depends(require("sales")),
):
    try:
        ids = [int(x) for x in record_ids.split(",") if x.strip().isdigit()]
        cnt = update_homestead_transaction_payment_method(
            ctx.merchant_id, record_type, ids, payment_type
        )
        lbl = payment_type.strip().capitalize() if payment_type.strip() else "Untagged"
        msg = f"Tagged Homestead transaction payment as {lbl} ({cnt} record{'s' if cnt != 1 else ''})."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return _redirect_sales_msg(next_url, msg)


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
    return _redirect_sales_msg(next_url, msg)


@router.post("/sales/{sale_id}/edit")
async def sales_edit_quick_sale(
    request: Request,
    sale_id: int,
    ctx: Ctx = Depends(require("sales")),
):
    form = await request.form()
    next_url = str(form.get("next_url") or "/sales")
    try:
        occ_utc = local_input_to_utc_iso(str(form.get("occurred_at_local") or ""))
        payment_type = str(form.get("payment_type") or "cash")
        cash_raw = str(form.get("cash_received") or "").strip()
        cash_cents = _parse_dollars_to_cents(cash_raw) if cash_raw else None
        total_raw = str(form.get("total_dollars") or "").strip()
        total_cents = _parse_dollars_to_cents(total_raw) if total_raw else None

        item_updates = []
        for k in form.keys():
            if not k.startswith("qty_"):
                continue
            si_id = int(k[4:])
            qty_raw = str(form.get(k) or "").strip()
            try:
                qty_val = float(qty_raw)
            except ValueError as exc:
                raise ValueError("Item quantities must be valid numbers.") from exc
            price_raw = str(form.get(f"price_{si_id}") or "").strip()
            price_cents = _parse_dollars_to_cents(price_raw)
            if price_cents is None:
                raise ValueError("Enter a valid unit price for every line item.")
            item_updates.append(
                {
                    "sale_item_id": si_id,
                    "quantity": qty_val,
                    "unit_price_cents": price_cents,
                }
            )

        update_quick_sale(
            ctx.merchant_id,
            sale_id=sale_id,
            payment_type=payment_type,
            occurred_at_utc=occ_utc,
            item_updates=item_updates or None,
            total_cents=total_cents,
            cash_received_cents=cash_cents,
        )
        msg = f"Updated Quick sale #{sale_id}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return _redirect_sales_msg(next_url, msg)


@router.post("/sales/{sale_id}/delete")
def sales_delete_quick_sale(
    sale_id: int,
    restore_stock: str = Form(""),
    next_url: str = Form("/sales"),
    ctx: Ctx = Depends(require("sales")),
):
    try:
        res = delete_quick_sale(ctx.merchant_id, sale_id, restore_stock=bool(restore_stock))
        if res["restore_stock"] and res["ledger_entries_affected"]:
            msg = f"Permanently deleted Quick sale #{sale_id} (stock restored)."
        else:
            msg = f"Permanently deleted Quick sale #{sale_id}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return _redirect_sales_msg(next_url, msg)


@router.post("/sales/{sale_id}/exclude")
def sales_exclude_zettle(
    sale_id: int,
    restore_stock: str = Form("1"),
    next_url: str = Form("/sales"),
    ctx: Ctx = Depends(require("sales")),
):
    try:
        res = exclude_zettle_sale(ctx.merchant_id, sale_id, restore_stock=bool(restore_stock))
        n = int(res.get("restored_items") or 0)
        suffix = f" and restored {n} stock item(s)" if n else ""
        msg = f"Excluded Zettle sale #{sale_id} from totals & insights{suffix}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return _redirect_sales_msg(next_url, msg)


@router.post("/sales/{sale_id}/restore")
def sales_restore(
    sale_id: int,
    rededuct_stock: str = Form("1"),
    next_url: str = Form("/sales"),
    ctx: Ctx = Depends(require("sales")),
):
    try:
        res = restore_sale(ctx.merchant_id, sale_id, rededuct_stock=bool(rededuct_stock))
        msg = f"Restored sale #{sale_id} to completed totals."
        if res.get("removed_adjustments"):
            msg += " Stock deduction re-applied."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return _redirect_sales_msg(next_url, msg)


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
    return safe_redirect(next_url, fallback="/sales", msg=msg)


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
    mp_id, utc_windows = _resolve_marketplace_windows(ctx.merchant_id, marketplace, rng)
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
    if not mp_id:
        hs_all = list_homestead_paid_transactions(
            ctx.merchant_id,
            date_from=rng["date_from"],
            date_to=rng["date_to"],
            tz=local_tz(),
        )
        for ht in _filter_homestead_txns(hs_all, source):
            time_str = ht["sort_ts"][11:19] if len(ht["sort_ts"]) >= 19 else "12:00:00"
            for it in ht["items"]:
                w.writerow([
                    ht["id"],
                    ht["date_str"],
                    time_str,
                    "homestead",
                    ht["payment_type"] or "",
                    "completed",
                    "Homestead",
                    _csv_safe(it["name_snapshot"]),
                    it["quantity"],
                    dollars(it["unit_price_cents"]),
                    dollars(it["subtotal_cents"]),
                    dollars(ht["total_cents"]),
                ])
    name = f"ironledger-sales-{rng['date_from']}_to_{rng['date_to']}.csv"
    return Response(
        buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
