"""Homestead Planner: Poultry & Pigs — Streamlit entry point."""

import os
import streamlit as st

from homestead.calendar_export import create_gcal_link, generate_ics_calendar
from homestead.config import (
    DEFAULT_BAG_WEIGHT,
    DEFAULT_BARREL_CAPACITY,
    LIVESTOCK_DATA,
    STATUS_OPTIONS,
    WORKSPACES,
    get_species_defaults,
)
from homestead.database import (
    _normalize_batch_payload,
    add_batch_harvest,
    add_batch_loss,
    add_batch_sale,
    delete_batch,
    delete_batch_harvest,
    delete_batch_loss,
    delete_batch_sale,
    export_backup_json,
    get_bag_weight,
    get_barrel_capacity,
    get_db_connection,
    get_db_path,
    get_int_setting,
    import_backup_json,
    init_db,
    list_batch_harvests,
    list_batch_losses,
    list_batch_sales,
    list_batches,
    save_batch,
    set_bag_weight,
    set_barrel_capacity,
    set_batch_active,
    set_batch_actual_feed,
    set_int_setting,
)
from homestead.financials import (
    compute_purchasing_schedule,
    compute_reconciliation_table,
    compute_roi_summary_table,
    compute_unified_financials_table,
)
from homestead.scheduling import (
    _batch_feed_on_day,
    _format_relative_days_badge,
    compute_batch_schedule,
    compute_combined_feed_schedule,
    compute_upcoming_milestones,
    get_batch_status_badge,
)
from homestead.ui.batch_logs_tab import render_batch_logs_tab
from homestead.ui.batch_modal import (
    _cancel_batch_modal_callback,
    _dismiss_batch_modal,
    _save_batch_modal_callback,
    apply_pending_editor_transitions,
    on_species_change,
    render_batch_editor_modal,
)
from homestead.ui.feed_tab import render_feed_tab
from homestead.ui.financials_tab import render_financials_tab
from homestead.ui.radar_tab import render_radar_tab
from homestead.ui.sidebar import render_sidebar
from homestead.ui.timeline_shim import st_timeline


def render_app() -> None:
    st.set_page_config(
        page_title="Homestead Planner: Poultry & Pigs",
        page_icon="🚜",
        layout="wide",
    )

    init_db()
    apply_pending_editor_transitions()

    barrel_capacity = get_barrel_capacity()
    bag_weight = get_bag_weight()

    all_batches = list_batches()
    all_losses = list_batch_losses()
    losses_by_batch: dict[int, list[dict]] = {}
    for l_row in all_losses:
        losses_by_batch.setdefault(int(l_row["batch_id"]), []).append(l_row)

    all_harvests = list_batch_harvests()
    harvests_by_batch: dict[int, list[dict]] = {}
    for h_row in all_harvests:
        harvests_by_batch.setdefault(int(h_row["batch_id"]), []).append(h_row)

    all_sales = list_batch_sales()
    sales_by_batch: dict[int, list[dict]] = {}
    for s_row in all_sales:
        sales_by_batch.setdefault(int(s_row["batch_id"]), []).append(s_row)

    all_schedules = [
        compute_batch_schedule(
            b,
            barrel_capacity=barrel_capacity,
            bag_weight=bag_weight,
            losses=losses_by_batch.get(int(b["id"]), []),
            harvests=harvests_by_batch.get(int(b["id"]), []),
            sales=sales_by_batch.get(int(b["id"]), []),
        )
        for b in all_batches
    ]

    filtered_schedules, filtered_active_schedules = render_sidebar(
        all_batches=all_batches,
        all_schedules=all_schedules,
        barrel_capacity=barrel_capacity,
        bag_weight=bag_weight,
    )

    if st.session_state.get("show_batch_modal", False):
        render_batch_editor_modal(barrel_capacity, bag_weight)

    st.title("🚜 Homestead Planner")

    if (
        "workspace_nav" not in st.session_state
        or st.session_state.workspace_nav not in WORKSPACES
    ):
        st.session_state.workspace_nav = WORKSPACES[0]
    if st.session_state.get("jump_to_workspace"):
        target_ws = st.session_state.pop("jump_to_workspace")
        if target_ws in WORKSPACES:
            st.session_state.workspace_nav = target_ws

    active_workspace = st.segmented_control(
        "Workspace Navigation",
        options=WORKSPACES,
        key="workspace_nav",
        selection_mode="single",
        label_visibility="collapsed",
        width="stretch",
    )
    if not active_workspace:
        active_workspace = WORKSPACES[0]

    # Empty state if no batches exist
    if not all_batches:
        with st.container(border=True):
            st.info(
                "👋 **No livestock batches saved yet.** Click **➕ Add Livestock Batch** below (or in the sidebar) to add your first batch, or restore a JSON backup under **⚙️ Settings & Backup** in the sidebar."
            )
            if st.button(
                "➕ Add Your First Livestock Batch",
                key="empty_add_batch_btn",
                type="primary",
            ):
                st.session_state.pending_reset = True
                st.session_state.show_batch_modal = True
                st.rerun()
        return

    # Precompute aggregated tables & metrics for active plan
    weekly_df, monthly_df, feed_stats = compute_combined_feed_schedule(
        filtered_active_schedules, barrel_capacity
    )
    purchasing_events_df, monthly_cash_df = compute_purchasing_schedule(
        filtered_active_schedules, bag_weight
    )
    roi_df = compute_roi_summary_table(filtered_active_schedules)
    reconciliation_df = compute_reconciliation_table(filtered_active_schedules)
    unified_fin_df = compute_unified_financials_table(filtered_active_schedules)
    upcoming_milestones = compute_upcoming_milestones(filtered_active_schedules)

    if active_workspace == "🗓️ Radar & Timeline":
        render_radar_tab(
            filtered_active_schedules=filtered_active_schedules,
            upcoming_milestones=upcoming_milestones,
            feed_stats=feed_stats,
            barrel_capacity=barrel_capacity,
        )
    elif active_workspace == "🐾 Batch Logs":
        render_batch_logs_tab(
            filtered_schedules=filtered_schedules,
            all_schedules=all_schedules,
        )
    elif active_workspace == "🌾 Feed & Shopping":
        render_feed_tab(
            weekly_df=weekly_df,
            monthly_df=monthly_df,
            feed_stats=feed_stats,
            purchasing_events_df=purchasing_events_df,
            monthly_cash_df=monthly_cash_df,
            barrel_capacity=barrel_capacity,
        )
    elif active_workspace == "💰 Financials & Freezer":
        render_financials_tab(
            filtered_active_schedules=filtered_active_schedules,
            unified_fin_df=unified_fin_df,
            roi_df=roi_df,
            reconciliation_df=reconciliation_df,
        )


__all__ = [
    "DEFAULT_BAG_WEIGHT",
    "DEFAULT_BARREL_CAPACITY",
    "LIVESTOCK_DATA",
    "STATUS_OPTIONS",
    "WORKSPACES",
    "_batch_feed_on_day",
    "_cancel_batch_modal_callback",
    "_dismiss_batch_modal",
    "_format_relative_days_badge",
    "_normalize_batch_payload",
    "_save_batch_modal_callback",
    "add_batch_harvest",
    "add_batch_loss",
    "add_batch_sale",
    "apply_pending_editor_transitions",
    "compute_batch_schedule",
    "compute_combined_feed_schedule",
    "compute_purchasing_schedule",
    "compute_reconciliation_table",
    "compute_roi_summary_table",
    "compute_unified_financials_table",
    "compute_upcoming_milestones",
    "create_gcal_link",
    "delete_batch",
    "delete_batch_harvest",
    "delete_batch_loss",
    "delete_batch_sale",
    "export_backup_json",
    "generate_ics_calendar",
    "get_bag_weight",
    "get_barrel_capacity",
    "get_batch_status_badge",
    "get_db_connection",
    "get_db_path",
    "get_int_setting",
    "get_species_defaults",
    "import_backup_json",
    "init_db",
    "list_batch_harvests",
    "list_batch_losses",
    "list_batch_sales",
    "list_batches",
    "on_species_change",
    "render_app",
    "render_batch_editor_modal",
    "render_batch_logs_tab",
    "render_feed_tab",
    "render_financials_tab",
    "render_radar_tab",
    "render_sidebar",
    "save_batch",
    "set_bag_weight",
    "set_barrel_capacity",
    "set_batch_active",
    "set_batch_actual_feed",
    "set_int_setting",
    "st_timeline",
]


if os.environ.get("FARM_APP_TESTING") != "1":
    render_app()
