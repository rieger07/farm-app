"""Workspace 2: 🐾 Batch Logs (Compact Batch Selector, More & Calendar Menu, and Action-First Logging)."""

from datetime import date
import streamlit as st

from homestead.calendar_export import create_gcal_link
from homestead.database import (
    add_batch_harvest,
    add_batch_loss,
    add_batch_sale,
    delete_batch,
    delete_batch_harvest,
    delete_batch_loss,
    delete_batch_sale,
    save_batch,
    set_batch_active,
    set_batch_actual_feed,
)
from homestead.scheduling import get_batch_status_badge


def render_batch_logs_tab(
    filtered_schedules: list[dict], all_schedules: list[dict]
) -> None:
    selectable_schedules = (
        filtered_schedules if filtered_schedules else all_schedules
    )
    schedule_by_id = {int(s["batch"]["id"]): s for s in selectable_schedules}
    batch_ids = list(schedule_by_id.keys())

    if (
        "selected_batch_id" not in st.session_state
        or st.session_state.selected_batch_id not in schedule_by_id
    ):
        st.session_state.selected_batch_id = batch_ids[0]

    def _format_batch_selector(bid: int) -> str:
        sc = schedule_by_id[bid]
        b_row = sc["batch"]
        badge = get_batch_status_badge(sc)
        return (
            f"{badge} | {b_row['name']} (#{bid}) — "
            f"{sc['surviving_quantity']}/{sc['initial_quantity']} {b_row['species'].title()}"
        )

    # Compact Batch Selector + Primary Edit + Tucked-Away 'More & Calendar' Menu
    with st.container(border=True):
        sel_col, edit_col, more_col = st.columns([4, 1.2, 1.3])
        with sel_col:
            selected_bid = st.selectbox(
                "Select Batch",
                options=batch_ids,
                index=batch_ids.index(st.session_state.selected_batch_id),
                format_func=_format_batch_selector,
                label_visibility="collapsed",
            )
            st.session_state.selected_batch_id = int(selected_bid)

        s = schedule_by_id[int(selected_bid)]
        b = s["batch"]

        with edit_col:
            if st.button(
                "✏️ Edit Batch",
                key=f"edit_{b['id']}",
                type="primary",
                use_container_width=True,
            ):
                st.session_state.pending_edit_batch = b
                st.session_state.show_batch_modal = True
                st.rerun()

        with more_col:
            with st.popover("⋯ More & Calendar", use_container_width=True):
                if st.button(
                    "📋 Duplicate Batch",
                    key=f"dup_{b['id']}",
                    use_container_width=True,
                ):
                    new_id = save_batch(
                        {
                            "name": f"{b['name']} (Copy)",
                            "species": b["species"],
                            "quantity": b["quantity"],
                            "start_date": b["start_date"],
                            "end_date": b["end_date"],
                            "days_to_birth": b["days_to_birth"],
                            "starter_weeks": b.get("starter_weeks"),
                            "starter_feed_lbs": b.get("starter_feed_lbs"),
                            "grower_weeks": b.get("grower_weeks"),
                            "grower_feed_lbs": b.get("grower_feed_lbs"),
                            "starter_bag_price": b.get("starter_bag_price"),
                            "grower_bag_price": b.get("grower_bag_price"),
                            "unit_cost": b.get("unit_cost"),
                            "processing_cost": b.get("processing_cost"),
                            "expected_yield_lbs": b.get("expected_yield_lbs"),
                            "sale_price_per_lb": b.get("sale_price_per_lb"),
                            "active": b["active"],
                            "notes": b.get("notes", ""),
                        }
                    )
                    st.session_state.selected_batch_id = new_id
                    st.rerun()

                toggle_label = (
                    "⏸️ Exclude from Plan"
                    if b["active"]
                    else "▶️ Include in Plan"
                )
                if st.button(
                    toggle_label,
                    key=f"toggle_{b['id']}",
                    use_container_width=True,
                ):
                    set_batch_active(b["id"], not bool(b["active"]))
                    st.rerun()

                if st.button(
                    "🗑️ Delete Batch",
                    key=f"del_{b['id']}",
                    use_container_width=True,
                ):
                    if st.session_state.editing_batch_id == b["id"]:
                        st.session_state.pending_reset = True
                    delete_batch(b["id"])
                    st.rerun()

                st.markdown("---")
                st.caption("📅 **Google Calendar Links**")
                if s["birth_start"] and s["birth_end"]:
                    birth_gcal = create_gcal_link(
                        f"{b['name']} - {s['birth_label']} Window",
                        s["birth_start"],
                        s["birth_end"],
                        f"Look out for {b['name']} ({s['initial_quantity']}x {b['species']})!",
                    )
                    st.link_button(
                        f"🐣 {s['birth_label']} ({s['birth_start']})",
                        birth_gcal,
                        use_container_width=True,
                    )
                if s["starter_weeks"] > 0 and s["grower_weeks"] > 0:
                    trans_gcal = create_gcal_link(
                        f"{b['name']} - Switch to Grower/Finisher Feed",
                        s["transition_date"],
                        s["transition_date"],
                        f"Switch {b['name']} to Grower feed ({s['grower_bags']} bags).",
                    )
                    st.link_button(
                        f"🌾 Ration Switch ({s['transition_date']})",
                        trans_gcal,
                        use_container_width=True,
                    )
                proc_gcal = create_gcal_link(
                    f"{b['name']} - Processing Date",
                    s["proc_date"],
                    s["proc_date"],
                    f"Processing date for {b['name']} ({s['surviving_quantity']} head).",
                )
                st.link_button(
                    f"🔪 Processing ({s['proc_date']})",
                    proc_gcal,
                    use_container_width=True,
                )

        # Single-Line Batch Vitals Strip (No multi-paragraph cards)
        bv1, bv2, bv3, bv4 = st.columns(4)
        bv1.metric(
            "🐾 Surviving Head",
            f"{s['surviving_quantity']} / {s['initial_quantity']} head",
            delta=f"{s['harvested_head']} harvested · {s['total_lost']} lost",
            delta_color="off",
        )
        bv2.metric(
            "🔪 Target Butcher Date",
            f"{s['proc_date']}",
            delta=f"🌾 Switch: {s['transition_date']}",
            delta_color="off",
        )
        bv3.metric(
            "🌾 Total Batch Feed",
            f"{s['reconciled_feed_lbs']:,.0f} lbs",
            delta=f"{s['starter_bags']} Starter + {s['grower_bags']} Grower bags",
            delta_color="off",
        )
        bv4.metric(
            "💰 Reconciled Batch Cost",
            f"${s['reconciled_total_cost']:,.2f}",
            delta=f"${s['reconciled_cost_per_lb_meat']:,.2f}/lb meat",
            delta_color="off",
        )
        if b.get("notes"):
            st.caption(f"📝 **Notes:** {b['notes']}")

    # Action-First Logging Tabs
    subtab_losses, subtab_harvest, subtab_sales = st.tabs(
        [
            f"📉 Batch Losses ({s['total_lost']} lost)",
            f"🔪 Harvest & Butcher Log ({s['harvested_head']}/{s['surviving_quantity']} head)",
            f"💵 Freezer & Sales Log ({s['freezer_lbs']:,.1f} lbs freezer · {s['sold_lbs']:,.1f} lbs sold)",
        ]
    )

    with subtab_losses:
        loss_col_form, loss_col_list = st.columns([1, 1])
        with loss_col_form:
            with st.container(border=True):
                st.markdown("**➕ Record a Loss**")
                if s["surviving_quantity"] > 0:
                    default_loss_date = min(
                        max(date.today(), s["start_date"]), s["proc_date"]
                    )
                    with st.form(f"loss_form_{b['id']}", clear_on_submit=True):
                        lf_c1, lf_c2 = st.columns(2)
                        loss_dt_input = lf_c1.date_input(
                            "Date of Loss",
                            value=default_loss_date,
                            min_value=s["start_date"],
                            max_value=s["proc_date"],
                            key=f"loss_dt_{b['id']}",
                        )
                        loss_cnt_input = lf_c2.number_input(
                            "Quantity Lost",
                            min_value=1,
                            max_value=max(1, s["surviving_quantity"]),
                            value=1,
                            step=1,
                            key=f"loss_cnt_{b['id']}",
                        )
                        loss_reason_input = st.text_input(
                            "Reason / Stage (optional)",
                            placeholder="e.g. Unhatched egg, brooder loss, predator...",
                            key=f"loss_reason_{b['id']}",
                        )
                        if st.form_submit_button(
                            "➕ Record Loss & Recalculate Feed",
                            use_container_width=True,
                        ):
                            resolved_loss_dt = (
                                loss_dt_input[0]
                                if isinstance(loss_dt_input, (list, tuple))
                                else loss_dt_input
                            )
                            add_batch_loss(
                                b["id"],
                                resolved_loss_dt.isoformat(),
                                min(
                                    int(loss_cnt_input),
                                    s["surviving_quantity"],
                                ),
                                loss_reason_input,
                            )
                            st.rerun()
                else:
                    st.warning(
                        "All animals in this batch have been recorded as lost."
                    )

        with loss_col_list:
            with st.container(border=True):
                st.markdown(
                    f"**Recorded Losses** *(Feed Saved: {s['feed_saved_by_losses']:,.1f} lbs)*"
                )
                if s["losses"]:
                    for l_entry in s["losses"]:
                        l_d = l_entry["loss_date"]
                        if l_d < s["proc_base"]:
                            stage_tag = s["gestation_label"]
                        elif l_d < s["transition_date"]:
                            stage_tag = "Starter"
                        else:
                            stage_tag = "Grower"

                        row_c1, row_c2 = st.columns([4, 1])
                        reason_txt = (
                            f" — *{l_entry['reason']}*"
                            if l_entry["reason"]
                            else ""
                        )
                        row_c1.markdown(
                            f"- **{l_entry['loss_date_str']}** ({stage_tag}): "
                            f"**-{l_entry['count']} head**{reason_txt}"
                        )
                        if row_c2.button(
                            "🗑️",
                            key=f"del_loss_{l_entry['id']}",
                            help="Remove this loss entry",
                        ):
                            delete_batch_loss(l_entry["id"])
                            st.rerun()
                else:
                    st.caption("No losses recorded for this batch.")

    with subtab_harvest:
        harv_col_form, harv_col_list = st.columns([1, 1])
        with harv_col_form:
            with st.container(border=True):
                st.markdown("**➕ Log Butcher / Harvest Weight**")
                if s["unharvested_surviving_head"] > 0:
                    default_harv_date = max(date.today(), s["proc_base"])
                    default_head_to_harv = s["unharvested_surviving_head"]
                    default_dressed = round(
                        default_head_to_harv * s["expected_yield_per_head"], 1
                    )
                    default_proc_total = round(
                        default_head_to_harv * s["processing_cost"], 2
                    )
                    with st.form(
                        f"harvest_form_{b['id']}", clear_on_submit=True
                    ):
                        hf_c1, hf_c2 = st.columns(2)
                        harv_dt_input = hf_c1.date_input(
                            "Harvest / Butcher Date",
                            value=default_harv_date,
                            key=f"harv_dt_{b['id']}",
                        )
                        harv_head_input = hf_c2.number_input(
                            "Head Harvested",
                            min_value=1,
                            max_value=max(1, s["unharvested_surviving_head"]),
                            value=default_head_to_harv,
                            step=1,
                            key=f"harv_head_{b['id']}",
                        )
                        hf_c3, hf_c4 = st.columns(2)
                        harv_lbs_input = hf_c3.number_input(
                            "Total Dressed Weight (lbs)",
                            min_value=0.0,
                            max_value=50000.0,
                            value=float(default_dressed),
                            step=1.0,
                            key=f"harv_lbs_{b['id']}",
                        )
                        harv_proc_input = hf_c4.number_input(
                            "Total Butcher Fee ($)",
                            min_value=0.0,
                            max_value=50000.0,
                            value=float(default_proc_total),
                            step=5.0,
                            key=f"harv_proc_{b['id']}",
                        )
                        harv_notes_input = st.text_input(
                            "Notes (optional)",
                            placeholder="e.g. Hanging weight, processor invoice...",
                            key=f"harv_notes_{b['id']}",
                        )
                        if st.form_submit_button(
                            "➕ Log Harvest Event",
                            use_container_width=True,
                        ):
                            resolved_harv_dt = (
                                harv_dt_input[0]
                                if isinstance(harv_dt_input, (list, tuple))
                                else harv_dt_input
                            )
                            add_batch_harvest(
                                b["id"],
                                resolved_harv_dt.isoformat(),
                                min(
                                    int(harv_head_input),
                                    s["unharvested_surviving_head"],
                                ),
                                float(harv_lbs_input),
                                float(harv_proc_input),
                                harv_notes_input,
                            )
                            st.rerun()
                else:
                    st.success(
                        "✅ All surviving animals in this batch have been harvested."
                    )

        with harv_col_list:
            with st.container(border=True):
                st.markdown(
                    f"**Recorded Harvests** *(Total: {s['actual_dressed_lbs']:,.1f} lbs dressed)*"
                )
                if s["harvests"]:
                    for h_entry in s["harvests"]:
                        avg_h = (
                            h_entry["dressed_lbs"] / h_entry["head_count"]
                            if h_entry["head_count"] > 0
                            else 0.0
                        )
                        h_note = (
                            f" — *{h_entry['notes']}*"
                            if h_entry["notes"]
                            else ""
                        )
                        hc1, hc2 = st.columns([4, 1])
                        hc1.markdown(
                            f"- **{h_entry['harvest_date_str']}**: **{h_entry['head_count']} head** → "
                            f"**{h_entry['dressed_lbs']:,.1f} lbs** ({avg_h:,.1f} lbs/head) | "
                            f"Fee: **${h_entry['processing_cost_total']:,.2f}**{h_note}"
                        )
                        if hc2.button(
                            "🗑️",
                            key=f"del_harv_{h_entry['id']}",
                            help="Remove this harvest entry",
                        ):
                            delete_batch_harvest(h_entry["id"])
                            st.rerun()
                else:
                    st.caption("No harvest events logged yet.")

            with st.expander(
                "⚖️ Actual Feed Used Override (End-of-Batch Reconciliation)"
            ):
                st.caption(
                    f"Calculated feed: **{s['total_feed']:,.1f} lbs** / **${s['total_feed_cost_bags']:,.2f}** ({s['total_bags']} bags)."
                )
                with st.form(
                    f"actual_feed_form_{b['id']}", clear_on_submit=False
                ):
                    af_c1, af_c2 = st.columns(2)
                    af_lbs_val = af_c1.number_input(
                        "Actual Total Feed Used (lbs)",
                        min_value=0.0,
                        max_value=500000.0,
                        value=float(s["reconciled_feed_lbs"]),
                        step=10.0,
                        key=f"af_lbs_{b['id']}",
                    )
                    af_cost_val = af_c2.number_input(
                        "Actual Total Feed Cost ($)",
                        min_value=0.0,
                        max_value=500000.0,
                        value=float(s["reconciled_feed_cost"]),
                        step=5.0,
                        key=f"af_cost_{b['id']}",
                    )
                    af_btn1, af_btn2 = st.columns(2)
                    save_af = af_btn1.form_submit_button(
                        "💾 Save Override", use_container_width=True
                    )
                    clear_af = af_btn2.form_submit_button(
                        "↩️ Reset", use_container_width=True
                    )
                    if save_af:
                        set_batch_actual_feed(
                            b["id"], float(af_lbs_val), float(af_cost_val)
                        )
                        st.rerun()
                    if clear_af:
                        set_batch_actual_feed(b["id"], None, None)
                        st.rerun()

    with subtab_sales:
        sale_col_form, sale_col_list = st.columns([1, 1])
        with sale_col_form:
            with st.container(border=True):
                st.markdown("**➕ Allocate Meat to Freezer or Customer Sale**")
                default_sale_wt = (
                    s["unallocated_harvested_lbs"]
                    if s["unallocated_harvested_lbs"] > 0
                    else 10.0
                )
                default_sale_rev = round(
                    default_sale_wt * s["sale_price_per_lb"], 2
                )
                with st.form(f"sale_form_{b['id']}", clear_on_submit=True):
                    sf_c1, sf_c2 = st.columns(2)
                    sale_dt_input = sf_c1.date_input(
                        "Date",
                        value=date.today(),
                        key=f"sale_dt_{b['id']}",
                    )
                    sale_type_input = sf_c2.selectbox(
                        "Allocation Type",
                        options=["Home Freezer", "Customer Sale"],
                        key=f"sale_type_{b['id']}",
                    )
                    sf_c3, sf_c4 = st.columns(2)
                    sale_wt_input = sf_c3.number_input(
                        "Weight (lbs)",
                        min_value=0.1,
                        max_value=50000.0,
                        value=float(max(0.1, default_sale_wt)),
                        step=1.0,
                        key=f"sale_wt_{b['id']}",
                    )
                    sale_rev_input = sf_c4.number_input(
                        "Total Sale Revenue ($) — if sold",
                        min_value=0.0,
                        max_value=500000.0,
                        value=float(default_sale_rev),
                        step=5.0,
                        key=f"sale_rev_{b['id']}",
                    )
                    sale_notes_input = st.text_input(
                        "Notes (optional)",
                        placeholder="e.g. Chest freezer #1, half hog to neighbor...",
                        key=f"sale_notes_{b['id']}",
                    )
                    if st.form_submit_button(
                        "➕ Log Allocation",
                        use_container_width=True,
                    ):
                        resolved_sale_dt = (
                            sale_dt_input[0]
                            if isinstance(sale_dt_input, (list, tuple))
                            else sale_dt_input
                        )
                        add_batch_sale(
                            b["id"],
                            resolved_sale_dt.isoformat(),
                            sale_type_input,
                            float(sale_wt_input),
                            (
                                0.0
                                if sale_type_input == "Home Freezer"
                                else float(sale_rev_input)
                            ),
                            sale_notes_input,
                        )
                        st.rerun()

        with sale_col_list:
            with st.container(border=True):
                eff_str = (
                    "🎉 $0.00/lb Free Meat!"
                    if s["effective_freezer_cost_per_lb"] == 0.0
                    else (
                        f"${s['effective_freezer_cost_per_lb']:,.2f}/lb"
                        if s["effective_freezer_cost_per_lb"] is not None
                        else "—"
                    )
                )
                st.markdown(
                    f"**Recorded Freezer & Sales** *(Freezer Cost: **{eff_str}** · Unallocated: {s['unallocated_harvested_lbs']:,.1f} lbs)*"
                )
                if s["sales"]:
                    for sl_entry in s["sales"]:
                        sl_note = (
                            f" — *{sl_entry['notes']}*"
                            if sl_entry["notes"]
                            else ""
                        )
                        if sl_entry["allocation_type"] == "Home Freezer":
                            badge = "❄️ Home Freezer"
                            price_info = ""
                        else:
                            badge = "💵 Sale"
                            price_info = f"→ **${sl_entry['revenue']:,.2f}**"

                        sc1, sc2 = st.columns([4, 1])
                        sc1.markdown(
                            f"- **{sl_entry['entry_date_str']}** [{badge}]: "
                            f"**{sl_entry['weight_lbs']:,.1f} lbs** {price_info}{sl_note}"
                        )
                        if sc2.button(
                            "🗑️",
                            key=f"del_sale_{sl_entry['id']}",
                            help="Remove this entry",
                        ):
                            delete_batch_sale(sl_entry["id"])
                            st.rerun()
                else:
                    st.caption(
                        "No home freezer or customer sale entries logged yet."
                    )
