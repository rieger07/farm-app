import streamlit as st
from streamlit_timeline import st_timeline
from datetime import timedelta, date
import math
import urllib.parse


def create_gcal_link(title, start_date, end_date, description):
    # Format dates to YYYYMMDD (no dashes for Google)
    start_fmt = start_date.strftime("%Y%m%d")
    end_fmt = end_date.strftime("%Y%m%d")

    base_url = "https://www.google.com/calendar/render?action=TEMPLATE"
    params = {
        "text": title,
        "dates": f"{start_fmt}/{end_fmt}",
        "details": description,
        "src": 11,
    }
    return base_url + "&" + urllib.parse.urlencode(params)


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
items = []
with st.form("homestead_form"):
    order_type = st.selectbox("Select Selection:", list(LIVESTOCK_DATA.keys()))

    # Dynamic prompt based on type
    date_label = "Select Arrival Date Range"
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
    if "pigs" in order_type:
        content = "Breeding"
    else:
        content = "Arrival"

    if len(selected_dates) == 2:

        start_date, end_date = selected_dates
        items.append(
            {
                "id": 1,
                "content": content,
                "start": start_date.strftime("%Y-%m-%d"),
                "end": end_date.strftime("%Y-%m-%d"),
            }
        )
        data = LIVESTOCK_DATA[order_type]

        # 1. Timeline Logic
        if data["days_to_birth"] > 0:
            event_start = start_date + timedelta(days=data["days_to_birth"])
            event_end = end_date + timedelta(days=data["days_to_birth"])
            st.info(
                f"📅 **Estimated Birth/Hatch Window:** {event_start} to {event_end}"
            )
            gcal_url = create_gcal_link(
                f"{order_type} {'Birth' if 'pigs' in order_type else 'Hatch'} Window",
                event_start,
                event_end,
                f"Look out for the new {order_type}!",
            )
            st.link_button("📅 Add Birth/Hatch Window to Google Calendar", gcal_url)
            if "pigs" in order_type:
                content = "Gestation"
            else:
                content = "Incubation"

            items.append(
                {
                    "id": 2,
                    "content": content,
                    "start": end_date.strftime("%Y-%m-%d"),
                    "end": event_end.strftime("%Y-%m-%d"),
                }
            )
            proc_base = event_end
        else:
            st.info("📦 **Arrival:** Immediate start (already hatched).")
            proc_base = start_date

        # 2. Feed & Storage Logic
        total_feed = quantity * data["feed_total"]
        barrels_needed = math.ceil(total_feed / BARREL_CAPACITY)
        proc_date = proc_base + timedelta(weeks=data["weeks_to_proc"])
        items.append(
            {
                "id": 3,
                "content": "Growing",
                "start": proc_base.strftime("%Y-%m-%d"),
                "end": proc_date.strftime("%Y-%m-%d"),
            }
        )
        items.append(
            {
                "id": 4,
                "content": "Processing",
                "start": proc_date.strftime("%Y-%m-%d"),
            }
        )
        # UI Display
        st.success(f"🔪 **Approximate Processing Date:** Starting around {proc_date}")
        gcal_url = create_gcal_link(
            f"{order_type} Processing Date",
            proc_date,
            proc_date,
            f"Look out for the new {order_type}!",
        )
        st.link_button("📅 Add Processing Date to Google Calendar", gcal_url)
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

        timeline = st_timeline(
            items,
            groups=[],
            options={
                "selectable": True,
            },
        )
        st.subheader("Selected Item")
        st.write(timeline)

    else:
        st.warning(
            "Please select a date **range** (start and end date) on the calendar."
        )
