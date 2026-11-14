"""@st.dialog modal for adding, editing, deleting, and resetting custom livestock species presets."""

import streamlit as st

from homestead.config import LIFECYCLE_TYPES
from homestead.database import (
    delete_species_preset,
    get_active_user_id,
    get_species_catalog,
    save_species_preset,
    seed_user_species_presets,
)


def _dismiss_species_modal() -> None:
    st.session_state.show_species_modal = False


@st.dialog(
    "🐾 Manage Livestock Species Presets",
    width="large",
    on_dismiss=_dismiss_species_modal,
)
def render_species_manager_modal(bag_weight: int) -> None:
    if not st.session_state.get("show_species_modal", False):
        st.rerun(scope="app")

    catalog = get_species_catalog()
    existing_keys = list(catalog.keys())
    selector_options = ["__new__"] + existing_keys

    def _fmt_option(opt: str) -> str:
        if opt == "__new__":
            return "➕ Add New Species Preset..."
        p = catalog.get(opt, {})
        lc_badge = LIFECYCLE_TYPES.get(
            p.get("lifecycle_type", "direct_arrival"), ""
        ).split(" ")[0]
        return f"{lc_badge} {opt.title()} ({p.get('weeks_to_proc', 8)} wks · {p.get('feed_total', 15.0):,.0f} lbs feed/head)"

    selected_opt = st.selectbox(
        "Select Preset to Edit or Create New",
        options=selector_options,
        key="species_mgr_selector",
        format_func=_fmt_option,
    )

    is_new = selected_opt == "__new__"
    if is_new:
        defaults = {
            "species": "",
            "lifecycle_type": "direct_arrival",
            "days_to_birth": 0,
            "starter_weeks": 3,
            "starter_feed_lbs": 3.0,
            "grower_weeks": 5,
            "grower_feed_lbs": 12.0,
            "starter_bag_price": 24.0,
            "grower_bag_price": 22.0,
            "unit_cost": 4.0,
            "processing_cost": 5.0,
            "expected_yield_lbs": 4.5,
            "sale_price_per_lb": 6.5,
        }
    else:
        defaults = catalog[selected_opt]

    sfx = "new" if is_new else selected_opt.replace(" ", "_")

    top_c1, top_c2, top_c3 = st.columns([1.4, 1.8, 1])
    with top_c1:
        species_name_input = st.text_input(
            "Species / Category Name",
            value="" if is_new else selected_opt.title(),
            key=f"sp_mgr_name_{sfx}",
            disabled=not is_new,
            placeholder="e.g. Meat Rabbits, Ducklings, Quail Eggs",
        )
    with top_c2:
        lc_keys = list(LIFECYCLE_TYPES.keys())
        default_lc = defaults.get("lifecycle_type", "direct_arrival")
        lc_idx = lc_keys.index(default_lc) if default_lc in lc_keys else 0
        lifecycle_type = st.selectbox(
            "Lifecycle Type",
            options=lc_keys,
            index=lc_idx,
            key=f"sp_mgr_lc_{sfx}",
            format_func=lambda k: LIFECYCLE_TYPES[k],
        )
    with top_c3:
        is_direct = lifecycle_type == "direct_arrival"
        default_days = (
            0
            if is_direct
            else (int(defaults["days_to_birth"]) or (114 if lifecycle_type == "mammal_breeding" else 21))
        )
        days_to_birth = st.number_input(
            "Gestation / Incubation (days)",
            min_value=0,
            max_value=400,
            value=0 if is_direct else default_days,
            disabled=is_direct,
            key=f"sp_mgr_days_{sfx}",
            step=1,
        )

    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("**🌱 Starter Ration Defaults**")
        starter_weeks = st.number_input(
            "Starter Phase (wks)",
            min_value=0,
            max_value=104,
            value=int(defaults["starter_weeks"]),
            key=f"sp_mgr_st_wks_{sfx}",
            step=1,
        )
        starter_feed_lbs = st.number_input(
            "Starter Feed/Head (lbs)",
            min_value=0.0,
            max_value=5000.0,
            value=float(defaults["starter_feed_lbs"]),
            key=f"sp_mgr_st_fd_{sfx}",
            step=1.0,
        )
        starter_bag_price = st.number_input(
            f"Starter Bag (${bag_weight}lb)",
            min_value=0.0,
            max_value=500.0,
            value=float(defaults["starter_bag_price"]),
            key=f"sp_mgr_st_pr_{sfx}",
            step=1.0,
        )
    with c2:
        st.markdown("**🌾 Grower Ration Defaults**")
        grower_weeks = st.number_input(
            "Grower/Finisher (wks)",
            min_value=0,
            max_value=260,
            value=int(defaults["grower_weeks"]),
            key=f"sp_mgr_gr_wks_{sfx}",
            step=1,
        )
        grower_feed_lbs = st.number_input(
            "Grower Feed/Head (lbs)",
            min_value=0.0,
            max_value=10000.0,
            value=float(defaults["grower_feed_lbs"]),
            key=f"sp_mgr_gr_fd_{sfx}",
            step=5.0,
        )
        grower_bag_price = st.number_input(
            f"Grower Bag (${bag_weight}lb)",
            min_value=0.0,
            max_value=500.0,
            value=float(defaults["grower_bag_price"]),
            key=f"sp_mgr_gr_pr_{sfx}",
            step=1.0,
        )
    with c3:
        st.markdown("**🥩 Cost & Yield Defaults**")
        unit_cost = st.number_input(
            "Acquisition $/Unit",
            min_value=0.0,
            max_value=5000.0,
            value=float(defaults["unit_cost"]),
            key=f"sp_mgr_ucost_{sfx}",
            step=1.0,
        )
        processing_cost = st.number_input(
            "Processing $/Head",
            min_value=0.0,
            max_value=5000.0,
            value=float(defaults["processing_cost"]),
            key=f"sp_mgr_pcost_{sfx}",
            step=1.0,
        )
        expected_yield_lbs = st.number_input(
            "Meat Yield (lbs/head)",
            min_value=0.0,
            max_value=2000.0,
            value=float(defaults["expected_yield_lbs"]),
            key=f"sp_mgr_yield_{sfx}",
            step=0.5,
        )
        sale_price_per_lb = st.number_input(
            "Target Sale Price ($/lb)",
            min_value=0.0,
            max_value=500.0,
            value=float(defaults["sale_price_per_lb"]),
            key=f"sp_mgr_sprice_{sfx}",
            step=0.5,
        )

    apply_to_active = False
    if not is_new:
        apply_to_active = st.checkbox(
            f"Apply updated defaults to existing active '{selected_opt.title()}' batches",
            value=False,
            key=f"sp_mgr_apply_active_{sfx}",
        )

    btn_c1, btn_c2, btn_c3 = st.columns([2, 1.2, 1.2])
    with btn_c1:
        save_btn_label = (
            "➕ Create Species Preset"
            if is_new
            else f"💾 Save '{selected_opt.title()}' Preset"
        )
        if st.button(
            save_btn_label,
            key=f"sp_mgr_save_btn_{sfx}",
            type="primary",
            width="stretch",
        ):
            target_species = (
                species_name_input.strip().lower()
                if is_new
                else selected_opt
            )
            if not target_species:
                st.error("Please enter a species name.")
            else:
                saved = save_species_preset(
                    target_species,
                    {
                        "lifecycle_type": lifecycle_type,
                        "days_to_birth": 0 if is_direct else int(days_to_birth),
                        "starter_weeks": int(starter_weeks),
                        "starter_feed_lbs": float(starter_feed_lbs),
                        "grower_weeks": int(grower_weeks),
                        "grower_feed_lbs": float(grower_feed_lbs),
                        "starter_bag_price": float(starter_bag_price),
                        "grower_bag_price": float(grower_bag_price),
                        "unit_cost": float(unit_cost),
                        "processing_cost": float(processing_cost),
                        "expected_yield_lbs": float(expected_yield_lbs),
                        "sale_price_per_lb": float(sale_price_per_lb),
                    },
                    apply_to_active_batches=bool(apply_to_active),
                )
                st.session_state.form_species = saved["species"]
                st.success(
                    f"Saved species preset '{saved['species'].title()}'!"
                )
                st.rerun()

    with btn_c2:
        if not is_new and len(existing_keys) > 1:
            if st.button(
                "🗑️ Delete Preset",
                key=f"sp_mgr_del_btn_{sfx}",
                width="stretch",
            ):
                delete_species_preset(selected_opt)
                st.session_state.species_mgr_selector = "__new__"
                st.rerun()

    with btn_c3:
        if st.button(
            "🔄 Reset 4 Defaults",
            key="sp_mgr_reset_defaults_btn",
            width="stretch",
            help="Restore the 4 built-in presets (Chicken Eggs, Chicken Chicks, Turkey Chicks, Kunekune Pigs).",
        ):
            uid = get_active_user_id()
            if uid is not None:
                seed_user_species_presets(uid, reset_defaults=True)
            st.rerun()

    st.divider()
    st.button(
        "Close",
        key="close_species_modal_btn",
        width="stretch",
        on_click=_dismiss_species_modal,
    )
