"""Workspace 3: 🌾 Feed & Shopping (Visual Charts + Chronological Shopping Schedule + Raw Tables Expander)."""

import pandas as pd
import streamlit as st


def render_feed_tab(
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    feed_stats: dict,
    purchasing_events_df: pd.DataFrame,
    monthly_cash_df: pd.DataFrame,
    barrel_capacity: int,
) -> None:
    if weekly_df.empty:
        st.warning(
            "No active batches match the current filters to compute a feed schedule."
        )
        return

    # Side-by-Side Visual Charts
    chart_col1, chart_col2 = st.columns(2)
    with chart_col1:
        with st.container(border=True):
            st.markdown(
                f"##### 🌾 Weekly Feed Burn · Peak **{feed_stats['peak_weekly_lbs']:,.1f} lbs/wk**"
            )
            ration_chart_df = weekly_df.set_index("Week Starting")[
                ["Starter Feed (lbs)", "Grower/Finisher Feed (lbs)"]
            ]
            st.bar_chart(ration_chart_df, height=280)

    with chart_col2:
        with st.container(border=True):
            st.markdown(
                f"##### 🛢️ Barrel Storage Demand · Peak **{feed_stats['peak_concurrent_barrels']} × {barrel_capacity}lb barrel(s)**"
            )
            barrel_chart_df = weekly_df.set_index("Week Starting")[
                ["Concurrent Barrels Needed (Remaining Active)"]
            ]
            st.area_chart(barrel_chart_df, height=280)

    # Chronological Feed & Livestock Shopping Schedule
    st.markdown("##### 🛒 Chronological Feed & Livestock Shopping Schedule")
    if not purchasing_events_df.empty:
        shop_c1, shop_c2 = st.columns([2, 1])
        with shop_c1:
            st.dataframe(
                purchasing_events_df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Feed (lbs)": st.column_config.NumberColumn(
                        format="%.1f lbs"
                    ),
                    "Cost ($)": st.column_config.NumberColumn(format="$%.2f"),
                },
            )
        with shop_c2:
            st.dataframe(
                monthly_cash_df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Feed Purchased (lbs)": st.column_config.NumberColumn(
                        format="%.1f lbs"
                    ),
                    "Total Monthly Outlay ($)": st.column_config.NumberColumn(
                        format="$%.2f"
                    ),
                },
            )

    # Raw Weekly & Monthly Feed Tables Tucked into Expander
    with st.expander("📋 View Raw Weekly & Monthly Feed Tables"):
        st.markdown("**Monthly Feed & Barrel Summary**")
        st.dataframe(monthly_df, use_container_width=True, hide_index=True)
        st.markdown("**Weekly Feed Breakdown by Batch**")
        st.dataframe(weekly_df, use_container_width=True, hide_index=True)
