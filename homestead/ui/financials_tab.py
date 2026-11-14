"""Workspace 4: 💰 Financials & Freezer (3 Headline Scorecard Metrics + 8-Column Economics Table + Detailed ROI Expander)."""

import pandas as pd
import streamlit as st


def render_financials_tab(
    filtered_active_schedules: list[dict],
    unified_fin_df: pd.DataFrame,
    roi_df: pd.DataFrame,
    reconciliation_df: pd.DataFrame,
) -> None:
    if not filtered_active_schedules:
        st.warning(
            "No active batches match the current filters to compute financials."
        )
        return

    combined_budget_cost = sum(
        s["total_budget_cost"] for s in filtered_active_schedules
    )
    combined_meat_lbs = sum(
        s["expected_meat_lbs"] for s in filtered_active_schedules
    )
    blended_cost_per_lb = (
        combined_budget_cost / combined_meat_lbs
        if combined_meat_lbs > 0
        else 0.0
    )

    tot_reconciled_cost = sum(
        s["reconciled_total_cost"] for s in filtered_active_schedules
    )
    tot_sold_lbs = sum(s["sold_lbs"] for s in filtered_active_schedules)
    tot_sales_rev = sum(
        s["actual_sales_revenue"] for s in filtered_active_schedules
    )
    tot_freezer_lbs = sum(s["freezer_lbs"] for s in filtered_active_schedules)
    tot_cash_net = tot_sales_rev - tot_reconciled_cost
    tot_net_cost_for_freezer = max(0.0, tot_reconciled_cost - tot_sales_rev)
    tot_eff_freezer_per_lb = (
        tot_net_cost_for_freezer / tot_freezer_lbs
        if tot_freezer_lbs > 0
        else None
    )

    # 3 Headline Scorecard Metrics
    sc1, sc2, sc3 = st.columns(3)
    with sc1:
        with st.container(border=True):
            st.metric(
                "💰 Total Outlay (Reconciled)",
                f"${tot_reconciled_cost:,.2f}",
                delta=f"Planned Budget: ${combined_budget_cost:,.2f} (${blended_cost_per_lb:,.2f}/lb est.)",
                delta_color="off",
            )
    with sc2:
        with st.container(border=True):
            st.metric(
                "💵 Cash Sales & Net Cash",
                f"${tot_sales_rev:,.2f} sales",
                delta=f"Net Cash: {'+' if tot_cash_net >= 0 else ''}${tot_cash_net:,.2f} ({tot_sold_lbs:,.1f} lbs sold)",
                delta_color="normal",
            )
    with sc3:
        with st.container(border=True):
            st.metric(
                "❄️ Home Freezer Meat",
                f"{tot_freezer_lbs:,.1f} lbs",
                delta=(
                    "🎉 $0.00/lb (Free Meat!)"
                    if tot_eff_freezer_per_lb == 0.0
                    else (
                        f"Effective Cost: ${tot_eff_freezer_per_lb:,.2f}/lb"
                        if tot_eff_freezer_per_lb is not None
                        else f"Expected Yield: {combined_meat_lbs:,.1f} lbs"
                    )
                ),
                delta_color="off",
            )

    # 1 Clean 8-Column Batch Economics Table
    st.markdown("##### 🥩 Batch Economics & Freezer Summary")
    st.dataframe(
        unified_fin_df,
        width="stretch",
        hide_index=True,
        column_config={
            "Reconciled Cost ($)": st.column_config.NumberColumn(
                format="$%.2f"
            ),
            "Meat Yield (lbs)": st.column_config.NumberColumn(
                format="%.1f lbs"
            ),
            "Cost / lb ($)": st.column_config.NumberColumn(format="$%.2f"),
            "Cash Sales ($)": st.column_config.NumberColumn(format="$%.2f"),
            "Freezer (lbs)": st.column_config.NumberColumn(format="%.1f lbs"),
        },
    )

    # Granular Cost & Ration Math Tucked into Expander
    with st.expander("🔍 View Detailed Cost, Ration & Planned ROI Breakdown"):
        st.markdown("**Planned Cost, Ration & ROI Breakdown**")
        st.dataframe(roi_df, width="stretch", hide_index=True)
        st.markdown("**Full Harvest & Sales Reconciliation Table**")
        st.dataframe(
            reconciliation_df,
            width="stretch",
            hide_index=True,
        )
