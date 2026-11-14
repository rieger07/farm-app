"""Workspace 1: 🗓️ Radar & Timeline (3 Operational Vitals + Swimlane Timeline + Action Agenda)."""

import streamlit as st

from homestead.ui.timeline_shim import st_timeline


def render_radar_tab(
    filtered_active_schedules: list[dict],
    upcoming_milestones: list[dict],
    feed_stats: dict,
    barrel_capacity: int,
) -> None:
    if not filtered_active_schedules:
        st.warning(
            "No active batches match the current Species / Status filters in the sidebar."
        )
        return

    total_active_batches = len(filtered_active_schedules)
    total_surviving_head = sum(
        s["surviving_quantity"] for s in filtered_active_schedules
    )
    total_lost_head = sum(s["total_lost"] for s in filtered_active_schedules)
    tot_harvested_head = sum(
        s["harvested_head"] for s in filtered_active_schedules
    )
    animals_on_ground = max(0, total_surviving_head - tot_harvested_head)

    combined_feed_lbs = sum(s["total_feed"] for s in filtered_active_schedules)
    combined_bags = sum(s["total_bags"] for s in filtered_active_schedules)

    # 3 Clean Vitals (Single-Number Operational Cards)
    v_col1, v_col2, v_col3 = st.columns(3)
    with v_col1:
        with st.container(border=True):
            st.metric(
                "🐾 Animals on Ground",
                f"{animals_on_ground:,} head",
                delta=(
                    f"{total_active_batches} active batch(es) · {tot_harvested_head} harvested"
                    if total_lost_head == 0
                    else f"{total_active_batches} batch(es) · -{total_lost_head} lost"
                ),
                delta_color="off",
            )
    with v_col2:
        with st.container(border=True):
            st.metric(
                "🌾 Feed & Barrel Storage",
                f"{combined_bags} bags ({combined_feed_lbs:,.0f} lbs)",
                delta=f"Peak {feed_stats['peak_concurrent_barrels']} × {barrel_capacity}lb barrel(s) needed",
                delta_color="off",
            )
    with v_col3:
        with st.container(border=True):
            if upcoming_milestones:
                nxt = upcoming_milestones[0]
                st.metric(
                    "⏳ Next Farm Milestone",
                    f"{nxt['icon']} {nxt['task']}",
                    delta=f"{nxt['badge']} ({nxt['date_display']}) · {nxt['batch_name']}",
                    delta_color="off",
                )
            else:
                st.metric(
                    "⏳ Next Farm Milestone",
                    "All Caught Up",
                    delta="No pending milestones",
                    delta_color="off",
                )

    # Side-by-Side Main View: Timeline (2/3) + Compact Action Agenda (1/3)
    tl_col, agenda_col = st.columns([2, 1])

    with tl_col:
        with st.container(border=True):
            st.markdown("##### 🗓️ Shared Herd Lifecycle Timeline")
            timeline_groups = []
            timeline_items = []
            for s in filtered_active_schedules:
                b = s["batch"]
                loss_badge = (
                    f" · ⚠️ -{s['total_lost']}" if s["total_lost"] > 0 else ""
                )
                harvest_badge = (
                    f" · 🥩 {s['harvested_head']} harvested"
                    if s["harvested_head"] > 0
                    else ""
                )
                timeline_groups.append(
                    {
                        "id": b["id"],
                        "content": (
                            f"<b>{b['name']}</b><br>"
                            f"<small>{s['surviving_quantity']}/{s['initial_quantity']}x {b['species'].title()}{loss_badge}{harvest_badge}</small>"
                        ),
                    }
                )
                timeline_items.extend(s["timeline_items"])

            selected_tl_item = st_timeline(
                timeline_items,
                groups=timeline_groups,
                options={
                    "selectable": True,
                    "stack": False,
                    "zoomable": True,
                    "verticalScroll": True,
                    "orientation": "top",
                    "margin": {"item": 8, "axis": 6},
                },
                height="380px",
            )

            if selected_tl_item:
                item_id = (
                    selected_tl_item.get("id", "")
                    if isinstance(selected_tl_item, dict)
                    else str(selected_tl_item)
                )
                matched_schedule = None
                for s in filtered_active_schedules:
                    if item_id.startswith(f"b{s['batch']['id']}_"):
                        matched_schedule = s
                        break
                if matched_schedule:
                    mb = matched_schedule["batch"]
                    act_bar1, act_bar2, act_bar3 = st.columns([3, 1.3, 1.1])
                    with act_bar1:
                        st.markdown(
                            f"**{mb['name']}** (`{matched_schedule['surviving_quantity']}/{matched_schedule['initial_quantity']} head`) · "
                            f"🌾 Switch: **{matched_schedule['transition_date']}** · "
                            f"🔪 Butcher: **{matched_schedule['proc_date']}**"
                        )
                    with act_bar2:
                        if st.button(
                            "🐾 Open Batch Logs",
                            key=f"tl_open_mgr_{mb['id']}",
                            type="primary",
                            use_container_width=True,
                        ):
                            st.session_state.selected_batch_id = int(mb["id"])
                            st.session_state.jump_to_workspace = "🐾 Batch Logs"
                            st.rerun()
                    with act_bar3:
                        if st.button(
                            "✏️ Edit Batch",
                            key=f"tl_edit_batch_{mb['id']}",
                            use_container_width=True,
                        ):
                            st.session_state.pending_edit_batch = mb
                            st.session_state.show_batch_modal = True
                            st.rerun()
            else:
                st.caption(
                    "💡 Click any bar on the timeline to open its logs or edit the batch."
                )

    with agenda_col:
        with st.container(border=True):
            st.markdown("##### 🔔 Next Up Action Agenda")
            if not upcoming_milestones:
                st.caption("No upcoming milestones scheduled.")
            else:
                for ms in upcoming_milestones[:5]:
                    st.markdown(
                        f"**{ms['icon']} {ms['task']}** · `{ms['badge']}`  \n"
                        f"**{ms['batch_name']}** — {ms['date_display']}"
                    )
                    st.caption(
                        f"{ms['details']} · [📅 Add to GCal]({ms['gcal_url']})"
                    )
                    st.divider()
                if len(upcoming_milestones) > 5:
                    with st.expander(
                        f"See all {len(upcoming_milestones)} milestones"
                    ):
                        for ms in upcoming_milestones[5:]:
                            st.markdown(
                                f"- **{ms['date_display']}** ({ms['badge']}): "
                                f"**{ms['batch_name']}** — {ms['icon']} {ms['task']}"
                            )
