"""Phase 1 Financial & Operational Insights: feed unit price trends, enterprise P&L, egg cost/doz & FCR."""

from __future__ import annotations

from datetime import date, timedelta
import statistics

from homestead.config import DEFAULT_BAG_WEIGHT, format_species_display
from homestead.db.batches import (
    list_batch_harvests,
    list_batch_sales,
    list_batches,
)
from homestead.db.core import get_db_connection, get_species_catalog
from homestead.db.eggs import get_layer_settings, list_egg_logs
from homestead.db.expenses import list_farm_expenses
from homestead.db.fiber import list_fiber_mill_lots, list_fiber_shearings
from homestead.db.herds import list_herd_feed_logs, list_herds
from homestead.db.shares import effective_share_weight, list_batch_harvest_shares
from shared.bridge import get_sku_completed_sales_summary


def _year_of(dt_str: str | None) -> int | None:
    s = str(dt_str or "").strip()
    if len(s) >= 4 and s[:4].isdigit():
        return int(s[:4])
    return None


def _fmt_month_label(ym: str) -> str:
    if len(ym) != 7 or ym[4] != "-":
        return ym
    try:
        dt = date.fromisoformat(f"{ym}-01")
        return dt.strftime("%b '%y")
    except ValueError:
        return ym


def _classify_batch_feed(feed_type: str | None) -> str:
    ft = str(feed_type or "").strip().lower()
    return "Starter" if ft == "starter" else "Grower"


def _classify_layer_feed(feed_type: str | None) -> str:
    ft = str(feed_type or "").strip().lower()
    if ft == "scratch":
        return "Scratch"
    if ft == "supplement":
        return "Supplement"
    return "Layer"


def _classify_herd_feed(feed_category: str | None) -> str:
    fc = str(feed_category or "").strip().lower()
    if fc == "hay":
        return "Hay"
    if fc == "supplement":
        return "Supplement"
    return "Herd Grain"


def _collect_all_feed_records(user_id: int, bag_weight: int) -> list[dict]:
    """Load positive-cost feed & hay purchase lines across batches, layers, and perennial herds."""
    uid = int(user_id)
    safe_bw = max(1, int(bag_weight or DEFAULT_BAG_WEIGHT))
    records: list[dict] = []

    with get_db_connection() as conn:
        b_rows = conn.execute(
            """
            SELECT fl.id, fl.batch_id, fl.feed_date, fl.feed_type, fl.bags,
                   fl.feed_lbs, fl.cost_total, fl.vendor, b.name AS batch_name
            FROM batch_feed_logs fl
            JOIN batches b ON b.id = fl.batch_id
            WHERE b.user_id = ? AND fl.cost_total > 0
            """,
            (uid,),
        ).fetchall()

        l_rows = conn.execute(
            """
            SELECT id, feed_date, feed_type, bags, feed_lbs, cost_total, vendor
            FROM layer_feed_logs
            WHERE user_id = ? AND cost_total > 0
            """,
            (uid,),
        ).fetchall()

        h_rows = conn.execute(
            """
            SELECT fl.id, fl.herd_id, fl.feed_date, fl.feed_category, fl.quantity,
                   fl.unit, fl.weight_lbs, fl.cost_total, fl.vendor, h.name AS herd_name
            FROM herd_feed_logs fl
            JOIN herds h ON h.id = fl.herd_id
            WHERE fl.user_id = ? AND fl.cost_total > 0
            """,
            (uid,),
        ).fetchall()

        fe_rows = conn.execute(
            """
            SELECT fe.id, fe.batch_id, fe.expense_date, fe.vendor, fe.amount, b.name AS batch_name
            FROM farm_expenses fe
            LEFT JOIN batches b ON b.id = fe.batch_id
            WHERE fe.user_id = ? AND fe.category = 'Feed' AND fe.amount > 0
            """,
            (uid,),
        ).fetchall()

    for r in b_rows:
        cost = round(float(r["cost_total"] or 0.0), 2)
        bags = float(r["bags"] or 0.0)
        lbs = float(r["feed_lbs"] or 0.0)
        if lbs <= 0 and bags > 0:
            lbs = bags * safe_bw
        fg = _classify_batch_feed(r["feed_type"])
        cost_per_lb = (cost / lbs) if lbs > 0 else None
        records.append(
            {
                "source": "batch",
                "preset_kind": fg.lower(),
                "feed_group": fg,
                "date": str(r["feed_date"] or "")[:10],
                "vendor": str(r["vendor"] or "").strip(),
                "allocation": str(r["batch_name"] or f"Batch #{r['batch_id']}"),
                "weight_lbs": round(lbs, 2),
                "bags": round(bags, 2),
                "bales": 0.0,
                "cost_total": cost,
                "cost_per_lb": round(cost_per_lb, 4) if cost_per_lb is not None else None,
                "bag_equiv_price": (
                    round(cost_per_lb * safe_bw, 2)
                    if cost_per_lb is not None
                    else (round(cost / bags, 2) if bags > 0 else None)
                ),
                "cost_per_bale": None,
                "source_url": f"/batches/{int(r['batch_id'])}",
            }
        )

    for r in l_rows:
        cost = round(float(r["cost_total"] or 0.0), 2)
        bags = float(r["bags"] or 0.0)
        lbs = float(r["feed_lbs"] or 0.0)
        if lbs <= 0 and bags > 0:
            lbs = bags * safe_bw
        fg = _classify_layer_feed(r["feed_type"])
        cost_per_lb = (cost / lbs) if lbs > 0 else None
        records.append(
            {
                "source": "layer",
                "preset_kind": fg.lower() if fg in ("Layer", "Scratch") else "",
                "feed_group": fg,
                "date": str(r["feed_date"] or "")[:10],
                "vendor": str(r["vendor"] or "").strip(),
                "allocation": "Laying Flock",
                "weight_lbs": round(lbs, 2),
                "bags": round(bags, 2),
                "bales": 0.0,
                "cost_total": cost,
                "cost_per_lb": round(cost_per_lb, 4) if cost_per_lb is not None else None,
                "bag_equiv_price": (
                    round(cost_per_lb * safe_bw, 2)
                    if cost_per_lb is not None
                    else (round(cost / bags, 2) if bags > 0 else None)
                ),
                "cost_per_bale": None,
                "source_url": "/eggs?tab=flock",
            }
        )

    for r in h_rows:
        cost = round(float(r["cost_total"] or 0.0), 2)
        qty = float(r["quantity"] or 0.0)
        unit = str(r["unit"] or "bags").strip().lower()
        fg = _classify_herd_feed(r["feed_category"])
        lbs = float(r["weight_lbs"] or 0.0)
        if lbs <= 0 and unit == "bags" and qty > 0:
            lbs = qty * safe_bw
        elif lbs <= 0 and unit == "lbs" and qty > 0:
            lbs = qty
        bales = qty if (fg == "Hay" and unit == "bales" and qty > 0) else 0.0
        bags = qty if unit == "bags" and qty > 0 else 0.0
        cost_per_lb = (cost / lbs) if lbs > 0 else None
        cost_per_bale = (cost / bales) if bales > 0 else None
        records.append(
            {
                "source": "herd",
                "preset_kind": "",
                "feed_group": fg,
                "date": str(r["feed_date"] or "")[:10],
                "vendor": str(r["vendor"] or "").strip(),
                "allocation": str(r["herd_name"] or f"Herd #{r['herd_id']}"),
                "weight_lbs": round(lbs, 2),
                "bags": round(bags, 2),
                "bales": round(bales, 2),
                "cost_total": cost,
                "cost_per_lb": round(cost_per_lb, 4) if cost_per_lb is not None else None,
                "bag_equiv_price": (
                    round(cost_per_lb * safe_bw, 2)
                    if (cost_per_lb is not None and fg != "Hay")
                    else (round(cost / bags, 2) if bags > 0 else None)
                ),
                "cost_per_bale": round(cost_per_bale, 2) if cost_per_bale is not None else None,
                "source_url": f"/herds?herd_id={int(r['herd_id'])}",
            }
        )

    for r in fe_rows:
        bid = r["batch_id"]
        records.append(
            {
                "source": "batch_expense" if bid else "general_expense",
                "preset_kind": "",
                "feed_group": "Other Feed",
                "date": str(r["expense_date"] or "")[:10],
                "vendor": str(r["vendor"] or "").strip(),
                "allocation": str(r["batch_name"] or f"Batch #{bid}") if bid else "General Feed",
                "weight_lbs": 0.0,
                "bags": 0.0,
                "bales": 0.0,
                "cost_total": round(float(r["amount"] or 0.0), 2),
                "cost_per_lb": None,
                "bag_equiv_price": None,
                "cost_per_bale": None,
                "source_url": f"/batches/{int(bid)}" if bid else "/financials?tab=taxes",
            }
        )

    records.sort(key=lambda x: x["date"])
    return records


def _build_feed_chart(series: list[dict]) -> dict | None:
    """Build pure SVG coordinate payload for monthly weighted $/lb trend."""
    pts_data = [m for m in series if m.get("avg_per_lb") is not None]
    if len(pts_data) < 2:
        return None
    w, h = 600.0, 170.0
    pad_l, pad_r, pad_t, pad_b = 54.0, 22.0, 18.0, 30.0
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b

    vals = [float(m["avg_per_lb"]) for m in pts_data]
    v_min = min(vals)
    v_max = max(vals)
    if v_max - v_min < 0.02:
        y_min = max(0.0, round(v_min - 0.05, 2))
        y_max = round(v_max + 0.05, 2)
    else:
        margin = (v_max - v_min) * 0.15
        y_min = max(0.0, round(v_min - margin, 2))
        y_max = round(v_max + margin, 2)
    y_span = max(0.01, y_max - y_min)

    dots: list[dict] = []
    poly_pts: list[str] = []
    n = len(pts_data)
    for idx, m in enumerate(pts_data):
        x = pad_l + (plot_w * idx / (n - 1))
        val = float(m["avg_per_lb"])
        y = pad_t + plot_h * (1.0 - ((val - y_min) / y_span))
        poly_pts.append(f"{x:.1f},{y:.1f}")
        dots.append(
            {
                "x": round(x, 1),
                "y": round(y, 1),
                "label": m["label"],
                "val_str": f"${val:.3f}/lb",
                "bag_str": f"${float(m['avg_bag_equiv'] or 0):.2f}/bag",
            }
        )
    return {
        "width": int(w),
        "height": int(h),
        "points": " ".join(poly_pts),
        "dots": dots,
        "y_min": y_min,
        "y_mid": round((y_min + y_max) / 2.0, 2),
        "y_max": y_max,
    }


def _compute_preset_calibrations(
    user_id: int, all_feed_records: list[dict], bag_weight: int
) -> list[dict]:
    """Compare trailing actual bag prices against saved Starter/Grower/Layer/Scratch presets."""
    catalog = get_species_catalog(user_id=user_id)
    layer_cfg = get_layer_settings(user_id)
    preset_values: dict[str, tuple[str, float]] = {}

    if catalog:
        st_prices = [float(p.get("starter_bag_price") or 0.0) for p in catalog.values() if p.get("starter_bag_price")]
        gr_prices = [float(p.get("grower_bag_price") or 0.0) for p in catalog.values() if p.get("grower_bag_price")]
        if st_prices:
            preset_values["starter"] = ("Starter Feed", round(sum(st_prices) / len(st_prices), 2))
        if gr_prices:
            preset_values["grower"] = ("Grower Feed", round(sum(gr_prices) / len(gr_prices), 2))
    preset_values["layer"] = ("Layer Feed", round(float(layer_cfg.get("bag_price") or 0.0), 2))
    preset_values["scratch"] = ("Scratch Grain", round(float(layer_cfg.get("scratch_bag_price") or 0.0), 2))

    cutoff_90 = (date.today() - timedelta(days=90)).isoformat()
    safe_bw = max(1, int(bag_weight or DEFAULT_BAG_WEIGHT))
    calibrations: list[dict] = []

    for kind, (lbl, preset_price) in preset_values.items():
        matching = [r for r in all_feed_records if r["preset_kind"] == kind and (r["weight_lbs"] > 0 or r["bags"] > 0)]
        if not matching:
            continue
        recent = [r for r in matching if r["date"] >= cutoff_90]
        sample = recent if recent else matching[-6:]
        w_lbs = sum(r["weight_lbs"] for r in sample)
        w_cost = sum(r["cost_total"] for r in sample)
        if w_lbs > 0:
            actual_bag = round((w_cost / w_lbs) * safe_bw, 2)
        else:
            tot_bags = sum(r["bags"] for r in sample)
            actual_bag = round(w_cost / tot_bags, 2) if tot_bags > 0 else 0.0
        if actual_bag <= 0:
            continue
        diff_ratio = abs(actual_bag - preset_price) / max(preset_price, 0.01)
        if diff_ratio < 0.05:
            continue
        delta_pct = round(((actual_bag - preset_price) / max(preset_price, 0.01)) * 100.0, 1)
        calibrations.append(
            {
                "kind": kind,
                "label": lbl,
                "actual_bag_price": actual_bag,
                "preset_bag_price": preset_price,
                "delta_pct": delta_pct,
                "sample_count": len(sample),
            }
        )
    return calibrations


def _compute_enterprise_breakdown(
    user_id: int,
    *,
    merchant_id: int | None,
    year: int | None,
    all_feed_records: list[dict],
) -> tuple[list[dict], float]:
    """Compute cash spend & revenue scorecard across farm enterprises."""
    uid = int(user_id)
    batches = list_batches(user_id=uid)
    batches_by_id = {int(b["id"]): b for b in batches}
    herds = list_herds(uid, include_inactive=True)
    fiber_herd_ids = {
        int(h["id"])
        for h in herds
        if h.get("herd_type") == "fiber" or h.get("species") in ("alpaca", "sheep")
    }

    # Batch direct spend & revenue
    batch_feed_spend = sum(
        r["cost_total"]
        for r in all_feed_records
        if r["source"] == "batch" and (year is None or _year_of(r["date"]) == year)
    )
    batch_other_spend = 0.0
    general_untagged_spend = 0.0
    for exp in list_farm_expenses(uid):
        if year is not None and _year_of(exp.get("expense_date")) != year:
            continue
        amt = float(exp.get("amount") or 0.0)
        if exp.get("batch_id"):
            if str(exp.get("category") or "").strip().lower() == "feed":
                batch_feed_spend += amt
            else:
                batch_other_spend += amt
        else:
            general_untagged_spend += amt

    all_harvests = list_batch_harvests(user_id=uid)
    all_shares = list_batch_harvest_shares(user_id=uid)
    shares_by_harvest: dict[int, list[dict]] = {}
    for sh in all_shares:
        shares_by_harvest.setdefault(int(sh["harvest_id"]), []).append(sh)

    batch_rev = 0.0
    for sa in list_batch_sales(user_id=uid):
        if sa.get("allocation_type") != "Customer Sale":
            continue
        if year is not None and _year_of(sa.get("entry_date")) != year:
            continue
        batch_rev += float(sa.get("revenue") or 0.0)

    for hv in all_harvests:
        proc = float(hv.get("processing_cost_total") or 0.0)
        if proc > 0 and (year is None or _year_of(hv.get("harvest_date")) == year):
            batch_other_spend += proc

        child_shares = shares_by_harvest.get(int(hv["id"]))
        if child_shares:
            carcass_lbs = float(hv.get("dressed_lbs") or 0.0)
            b = batches_by_id.get(int(hv["batch_id"]), {})
            sp = float(b.get("sale_price_per_lb") or 0.0)
            for sh in child_shares:
                if not bool(sh.get("for_sale", 1)):
                    continue
                if str(sh.get("payment_status") or "").lower() != "paid" and str(sh.get("pickup_status") or "").lower() != "picked_up":
                    continue
                cash_yr = _year_of(sh.get("paid_at")) or _year_of(hv.get("harvest_date"))
                if year is not None and cash_yr != year:
                    continue
                ap = sh.get("actual_sale_price")
                batch_rev += float(ap) if ap is not None else round(effective_share_weight(sh, carcass_lbs) * sp, 2)
            continue
        if year is not None and _year_of(hv.get("harvest_date")) != year:
            continue
        if not bool(hv.get("for_sale", 1)):
            continue
        pay_stat = str(hv.get("payment_status") or "").lower()
        pick_stat = str(hv.get("pickup_status") or "").lower()
        ap = hv.get("actual_sale_price")
        if not (pay_stat == "paid" or pick_stat == "picked_up" or ap is not None):
            continue
        if ap is not None:
            batch_rev += float(ap)
        else:
            b = batches_by_id.get(int(hv["batch_id"]), {})
            batch_rev += round(float(hv.get("dressed_lbs") or 0.0) * float(b.get("sale_price_per_lb") or 0.0), 2)

    # Layer spend & linked POS revenue
    layer_feed_spend = sum(
        r["cost_total"]
        for r in all_feed_records
        if r["source"] == "layer" and (year is None or _year_of(r["date"]) == year)
    )
    layer_cfg = get_layer_settings(uid)
    egg_sku_ids: set[int] = set()
    for sp_meta in (layer_cfg.get("species_settings") or {}).values():
        sid = sp_meta.get("ledger_sku_id")
        if sid:
            egg_sku_ids.add(int(sid))
    layer_rev = 0.0
    for sid in sorted(egg_sku_ids):
        _, rev = get_sku_completed_sales_summary(sid, year=year)
        layer_rev += rev

    # Breeding vs. Fiber perennial herds
    breeding_feed_spend = 0.0
    fiber_feed_spend = 0.0
    for fl in list_herd_feed_logs(uid, limit=5000):
        if year is not None and _year_of(fl.get("feed_date")) != year:
            continue
        cost = float(fl.get("cost_total") or 0.0)
        if int(fl["herd_id"]) in fiber_herd_ids:
            fiber_feed_spend += cost
        else:
            breeding_feed_spend += cost

    fiber_other_spend = 0.0
    for sh in list_fiber_shearings(uid, year=year):
        fiber_other_spend += float(sh.get("shearing_fee") or 0.0)
    fiber_sku_ids: set[int] = set()
    for lot in list_fiber_mill_lots(uid):
        dt_str = lot.get("received_date") or lot.get("sent_date") or str(lot.get("created_at") or "")[:10]
        if year is None or _year_of(dt_str) == year:
            fiber_other_spend += float(lot.get("total_mill_cost") or 0.0)
        for out_item in lot.get("outputs") or []:
            if out_item.get("ledger_sku_id"):
                fiber_sku_ids.add(int(out_item["ledger_sku_id"]))

    fiber_rev = 0.0
    for sid in sorted(fiber_sku_ids):
        _, rev = get_sku_completed_sales_summary(sid, year=year)
        fiber_rev += rev

    rows = [
        {
            "key": "batches",
            "name": "Meat Batches",
            "subtitle": "Feed, processing, batch receipts & meat sales",
            "feed_spend": round(batch_feed_spend, 2),
            "other_spend": round(batch_other_spend, 2),
            "total_spend": round(batch_feed_spend + batch_other_spend, 2),
            "revenue": round(batch_rev, 2),
            "net": round(batch_rev - (batch_feed_spend + batch_other_spend), 2),
            "revenue_note": "",
            "source_url": "/batches",
        },
        {
            "key": "layers",
            "name": "Laying Flocks & Eggs",
            "subtitle": "Layer/scratch feed & linked POS egg sales",
            "feed_spend": round(layer_feed_spend, 2),
            "other_spend": 0.0,
            "total_spend": round(layer_feed_spend, 2),
            "revenue": round(layer_rev, 2) if egg_sku_ids else None,
            "net": round(layer_rev - layer_feed_spend, 2) if egg_sku_ids else None,
            "revenue_note": "" if egg_sku_ids else "Link egg SKU in Settings",
            "source_url": "/eggs",
        },
        {
            "key": "breeding",
            "name": "Breeding Herds",
            "subtitle": "Perennial breeding stock upkeep",
            "feed_spend": round(breeding_feed_spend, 2),
            "other_spend": 0.0,
            "total_spend": round(breeding_feed_spend, 2),
            "revenue": None,
            "net": None,
            "revenue_note": "Overhead → litters",
            "source_url": "/herds",
        },
        {
            "key": "fiber",
            "name": "Alpaca & Fiber Herds",
            "subtitle": "Grain, hay, shearing, milling & yarn POS sales",
            "feed_spend": round(fiber_feed_spend, 2),
            "other_spend": round(fiber_other_spend, 2),
            "total_spend": round(fiber_feed_spend + fiber_other_spend, 2),
            "revenue": round(fiber_rev, 2) if fiber_sku_ids else None,
            "net": round(fiber_rev - (fiber_feed_spend + fiber_other_spend), 2) if fiber_sku_ids else None,
            "revenue_note": "" if fiber_sku_ids else "Link fiber SKU in Mill Lots",
            "source_url": "/herds?tab=mill",
        },
    ]
    total_feed_all = sum(r["feed_spend"] for r in rows)
    for r in rows:
        r["feed_share_pct"] = (
            round((r["feed_spend"] / total_feed_all) * 100.0, 1)
            if total_feed_all > 0
            else 0.0
        )
    return rows, round(general_untagged_spend, 2)


def _compute_egg_monthly_cost(user_id: int, *, year: int | None) -> dict:
    """Compute monthly egg feed cost per usable dozen across egg_logs & layer_feed_logs."""
    uid = int(user_id)
    eggs_by_m: dict[str, int] = {}
    for e in list_egg_logs(uid, species="all", limit=5000):
        d = str(e.get("log_date") or "")[:10]
        if year is not None and _year_of(d) != year:
            continue
        usable = max(0, int(e.get("total_collected") or 0) - int(e.get("cracked_unusable") or 0))
        if len(d) >= 7:
            ym = d[:7]
            eggs_by_m[ym] = eggs_by_m.get(ym, 0) + usable

    feed_by_m: dict[str, float] = {}
    with get_db_connection() as conn:
        l_rows = conn.execute(
            "SELECT feed_date, cost_total FROM layer_feed_logs WHERE user_id = ?",
            (uid,),
        ).fetchall()
    for r in l_rows:
        d = str(r["feed_date"] or "")[:10]
        if year is not None and _year_of(d) != year:
            continue
        if len(d) >= 7:
            ym = d[:7]
            feed_by_m[ym] = feed_by_m.get(ym, 0.0) + float(r["cost_total"] or 0.0)

    months = sorted(set(eggs_by_m) | set(feed_by_m), reverse=True)
    rows: list[dict] = []
    tot_eggs = 0
    tot_feed = 0.0
    for ym in months:
        eggs = eggs_by_m.get(ym, 0)
        fc = round(feed_by_m.get(ym, 0.0), 2)
        doz = round(eggs / 12.0, 2)
        cpd = round(fc / doz, 2) if doz > 0 else None
        tot_eggs += eggs
        tot_feed += fc
        rows.append(
            {
                "month": ym,
                "label": _fmt_month_label(ym),
                "usable_eggs": eggs,
                "dozens": round(doz, 1),
                "feed_cost": fc,
                "cost_per_dozen": cpd,
            }
        )
    tot_doz = tot_eggs / 12.0
    priced_rows = [r for r in rows if r["cost_per_dozen"] is not None]
    best = min(priced_rows, key=lambda r: r["cost_per_dozen"]) if priced_rows else None
    worst = max(priced_rows, key=lambda r: r["cost_per_dozen"]) if priced_rows else None
    return {
        "months": rows,
        "total_eggs": tot_eggs,
        "total_dozens": round(tot_doz, 1),
        "total_feed_cost": round(tot_feed, 2),
        "avg_cost_per_dozen": round(tot_feed / tot_doz, 2) if tot_doz > 0 else None,
        "best_month": best,
        "worst_month": worst,
    }


def _compute_batch_benchmarks(schedules: list[dict], *, year: int | None) -> list[dict]:
    """Build Planned vs. Actual & FCR benchmark rows from computed batch schedules."""
    rows: list[dict] = []
    for s in schedules:
        b = s["batch"]
        if year is not None:
            s_yr = _year_of(b.get("start_date"))
            p_yr = s["proc_date"].year if s.get("proc_date") else None
            if s_yr != year and p_yr != year:
                continue
        planned_feed = round(float(s["total_feed_cost_bags"] or 0.0), 2)
        actual_feed = round(float(s["logged_feed_cost"]), 2) if s.get("has_actual_feed") else None
        feed_var_pct = (
            round(((actual_feed - planned_feed) / planned_feed) * 100.0, 1)
            if (actual_feed is not None and planned_feed > 0)
            else None
        )
        rows.append(
            {
                "id": int(b["id"]),
                "name": str(b["name"]),
                "species_display": format_species_display(b.get("species")),
                "start_date": str(b.get("start_date") or ""),
                "survival_pct": float(s.get("survival_pct") or 0.0),
                "surviving_quantity": int(s["surviving_quantity"]),
                "initial_quantity": int(s["initial_quantity"]),
                "harvested_head": int(s["harvested_head"]),
                "planned_feed_cost": planned_feed,
                "actual_feed_cost": actual_feed,
                "logged_feed_lbs": float(s.get("logged_feed_lbs") or 0.0),
                "planned_feed_lbs": round(float(s["starter_feed"] + s["grower_feed"]), 1),
                "feed_variance_pct": feed_var_pct,
                "actual_fcr": s.get("actual_fcr"),
                "actual_avg_yield": float(s.get("actual_avg_yield_per_head") or 0.0),
                "expected_yield": float(s.get("expected_yield_per_head") or 0.0),
                "reconciled_cost_per_lb": float(s.get("reconciled_cost_per_lb_meat") or 0.0),
                "planned_cost_per_lb": float(s.get("total_cost_per_lb_meat") or 0.0),
                "actual_cash_profit": float(s.get("actual_cash_profit") or 0.0),
                "effective_freezer_cost_per_lb": s.get("effective_freezer_cost_per_lb"),
            }
        )
    rows.sort(key=lambda r: (r["start_date"], r["id"]), reverse=True)
    return rows


def compute_farm_insights(
    user_id: int,
    *,
    merchant_id: int | None = None,
    year: int | None = None,
    schedules: list[dict] | None = None,
    bag_weight: int = DEFAULT_BAG_WEIGHT,
) -> dict:
    """Compute Phase 1 Financial & Operational Insights for Financials -> Insights tab."""
    uid = int(user_id)
    safe_bw = max(1, int(bag_weight or DEFAULT_BAG_WEIGHT))
    all_feed = _collect_all_feed_records(uid, safe_bw)
    yr_feed = [r for r in all_feed if year is None or _year_of(r["date"]) == year]

    # 1. Headline Feed KPIs + Monthly Series
    total_feed_spend = round(sum(r["cost_total"] for r in yr_feed), 2)
    total_feed_lbs = round(sum(r["weight_lbs"] for r in yr_feed), 1)
    weighted_recs = [r for r in yr_feed if r["weight_lbs"] > 0]
    w_spend = sum(r["cost_total"] for r in weighted_recs)
    w_lbs = sum(r["weight_lbs"] for r in weighted_recs)
    avg_cost_per_lb = round(w_spend / w_lbs, 4) if w_lbs > 0 else None
    avg_bag_equiv = round((w_spend / w_lbs) * safe_bw, 2) if w_lbs > 0 else None

    hay_recs = [r for r in yr_feed if r["feed_group"] == "Hay"]
    hay_spend = round(sum(r["cost_total"] for r in hay_recs), 2)
    hay_bales = round(sum(r["bales"] for r in hay_recs), 1)
    hay_bale_spend = sum(r["cost_total"] for r in hay_recs if r["bales"] > 0)
    hay_avg_bale = round(hay_bale_spend / hay_bales, 2) if hay_bales > 0 else None

    by_month: dict[str, dict] = {}
    by_type: dict[str, dict] = {}
    by_vendor: dict[str, dict] = {}
    named_vendors: set[str] = set()

    for r in yr_feed:
        if r["vendor"]:
            named_vendors.add(r["vendor"].lower())

        # Monthly rollup
        ym = r["date"][:7] if len(r["date"]) >= 7 else ""
        if ym:
            mb = by_month.setdefault(
                ym,
                {
                    "month": ym,
                    "label": _fmt_month_label(ym),
                    "total_spend": 0.0,
                    "total_lbs": 0.0,
                    "w_spend": 0.0,
                    "w_lbs": 0.0,
                    "hay_bale_spend": 0.0,
                    "hay_bales": 0.0,
                    "count": 0,
                },
            )
            mb["total_spend"] += r["cost_total"]
            mb["total_lbs"] += r["weight_lbs"]
            mb["count"] += 1
            if r["weight_lbs"] > 0:
                mb["w_spend"] += r["cost_total"]
                mb["w_lbs"] += r["weight_lbs"]
            if r["bales"] > 0:
                mb["hay_bale_spend"] += r["cost_total"]
                mb["hay_bales"] += r["bales"]

        # Type rollup
        fg = r["feed_group"]
        tb = by_type.setdefault(
            fg,
            {
                "group": fg,
                "count": 0,
                "total_spend": 0.0,
                "total_lbs": 0.0,
                "w_spend": 0.0,
                "w_lbs": 0.0,
                "bale_spend": 0.0,
                "bales": 0.0,
                "pts": [],
            },
        )
        tb["count"] += 1
        tb["total_spend"] += r["cost_total"]
        tb["total_lbs"] += r["weight_lbs"]
        if r["weight_lbs"] > 0:
            tb["w_spend"] += r["cost_total"]
            tb["w_lbs"] += r["weight_lbs"]
            tb["pts"].append(r["cost_per_lb"])
        if r["bales"] > 0:
            tb["bale_spend"] += r["cost_total"]
            tb["bales"] += r["bales"]

        # Vendor rollup
        v_raw = r["vendor"] or "Unspecified"
        v_key = v_raw.lower() if r["vendor"] else "__unspecified__"
        vb = by_vendor.setdefault(
            v_key,
            {
                "vendor": v_raw,
                "is_named": bool(r["vendor"]),
                "count": 0,
                "total_spend": 0.0,
                "total_lbs": 0.0,
                "w_spend": 0.0,
                "w_lbs": 0.0,
                "grain_spend": 0.0,
                "grain_lbs": 0.0,
                "bale_spend": 0.0,
                "bales": 0.0,
                "type_spend": {},
            },
        )
        vb["count"] += 1
        vb["total_spend"] += r["cost_total"]
        vb["total_lbs"] += r["weight_lbs"]
        if r["weight_lbs"] > 0:
            vb["w_spend"] += r["cost_total"]
            vb["w_lbs"] += r["weight_lbs"]
            if fg != "Hay":
                vb["grain_spend"] += r["cost_total"]
                vb["grain_lbs"] += r["weight_lbs"]
        if r["bales"] > 0:
            vb["bale_spend"] += r["cost_total"]
            vb["bales"] += r["bales"]
        vb["type_spend"][fg] = vb["type_spend"].get(fg, 0.0) + r["cost_total"]

    feed_monthly_series: list[dict] = []
    for ym in sorted(by_month):
        mb = by_month[ym]
        avg_lb = round(mb["w_spend"] / mb["w_lbs"], 4) if mb["w_lbs"] > 0 else None
        feed_monthly_series.append(
            {
                "month": ym,
                "label": mb["label"],
                "total_spend": round(mb["total_spend"], 2),
                "total_lbs": round(mb["total_lbs"], 1),
                "avg_per_lb": avg_lb,
                "avg_bag_equiv": round(avg_lb * safe_bw, 2) if avg_lb is not None else None,
                "hay_avg_bale": (
                    round(mb["hay_bale_spend"] / mb["hay_bales"], 2)
                    if mb["hay_bales"] > 0
                    else None
                ),
                "count": mb["count"],
            }
        )

    feed_chart = _build_feed_chart(feed_monthly_series)

    feed_by_type: list[dict] = []
    for tb in sorted(by_type.values(), key=lambda x: -x["total_spend"]):
        avg_lb = round(tb["w_spend"] / tb["w_lbs"], 4) if tb["w_lbs"] > 0 else None
        pts = tb["pts"]
        latest_lb = pts[-1] if pts else None
        delta_pct = (
            round(((pts[-1] - pts[0]) / pts[0]) * 100.0, 1)
            if (len(pts) >= 2 and pts[0] > 0)
            else None
        )
        feed_by_type.append(
            {
                "group": tb["group"],
                "count": tb["count"],
                "total_spend": round(tb["total_spend"], 2),
                "total_lbs": round(tb["total_lbs"], 1),
                "avg_per_lb": avg_lb,
                "avg_bag_equiv": (
                    round(avg_lb * safe_bw, 2)
                    if (avg_lb is not None and tb["group"] != "Hay")
                    else None
                ),
                "avg_per_bale": round(tb["bale_spend"] / tb["bales"], 2) if tb["bales"] > 0 else None,
                "latest_per_lb": round(latest_lb, 4) if latest_lb is not None else None,
                "delta_pct": delta_pct,
            }
        )

    vendor_rows: list[dict] = []
    for vb in by_vendor.values():
        avg_lb = round(vb["w_spend"] / vb["w_lbs"], 4) if vb["w_lbs"] > 0 else None
        grain_lb = round(vb["grain_spend"] / vb["grain_lbs"], 4) if vb["grain_lbs"] > 0 else None
        top_types = ", ".join(
            k for k, _ in sorted(vb["type_spend"].items(), key=lambda kv: -kv[1])[:3]
        )
        vendor_rows.append(
            {
                "vendor": vb["vendor"],
                "is_named": vb["is_named"],
                "count": vb["count"],
                "total_spend": round(vb["total_spend"], 2),
                "total_lbs": round(vb["total_lbs"], 1),
                "avg_per_lb": avg_lb,
                "grain_avg_per_lb": grain_lb,
                "avg_bag_equiv": round(grain_lb * safe_bw, 2) if grain_lb is not None else None,
                "hay_avg_bale": round(vb["bale_spend"] / vb["bales"], 2) if vb["bales"] > 0 else None,
                "feed_types": top_types,
                "cheapest_grain": False,
            }
        )
    vendor_rows.sort(key=lambda v: (not v["is_named"], -v["total_spend"], v["vendor"].lower()))
    grain_vendors = [v for v in vendor_rows if v["is_named"] and v["grain_avg_per_lb"] is not None]
    if len(grain_vendors) >= 2:
        best_v = min(grain_vendors, key=lambda v: v["grain_avg_per_lb"])
        best_v["cheapest_grain"] = True
    else:
        named_w = [v for v in vendor_rows if v["is_named"] and v["avg_per_lb"] is not None]
        if len(named_w) >= 2:
            min(named_w, key=lambda v: v["avg_per_lb"])["cheapest_grain"] = True

    # 2. Outlier scan (>40% off category median $/lb, >= 3 weighted entries in group)
    price_outliers: list[dict] = []
    by_group_weighted: dict[str, list[dict]] = {}
    for r in yr_feed:
        if r["cost_per_lb"] is not None:
            by_group_weighted.setdefault(r["feed_group"], []).append(r)
    for fg, grp_recs in by_group_weighted.items():
        if len(grp_recs) < 3:
            continue
        med = float(statistics.median(r["cost_per_lb"] for r in grp_recs))
        if med <= 0:
            continue
        for r in grp_recs:
            ratio = (r["cost_per_lb"] - med) / med
            if abs(ratio) > 0.40:
                price_outliers.append(
                    {
                        "date": r["date"],
                        "vendor": r["vendor"] or "—",
                        "feed_group": fg,
                        "allocation": r["allocation"],
                        "weight_lbs": r["weight_lbs"],
                        "cost_total": r["cost_total"],
                        "cost_per_lb": r["cost_per_lb"],
                        "median_per_lb": round(med, 4),
                        "pct_diff": round(ratio * 100.0, 0),
                        "source_url": r["source_url"],
                    }
                )
    price_outliers.sort(key=lambda o: -abs(o["pct_diff"]))
    price_outliers = price_outliers[:10]

    # 3. Preset calibrations, enterprise P&L, egg cost/doz, and batch FCR
    preset_calibrations = _compute_preset_calibrations(uid, all_feed, safe_bw)
    enterprises, untagged_general_spend = _compute_enterprise_breakdown(
        uid, merchant_id=merchant_id, year=year, all_feed_records=all_feed
    )
    egg_cost_summary = _compute_egg_monthly_cost(uid, year=year)
    batch_benchmarks = _compute_batch_benchmarks(schedules or [], year=year)

    return {
        "bag_weight": safe_bw,
        "feed_kpis": {
            "total_feed_spend": total_feed_spend,
            "total_feed_lbs": total_feed_lbs,
            "avg_cost_per_lb": avg_cost_per_lb,
            "avg_bag_equiv": avg_bag_equiv,
            "hay_spend": hay_spend,
            "hay_bales": hay_bales,
            "hay_avg_bale": hay_avg_bale,
            "entry_count": len(yr_feed),
            "vendor_count": len(named_vendors),
        },
        "feed_monthly_series": feed_monthly_series,
        "feed_chart": feed_chart,
        "feed_by_type": feed_by_type,
        "vendor_comparison": vendor_rows,
        "preset_calibrations": preset_calibrations,
        "price_outliers": price_outliers,
        "enterprises": enterprises,
        "untagged_general_spend": untagged_general_spend,
        "egg_cost_summary": egg_cost_summary,
        "batch_benchmarks": batch_benchmarks,
    }
