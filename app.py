import streamlit as st
from datetime import timedelta, date
import math

# Livestock Data Dictionary
# days_to_birth: Gestation or Incubation days
# weeks_to_proc: Weeks from birth/hatch to harvest
# feed_total: Total lbs of supplemental feed per animal for its lifetime
LIVESTOCK_DATA = {
    "chicken eggs": {"days_to_birth": 21, "weeks_to_proc": 8, "feed_total": 15},
    "chicken chicks": {"days_to_birth": 0, "weeks_to_proc": 8, "feed_total": 15},
    "turkey chicks": {"days_to_birth": 0, "weeks_to_proc": 18, "feed_total": 75},
    "kunekune pigs": {"days_to_birth": 116, "weeks_to_proc": 52, "feed_total": 650},
}

BARREL_CAPACITY = 300  # lbs per barrel

st.title("🚜 Homestead Planner: Poultry & Pigs")

with st.form("homestead_form"):
    order_type = st.selectbox("Select Selection:", list(LIVESTOCK_DATA.keys()))

    # Dynamic prompt based on type
    date_label = "Select Arrival/Breeding Date Range"
    if "pigs" in order_type:
        date_label = "Select Expected Date Range of Breeding"

    today = date.today()
    selected_dates = st.date_input(
        date_label,
        value=(today + timedelta(days=7), today + timedelta(days=14)),
        min_value=today,
    )

    quantity = st.number_input("Quantity", min_value=1, value=1, step=1)

    submitted = st.form_submit_button("Calculate Timeline & Storage")

if submitted:
    if len(selected_dates) == 2:
        start_date, end_date = selected_dates
        data = LIVESTOCK_DATA[order_type]

        # 1. Timeline Logic
        if data["days_to_birth"] > 0:
            event_start = start_date + timedelta(days=data["days_to_birth"])
            event_end = end_date + timedelta(days=data["days_to_birth"])
            st.info(
                f"📅 **Estimated Birth/Hatch Window:** {event_start} to {event_end}"
            )
            proc_base = event_start
        else:
            st.info("📦 **Arrival:** Immediate start (already hatched).")
            proc_base = start_date

        # 2. Feed & Storage Logic
        total_feed = quantity * data["feed_total"]
        barrels_needed = math.ceil(total_feed / BARREL_CAPACITY)
        proc_date = proc_base + timedelta(weeks=data["weeks_to_proc"])

        # UI Display
        st.success(f"🔪 **Approximate Processing Date:** Starting around {proc_date}")

        c1, c2 = st.columns(2)
        c1.metric("Total Feed Needed", f"{total_feed} lbs")
        c2.metric("300lb Barrels", f"{barrels_needed}")

        # 3. Expanded Assumptions
        with st.expander("🔍 View Data Assumptions"):
            st.write(f"**Species:** {order_type.title()}")
            st.write(f"- **Gestation/Incubation:** {data['days_to_birth']} days")
            st.write(f"- **Time to Maturity:** {data['weeks_to_proc']} weeks")
            st.write(f"- **Estimated Grain per Animal:** {data['feed_total']} lbs")
            st.write(f"- **Storage Capacity:** {BARREL_CAPACITY} lbs per barrel")
            if "pigs" in order_type:
                st.caption("*Note: Kunekune feed needs vary based on pasture quality.*")
    else:
        st.warning(
            "Please select a date **range** (start and end date) on the calendar."
        )
