"""Financials, shopping schedule, ROI summary, and harvest/freezer reconciliation tables."""

import pandas as pd

from homestead.config import DEFAULT_BAG_WEIGHT


def compute_purchasing_schedule(
    schedules: list[dict], bag_weight: int = DEFAULT_BAG_WEIGHT
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Build an itemized chronological Purchasing & Cash-Flow Schedule and a Monthly Cash-Flow Summary
    showing when to buy animals, Starter feed bags, Grower/Finisher feed bags, and pay processing fees.
    """
    if not schedules:
        return pd.DataFrame(), pd.DataFrame()

    events = []
    for s in schedules:
        b = s["batch"]
        b_label = f"{b['name']} (#{b['id']})"

        if s["acquisition_cost"] > 0:
            events.append(
                {
                    "Date": s["start_date"].isoformat(),
                    "Month": s["start_date"].isoformat()[:7],
                    "Batch": b_label,
                    "Category": "🐣 Animal / Egg Acquisition",
                    "Details": f"{s['initial_quantity']} × {b['species'].title()} @ ${s['unit_cost']:,.2f}",
                    "Feed Bags": 0,
                    "Feed (lbs)": 0.0,
                    "Cost ($)": round(s["acquisition_cost"], 2),
                }
            )

        if s["starter_bags"] > 0:
            events.append(
                {
                    "Date": s["proc_base"].isoformat(),
                    "Month": s["proc_base"].isoformat()[:7],
                    "Batch": b_label,
                    "Category": "🌱 Starter Feed Purchase",
                    "Details": (
                        f"{s['starter_bags']} × {bag_weight}lb bags "
                        f"({s['starter_feed']:,.1f} lbs needed) @ ${s['starter_bag_price']:,.2f}/bag"
                    ),
                    "Feed Bags": int(s["starter_bags"]),
                    "Feed (lbs)": round(s["starter_feed"], 1),
                    "Cost ($)": round(s["starter_cost_bags"], 2),
                }
            )

        if s["grower_bags"] > 0:
            events.append(
                {
                    "Date": s["transition_date"].isoformat(),
                    "Month": s["transition_date"].isoformat()[:7],
                    "Batch": b_label,
                    "Category": "🌾 Grower/Finisher Feed Purchase",
                    "Details": (
                        f"{s['grower_bags']} × {bag_weight}lb bags "
                        f"({s['grower_feed']:,.1f} lbs needed) @ ${s['grower_bag_price']:,.2f}/bag"
                    ),
                    "Feed Bags": int(s["grower_bags"]),
                    "Feed (lbs)": round(s["grower_feed"], 1),
                    "Cost ($)": round(s["grower_cost_bags"], 2),
                }
            )

        if s["total_processing_cost"] > 0:
            events.append(
                {
                    "Date": s["proc_date"].isoformat(),
                    "Month": s["proc_date"].isoformat()[:7],
                    "Batch": b_label,
                    "Category": "🔪 Processing / Butcher Fee",
                    "Details": (
                        f"{s['surviving_quantity']} harvested head @ ${s['processing_cost']:,.2f}/head"
                    ),
                    "Feed Bags": 0,
                    "Feed (lbs)": 0.0,
                    "Cost ($)": round(s["total_processing_cost"], 2),
                }
            )

    events_df = pd.DataFrame(events)
    if events_df.empty:
        return events_df, pd.DataFrame()

    events_df = events_df.sort_values(
        by=["Date", "Batch", "Category"]
    ).reset_index(drop=True)

    monthly_cash_df = (
        events_df.groupby("Month", as_index=False)
        .agg(
            Total_Bags=("Feed Bags", "sum"),
            Total_Feed_lbs=("Feed (lbs)", "sum"),
            Total_Cost=("Cost ($)", "sum"),
        )
        .rename(
            columns={
                "Total_Bags": f"Feed Bags Purchased ({bag_weight}lb)",
                "Total_Feed_lbs": "Feed Purchased (lbs)",
                "Total_Cost": "Total Monthly Outlay ($)",
            }
        )
    )
    monthly_cash_df["Total Monthly Outlay ($)"] = monthly_cash_df[
        "Total Monthly Outlay ($)"
    ].round(2)
    return events_df, monthly_cash_df


def compute_roi_summary_table(schedules: list[dict]) -> pd.DataFrame:
    if not schedules:
        return pd.DataFrame()

    rows = []
    for s in schedules:
        b = s["batch"]
        rows.append(
            {
                "Batch": f"{b['name']} (#{b['id']})",
                "Species": b["species"].title(),
                "Head (Surviving / Initial)": f"{s['surviving_quantity']} / {s['initial_quantity']}",
                "Starter Feed": f"{s['starter_feed']:,.1f} lbs ({s['starter_bags']} bags)",
                "Grower Feed": f"{s['grower_feed']:,.1f} lbs ({s['grower_bags']} bags)",
                "Feed Saved (Losses)": f"{s['feed_saved_by_losses']:,.1f} lbs",
                "Acquisition ($)": round(s["acquisition_cost"], 2),
                "Feed Cost - Bags ($)": round(s["total_feed_cost_bags"], 2),
                "Feed Cost - Exact ($)": round(s["total_feed_cost_exact"], 2),
                "Processing ($)": round(s["total_processing_cost"], 2),
                "Total Cost ($)": round(s["total_budget_cost"], 2),
                "Expected Meat (lbs)": round(s["expected_meat_lbs"], 1),
                "Feed $/lb Meat": round(s["feed_cost_per_lb_meat"], 2),
                "Total $/lb Meat": round(s["total_cost_per_lb_meat"], 2),
                "Sale $/lb": round(s["sale_price_per_lb"], 2),
                "Est. Revenue ($)": round(s["estimated_revenue"], 2),
                "Est. Profit ($)": round(s["estimated_profit"], 2),
                "Profit $/lb": round(s["profit_per_lb_meat"], 2),
                "ROI (%)": f"{s['roi_margin_pct']:.1f}%",
            }
        )
    return pd.DataFrame(rows)


def compute_reconciliation_table(schedules: list[dict]) -> pd.DataFrame:
    """Build an Estimated vs. Actual Harvest, Home Freezer & Sales Reconciliation summary table."""
    if not schedules:
        return pd.DataFrame()

    rows = []
    for s in schedules:
        b = s["batch"]
        if s["effective_freezer_cost_per_lb"] is None:
            freezer_cost_str = "—"
        elif s["effective_freezer_cost_per_lb"] == 0.0:
            freezer_cost_str = "🎉 $0.00/lb (Free Meat!)"
        else:
            freezer_cost_str = f"${s['effective_freezer_cost_per_lb']:,.2f}/lb"

        rows.append(
            {
                "Batch": f"{b['name']} (#{b['id']})",
                "Harvested Head": f"{s['harvested_head']} / {s['surviving_quantity']}",
                "Est. Meat (lbs)": round(s["expected_meat_lbs"], 1),
                "Actual Harvested (lbs)": round(s["actual_dressed_lbs"], 1),
                "Est. Cost ($)": round(s["total_budget_cost"], 2),
                "Reconciled Cost ($)": round(s["reconciled_total_cost"], 2),
                "Reconciled $/lb": round(s["reconciled_cost_per_lb_meat"], 2),
                "Sold (lbs)": round(s["sold_lbs"], 1),
                "Cash Sales ($)": round(s["actual_sales_revenue"], 2),
                "Home Freezer (lbs)": round(s["freezer_lbs"], 1),
                "Unallocated (lbs)": round(s["unallocated_harvested_lbs"], 1),
                "Effective Freezer $/lb": freezer_cost_str,
                "Cash Net ($)": round(s["actual_cash_profit"], 2),
                "Total Realized Profit ($)": round(
                    s["total_realized_profit"], 2
                ),
            }
        )
    return pd.DataFrame(rows)


def compute_unified_financials_table(schedules: list[dict]) -> pd.DataFrame:
    """Build a clean 8-column Batch Economics & Freezer table."""
    if not schedules:
        return pd.DataFrame()

    rows = []
    for s in schedules:
        b = s["batch"]
        if s["effective_freezer_cost_per_lb"] is None:
            freezer_cost_str = "—"
        elif s["effective_freezer_cost_per_lb"] == 0.0:
            freezer_cost_str = "🎉 $0.00/lb (Free!)"
        else:
            freezer_cost_str = f"${s['effective_freezer_cost_per_lb']:,.2f}/lb"

        meat_lbs = (
            s["actual_dressed_lbs"]
            if s["harvested_head"] > 0
            else s["expected_meat_lbs"]
        )
        rows.append(
            {
                "Batch": f"{b['name']} (#{b['id']})",
                "Head (Surviving / Harvested)": f"{s['surviving_quantity']} surviving ({s['harvested_head']} harvested)",
                "Reconciled Cost ($)": round(s["reconciled_total_cost"], 2),
                "Meat Yield (lbs)": round(meat_lbs, 1),
                "Cost / lb ($)": round(s["reconciled_cost_per_lb_meat"], 2),
                "Cash Sales ($)": round(s["actual_sales_revenue"], 2),
                "Freezer (lbs)": round(s["freezer_lbs"], 1),
                "Effective Freezer $/lb": freezer_cost_str,
            }
        )
    return pd.DataFrame(rows)
