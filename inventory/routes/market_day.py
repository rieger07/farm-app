"""Active Market Day routes: booth pack-out, restock, Zettle sync, closeout & reopen."""

from datetime import date, datetime, timedelta
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse

from inventory import sync
from inventory.insights import recommend_cash_drawer, suggest_pack_list
from inventory.market_day import (
    close_market_day,
    compute_market_day_status,
    list_recent_and_upcoming_market_days,
    reopen_market_day,
    restock_market_day_item,
    save_market_day_pack,
)
from inventory.marketplaces import (
    WEATHER_OPTIONS,
    list_marketplaces,
    marketplace_utc_windows,
)
from inventory.web import (
    Ctx,
    _parse_dollars_to_cents,
    _sales_query,
    local_tz,
    render,
    require,
)

router = APIRouter()


@router.get("/market-day")
def market_day_page(
    request: Request,
    market_date: str = Query("", alias="date"),
    edit: int = 0,
    msg: str = "",
    ctx: Ctx = Depends(require("market_day")),
):
    tz = local_tz()
    today_d = datetime.now(tz).date()
    today_iso = today_d.isoformat()
    tomorrow_iso = (today_d + timedelta(days=1)).isoformat()
    days_until_sat = (5 - today_d.weekday()) % 7
    next_sat_iso = (today_d + timedelta(days=days_until_sat or 7)).isoformat()

    selected_date = (market_date or "").strip() or today_iso
    try:
        date.fromisoformat(selected_date)
    except ValueError:
        selected_date = today_iso

    mday_status = compute_market_day_status(ctx.merchant_id, selected_date, tz)
    marketplaces = list_marketplaces(ctx.merchant_id)
    recent_days = list_recent_and_upcoming_market_days(ctx.merchant_id, today_iso, limit=8)

    rng = _sales_query("quarter", "", "")
    default_pack = suggest_pack_list(ctx.merchant_id, rng["since"], rng["until"], tz)
    default_map = {
        str(it["sku_id"]): it["pack_qty"]
        for it in default_pack["items"]
        if it.get("sku_id") is not None
    }
    suggested_by_mp: dict[str, dict] = {"default": default_map}
    for mp in marketplaces:
        wins = marketplace_utc_windows(ctx.merchant_id, mp["id"], rng["date_from"], rng["date_to"], tz)
        if wins:
            mp_pack = suggest_pack_list(ctx.merchant_id, rng["since"], rng["until"], tz, utc_windows=wins)
            mp_map = {
                str(it["sku_id"]): it["pack_qty"]
                for it in mp_pack["items"]
                if it.get("sku_id") is not None
            }
            suggested_by_mp[str(mp["id"])] = mp_map or default_map
        else:
            suggested_by_mp[str(mp["id"])] = default_map

    drawer = recommend_cash_drawer(ctx.merchant_id, rng["since"], rng["until"], tz)
    categories = sorted({s["category"] for s in mday_status["skus"] if s["category"]})

    return render(
        request,
        "market_day.html",
        ctx,
        "market_day",
        md=mday_status,
        marketplaces=marketplaces,
        recent_days=recent_days,
        suggested_by_mp=suggested_by_mp,
        recommended_cash_cents=drawer["recommended_total_cents"],
        categories=categories,
        weather_options=WEATHER_OPTIONS,
        today_iso=today_iso,
        tomorrow_iso=tomorrow_iso,
        next_sat_iso=next_sat_iso,
        show_pack_editor=bool(edit) or mday_status["state"] == "unstarted" or mday_status["packed_sku_count"] == 0,
        msg=msg,
    )


@router.post("/market-day/pack")
async def market_day_pack_submit(
    request: Request,
    ctx: Ctx = Depends(require("market_day")),
):
    form = await request.form()
    market_date = str(form.get("market_date") or "").strip()
    raw_mp = str(form.get("marketplace_id") or "").strip()
    mp_id = int(raw_mp) if raw_mp.isdigit() else None
    new_mp = str(form.get("new_marketplace_name") or "").strip()
    booth_cents = _parse_dollars_to_cents(str(form.get("booth_fee") or ""))
    travel_cents = _parse_dollars_to_cents(str(form.get("travel_cost") or ""))
    start_cash_cents = _parse_dollars_to_cents(str(form.get("starting_cash") or "")) or 0
    weather = str(form.get("weather") or "").strip()
    notes = str(form.get("notes") or "").strip()

    brought_by_sku: dict[int, float] = {}
    for k, v in form.items():
        if k.startswith("brought_"):
            sid_str = k[len("brought_"):]
            if sid_str.isdigit():
                raw_val = str(v or "").strip().replace(",", ".")
                try:
                    qty = max(0.0, float(raw_val)) if raw_val else 0.0
                except ValueError:
                    qty = 0.0
                brought_by_sku[int(sid_str)] = qty

    try:
        save_market_day_pack(
            ctx.merchant_id,
            market_date=market_date,
            marketplace_id=mp_id,
            brought_by_sku=brought_by_sku,
            new_marketplace_name=new_mp,
            booth_fee_cents=booth_cents,
            travel_cost_cents=travel_cents,
            starting_cash_cents=start_cash_cents,
            weather=weather,
            notes=notes,
        )
        packed_n = sum(1 for q in brought_by_sku.values() if q > 1e-9)
        today_iso = datetime.now(local_tz()).date().isoformat()
        if market_date > today_iso:
            msg = f"Saved pack-ahead list for {market_date} ({packed_n} products)."
        else:
            msg = f"Market Day active for {market_date} ({packed_n} products packed)."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(
        f"/market-day?date={quote_plus(market_date)}&msg={quote_plus(msg)}",
        status_code=303,
    )


@router.post("/market-day/restock")
def market_day_restock_submit(
    market_date: str = Form(...),
    sku_id: int = Form(...),
    delta_qty: float = Form(1.0),
    ctx: Ctx = Depends(require("market_day")),
):
    try:
        new_q = restock_market_day_item(ctx.merchant_id, market_date, sku_id, delta_qty)
        msg = f"Updated booth brought count to {new_q}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(
        f"/market-day?date={quote_plus(market_date)}&msg={quote_plus(msg)}",
        status_code=303,
    )


@router.post("/market-day/sync")
def market_day_sync_now(
    market_date: str = Form(""),
    ctx: Ctx = Depends(require("market_day")),
):
    target_date = (market_date or "").strip() or datetime.now(local_tz()).date().isoformat()
    if sync.is_connected(ctx.merchant_id):
        res = sync.sync_merchant(ctx.merchant_id)
        if res.get("error"):
            msg = f"Zettle sync error: {res['error']}"
        else:
            msg = f"Synced Zettle ({res.get('new_sales', 0)} new card sales)."
    else:
        msg = "Zettle is not connected for this workspace."
    return RedirectResponse(
        f"/market-day?date={quote_plus(target_date)}&msg={quote_plus(msg)}",
        status_code=303,
    )


@router.post("/market-day/close")
async def market_day_close_submit(
    request: Request,
    ctx: Ctx = Depends(require("market_day")),
):
    form = await request.form()
    market_date = str(form.get("market_date") or "").strip()
    counted_cash = _parse_dollars_to_cents(str(form.get("counted_cash") or ""))
    weather = str(form.get("weather") or "").strip()
    notes = str(form.get("notes") or "").strip()

    if sync.is_connected(ctx.merchant_id):
        try:
            sync.sync_merchant(ctx.merchant_id)
        except Exception:
            pass

    write_offs: dict[int, float] = {}
    counted_lefts: dict[int, float | None] = {}
    for k, v in form.items():
        if k.startswith("writeoff_"):
            sid_str = k[len("writeoff_"):]
            if sid_str.isdigit():
                raw = str(v or "").strip().replace(",", ".")
                try:
                    write_offs[int(sid_str)] = max(0.0, float(raw)) if raw else 0.0
                except ValueError:
                    write_offs[int(sid_str)] = 0.0
        elif k.startswith("counted_left_"):
            sid_str = k[len("counted_left_"):]
            if sid_str.isdigit():
                raw = str(v or "").strip().replace(",", ".")
                if raw == "":
                    counted_lefts[int(sid_str)] = None
                else:
                    try:
                        counted_lefts[int(sid_str)] = max(0.0, float(raw))
                    except ValueError:
                        counted_lefts[int(sid_str)] = None

    try:
        res = close_market_day(
            ctx.merchant_id,
            market_date=market_date,
            tz=local_tz(),
            counted_cash_cents=counted_cash,
            write_offs=write_offs,
            counted_lefts=counted_lefts,
            weather=weather,
            notes=notes,
            created_by=ctx.user["id"],
        )
        adj = res["adjustments_written"]
        extra = f" ({adj} stock adjustment{'s' if adj != 1 else ''} recorded)" if adj else ""
        msg = f"Closed out Market Day for {market_date}{extra}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(
        f"/market-day?date={quote_plus(market_date)}&msg={quote_plus(msg)}",
        status_code=303,
    )


@router.post("/market-day/reopen")
def market_day_reopen_submit(
    market_date: str = Form(...),
    ctx: Ctx = Depends(require("market_day")),
):
    try:
        res = reopen_market_day(ctx.merchant_id, market_date, created_by=ctx.user["id"])
        rev = res["reversed_adjustments"]
        extra = f" (reversed {rev} closeout adjustment{'s' if rev != 1 else ''})" if rev else ""
        msg = f"Reopened Market Day for {market_date}{extra}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(
        f"/market-day?date={quote_plus(market_date)}&msg={quote_plus(msg)}",
        status_code=303,
    )
