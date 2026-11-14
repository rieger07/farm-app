"""Minimal sidebar: User badge, Add Livestock Batch button, Species Presets button, herd filters, .ics export, and Settings & Backup."""

from datetime import date
import streamlit as st

from homestead.calendar_export import generate_ics_calendar
from homestead.config import STATUS_OPTIONS
from homestead.database import (
    export_backup_json,
    import_backup_json,
    set_bag_weight,
    set_barrel_capacity,
)
from homestead.scheduling import get_batch_status_badge
from homestead.telemetry import log_telemetry_event, record_ui_action
from homestead.ui.auth_view import logout_user_session


def render_sidebar(
    all_batches: list[dict],
    all_schedules: list[dict],
    barrel_capacity: int,
    bag_weight: int,
    current_user: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """Render the minimal sidebar and return (filtered_schedules, filtered_active_schedules)."""
    with st.sidebar:
        st.markdown("## 🚜 Homestead Planner")
        if current_user:
            role_tag = " · 🛡️ Admin" if current_user.get("is_admin") else ""
            st.caption(
                f"👤 **{current_user['display_name']}** (`@{current_user['username']}`){role_tag}"
            )
            u_btn1, u_btn2 = st.columns(2)
            with u_btn1:
                acct_label = (
                    "👥 Users & PW"
                    if current_user.get("is_admin")
                    else "🔑 Account"
                )
                if st.button(
                    acct_label,
                    key="sidebar_user_admin_btn",
                    width="stretch",
                ):
                    record_ui_action("Open User / Admin Modal", event_type="modal")
                    st.session_state.show_batch_modal = False
                    st.session_state.show_species_modal = False
                    st.session_state.show_user_modal = True
                    st.rerun()
            with u_btn2:
                if st.button(
                    "🚪 Log Out",
                    key="sidebar_logout_btn",
                    width="stretch",
                ):
                    logout_user_session()
                    st.rerun()
        else:
            st.caption("Poultry, Pigs & Custom Livestock Operations")

        if st.button(
            "➕ Add Livestock Batch",
            key="sidebar_add_batch_btn",
            type="primary",
            width="stretch",
        ):
            record_ui_action("Open Add Batch Modal", event_type="modal")
            st.session_state.pending_reset = True
            st.session_state.show_species_modal = False
            st.session_state.show_user_modal = False
            st.session_state.show_batch_modal = True
            st.rerun()

        if st.button(
            "🐾 Manage Species Presets",
            key="sidebar_manage_species_btn",
            width="stretch",
        ):
            record_ui_action("Open Species Presets Modal", event_type="modal")
            st.session_state.show_batch_modal = False
            st.session_state.show_user_modal = False
            st.session_state.show_species_modal = True
            st.rerun()

        st.divider()
        st.markdown("##### 🔍 Herd Filters")
        species_present = sorted({b["species"] for b in all_batches})
        if species_present:
            prev_known_species = st.session_state.get(
                "_known_species_present", []
            )
            current_sp_selection = [
                sp
                for sp in st.session_state.get("sidebar_species_filter", [])
                if sp in species_present
            ]
            # Auto-include any newly added species so new batches are never hidden by stale filters
            for sp in species_present:
                if sp not in prev_known_species and sp not in current_sp_selection:
                    current_sp_selection.append(sp)
            st.session_state._known_species_present = list(species_present)

            # Default back to "Select All" if cleared (clicking X)
            if not current_sp_selection:
                current_sp_selection = list(species_present)
            st.session_state.sidebar_species_filter = current_sp_selection

            selected_species_filter = st.multiselect(
                "Filter by Species",
                options=species_present,
                key="sidebar_species_filter",
                format_func=lambda s: s.title(),
            )
            if not selected_species_filter:
                selected_species_filter = list(species_present)
        else:
            selected_species_filter = []

        current_status_selection = [
            st_opt
            for st_opt in st.session_state.get("sidebar_status_filter", [])
            if st_opt in STATUS_OPTIONS
        ]
        if not current_status_selection:
            current_status_selection = list(STATUS_OPTIONS)
        st.session_state.sidebar_status_filter = current_status_selection

        selected_status_filter = st.multiselect(
            "Filter by Batch Status",
            options=STATUS_OPTIONS,
            key="sidebar_status_filter",
        )
        if not selected_status_filter:
            selected_status_filter = list(STATUS_OPTIONS)

        filtered_schedules = [
            s
            for s in all_schedules
            if s["batch"]["species"] in selected_species_filter
            and get_batch_status_badge(s) in selected_status_filter
        ]
        filtered_active_schedules = [
            s for s in filtered_schedules if s["batch"]["active"]
        ]

        active_uid = (
            int(current_user["id"])
            if current_user and current_user.get("id") is not None
            else None
        )
        active_uname = (
            str(current_user.get("username", "")) if current_user else ""
        )

        if filtered_active_schedules:
            def _build_ics(scheds=list(filtered_active_schedules), uid=active_uid, uname=active_uname) -> str:
                import time as _t
                t0 = _t.perf_counter()
                out = generate_ics_calendar(scheds)
                log_telemetry_event(
                    event_type="export",
                    action="Export Calendar (.ics)",
                    duration_ms=(_t.perf_counter() - t0) * 1000.0,
                    details={"batches": len(scheds)},
                    user_id=uid,
                    username=uname,
                )
                return out

            st.download_button(
                "📅 Export Active Plan (.ics)",
                data=_build_ics,
                file_name="homestead_schedule.ics",
                mime="text/calendar",
                on_click="ignore",
                width="stretch",
                help="Download an iCalendar (.ics) file with all active batch milestones.",
            )

        st.divider()
        with st.expander("⚙️ Settings & Backup", expanded=False):
            st.caption("Storage barrel capacity & feed bag size")
            s_col1, s_col2 = st.columns(2)
            new_barrel_cap = s_col1.number_input(
                "Barrel (lbs)",
                min_value=1,
                value=max(1, int(barrel_capacity)),
                step=25,
            )
            new_bag_weight = s_col2.number_input(
                "Bag (lbs)",
                min_value=1,
                value=max(1, int(bag_weight)),
                step=5,
            )
            if int(new_barrel_cap) != barrel_capacity:
                record_ui_action(
                    "Update Barrel Capacity",
                    event_type="mutation",
                    details={"barrel_capacity": int(new_barrel_cap)},
                )
                set_barrel_capacity(int(new_barrel_cap))
                st.rerun()
            if int(new_bag_weight) != bag_weight:
                record_ui_action(
                    "Update Bag Weight",
                    event_type="mutation",
                    details={"bag_weight": int(new_bag_weight)},
                )
                set_bag_weight(int(new_bag_weight))
                st.rerun()

            st.markdown("---")
            def _build_json(uid=active_uid, uname=active_uname) -> str:
                import time as _t
                t0 = _t.perf_counter()
                out = export_backup_json(user_id=uid)
                log_telemetry_event(
                    event_type="export",
                    action="Download Backup (.json)",
                    duration_ms=(_t.perf_counter() - t0) * 1000.0,
                    details={"bytes": len(out)},
                    user_id=uid,
                    username=uname,
                )
                return out

            st.download_button(
                "⬇️ Download Backup (.json)",
                data=_build_json,
                file_name=f"homestead_plan_{date.today().isoformat()}.json",
                mime="application/json",
                on_click="ignore",
                width="stretch",
            )
            uploaded_file = st.file_uploader(
                "Restore from JSON", type=["json"]
            )
            import_mode = st.radio(
                "How should imported batches be applied?",
                [
                    "Update matching & add new (by batch name)",
                    "Add all as new batches (keep existing)",
                    "Replace all batches (wipe & restore)",
                ],
                captions=[
                    "Updates existing batches with the same name and adds any new ones.",
                    "Appends every batch from the file as a brand-new entry alongside current batches.",
                    "Deletes all current batches first and restores only what is in the file.",
                ],
                index=0,
            )
            if uploaded_file is not None and st.button(
                "📥 Import JSON Backup",
                type="primary",
                width="stretch",
            ):
                try:
                    raw_content = uploaded_file.read().decode("utf-8")
                    imported_n = import_backup_json(
                        raw_content,
                        replace_existing=(
                            import_mode == "Replace all batches (wipe & restore)"
                        ),
                        merge_by_name=(
                            import_mode
                            == "Update matching & add new (by batch name)"
                        ),
                    )
                    record_ui_action(
                        "Import JSON Backup",
                        event_type="mutation",
                        details={"imported_batches": imported_n, "mode": import_mode},
                    )
                    st.success(f"Imported {imported_n} batch(es)!")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Failed to import backup: {exc}")

    return filtered_schedules, filtered_active_schedules
