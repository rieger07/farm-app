"""Batch lifecycle scheduling, phased feed burn, barrel storage demand, and farm milestones."""

from datetime import date, timedelta
import math
import pandas as pd

from homestead.calendar_export import create_gcal_link
from homestead.config import (
    DEFAULT_BAG_WEIGHT,
    DEFAULT_BARREL_CAPACITY,
    get_species_defaults,
)
from homestead.database import (
    list_batch_harvests,
    list_batch_losses,
    list_batch_sales,
)


def get_batch_status_badge(schedule: dict) -> str:
    b = schedule["batch"]
    if not b["active"]:
        return "⚪ Excluded"
    if schedule["is_fully_harvested"]:
        return "🥩 Harvested"
    return "🟢 Active"


def compute_batch_schedule(
    batch: dict,
    barrel_capacity: int = DEFAULT_BARREL_CAPACITY,
    bag_weight: int = DEFAULT_BAG_WEIGHT,
    losses: list[dict] | None = None,
    harvests: list[dict] | None = None,
    sales: list[dict] | None = None,
) -> dict:
    start_dt = date.fromisoformat(batch["start_date"])
    end_dt = date.fromisoformat(batch["end_date"])
    if end_dt < start_dt:
        start_dt, end_dt = end_dt, start_dt

    species = batch["species"]
    defaults = get_species_defaults(species)
    is_pigs = "pigs" in species.lower()
    days_to_birth = int(batch["days_to_birth"])
    initial_quantity = max(1, int(batch["quantity"]))

    starter_weeks = int(
        batch["starter_weeks"]
        if batch.get("starter_weeks") is not None
        else defaults["starter_weeks"]
    )
    grower_weeks = int(
        batch["grower_weeks"]
        if batch.get("grower_weeks") is not None
        else defaults["grower_weeks"]
    )
    if starter_weeks + grower_weeks < 1:
        grower_weeks = max(1, int(batch.get("weeks_to_proc", 8)))
    weeks_to_proc = max(1, starter_weeks + grower_weeks)

    starter_feed_per_animal = float(
        batch["starter_feed_lbs"]
        if batch.get("starter_feed_lbs") is not None
        else defaults["starter_feed_lbs"]
    )
    grower_feed_per_animal = float(
        batch["grower_feed_lbs"]
        if batch.get("grower_feed_lbs") is not None
        else defaults["grower_feed_lbs"]
    )
    starter_bag_price = float(
        batch["starter_bag_price"]
        if batch.get("starter_bag_price") is not None
        else defaults["starter_bag_price"]
    )
    grower_bag_price = float(
        batch["grower_bag_price"]
        if batch.get("grower_bag_price") is not None
        else defaults["grower_bag_price"]
    )
    unit_cost = float(
        batch["unit_cost"]
        if batch.get("unit_cost") is not None
        else defaults["unit_cost"]
    )
    processing_cost = float(
        batch["processing_cost"]
        if batch.get("processing_cost") is not None
        else defaults["processing_cost"]
    )
    expected_yield_per_head = float(
        batch["expected_yield_lbs"]
        if batch.get("expected_yield_lbs") is not None
        else defaults["expected_yield_lbs"]
    )
    sale_price_per_lb = float(
        batch["sale_price_per_lb"]
        if batch.get("sale_price_per_lb") is not None
        else defaults["sale_price_per_lb"]
    )

    batch_id = int(batch["id"])
    if losses is None:
        losses = list_batch_losses(batch_id)
    if harvests is None:
        harvests = list_batch_harvests(batch_id)
    if sales is None:
        sales = list_batch_sales(batch_id)

    parsed_losses = []
    for l in losses:
        try:
            l_dt = date.fromisoformat(str(l["loss_date"]))
        except ValueError:
            l_dt = start_dt
        parsed_losses.append(
            {
                "id": l.get("id", 0),
                "batch_id": batch_id,
                "loss_date": l_dt,
                "loss_date_str": l_dt.isoformat(),
                "count": max(1, int(l.get("count", 1))),
                "reason": str(l.get("reason", "")),
            }
        )
    parsed_losses.sort(key=lambda x: (x["loss_date"], x["id"]))

    parsed_harvests = []
    for h in harvests:
        try:
            h_dt = date.fromisoformat(str(h["harvest_date"]))
        except ValueError:
            h_dt = end_dt
        parsed_harvests.append(
            {
                "id": h.get("id", 0),
                "batch_id": batch_id,
                "harvest_date": h_dt,
                "harvest_date_str": h_dt.isoformat(),
                "head_count": max(1, int(h.get("head_count", 1))),
                "dressed_lbs": max(0.0, float(h.get("dressed_lbs", 0.0))),
                "processing_cost_total": max(
                    0.0, float(h.get("processing_cost_total", 0.0))
                ),
                "notes": str(h.get("notes", "")),
            }
        )
    parsed_harvests.sort(key=lambda x: (x["harvest_date"], x["id"]))

    parsed_sales = []
    for sl in sales:
        try:
            s_dt = date.fromisoformat(str(sl["entry_date"]))
        except ValueError:
            s_dt = end_dt
        parsed_sales.append(
            {
                "id": sl.get("id", 0),
                "batch_id": batch_id,
                "entry_date": s_dt,
                "entry_date_str": s_dt.isoformat(),
                "allocation_type": str(
                    sl.get("allocation_type", "Customer Sale")
                ),
                "weight_lbs": max(0.0, float(sl.get("weight_lbs", 0.0))),
                "revenue": max(0.0, float(sl.get("revenue", 0.0))),
                "notes": str(sl.get("notes", "")),
            }
        )
    parsed_sales.sort(key=lambda x: (x["entry_date"], x["id"]))

    def surviving_on_date(d: date) -> int:
        lost_so_far = sum(
            item["count"] for item in parsed_losses if item["loss_date"] <= d
        )
        return max(0, initial_quantity - lost_so_far)

    def feeding_on_date(d: date) -> int:
        """Animals still on feed on date `d` (subtracts losses up to `d` and harvests strictly before `d`)."""
        lost_so_far = sum(
            item["count"] for item in parsed_losses if item["loss_date"] <= d
        )
        harvested_before = sum(
            item["head_count"]
            for item in parsed_harvests
            if item["harvest_date"] < d
        )
        return max(0, initial_quantity - lost_so_far - harvested_before)

    total_lost = min(
        initial_quantity, sum(item["count"] for item in parsed_losses)
    )
    surviving_quantity = max(0, initial_quantity - total_lost)

    initial_label = "Breeding" if is_pigs else "Arrival"
    gestation_label = "Gestation" if is_pigs else "Incubation"
    birth_label = "Birth" if is_pigs else "Hatch"

    timeline_items = []

    # Phase 1: Breeding or Arrival
    if start_dt == end_dt:
        timeline_items.append(
            {
                "id": f"b{batch_id}_init",
                "group": batch_id,
                "content": f"📥 {initial_label}",
                "start": start_dt.isoformat(),
                "end": (end_dt + timedelta(days=1)).isoformat(),
                "style": "background-color: #dbeafe; border-color: #2563eb; color: #1e3a8a;",
                "title": f"{batch['name']} — {initial_label}: {start_dt.isoformat()}",
            }
        )
    else:
        timeline_items.append(
            {
                "id": f"b{batch_id}_init",
                "group": batch_id,
                "content": f"📥 {initial_label}",
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "style": "background-color: #dbeafe; border-color: #2563eb; color: #1e3a8a;",
                "title": f"{batch['name']} — {initial_label}: {start_dt.isoformat()} to {end_dt.isoformat()}",
            }
        )

    # Phase 2: Gestation / Incubation (if applicable)
    if days_to_birth > 0:
        birth_start = start_dt + timedelta(days=days_to_birth)
        birth_end = end_dt + timedelta(days=days_to_birth)
        timeline_items.append(
            {
                "id": f"b{batch_id}_gest",
                "group": batch_id,
                "content": f"⏳ {gestation_label}",
                "start": end_dt.isoformat(),
                "end": birth_end.isoformat(),
                "style": "background-color: #fef3c7; border-color: #d97706; color: #78350f;",
                "title": (
                    f"{batch['name']} — {gestation_label} ({days_to_birth}d) | "
                    f"{birth_label} Window: {birth_start.isoformat()} to {birth_end.isoformat()}"
                ),
            }
        )
        proc_base = birth_end
        proc_window_start = birth_start + timedelta(weeks=weeks_to_proc)
    else:
        birth_start = None
        birth_end = None
        proc_base = start_dt
        proc_window_start = start_dt + timedelta(weeks=weeks_to_proc)

    transition_date = proc_base + timedelta(weeks=starter_weeks)
    proc_date = transition_date + timedelta(weeks=grower_weeks)

    # Day-by-day feed consumption adjusted for losses AND early/split harvests
    starter_days = max(0, (transition_date - proc_base).days)
    daily_starter_per_head = (
        starter_feed_per_animal / starter_days if starter_days > 0 else 0.0
    )
    starter_feed_actual = 0.0
    starter_feed_loss_only = 0.0
    for day_offset in range(starter_days):
        d = proc_base + timedelta(days=day_offset)
        starter_feed_actual += feeding_on_date(d) * daily_starter_per_head
        starter_feed_loss_only += surviving_on_date(d) * daily_starter_per_head
    starter_feed_actual = round(starter_feed_actual, 2)
    starter_feed_loss_only = round(starter_feed_loss_only, 2)
    starter_feed_planned = round(
        initial_quantity * starter_feed_per_animal, 2
    )

    grower_days = max(0, (proc_date - transition_date).days)
    daily_grower_per_head = (
        grower_feed_per_animal / grower_days if grower_days > 0 else 0.0
    )
    grower_feed_actual = 0.0
    grower_feed_loss_only = 0.0
    for day_offset in range(grower_days):
        d = transition_date + timedelta(days=day_offset)
        grower_feed_actual += feeding_on_date(d) * daily_grower_per_head
        grower_feed_loss_only += surviving_on_date(d) * daily_grower_per_head
    grower_feed_actual = round(grower_feed_actual, 2)
    grower_feed_loss_only = round(grower_feed_loss_only, 2)
    grower_feed_planned = round(initial_quantity * grower_feed_per_animal, 2)

    total_feed_planned = round(starter_feed_planned + grower_feed_planned, 2)
    total_feed_loss_only = round(
        starter_feed_loss_only + grower_feed_loss_only, 2
    )
    total_feed = round(starter_feed_actual + grower_feed_actual, 2)
    feed_saved_by_losses = round(
        max(0.0, total_feed_planned - total_feed_loss_only), 2
    )
    feed_saved_by_early_harvest = round(
        max(0.0, total_feed_loss_only - total_feed), 2
    )

    starter_weekly_feed = (
        starter_feed_actual / starter_weeks if starter_weeks > 0 else 0.0
    )
    grower_weekly_feed = (
        grower_feed_actual / grower_weeks if grower_weeks > 0 else 0.0
    )
    weekly_feed = total_feed / weeks_to_proc

    barrels_needed = math.ceil(total_feed / max(1, barrel_capacity))
    safe_bag_wt = max(1, bag_weight)
    starter_bags = (
        math.ceil(starter_feed_actual / safe_bag_wt)
        if starter_feed_actual > 0
        else 0
    )
    grower_bags = (
        math.ceil(grower_feed_actual / safe_bag_wt)
        if grower_feed_actual > 0
        else 0
    )
    total_bags = starter_bags + grower_bags

    # Budget & ROI Calculations (Estimated / Planned Baseline)
    starter_cost_bags = round(starter_bags * starter_bag_price, 2)
    grower_cost_bags = round(grower_bags * grower_bag_price, 2)
    total_feed_cost_bags = round(starter_cost_bags + grower_cost_bags, 2)

    starter_cost_exact = round(
        (starter_feed_actual / safe_bag_wt) * starter_bag_price, 2
    )
    grower_cost_exact = round(
        (grower_feed_actual / safe_bag_wt) * grower_bag_price, 2
    )
    total_feed_cost_exact = round(starter_cost_exact + grower_cost_exact, 2)

    acquisition_cost = round(initial_quantity * unit_cost, 2)
    total_processing_cost = round(surviving_quantity * processing_cost, 2)
    total_budget_cost = round(
        acquisition_cost + total_feed_cost_bags + total_processing_cost, 2
    )

    expected_meat_lbs = round(surviving_quantity * expected_yield_per_head, 2)
    feed_cost_per_lb_meat = (
        round(total_feed_cost_bags / expected_meat_lbs, 2)
        if expected_meat_lbs > 0
        else 0.0
    )
    total_cost_per_lb_meat = (
        round(total_budget_cost / expected_meat_lbs, 2)
        if expected_meat_lbs > 0
        else 0.0
    )
    estimated_revenue = round(expected_meat_lbs * sale_price_per_lb, 2)
    estimated_profit = round(estimated_revenue - total_budget_cost, 2)
    profit_per_lb_meat = (
        round(sale_price_per_lb - total_cost_per_lb_meat, 2)
        if expected_meat_lbs > 0
        else 0.0
    )
    roi_margin_pct = (
        round((estimated_profit / total_budget_cost) * 100.0, 1)
        if total_budget_cost > 0
        else 0.0
    )

    # -----------------------------------------------------------------------
    # Actual Harvest, Feed Override & Sales / Home Freezer Reconciliation
    # -----------------------------------------------------------------------
    harvested_head = min(
        surviving_quantity,
        sum(item["head_count"] for item in parsed_harvests),
    )
    unharvested_surviving_head = max(0, surviving_quantity - harvested_head)
    is_fully_harvested = (
        surviving_quantity > 0 and unharvested_surviving_head == 0
    )

    actual_dressed_lbs = round(
        sum(item["dressed_lbs"] for item in parsed_harvests), 2
    )
    actual_avg_yield_per_head = (
        round(actual_dressed_lbs / harvested_head, 2)
        if harvested_head > 0
        else 0.0
    )
    actual_harvest_proc_cost = round(
        sum(item["processing_cost_total"] for item in parsed_harvests), 2
    )

    actual_feed_lbs_override = (
        float(batch["actual_feed_lbs"])
        if batch.get("actual_feed_lbs") is not None
        else None
    )
    actual_feed_cost_override = (
        float(batch["actual_feed_cost"])
        if batch.get("actual_feed_cost") is not None
        else None
    )
    reconciled_feed_lbs = (
        round(actual_feed_lbs_override, 2)
        if actual_feed_lbs_override is not None
        else total_feed
    )
    reconciled_feed_cost = (
        round(actual_feed_cost_override, 2)
        if actual_feed_cost_override is not None
        else total_feed_cost_bags
    )

    # Processing cost uses logged actual harvest processing fees + estimated fee for any remaining unharvested head
    reconciled_processing_cost = (
        round(
            actual_harvest_proc_cost
            + (unharvested_surviving_head * processing_cost),
            2,
        )
        if parsed_harvests
        else total_processing_cost
    )
    reconciled_total_cost = round(
        acquisition_cost + reconciled_feed_cost + reconciled_processing_cost, 2
    )

    # Total meat yield blends actual harvested dressed lbs + expected yield for any remaining unharvested animals
    reconciled_meat_lbs = (
        round(
            actual_dressed_lbs
            + (unharvested_surviving_head * expected_yield_per_head),
            2,
        )
        if parsed_harvests
        else expected_meat_lbs
    )
    reconciled_cost_per_lb_meat = (
        round(reconciled_total_cost / reconciled_meat_lbs, 2)
        if reconciled_meat_lbs > 0
        else 0.0
    )

    sold_lbs = round(
        sum(
            item["weight_lbs"]
            for item in parsed_sales
            if item["allocation_type"] == "Customer Sale"
        ),
        2,
    )
    actual_sales_revenue = round(
        sum(
            item["revenue"]
            for item in parsed_sales
            if item["allocation_type"] == "Customer Sale"
        ),
        2,
    )
    freezer_lbs = round(
        sum(
            item["weight_lbs"]
            for item in parsed_sales
            if item["allocation_type"] == "Home Freezer"
        ),
        2,
    )
    allocated_lbs = round(sold_lbs + freezer_lbs, 2)
    unallocated_harvested_lbs = round(
        max(0.0, actual_dressed_lbs - allocated_lbs), 2
    )

    imputed_freezer_value = round(freezer_lbs * sale_price_per_lb, 2)
    actual_cash_profit = round(actual_sales_revenue - reconciled_total_cost, 2)
    total_realized_value = round(
        actual_sales_revenue + imputed_freezer_value, 2
    )
    total_realized_profit = round(
        total_realized_value - reconciled_total_cost, 2
    )

    # Effective Cost per lb of Home Freezer Meat after offsetting batch costs with customer sales revenue
    net_cost_for_freezer = max(0.0, reconciled_total_cost - actual_sales_revenue)
    effective_freezer_cost_per_lb = (
        round(net_cost_for_freezer / freezer_lbs, 2)
        if freezer_lbs > 0
        else None
    )

    # Phase 3a: Starter Timeline Item
    if starter_weeks > 0:
        timeline_items.append(
            {
                "id": f"b{batch_id}_starter",
                "group": batch_id,
                "content": f"🌱 Starter ({starter_weekly_feed:.1f} lbs/wk)",
                "start": proc_base.isoformat(),
                "end": transition_date.isoformat(),
                "style": "background-color: #ecfccb; border-color: #65a30d; color: #365314;",
                "title": (
                    f"{batch['name']} — Starter Phase ({starter_weeks} wks): "
                    f"{proc_base.isoformat()} to {transition_date.isoformat()} | "
                    f"Feed: {starter_feed_actual:,.1f} lbs ({starter_bags} bags @ ${starter_bag_price:.2f})"
                ),
            }
        )

    # Phase 3b: Grower/Finisher Timeline Item
    if grower_weeks > 0:
        timeline_items.append(
            {
                "id": f"b{batch_id}_grow",
                "group": batch_id,
                "content": f"🌾 Grower/Finisher ({grower_weekly_feed:.1f} lbs/wk)",
                "start": transition_date.isoformat(),
                "end": proc_date.isoformat(),
                "style": "background-color: #dcfce7; border-color: #16a34a; color: #14532d;",
                "title": (
                    f"{batch['name']} — Grower/Finisher Phase ({grower_weeks} wks): "
                    f"{transition_date.isoformat()} to {proc_date.isoformat()} | "
                    f"Feed: {grower_feed_actual:,.1f} lbs ({grower_bags} bags @ ${grower_bag_price:.2f})"
                ),
            }
        )

    # Phase 4: Planned Processing Timeline Item (if unharvested animals remain or no harvest logged yet)
    if unharvested_surviving_head > 0 or not parsed_harvests:
        timeline_items.append(
            {
                "id": f"b{batch_id}_proc",
                "group": batch_id,
                "content": f"🔪 Target Processing ({unharvested_surviving_head} head)",
                "start": proc_date.isoformat(),
                "style": "background-color: #fee2e2; border-color: #dc2626; color: #7f1d1d;",
                "title": (
                    f"{batch['name']} — Planned Processing Date: {proc_date.isoformat()} | "
                    f"Remaining to harvest: {unharvested_surviving_head}/{surviving_quantity} head"
                ),
            }
        )

    # Actual Harvest Markers on Timeline Swimlane
    for h_item in parsed_harvests:
        note_suffix = f" ({h_item['notes']})" if h_item["notes"] else ""
        timeline_items.append(
            {
                "id": f"b{batch_id}_harvest_{h_item['id']}",
                "group": batch_id,
                "content": f"🥩 Harvested {h_item['head_count']} ({h_item['dressed_lbs']:,.1f} lbs)",
                "start": h_item["harvest_date_str"],
                "style": "background-color: #d1fae5; border-color: #059669; color: #065f46; font-weight: 600;",
                "title": (
                    f"{batch['name']} — Actual Harvest on {h_item['harvest_date_str']}: "
                    f"{h_item['head_count']} head → {h_item['dressed_lbs']:,.1f} lbs dressed "
                    f"(Processing fee: ${h_item['processing_cost_total']:,.2f}){note_suffix}"
                ),
            }
        )

    # Loss Markers on Timeline Swimlane
    for l_item in parsed_losses:
        reason_suffix = f" ({l_item['reason']})" if l_item["reason"] else ""
        timeline_items.append(
            {
                "id": f"b{batch_id}_loss_{l_item['id']}",
                "group": batch_id,
                "content": f"⚠️ -{l_item['count']} Lost",
                "start": l_item["loss_date_str"],
                "style": "background-color: #fef2f2; border-color: #ef4444; color: #991b1b; font-weight: 600;",
                "title": (
                    f"{batch['name']} — Loss on {l_item['loss_date_str']}: "
                    f"-{l_item['count']} head{reason_suffix}"
                ),
            }
        )

    return {
        "batch": batch,
        "losses": parsed_losses,
        "harvests": parsed_harvests,
        "sales": parsed_sales,
        "initial_quantity": initial_quantity,
        "total_lost": total_lost,
        "surviving_quantity": surviving_quantity,
        "harvested_head": harvested_head,
        "unharvested_surviving_head": unharvested_surviving_head,
        "is_fully_harvested": is_fully_harvested,
        "surviving_on_date": surviving_on_date,
        "feeding_on_date": feeding_on_date,
        "start_date": start_dt,
        "end_date": end_dt,
        "initial_label": initial_label,
        "gestation_label": gestation_label,
        "birth_label": birth_label,
        "birth_start": birth_start,
        "birth_end": birth_end,
        "proc_base": proc_base,
        "transition_date": transition_date,
        "proc_window_start": proc_window_start,
        "proc_date": proc_date,
        "starter_weeks": starter_weeks,
        "grower_weeks": grower_weeks,
        "weeks_to_proc": weeks_to_proc,
        "starter_feed_per_animal": starter_feed_per_animal,
        "grower_feed_per_animal": grower_feed_per_animal,
        "daily_starter_per_head": daily_starter_per_head,
        "daily_grower_per_head": daily_grower_per_head,
        "starter_feed_planned": starter_feed_planned,
        "grower_feed_planned": grower_feed_planned,
        "total_feed_planned": total_feed_planned,
        "starter_feed": starter_feed_actual,
        "grower_feed": grower_feed_actual,
        "total_feed": total_feed,
        "feed_saved_by_losses": feed_saved_by_losses,
        "feed_saved_by_early_harvest": feed_saved_by_early_harvest,
        "starter_weekly_feed": starter_weekly_feed,
        "grower_weekly_feed": grower_weekly_feed,
        "weekly_feed": weekly_feed,
        "barrels_needed": barrels_needed,
        "starter_bags": starter_bags,
        "grower_bags": grower_bags,
        "total_bags": total_bags,
        "starter_bag_price": starter_bag_price,
        "grower_bag_price": grower_bag_price,
        "starter_cost_bags": starter_cost_bags,
        "grower_cost_bags": grower_cost_bags,
        "total_feed_cost_bags": total_feed_cost_bags,
        "total_feed_cost_exact": total_feed_cost_exact,
        "unit_cost": unit_cost,
        "acquisition_cost": acquisition_cost,
        "processing_cost": processing_cost,
        "total_processing_cost": total_processing_cost,
        "total_budget_cost": total_budget_cost,
        "expected_yield_per_head": expected_yield_per_head,
        "expected_meat_lbs": expected_meat_lbs,
        "feed_cost_per_lb_meat": feed_cost_per_lb_meat,
        "total_cost_per_lb_meat": total_cost_per_lb_meat,
        "sale_price_per_lb": sale_price_per_lb,
        "estimated_revenue": estimated_revenue,
        "estimated_profit": estimated_profit,
        "profit_per_lb_meat": profit_per_lb_meat,
        "roi_margin_pct": roi_margin_pct,
        "actual_dressed_lbs": actual_dressed_lbs,
        "actual_avg_yield_per_head": actual_avg_yield_per_head,
        "actual_harvest_proc_cost": actual_harvest_proc_cost,
        "actual_feed_lbs_override": actual_feed_lbs_override,
        "actual_feed_cost_override": actual_feed_cost_override,
        "reconciled_feed_lbs": reconciled_feed_lbs,
        "reconciled_feed_cost": reconciled_feed_cost,
        "reconciled_processing_cost": reconciled_processing_cost,
        "reconciled_total_cost": reconciled_total_cost,
        "reconciled_meat_lbs": reconciled_meat_lbs,
        "reconciled_cost_per_lb_meat": reconciled_cost_per_lb_meat,
        "sold_lbs": sold_lbs,
        "actual_sales_revenue": actual_sales_revenue,
        "freezer_lbs": freezer_lbs,
        "allocated_lbs": allocated_lbs,
        "unallocated_harvested_lbs": unallocated_harvested_lbs,
        "imputed_freezer_value": imputed_freezer_value,
        "actual_cash_profit": actual_cash_profit,
        "total_realized_value": total_realized_value,
        "total_realized_profit": total_realized_profit,
        "net_cost_for_freezer": net_cost_for_freezer,
        "effective_freezer_cost_per_lb": effective_freezer_cost_per_lb,
        "timeline_items": timeline_items,
    }


def _batch_feed_on_day(s: dict, d: date) -> tuple[float, float]:
    """Return (starter_lbs, grower_lbs) consumed by schedule `s` on day `d`, adjusted for losses and early harvests."""
    head_fn = s.get("feeding_on_date", s["surviving_on_date"])
    if s["proc_base"] <= d < s["transition_date"] and s["starter_weeks"] > 0:
        return head_fn(d) * s["daily_starter_per_head"], 0.0
    if s["transition_date"] <= d < s["proc_date"] and s["grower_weeks"] > 0:
        return 0.0, head_fn(d) * s["daily_grower_per_head"]
    return 0.0, 0.0


def compute_combined_feed_schedule(
    schedules: list[dict], barrel_capacity: int = DEFAULT_BARREL_CAPACITY
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """
    Build weekly and monthly time-phased feed demand across active batches,
    accounting for Starter vs. Grower/Finisher phases and dated batch losses.
    """
    if not schedules:
        empty_df = pd.DataFrame()
        return (
            empty_df,
            empty_df,
            {
                "peak_weekly_lbs": 0.0,
                "peak_concurrent_lbs": 0.0,
                "peak_concurrent_barrels": 0,
            },
        )

    min_grow_start = min(s["proc_base"] for s in schedules)
    max_grow_end = max(s["proc_date"] for s in schedules)

    # Precompute daily feed per schedule so remaining active feed is fast and exact
    daily_maps: dict[int, dict[date, tuple[float, float]]] = {}
    for s in schedules:
        bid = int(s["batch"]["id"])
        d_map: dict[date, tuple[float, float]] = {}
        curr_d = s["proc_base"]
        while curr_d < s["proc_date"]:
            d_map[curr_d] = _batch_feed_on_day(s, curr_d)
            curr_d += timedelta(days=1)
        daily_maps[bid] = d_map

    first_monday = min_grow_start - timedelta(days=min_grow_start.weekday())
    records = []

    curr_week_start = first_monday
    while curr_week_start < max_grow_end:
        curr_week_end = curr_week_start + timedelta(days=7)
        row: dict[str, float | int | str] = {
            "Week Starting": curr_week_start.isoformat(),
            "Starter Feed (lbs)": 0.0,
            "Grower/Finisher Feed (lbs)": 0.0,
            "Total Weekly Feed (lbs)": 0.0,
            "Remaining Active Batch Feed (lbs)": 0.0,
        }

        for s in schedules:
            bid = int(s["batch"]["id"])
            b_name = f"{s['batch']['name']} (#{bid})"
            d_map = daily_maps[bid]

            st_week = 0.0
            gr_week = 0.0
            for day_offset in range(7):
                d = curr_week_start + timedelta(days=day_offset)
                if d in d_map:
                    st_d, gr_d = d_map[d]
                    st_week += st_d
                    gr_week += gr_d

            batch_week_total = st_week + gr_week
            row[b_name] = round(batch_week_total, 2)
            row["Starter Feed (lbs)"] = float(row["Starter Feed (lbs)"]) + st_week
            row["Grower/Finisher Feed (lbs)"] = (
                float(row["Grower/Finisher Feed (lbs)"]) + gr_week
            )
            row["Total Weekly Feed (lbs)"] = (
                float(row["Total Weekly Feed (lbs)"]) + batch_week_total
            )

            # If this batch is actively growing during this week, sum remaining feed from overlap_start to proc_date
            overlap_start = max(curr_week_start, s["proc_base"])
            overlap_end = min(curr_week_end, s["proc_date"])
            if overlap_start < overlap_end:
                rem_feed = sum(
                    st_d + gr_d
                    for d_key, (st_d, gr_d) in d_map.items()
                    if d_key >= overlap_start
                )
                row["Remaining Active Batch Feed (lbs)"] = (
                    float(row["Remaining Active Batch Feed (lbs)"]) + rem_feed
                )

        row["Starter Feed (lbs)"] = round(float(row["Starter Feed (lbs)"]), 2)
        row["Grower/Finisher Feed (lbs)"] = round(
            float(row["Grower/Finisher Feed (lbs)"]), 2
        )
        row["Total Weekly Feed (lbs)"] = round(
            float(row["Total Weekly Feed (lbs)"]), 2
        )
        row["Remaining Active Batch Feed (lbs)"] = round(
            float(row["Remaining Active Batch Feed (lbs)"]), 2
        )
        row["Concurrent Barrels Needed (Remaining Active)"] = math.ceil(
            float(row["Remaining Active Batch Feed (lbs)"])
            / max(1, barrel_capacity)
        )
        records.append(row)
        curr_week_start = curr_week_end

    weekly_df = pd.DataFrame(records)
    if weekly_df.empty:
        return (
            weekly_df,
            pd.DataFrame(),
            {
                "peak_weekly_lbs": 0.0,
                "peak_concurrent_lbs": 0.0,
                "peak_concurrent_barrels": 0,
            },
        )

    monthly_df = weekly_df.copy()
    monthly_df["Month"] = monthly_df["Week Starting"].str.slice(0, 7)
    monthly_summary = (
        monthly_df.groupby("Month", as_index=False)
        .agg(
            Starter_Feed_lbs=("Starter Feed (lbs)", "sum"),
            Grower_Feed_lbs=("Grower/Finisher Feed (lbs)", "sum"),
            Monthly_Feed_lbs=("Total Weekly Feed (lbs)", "sum"),
            Peak_Concurrent_Barrels=(
                "Concurrent Barrels Needed (Remaining Active)",
                "max",
            ),
        )
        .rename(
            columns={
                "Starter_Feed_lbs": "Starter Feed (lbs)",
                "Grower_Feed_lbs": "Grower/Finisher Feed (lbs)",
                "Monthly_Feed_lbs": "Monthly Feed Consumed (lbs)",
                "Peak_Concurrent_Barrels": "Peak Concurrent Barrels in Month",
            }
        )
    )
    for col in [
        "Starter Feed (lbs)",
        "Grower/Finisher Feed (lbs)",
        "Monthly Feed Consumed (lbs)",
    ]:
        monthly_summary[col] = monthly_summary[col].round(1)
    monthly_summary["Barrels Consumed in Month"] = monthly_summary[
        "Monthly Feed Consumed (lbs)"
    ].apply(lambda lbs: round(lbs / max(1, barrel_capacity), 2))

    stats = {
        "peak_weekly_lbs": float(weekly_df["Total Weekly Feed (lbs)"].max()),
        "peak_concurrent_lbs": float(
            weekly_df["Remaining Active Batch Feed (lbs)"].max()
        ),
        "peak_concurrent_barrels": int(
            weekly_df["Concurrent Barrels Needed (Remaining Active)"].max()
        ),
    }
    return weekly_df, monthly_summary, stats


def _format_relative_days_badge(
    start_d: date, end_d: date, today: date
) -> tuple[str, int]:
    """Return a human-friendly relative day badge and integer days_away."""
    days_away = (start_d - today).days
    if start_d <= today <= end_d:
        return "🟢 Today / Active Now", 0
    if days_away == 1:
        return "⏳ Tomorrow", 1
    if 1 < days_away <= 14:
        return f"⏳ In {days_away} days", days_away
    if days_away > 14:
        return f"📅 In {days_away} days", days_away
    days_ago = (today - end_d).days
    if days_ago == 1:
        return "🕓 Yesterday", days_away
    return f"🕓 {days_ago}d ago", days_away


def compute_upcoming_milestones(
    schedules: list[dict], today: date | None = None
) -> list[dict]:
    """
    Build a chronological list of farm milestones (Arrival/Breeding, Hatch/Birth,
    Starter -> Grower Ration Switch, and Processing Dates) across schedules.
    Prioritizes upcoming/active milestones (end_date >= today), falling back to
    chronological order if all milestones are historical.
    """
    if today is None:
        today = date.today()

    milestones: list[dict] = []
    for s in schedules:
        b = s["batch"]
        bid = int(b["id"])
        b_name = b["name"]
        sp_title = b["species"].title()

        # 1. Arrival / Breeding Window
        init_badge, init_days = _format_relative_days_badge(
            s["start_date"], s["end_date"], today
        )
        init_date_str = (
            s["start_date"].isoformat()
            if s["start_date"] == s["end_date"]
            else f"{s['start_date'].isoformat()} → {s['end_date'].isoformat()}"
        )
        milestones.append(
            {
                "batch_id": bid,
                "batch_name": b_name,
                "species": sp_title,
                "icon": "📥",
                "task": f"{s['initial_label']} Window",
                "event_date": s["start_date"],
                "end_date": s["end_date"],
                "date_display": init_date_str,
                "badge": init_badge,
                "days_away": init_days,
                "details": f"{s['initial_quantity']} × {sp_title} (${s['acquisition_cost']:,.2f})",
                "gcal_url": create_gcal_link(
                    f"{b_name} - {s['initial_label']} Window",
                    s["start_date"],
                    s["end_date"],
                    f"{s['initial_label']} window for {b_name} ({s['initial_quantity']}x {sp_title}).",
                ),
            }
        )

        # 2. Hatch / Birth Window
        if s["birth_start"] and s["birth_end"]:
            b_badge, b_days = _format_relative_days_badge(
                s["birth_start"], s["birth_end"], today
            )
            b_date_str = (
                s["birth_start"].isoformat()
                if s["birth_start"] == s["birth_end"]
                else f"{s['birth_start'].isoformat()} → {s['birth_end'].isoformat()}"
            )
            milestones.append(
                {
                    "batch_id": bid,
                    "batch_name": b_name,
                    "species": sp_title,
                    "icon": "🐣",
                    "task": f"{s['birth_label']} Window",
                    "event_date": s["birth_start"],
                    "end_date": s["birth_end"],
                    "date_display": b_date_str,
                    "badge": b_badge,
                    "days_away": b_days,
                    "details": f"Start {s['starter_bags']} Starter bag(s) ({s['starter_feed']:,.0f} lbs)",
                    "gcal_url": create_gcal_link(
                        f"{b_name} - {s['birth_label']} Window",
                        s["birth_start"],
                        s["birth_end"],
                        f"Estimated {s['birth_label'].lower()} window for {b_name} ({s['initial_quantity']}x {sp_title}).",
                    ),
                }
            )

        # 3. Starter -> Grower Ration Switch
        if s["starter_weeks"] > 0 and s["grower_weeks"] > 0:
            tr_badge, tr_days = _format_relative_days_badge(
                s["transition_date"], s["transition_date"], today
            )
            milestones.append(
                {
                    "batch_id": bid,
                    "batch_name": b_name,
                    "species": sp_title,
                    "icon": "🌾",
                    "task": "Starter → Grower Switch",
                    "event_date": s["transition_date"],
                    "end_date": s["transition_date"],
                    "date_display": s["transition_date"].isoformat(),
                    "badge": tr_badge,
                    "days_away": tr_days,
                    "details": f"Switch to {s['grower_bags']} Grower bag(s) ({s['grower_feed']:,.0f} lbs)",
                    "gcal_url": create_gcal_link(
                        f"{b_name} - Switch to Grower/Finisher Feed",
                        s["transition_date"],
                        s["transition_date"],
                        (
                            f"Switch {b_name} from Starter to Grower/Finisher feed. "
                            f"Need {s['grower_bags']} bags ({s['grower_feed']:,.1f} lbs)."
                        ),
                    ),
                }
            )

        # 4. Target Processing Date
        if not s["is_fully_harvested"]:
            pr_badge, pr_days = _format_relative_days_badge(
                s["proc_date"], s["proc_date"], today
            )
            milestones.append(
                {
                    "batch_id": bid,
                    "batch_name": b_name,
                    "species": sp_title,
                    "icon": "🔪",
                    "task": "Target Processing Date",
                    "event_date": s["proc_date"],
                    "end_date": s["proc_date"],
                    "date_display": s["proc_date"].isoformat(),
                    "badge": pr_badge,
                    "days_away": pr_days,
                    "details": (
                        f"{s['unharvested_surviving_head']} head → "
                        f"~{s['expected_meat_lbs']:,.1f} lbs meat"
                    ),
                    "gcal_url": create_gcal_link(
                        f"{b_name} - Processing Date",
                        s["proc_date"],
                        s["proc_date"],
                        (
                            f"Processing date for {b_name} ({s['surviving_quantity']} surviving head). "
                            f"Expected meat yield: {s['expected_meat_lbs']:,.1f} lbs."
                        ),
                    ),
                }
            )

    milestones.sort(key=lambda m: (m["event_date"], m["batch_id"], m["task"]))
    upcoming = [m for m in milestones if m["end_date"] >= today]
    return upcoming if upcoming else milestones
