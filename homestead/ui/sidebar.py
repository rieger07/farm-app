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
                    use_container_width=True,
                ):
                    st.session_state.show_batch_modal = False
                    st.session_state.show_species_modal = False
                    st.session_state.show_user_modal = True
                    st.rerun()
            with u_btn2:
                if st.button(
                    "🚪 Log Out",
                    key="sidebar_logout_btn",
                    use_container_width=True,
                ):
                    logout_user_session()
                    st.rerun()
        else:
            st.caption("Poultry, Pigs & Custom Livestock Operations")

        if st.button(
            "➕ Add Livestock Batch",
            key="sidebar_add_batch_btn",
            type="primary",
            use_container_width=True,
        ):
            st.session_state.pending_reset = True
            st.session_state.show_species_modal = False
            st.session_state.show_user_modal = False
            st.session_state.show_batch_modal = True
            st.rerun()

        if st.button(
            "🐾 Manage Species Presets",
            key="sidebar_manage_species_btn",
            use_container_width=True,
        ):
            st.session_state.show_batch_modal = False
            st.session_state.show_user_modal = False
            st.session_state.show_species_modal = True
            st.rerun()

        st.divider()
        st.markdown("##### 🔍 Herd Filters")
        species_present = sorted({b["species"] for b in all_batches})
        if species_present:
            selected_species_filter = st.multiselect(
                "Filter by Species",
                options=species_present,
                default=species_present,
                format_func=lambda s: s.title(),
            )
        else:
            selected_species_filter = []

        selected_status_filter = st.multiselect(
            "Filter by Batch Status",
            options=STATUS_OPTIONS,
            default=STATUS_OPTIONS,
        )

        filtered_schedules = [
            s
            for s in all_schedules
            if s["batch"]["species"] in selected_species_filter
            and get_batch_status_badge(s) in selected_status_filter
        ]
        filtered_active_schedules = [
            s for s in filtered_schedules if s["batch"]["active"]
        ]

        if filtered_active_schedules:
            ics_content = generate_ics_calendar(filtered_active_schedules)
            st.download_button(
                "📅 Export Active Plan (.ics)",
                data=ics_content,
                file_name="homestead_schedule.ics",
                mime="text/calendar",
                use_container_width=True,
                help="Download an iCalendar (.ics) file with all active batch milestones.",
            )

        st.divider()
        with st.expander("⚙️ Settings & Backup", expanded=False):
            st.caption("Storage barrel capacity & feed bag size")
            s_col1, s_col2 = st.columns(2)
            new_barrel_cap = s_col1.number_input(
                "Barrel (lbs)",
                min_value=10,
                max_value=5000,
                value=barrel_capacity,
                step=25,
            )
            new_bag_weight = s_col2.number_input(
                "Bag (lbs)",
                min_value=5,
                max_value=2000,
                value=bag_weight,
                step=5,
            )
            if int(new_barrel_cap) != barrel_capacity:
                set_barrel_capacity(int(new_barrel_cap))
                st.rerun()
            if int(new_bag_weight) != bag_weight:
                set_bag_weight(int(new_bag_weight))
                st.rerun()

            st.markdown("---")
            backup_str = export_backup_json()
            st.download_button(
                "⬇️ Download Backup (.json)",
                data=backup_str,
                file_name=f"homestead_plan_{date.today().isoformat()}.json",
                mime="application/json",
                use_container_width=True,
            )
            uploaded_file = st.file_uploader(
                "Restore from JSON", type=["json"]
            )
            import_mode = st.radio(
                "Import Mode",
                ["Merge with existing batches", "Replace existing batches"],
                index=0,
            )
            if uploaded_file is not None and st.button(
                "📥 Import JSON Backup",
                type="primary",
                use_container_width=True,
            ):
                try:
                    raw_content = uploaded_file.read().decode("utf-8")
                    imported_n = import_backup_json(
                        raw_content,
                        replace_existing=(
                            import_mode == "Replace existing batches"
                        ),
                    )
                    st.success(f"Imported {imported_n} batch(es)!")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Failed to import backup: {exc}")

    return filtered_schedules, filtered_active_schedules
