"""Quick-Add and Edit Livestock Batch @st.dialog modal and form session_state transitions."""

from datetime import date, timedelta
import streamlit as st

from homestead.config import (
    LIFECYCLE_TYPES,
    get_lifecycle_labels,
    infer_lifecycle_type,
)
from homestead.database import (
    _normalize_batch_payload,
    get_species_catalog,
    get_species_defaults,
    list_batches,
    save_batch,
    save_species_preset,
)
from homestead.scheduling import compute_batch_schedule

NEW_SPECIES_OPTION = "__new_species__"


def on_species_change() -> None:
    selected = st.session_state.form_species
    if selected == NEW_SPECIES_OPTION:
        st.session_state.form_lifecycle_type = "direct_arrival"
        st.session_state.form_days_to_birth = 0
        return
    defaults = get_species_defaults(selected)
    st.session_state.form_lifecycle_type = str(
        defaults.get("lifecycle_type", "direct_arrival")
    )
    st.session_state.form_days_to_birth = int(defaults["days_to_birth"])
    st.session_state.form_starter_weeks = int(defaults["starter_weeks"])
    st.session_state.form_starter_feed_lbs = float(defaults["starter_feed_lbs"])
    st.session_state.form_grower_weeks = int(defaults["grower_weeks"])
    st.session_state.form_grower_feed_lbs = float(defaults["grower_feed_lbs"])
    st.session_state.form_starter_bag_price = float(
        defaults["starter_bag_price"]
    )
    st.session_state.form_grower_bag_price = float(defaults["grower_bag_price"])
    st.session_state.form_unit_cost = float(defaults["unit_cost"])
    st.session_state.form_processing_cost = float(defaults["processing_cost"])
    st.session_state.form_expected_yield_lbs = float(
        defaults["expected_yield_lbs"]
    )
    st.session_state.form_sale_price_per_lb = float(
        defaults["sale_price_per_lb"]
    )


def apply_pending_editor_transitions() -> None:
    today = date.today()
    catalog = get_species_catalog()
    catalog_keys = list(catalog.keys())
    first_species = catalog_keys[0] if catalog_keys else "chicken chicks"

    if "editing_batch_id" not in st.session_state:
        st.session_state.editing_batch_id = None
    if (
        "form_species" not in st.session_state
        or (
            st.session_state.form_species != NEW_SPECIES_OPTION
            and st.session_state.form_species not in catalog
        )
    ):
        st.session_state.form_species = first_species

    if st.session_state.get("pending_reset", False):
        st.session_state.editing_batch_id = None
        if st.session_state.form_species == NEW_SPECIES_OPTION:
            st.session_state.form_species = first_species
        default_sp = st.session_state.form_species
        defaults = get_species_defaults(default_sp)
        st.session_state.form_name = ""
        st.session_state.form_new_species_name = ""
        st.session_state.form_save_as_preset = False
        st.session_state.form_lifecycle_type = str(
            defaults.get("lifecycle_type", "direct_arrival")
        )
        st.session_state.form_quantity = 1
        st.session_state.form_dates = (
            today + timedelta(days=7),
            today + timedelta(days=14),
        )
        st.session_state.form_days_to_birth = int(defaults["days_to_birth"])
        st.session_state.form_starter_weeks = int(defaults["starter_weeks"])
        st.session_state.form_starter_feed_lbs = float(
            defaults["starter_feed_lbs"]
        )
        st.session_state.form_grower_weeks = int(defaults["grower_weeks"])
        st.session_state.form_grower_feed_lbs = float(
            defaults["grower_feed_lbs"]
        )
        st.session_state.form_starter_bag_price = float(
            defaults["starter_bag_price"]
        )
        st.session_state.form_grower_bag_price = float(
            defaults["grower_bag_price"]
        )
        st.session_state.form_unit_cost = float(defaults["unit_cost"])
        st.session_state.form_processing_cost = float(
            defaults["processing_cost"]
        )
        st.session_state.form_expected_yield_lbs = float(
            defaults["expected_yield_lbs"]
        )
        st.session_state.form_sale_price_per_lb = float(
            defaults["sale_price_per_lb"]
        )
        st.session_state.form_active = True
        st.session_state.form_notes = ""
        st.session_state.pending_reset = False

    pending_batch = st.session_state.get("pending_edit_batch")
    if pending_batch is not None:
        raw_sp = str(pending_batch["species"]).strip().lower()
        sp = raw_sp if raw_sp in catalog else first_species
        defaults = get_species_defaults(sp)
        st.session_state.editing_batch_id = pending_batch["id"]
        st.session_state.form_name = pending_batch["name"]
        st.session_state.form_species = sp
        st.session_state.form_new_species_name = ""
        st.session_state.form_save_as_preset = False
        st.session_state.form_lifecycle_type = str(
            pending_batch.get("lifecycle_type")
            or defaults.get("lifecycle_type", "direct_arrival")
        )
        st.session_state.form_quantity = int(pending_batch["quantity"])
        st.session_state.form_dates = (
            date.fromisoformat(pending_batch["start_date"]),
            date.fromisoformat(pending_batch["end_date"]),
        )
        st.session_state.form_days_to_birth = int(
            pending_batch["days_to_birth"]
        )
        st.session_state.form_starter_weeks = int(
            pending_batch.get("starter_weeks")
            if pending_batch.get("starter_weeks") is not None
            else defaults["starter_weeks"]
        )
        st.session_state.form_starter_feed_lbs = float(
            pending_batch.get("starter_feed_lbs")
            if pending_batch.get("starter_feed_lbs") is not None
            else defaults["starter_feed_lbs"]
        )
        st.session_state.form_grower_weeks = int(
            pending_batch.get("grower_weeks")
            if pending_batch.get("grower_weeks") is not None
            else defaults["grower_weeks"]
        )
        st.session_state.form_grower_feed_lbs = float(
            pending_batch.get("grower_feed_lbs")
            if pending_batch.get("grower_feed_lbs") is not None
            else defaults["grower_feed_lbs"]
        )
        st.session_state.form_starter_bag_price = float(
            pending_batch.get("starter_bag_price")
            if pending_batch.get("starter_bag_price") is not None
            else defaults["starter_bag_price"]
        )
        st.session_state.form_grower_bag_price = float(
            pending_batch.get("grower_bag_price")
            if pending_batch.get("grower_bag_price") is not None
            else defaults["grower_bag_price"]
        )
        st.session_state.form_unit_cost = float(
            pending_batch.get("unit_cost")
            if pending_batch.get("unit_cost") is not None
            else defaults["unit_cost"]
        )
        st.session_state.form_processing_cost = float(
            pending_batch.get("processing_cost")
            if pending_batch.get("processing_cost") is not None
            else defaults["processing_cost"]
        )
        st.session_state.form_expected_yield_lbs = float(
            pending_batch.get("expected_yield_lbs")
            if pending_batch.get("expected_yield_lbs") is not None
            else defaults["expected_yield_lbs"]
        )
        st.session_state.form_sale_price_per_lb = float(
            pending_batch.get("sale_price_per_lb")
            if pending_batch.get("sale_price_per_lb") is not None
            else defaults["sale_price_per_lb"]
        )
        st.session_state.form_active = bool(pending_batch["active"])
        st.session_state.form_notes = pending_batch.get("notes", "")
        st.session_state.pending_edit_batch = None

    active_sp = (
        first_species
        if st.session_state.form_species == NEW_SPECIES_OPTION
        else st.session_state.form_species
    )
    defaults = get_species_defaults(active_sp)
    default_map = {
        "form_name": "",
        "form_new_species_name": "",
        "form_save_as_preset": False,
        "form_lifecycle_type": str(
            defaults.get("lifecycle_type", "direct_arrival")
        ),
        "form_quantity": 1,
        "form_dates": (today + timedelta(days=7), today + timedelta(days=14)),
        "form_days_to_birth": int(defaults["days_to_birth"]),
        "form_starter_weeks": int(defaults["starter_weeks"]),
        "form_starter_feed_lbs": float(defaults["starter_feed_lbs"]),
        "form_grower_weeks": int(defaults["grower_weeks"]),
        "form_grower_feed_lbs": float(defaults["grower_feed_lbs"]),
        "form_starter_bag_price": float(defaults["starter_bag_price"]),
        "form_grower_bag_price": float(defaults["grower_bag_price"]),
        "form_unit_cost": float(defaults["unit_cost"]),
        "form_processing_cost": float(defaults["processing_cost"]),
        "form_expected_yield_lbs": float(defaults["expected_yield_lbs"]),
        "form_sale_price_per_lb": float(defaults["sale_price_per_lb"]),
        "form_active": True,
        "form_notes": "",
    }
    for k, v in default_map.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _dismiss_batch_modal() -> None:
    st.session_state.show_batch_modal = False


def _save_batch_modal_callback() -> None:
    today = date.today()
    raw_order_type = str(
        st.session_state.get("form_species", "chicken chicks")
    )
    custom_species_name = str(
        st.session_state.get("form_new_species_name", "")
    ).strip().lower()
    is_creating_species = raw_order_type == NEW_SPECIES_OPTION
    order_type = (
        (custom_species_name or "custom livestock")
        if is_creating_species
        else raw_order_type.strip().lower()
    )

    batch_name = str(st.session_state.get("form_name", "")).strip()
    quantity = int(st.session_state.get("form_quantity", 1))
    selected_dates = st.session_state.get("form_dates", (today, today))
    days_to_birth = int(st.session_state.get("form_days_to_birth", 0))
    lifecycle_type = str(
        st.session_state.get("form_lifecycle_type")
        or infer_lifecycle_type(order_type, days_to_birth)
    )
    if lifecycle_type == "direct_arrival":
        days_to_birth = 0
    starter_weeks = int(st.session_state.get("form_starter_weeks", 3))
    starter_feed_lbs = float(st.session_state.get("form_starter_feed_lbs", 3.0))
    grower_weeks = int(st.session_state.get("form_grower_weeks", 5))
    grower_feed_lbs = float(st.session_state.get("form_grower_feed_lbs", 12.0))
    starter_bag_price = float(
        st.session_state.get("form_starter_bag_price", 24.0)
    )
    grower_bag_price = float(
        st.session_state.get("form_grower_bag_price", 22.0)
    )
    unit_cost = float(st.session_state.get("form_unit_cost", 4.0))
    processing_cost = float(st.session_state.get("form_processing_cost", 5.0))
    expected_yield_lbs = float(
        st.session_state.get("form_expected_yield_lbs", 4.5)
    )
    sale_price_per_lb = float(
        st.session_state.get("form_sale_price_per_lb", 6.5)
    )
    active_flag = bool(st.session_state.get("form_active", True))
    notes = str(st.session_state.get("form_notes", "")).strip()
    save_as_preset = bool(st.session_state.get("form_save_as_preset", False))

    if isinstance(selected_dates, (list, tuple)):
        if len(selected_dates) == 2:
            start_d, end_d = selected_dates[0], selected_dates[1]
        elif len(selected_dates) == 1:
            start_d = end_d = selected_dates[0]
        else:
            start_d = end_d = today
    elif isinstance(selected_dates, date):
        start_d = end_d = selected_dates
    else:
        start_d = end_d = today

    if end_d < start_d:
        start_d, end_d = end_d, start_d

    eff_grower_weeks = (
        1 if (starter_weeks + grower_weeks < 1) else grower_weeks
    )

    if is_creating_species or save_as_preset:
        save_species_preset(
            order_type,
            {
                "lifecycle_type": lifecycle_type,
                "days_to_birth": days_to_birth,
                "starter_weeks": starter_weeks,
                "starter_feed_lbs": starter_feed_lbs,
                "grower_weeks": eff_grower_weeks,
                "grower_feed_lbs": grower_feed_lbs,
                "starter_bag_price": starter_bag_price,
                "grower_bag_price": grower_bag_price,
                "unit_cost": unit_cost,
                "processing_cost": processing_cost,
                "expected_yield_lbs": expected_yield_lbs,
                "sale_price_per_lb": sale_price_per_lb,
            },
        )
        st.session_state.form_species = order_type

    existing_count = len(list_batches())
    resolved_name = (
        batch_name or f"{order_type.title()} #{existing_count + 1}"
    )

    saved_id = save_batch(
        {
            "name": resolved_name,
            "species": order_type,
            "lifecycle_type": lifecycle_type,
            "quantity": quantity,
            "start_date": start_d.isoformat(),
            "end_date": end_d.isoformat(),
            "days_to_birth": days_to_birth,
            "starter_weeks": starter_weeks,
            "starter_feed_lbs": starter_feed_lbs,
            "grower_weeks": eff_grower_weeks,
            "grower_feed_lbs": grower_feed_lbs,
            "starter_bag_price": starter_bag_price,
            "grower_bag_price": grower_bag_price,
            "unit_cost": unit_cost,
            "processing_cost": processing_cost,
            "expected_yield_lbs": expected_yield_lbs,
            "sale_price_per_lb": sale_price_per_lb,
            "active": active_flag,
            "notes": notes,
        },
        batch_id=st.session_state.get("editing_batch_id"),
    )
    st.session_state.selected_batch_id = saved_id
    st.session_state.show_batch_modal = False
    st.session_state.pending_reset = True


def _cancel_batch_modal_callback() -> None:
    st.session_state.show_batch_modal = False
    st.session_state.pending_reset = True


@st.dialog(
    "🚜 Livestock Batch Editor",
    width="large",
    on_dismiss=_dismiss_batch_modal,
)
def render_batch_editor_modal(barrel_capacity: int, bag_weight: int) -> None:
    if not st.session_state.get("show_batch_modal", False):
        st.rerun(scope="app")

    is_editing = st.session_state.editing_batch_id is not None
    st.markdown(
        f"### {'✏️ Edit Livestock Batch' if is_editing else '➕ Quick-Add Livestock Batch'}"
    )

    today = date.today()
    catalog = get_species_catalog()
    species_options = list(catalog.keys()) + [NEW_SPECIES_OPTION]

    def _fmt_species_opt(sp: str) -> str:
        if sp == NEW_SPECIES_OPTION:
            return "➕ Create New Species Preset..."
        return sp.title()

    # 4 Core Essentials Up Front
    b_col1, b_col2 = st.columns(2)
    with b_col1:
        selected_species_opt = st.selectbox(
            "Select Livestock Type:",
            species_options,
            key="form_species",
            on_change=on_species_change,
            format_func=_fmt_species_opt,
        )
        is_new_species = selected_species_opt == NEW_SPECIES_OPTION
        if is_new_species:
            new_sp_name = st.text_input(
                "New Species Name",
                key="form_new_species_name",
                placeholder="e.g. Meat Rabbits, Ducklings, Quail Eggs",
            )
            order_type = new_sp_name.strip().lower() or "custom livestock"
        else:
            order_type = selected_species_opt

        quantity = st.number_input(
            "Initial Quantity (head / eggs)",
            min_value=1,
            key="form_quantity",
            step=1,
        )
    with b_col2:
        if is_new_species:
            st.selectbox(
                "Lifecycle Type",
                options=list(LIFECYCLE_TYPES.keys()),
                key="form_lifecycle_type",
                format_func=lambda k: LIFECYCLE_TYPES[k],
            )
        current_lc = str(
            st.session_state.get("form_lifecycle_type")
            or catalog.get(order_type, {}).get("lifecycle_type")
            or infer_lifecycle_type(order_type, int(st.session_state.get("form_days_to_birth", 0)))
        )
        lc_labels = get_lifecycle_labels(
            current_lc,
            species=order_type,
            days_to_birth=int(st.session_state.get("form_days_to_birth", 0)),
        )
        selected_dates = st.date_input(
            lc_labels["date_input_label"],
            key="form_dates",
            help="Select either a single date (click same date twice) or a start and end date range.",
        )
        batch_name = st.text_input(
            "Batch Name (optional)",
            key="form_name",
            placeholder=f"e.g. Spring {order_type.title()}",
        )

    # Optional Expander for Ration, Pricing & Notes Overrides
    with st.expander(
        "⚙️ Customize Feed Rations, Pricing & Notes (Optional)",
        expanded=is_new_species,
    ):
        ov_c1, ov_c2, ov_c3 = st.columns(3)
        with ov_c1:
            st.markdown("**🌱 Starter Ration**")
            starter_weeks = st.number_input(
                "Starter Phase (wks)",
                min_value=0,
                max_value=104,
                key="form_starter_weeks",
                step=1,
            )
            starter_feed_lbs = st.number_input(
                "Starter Feed/Head (lbs)",
                min_value=0.0,
                max_value=5000.0,
                key="form_starter_feed_lbs",
                step=1.0,
            )
            starter_bag_price = st.number_input(
                f"Starter Bag (${bag_weight}lb)",
                min_value=0.0,
                max_value=500.0,
                key="form_starter_bag_price",
                step=1.0,
            )
        with ov_c2:
            st.markdown("**🌾 Grower Ration**")
            grower_weeks = st.number_input(
                "Grower/Finisher (wks)",
                min_value=0,
                max_value=260,
                key="form_grower_weeks",
                step=1,
            )
            grower_feed_lbs = st.number_input(
                "Grower Feed/Head (lbs)",
                min_value=0.0,
                max_value=10000.0,
                key="form_grower_feed_lbs",
                step=5.0,
            )
            grower_bag_price = st.number_input(
                f"Grower Bag (${bag_weight}lb)",
                min_value=0.0,
                max_value=500.0,
                key="form_grower_bag_price",
                step=1.0,
            )
        with ov_c3:
            st.markdown("**🥩 Costs & Yield**")
            unit_cost = st.number_input(
                "Acquisition $/Unit",
                min_value=0.0,
                max_value=5000.0,
                key="form_unit_cost",
                step=1.0,
            )
            processing_cost = st.number_input(
                "Processing $/Head",
                min_value=0.0,
                max_value=5000.0,
                key="form_processing_cost",
                step=1.0,
            )
            expected_yield_lbs = st.number_input(
                "Meat Yield (lbs/head)",
                min_value=0.0,
                max_value=2000.0,
                key="form_expected_yield_lbs",
                step=0.5,
            )
            sale_price_per_lb = st.number_input(
                "Target Sale Price ($/lb)",
                min_value=0.0,
                max_value=500.0,
                key="form_sale_price_per_lb",
                step=0.5,
            )

        meta_c1, meta_c2 = st.columns([1, 2])
        with meta_c1:
            if not is_new_species:
                st.selectbox(
                    "Lifecycle Type",
                    options=list(LIFECYCLE_TYPES.keys()),
                    key="form_lifecycle_type",
                    format_func=lambda k: LIFECYCLE_TYPES[k],
                )
            days_to_birth = st.number_input(
                "Gestation / Incubation (days)",
                min_value=0,
                max_value=400,
                key="form_days_to_birth",
                disabled=(st.session_state.get("form_lifecycle_type") == "direct_arrival"),
                step=1,
            )
            active_flag = st.checkbox(
                "Include in active herd plan",
                key="form_active",
            )
            if not is_new_species:
                st.checkbox(
                    f"Save rations/prices as '{order_type.title()}' preset default",
                    key="form_save_as_preset",
                )
        with meta_c2:
            notes = st.text_area(
                "Batch Notes (optional)",
                key="form_notes",
                placeholder="Breeder/hatchery source, pasture paddock, butcher info...",
                height=96,
            )

    # Resolve dates & compute 1-line live summary preview
    if isinstance(selected_dates, (list, tuple)):
        if len(selected_dates) == 2:
            start_d, end_d = selected_dates[0], selected_dates[1]
        elif len(selected_dates) == 1:
            start_d = end_d = selected_dates[0]
        else:
            start_d = end_d = today
    elif isinstance(selected_dates, date):
        start_d = end_d = selected_dates
    else:
        start_d = end_d = today

    if end_d < start_d:
        start_d, end_d = end_d, start_d

    eff_grower_weeks = (
        1 if (int(starter_weeks) + int(grower_weeks) < 1) else int(grower_weeks)
    )
    eff_days_to_birth = (
        0
        if st.session_state.get("form_lifecycle_type") == "direct_arrival"
        else int(days_to_birth)
    )
    preview_payload = _normalize_batch_payload(
        {
            "name": batch_name.strip() or f"{order_type.title()} Preview",
            "species": order_type,
            "lifecycle_type": st.session_state.get("form_lifecycle_type"),
            "quantity": quantity,
            "start_date": start_d.isoformat(),
            "end_date": end_d.isoformat(),
            "days_to_birth": eff_days_to_birth,
            "starter_weeks": int(starter_weeks),
            "starter_feed_lbs": float(starter_feed_lbs),
            "grower_weeks": eff_grower_weeks,
            "grower_feed_lbs": float(grower_feed_lbs),
            "starter_bag_price": float(starter_bag_price),
            "grower_bag_price": float(grower_bag_price),
            "unit_cost": float(unit_cost),
            "processing_cost": float(processing_cost),
            "expected_yield_lbs": float(expected_yield_lbs),
            "sale_price_per_lb": float(sale_price_per_lb),
            "active": active_flag,
            "notes": notes.strip(),
        }
    )
    preview_payload["id"] = st.session_state.editing_batch_id or 0
    preview_sched = compute_batch_schedule(
        preview_payload,
        barrel_capacity=barrel_capacity,
        bag_weight=bag_weight,
        losses=[],
        harvests=[],
        sales=[],
    )

    hatch_part = (
        f"{preview_sched.get('birth_icon', '🐣')} **{preview_sched['birth_label']}:** {preview_sched['birth_start']} → "
        if preview_sched["birth_start"]
        else ""
    )
    st.info(
        f"📊 **Live Preview:** {hatch_part}"
        f"🌾 **Switch:** {preview_sched['transition_date']} → "
        f"🔪 **Butcher:** {preview_sched['proc_date']}  \n"
        f"**Feed:** {preview_sched['total_feed']:,.0f} lbs (**{preview_sched['total_bags']} bags** / {preview_sched['barrels_needed']} barrel(s)) · "
        f"**Est. Cost:** ${preview_sched['total_budget_cost']:,.2f} (${preview_sched['total_cost_per_lb_meat']:,.2f}/lb) · "
        f"**Expected Yield:** {preview_sched['expected_meat_lbs']:,.1f} lbs"
    )

    btn_c1, btn_c2 = st.columns([2, 1])
    save_label = "💾 Update Batch" if is_editing else "➕ Save Batch"
    btn_c1.button(
        save_label,
        key="modal_save_batch_btn",
        type="primary",
        use_container_width=True,
        on_click=_save_batch_modal_callback,
    )
    btn_c2.button(
        "Cancel",
        key="modal_cancel_batch_btn",
        use_container_width=True,
        on_click=_cancel_batch_modal_callback,
    )
