from datetime import date, datetime, timedelta
import hashlib
import importlib.util
import json
import math
import os
import sqlite3
import sys
import types
import urllib.parse
import pandas as pd
import streamlit as st

# Provide a lightweight stdlib shim for pkg_resources (removed in setuptools>=82),
# which streamlit-vis-timeline imports to locate its frontend/build directory.
if "pkg_resources" not in sys.modules:
    try:
        import pkg_resources  # type: ignore # noqa: F401
    except ModuleNotFoundError:
        _pkg_res = types.ModuleType("pkg_resources")

        def _resource_filename(package_or_requirement: str, resource_name: str) -> str:
            spec = importlib.util.find_spec(package_or_requirement)
            if spec and spec.origin:
                return os.path.join(os.path.dirname(spec.origin), resource_name)
            raise ModuleNotFoundError(
                f"No module named {package_or_requirement!r}"
            )

        _pkg_res.resource_filename = _resource_filename  # type: ignore[attr-defined]
        sys.modules["pkg_resources"] = _pkg_res

from streamlit_timeline import st_timeline

# Livestock Data Dictionary (Default Species Presets)
# days_to_birth: Gestation or Incubation days
# weeks_to_proc: Weeks from birth/hatch to harvest
# feed_total: Total lbs of supplemental feed per animal for its lifetime
LIVESTOCK_DATA = {
    "chicken eggs": {"days_to_birth": 21, "weeks_to_proc": 8, "feed_total": 15.0},
    "chicken chicks": {"days_to_birth": 0, "weeks_to_proc": 8, "feed_total": 15.0},
    "turkey chicks": {"days_to_birth": 0, "weeks_to_proc": 18, "feed_total": 75.0},
    "kunekune pigs": {"days_to_birth": 116, "weeks_to_proc": 52, "feed_total": 650.0},
}

DEFAULT_BARREL_CAPACITY = 300  # lbs per barrel


def get_db_path() -> str:
    return os.environ.get("FARM_DB_PATH", os.path.join("data", "homestead.db"))


# ---------------------------------------------------------------------------
# Database Persistence Layer (SQLite + JSON Import/Export)
# ---------------------------------------------------------------------------
def get_db_connection() -> sqlite3.Connection:
    db_path = get_db_path()
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                species TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                days_to_birth INTEGER NOT NULL,
                weeks_to_proc INTEGER NOT NULL,
                feed_total REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.commit()


def get_barrel_capacity() -> int:
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'barrel_capacity'"
        ).fetchone()
        if row:
            try:
                return max(1, int(float(row["value"])))
            except (ValueError, TypeError):
                return DEFAULT_BARREL_CAPACITY
    return DEFAULT_BARREL_CAPACITY


def set_barrel_capacity(capacity: int) -> None:
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value)
            VALUES ('barrel_capacity', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(max(1, int(capacity))),),
        )
        conn.commit()


def list_batches() -> list[dict]:
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM batches ORDER BY start_date ASC, id ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def save_batch(batch_data: dict, batch_id: int | None = None) -> int:
    with get_db_connection() as conn:
        if batch_id is not None:
            conn.execute(
                """
                UPDATE batches
                SET name = ?, species = ?, quantity = ?, start_date = ?, end_date = ?,
                    days_to_birth = ?, weeks_to_proc = ?, feed_total = ?, active = ?, notes = ?
                WHERE id = ?
                """,
                (
                    str(batch_data["name"]),
                    str(batch_data["species"]),
                    int(batch_data["quantity"]),
                    str(batch_data["start_date"]),
                    str(batch_data["end_date"]),
                    int(batch_data["days_to_birth"]),
                    int(batch_data["weeks_to_proc"]),
                    float(batch_data["feed_total"]),
                    1 if batch_data.get("active", True) else 0,
                    str(batch_data.get("notes", "")),
                    int(batch_id),
                ),
            )
            conn.commit()
            return int(batch_id)
        else:
            cursor = conn.execute(
                """
                INSERT INTO batches (
                    name, species, quantity, start_date, end_date,
                    days_to_birth, weeks_to_proc, feed_total, active, notes, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(batch_data["name"]),
                    str(batch_data["species"]),
                    int(batch_data["quantity"]),
                    str(batch_data["start_date"]),
                    str(batch_data["end_date"]),
                    int(batch_data["days_to_birth"]),
                    int(batch_data["weeks_to_proc"]),
                    float(batch_data["feed_total"]),
                    1 if batch_data.get("active", True) else 0,
                    str(batch_data.get("notes", "")),
                    datetime.utcnow().isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)


def set_batch_active(batch_id: int, active: bool) -> None:
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE batches SET active = ? WHERE id = ?",
            (1 if active else 0, int(batch_id)),
        )
        conn.commit()


def delete_batch(batch_id: int) -> None:
    with get_db_connection() as conn:
        conn.execute("DELETE FROM batches WHERE id = ?", (int(batch_id),))
        conn.commit()


def export_backup_json() -> str:
    payload = {
        "version": 1,
        "exported_at": datetime.utcnow().isoformat(timespec="seconds"),
        "settings": {"barrel_capacity": get_barrel_capacity()},
        "batches": list_batches(),
    }
    return json.dumps(payload, indent=2)


def import_backup_json(raw_json: str, replace_existing: bool = False) -> int:
    data = json.loads(raw_json)
    if isinstance(data, list):
        batches_to_import = data
        settings_to_import = {}
    elif isinstance(data, dict):
        batches_to_import = data.get("batches", [])
        settings_to_import = data.get("settings", {})
    else:
        raise ValueError("Invalid JSON backup format.")

    if "barrel_capacity" in settings_to_import:
        set_barrel_capacity(int(settings_to_import["barrel_capacity"]))

    with get_db_connection() as conn:
        if replace_existing:
            conn.execute("DELETE FROM batches")
        count = 0
        for item in batches_to_import:
            species = str(item.get("species", "chicken chicks")).lower()
            defaults = LIVESTOCK_DATA.get(
                species, {"days_to_birth": 0, "weeks_to_proc": 8, "feed_total": 15.0}
            )
            start_str = str(item.get("start_date", date.today().isoformat()))
            end_str = str(item.get("end_date", start_str))
            conn.execute(
                """
                INSERT INTO batches (
                    name, species, quantity, start_date, end_date,
                    days_to_birth, weeks_to_proc, feed_total, active, notes, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(item.get("name") or f"{species.title()} Batch"),
                    species,
                    max(1, int(item.get("quantity", 1))),
                    start_str,
                    end_str,
                    max(0, int(item.get("days_to_birth", defaults["days_to_birth"]))),
                    max(1, int(item.get("weeks_to_proc", defaults["weeks_to_proc"]))),
                    max(0.0, float(item.get("feed_total", defaults["feed_total"]))),
                    1 if item.get("active", 1) else 0,
                    str(item.get("notes", "")),
                    str(
                        item.get("created_at")
                        or datetime.utcnow().isoformat(timespec="seconds")
                    ),
                ),
            )
            count += 1
        conn.commit()
    return count


# ---------------------------------------------------------------------------
# Calendar & Lifecycle Calculation Helpers
# ---------------------------------------------------------------------------
def create_gcal_link(
    title: str, start_date: date, end_date_inclusive: date, description: str
) -> str:
    """Create a Google Calendar all-day template link (end date is exclusive in GCal)."""
    start_fmt = start_date.strftime("%Y%m%d")
    end_exclusive = end_date_inclusive + timedelta(days=1)
    end_fmt = end_exclusive.strftime("%Y%m%d")

    base_url = "https://www.google.com/calendar/render?action=TEMPLATE"
    params = {
        "text": title,
        "dates": f"{start_fmt}/{end_fmt}",
        "details": description,
        "src": 11,
    }
    return base_url + "&" + urllib.parse.urlencode(params)


def compute_batch_schedule(batch: dict, barrel_capacity: int) -> dict:
    start_dt = date.fromisoformat(batch["start_date"])
    end_dt = date.fromisoformat(batch["end_date"])
    if end_dt < start_dt:
        start_dt, end_dt = end_dt, start_dt

    species = batch["species"]
    is_pigs = "pigs" in species.lower()
    days_to_birth = int(batch["days_to_birth"])
    weeks_to_proc = max(1, int(batch["weeks_to_proc"]))
    quantity = max(1, int(batch["quantity"]))
    feed_per_animal = float(batch["feed_total"])

    initial_label = "Breeding" if is_pigs else "Arrival"
    gestation_label = "Gestation" if is_pigs else "Incubation"
    birth_label = "Birth" if is_pigs else "Hatch"

    timeline_items = []
    batch_id = batch["id"]

    # Phase 1: Breeding or Arrival
    if start_dt == end_dt:
        timeline_items.append(
            {
                "id": f"b{batch_id}_init",
                "group": batch_id,
                "content": f"📥 {initial_label}",
                "start": start_dt.isoformat(),
                "end": (end_dt + timedelta(days=1)).isoformat(),
                "style": "background-color: #dbeafe; border-color: #2563eb; color: #1e3a8a;",
                "title": f"{batch['name']} — {initial_label}: {start_dt.isoformat()}",
            }
        )
    else:
        timeline_items.append(
            {
                "id": f"b{batch_id}_init",
                "group": batch_id,
                "content": f"📥 {initial_label}",
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "style": "background-color: #dbeafe; border-color: #2563eb; color: #1e3a8a;",
                "title": f"{batch['name']} — {initial_label}: {start_dt.isoformat()} to {end_dt.isoformat()}",
            }
        )

    # Phase 2: Gestation / Incubation (if applicable)
    if days_to_birth > 0:
        birth_start = start_dt + timedelta(days=days_to_birth)
        birth_end = end_dt + timedelta(days=days_to_birth)
        timeline_items.append(
            {
                "id": f"b{batch_id}_gest",
                "group": batch_id,
                "content": f"⏳ {gestation_label}",
                "start": end_dt.isoformat(),
                "end": birth_end.isoformat(),
                "style": "background-color: #fef3c7; border-color: #d97706; color: #78350f;",
                "title": (
                    f"{batch['name']} — {gestation_label} ({days_to_birth}d) | "
                    f"{birth_label} Window: {birth_start.isoformat()} to {birth_end.isoformat()}"
                ),
            }
        )
        proc_base = birth_end
        proc_window_start = birth_start + timedelta(weeks=weeks_to_proc)
    else:
        birth_start = None
        birth_end = None
        proc_base = start_dt
        proc_window_start = start_dt + timedelta(weeks=weeks_to_proc)

    # Phase 3: Growing & Processing
    proc_date = proc_base + timedelta(weeks=weeks_to_proc)
    total_feed = quantity * feed_per_animal
    weekly_feed = total_feed / weeks_to_proc
    barrels_needed = math.ceil(total_feed / max(1, barrel_capacity))

    timeline_items.append(
        {
            "id": f"b{batch_id}_grow",
            "group": batch_id,
            "content": f"🌾 Growing ({weekly_feed:.1f} lbs/wk)",
            "start": proc_base.isoformat(),
            "end": proc_date.isoformat(),
            "style": "background-color: #dcfce7; border-color: #16a34a; color: #14532d;",
            "title": (
                f"{batch['name']} — Growing ({weeks_to_proc} wks): "
                f"{proc_base.isoformat()} to {proc_date.isoformat()} | "
                f"Total Feed: {total_feed:,.1f} lbs ({barrels_needed} barrels)"
            ),
        }
    )

    timeline_items.append(
        {
            "id": f"b{batch_id}_proc",
            "group": batch_id,
            "content": "🔪 Processing",
            "start": proc_date.isoformat(),
            "style": "background-color: #fee2e2; border-color: #dc2626; color: #7f1d1d;",
            "title": f"{batch['name']} — Approximate Processing Date: {proc_date.isoformat()}",
        }
    )

    return {
        "batch": batch,
        "start_date": start_dt,
        "end_date": end_dt,
        "initial_label": initial_label,
        "gestation_label": gestation_label,
        "birth_label": birth_label,
        "birth_start": birth_start,
        "birth_end": birth_end,
        "proc_base": proc_base,
        "proc_window_start": proc_window_start,
        "proc_date": proc_date,
        "total_feed": total_feed,
        "weekly_feed": weekly_feed,
        "barrels_needed": barrels_needed,
        "timeline_items": timeline_items,
    }


def compute_combined_feed_schedule(
    schedules: list[dict], barrel_capacity: int
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """
    Build weekly and monthly time-phased feed demand across active batches.
    Each batch consumes `total_feed / weeks_to_proc` uniformly over `[proc_base, proc_date)`.
    """
    if not schedules:
        empty_df = pd.DataFrame()
        return (
            empty_df,
            empty_df,
            {
                "peak_weekly_lbs": 0.0,
                "peak_concurrent_lbs": 0.0,
                "peak_concurrent_barrels": 0,
            },
        )

    min_grow_start = min(s["proc_base"] for s in schedules)
    max_grow_end = max(s["proc_date"] for s in schedules)

    # Align start to the Monday of min_grow_start
    first_monday = min_grow_start - timedelta(days=min_grow_start.weekday())
    records = []

    curr_week_start = first_monday
    while curr_week_start < max_grow_end:
        curr_week_end = curr_week_start + timedelta(days=7)
        row = {
            "Week Starting": curr_week_start.isoformat(),
            "Total Weekly Feed (lbs)": 0.0,
            "Remaining Active Batch Feed (lbs)": 0.0,
        }

        for s in schedules:
            b_name = f"{s['batch']['name']} (#{s['batch']['id']})"
            grow_start = s["proc_base"]
            grow_end = s["proc_date"]
            total_days = max(1, (grow_end - grow_start).days)
            daily_rate = s["total_feed"] / total_days

            # Overlap between [curr_week_start, curr_week_end) and [grow_start, grow_end)
            overlap_start = max(curr_week_start, grow_start)
            overlap_end = min(curr_week_end, grow_end)
            overlap_days = max(0, (overlap_end - overlap_start).days)

            feed_this_week = overlap_days * daily_rate
            row[b_name] = round(feed_this_week, 2)
            row["Total Weekly Feed (lbs)"] += feed_this_week

            # If this batch is actively growing during this week, how much feed remains from overlap_start to grow_end?
            if overlap_days > 0:
                remaining_days = max(0, (grow_end - overlap_start).days)
                row["Remaining Active Batch Feed (lbs)"] += remaining_days * daily_rate

        row["Total Weekly Feed (lbs)"] = round(row["Total Weekly Feed (lbs)"], 2)
        row["Remaining Active Batch Feed (lbs)"] = round(
            row["Remaining Active Batch Feed (lbs)"], 2
        )
        row["Concurrent Barrels Needed (Remaining Active)"] = math.ceil(
            row["Remaining Active Batch Feed (lbs)"] / max(1, barrel_capacity)
        )
        records.append(row)
        curr_week_start = curr_week_end

    weekly_df = pd.DataFrame(records)
    if weekly_df.empty:
        return (
            weekly_df,
            pd.DataFrame(),
            {
                "peak_weekly_lbs": 0.0,
                "peak_concurrent_lbs": 0.0,
                "peak_concurrent_barrels": 0,
            },
        )

    # Monthly aggregation
    monthly_df = weekly_df.copy()
    monthly_df["Month"] = monthly_df["Week Starting"].str.slice(0, 7)
    monthly_summary = (
        monthly_df.groupby("Month", as_index=False)
        .agg(
            Monthly_Feed_lbs=("Total Weekly Feed (lbs)", "sum"),
            Peak_Concurrent_Barrels=(
                "Concurrent Barrels Needed (Remaining Active)",
                "max",
            ),
        )
        .rename(
            columns={
                "Monthly_Feed_lbs": "Monthly Feed Consumed (lbs)",
                "Peak_Concurrent_Barrels": "Peak Concurrent Barrels in Month",
            }
        )
    )
    monthly_summary["Monthly Feed Consumed (lbs)"] = monthly_summary[
        "Monthly Feed Consumed (lbs)"
    ].round(1)
    monthly_summary["Barrels Consumed in Month"] = monthly_summary[
        "Monthly Feed Consumed (lbs)"
    ].apply(lambda lbs: round(lbs / max(1, barrel_capacity), 2))

    stats = {
        "peak_weekly_lbs": float(weekly_df["Total Weekly Feed (lbs)"].max()),
        "peak_concurrent_lbs": float(
            weekly_df["Remaining Active Batch Feed (lbs)"].max()
        ),
        "peak_concurrent_barrels": int(
            weekly_df["Concurrent Barrels Needed (Remaining Active)"].max()
        ),
    }
    return weekly_df, monthly_summary, stats


def generate_ics_calendar(schedules: list[dict]) -> str:
    """Generate an RFC 5545 .ics calendar file for all active livestock batches."""
    dtstamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Homestead Planner//Poultry & Pigs//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]

    def add_event(
        uid_seed: str, summary: str, start_d: date, end_d_inc: date, desc: str
    ) -> None:
        uid = hashlib.md5(uid_seed.encode("utf-8")).hexdigest() + "@homesteadplanner"
        dtstart = start_d.strftime("%Y%m%d")
        dtend = (end_d_inc + timedelta(days=1)).strftime("%Y%m%d")
        clean_desc = desc.replace("\n", "\\n")
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{dtstamp}",
                f"DTSTART;VALUE=DATE:{dtstart}",
                f"DTEND;VALUE=DATE:{dtend}",
                f"SUMMARY:{summary}",
                f"DESCRIPTION:{clean_desc}",
                "END:VEVENT",
            ]
        )

    for s in schedules:
        b = s["batch"]
        b_title = f"{b['name']} ({b['quantity']}x {b['species'].title()})"
        add_event(
            f"batch-{b['id']}-init-{s['start_date']}",
            f"🚜 {b['name']}: {s['initial_label']}",
            s["start_date"],
            s["end_date"],
            f"{s['initial_label']} window for {b_title}. Notes: {b.get('notes', '')}",
        )
        if s["birth_start"] and s["birth_end"]:
            add_event(
                f"batch-{b['id']}-birth-{s['birth_start']}",
                f"🐣 {b['name']}: {s['birth_label']} Window",
                s["birth_start"],
                s["birth_end"],
                f"Estimated {s['birth_label'].lower()} window for {b_title}.",
            )
        add_event(
            f"batch-{b['id']}-proc-{s['proc_date']}",
            f"🔪 {b['name']}: Processing Date",
            s["proc_date"],
            s["proc_date"],
            (
                f"Approximate processing date for {b_title}. "
                f"Total lifetime feed: {s['total_feed']:,.1f} lbs ({s['barrels_needed']} barrels)."
            ),
        )

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


# ---------------------------------------------------------------------------
# Streamlit UI Application
# ---------------------------------------------------------------------------
def on_species_change() -> None:
    selected = st.session_state.form_species
    defaults = LIVESTOCK_DATA[selected]
    st.session_state.form_days_to_birth = int(defaults["days_to_birth"])
    st.session_state.form_weeks_to_proc = int(defaults["weeks_to_proc"])
    st.session_state.form_feed_total = float(defaults["feed_total"])


def apply_pending_editor_transitions() -> None:
    today = date.today()
    if "editing_batch_id" not in st.session_state:
        st.session_state.editing_batch_id = None
    if "form_species" not in st.session_state:
        st.session_state.form_species = list(LIVESTOCK_DATA.keys())[0]

    if st.session_state.get("pending_reset", False):
        st.session_state.editing_batch_id = None
        default_sp = st.session_state.form_species
        defaults = LIVESTOCK_DATA[default_sp]
        st.session_state.form_name = ""
        st.session_state.form_quantity = 1
        st.session_state.form_dates = (
            today + timedelta(days=7),
            today + timedelta(days=14),
        )
        st.session_state.form_days_to_birth = int(defaults["days_to_birth"])
        st.session_state.form_weeks_to_proc = int(defaults["weeks_to_proc"])
        st.session_state.form_feed_total = float(defaults["feed_total"])
        st.session_state.form_active = True
        st.session_state.form_notes = ""
        st.session_state.pending_reset = False

    pending_batch = st.session_state.get("pending_edit_batch")
    if pending_batch is not None:
        st.session_state.editing_batch_id = pending_batch["id"]
        st.session_state.form_name = pending_batch["name"]
        st.session_state.form_species = (
            pending_batch["species"]
            if pending_batch["species"] in LIVESTOCK_DATA
            else list(LIVESTOCK_DATA.keys())[0]
        )
        st.session_state.form_quantity = int(pending_batch["quantity"])
        st.session_state.form_dates = (
            date.fromisoformat(pending_batch["start_date"]),
            date.fromisoformat(pending_batch["end_date"]),
        )
        st.session_state.form_days_to_birth = int(pending_batch["days_to_birth"])
        st.session_state.form_weeks_to_proc = int(pending_batch["weeks_to_proc"])
        st.session_state.form_feed_total = float(pending_batch["feed_total"])
        st.session_state.form_active = bool(pending_batch["active"])
        st.session_state.form_notes = pending_batch.get("notes", "")
        st.session_state.pending_edit_batch = None

    defaults = LIVESTOCK_DATA[st.session_state.form_species]
    if "form_name" not in st.session_state:
        st.session_state.form_name = ""
    if "form_quantity" not in st.session_state:
        st.session_state.form_quantity = 1
    if "form_dates" not in st.session_state:
        st.session_state.form_dates = (
            today + timedelta(days=7),
            today + timedelta(days=14),
        )
    if "form_days_to_birth" not in st.session_state:
        st.session_state.form_days_to_birth = int(defaults["days_to_birth"])
    if "form_weeks_to_proc" not in st.session_state:
        st.session_state.form_weeks_to_proc = int(defaults["weeks_to_proc"])
    if "form_feed_total" not in st.session_state:
        st.session_state.form_feed_total = float(defaults["feed_total"])
    if "form_active" not in st.session_state:
        st.session_state.form_active = True
    if "form_notes" not in st.session_state:
        st.session_state.form_notes = ""


def render_app() -> None:
    st.set_page_config(
        page_title="Homestead Planner: Poultry & Pigs",
        page_icon="🚜",
        layout="wide",
    )

    init_db()
    apply_pending_editor_transitions()

    # -----------------------------------------------------------------------
    # Sidebar: Add / Edit Batch, Global Settings & JSON Backup
    # -----------------------------------------------------------------------
    with st.sidebar:
        st.header(
            "✏️ Edit Livestock Batch"
            if st.session_state.editing_batch_id is not None
            else "➕ Add Livestock Batch"
        )

        # Species selection sits outside the form so changing it immediately pre-fills defaults
        order_type = st.selectbox(
            "Select Livestock Type:",
            list(LIVESTOCK_DATA.keys()),
            key="form_species",
            on_change=on_species_change,
            format_func=lambda s: s.title(),
        )

        date_label = (
            "Expected Breeding Date (or Range)"
            if "pigs" in order_type
            else "Arrival Date (or Range)"
        )

        today = date.today()

        with st.form("batch_form", clear_on_submit=False):
            batch_name = st.text_input(
                "Batch Name (optional)",
                key="form_name",
                placeholder=f"e.g. Spring {order_type.title()}",
            )

            selected_dates = st.date_input(
                date_label,
                key="form_dates",
                help="Select either a single date (click same date twice) or a start and end date range. Past and future dates are supported.",
            )

            quantity = st.number_input(
                "Quantity (head / eggs)",
                min_value=1,
                key="form_quantity",
                step=1,
            )

            with st.expander("⚙️ Customize Lifecycle & Feed Overrides", expanded=False):
                days_to_birth = st.number_input(
                    "Gestation / Incubation (days)",
                    min_value=0,
                    max_value=400,
                    key="form_days_to_birth",
                    step=1,
                )
                weeks_to_proc = st.number_input(
                    "Growing Time to Processing (weeks)",
                    min_value=1,
                    max_value=260,
                    key="form_weeks_to_proc",
                    step=1,
                )
                feed_total = st.number_input(
                    "Lifetime Feed per Animal (lbs)",
                    min_value=0.0,
                    max_value=10000.0,
                    key="form_feed_total",
                    step=5.0,
                )
                if "pigs" in order_type:
                    st.caption(
                        "*Note: Kunekune feed needs vary based on pasture quality.*"
                    )

            notes = st.text_area(
                "Batch Notes (optional)",
                key="form_notes",
                placeholder="Breeder/hatchery source, pasture paddock, butcher info...",
                height=68,
            )

            active_flag = st.checkbox(
                "Include in active herd plan & timeline",
                key="form_active",
            )

            if st.session_state.editing_batch_id is not None:
                btn_col1, btn_col2 = st.columns(2)
                submitted = btn_col1.form_submit_button(
                    "💾 Update Batch", use_container_width=True, type="primary"
                )
                cancel_edit = btn_col2.form_submit_button(
                    "Cancel", use_container_width=True
                )
            else:
                submitted = st.form_submit_button(
                    "➕ Save Batch", use_container_width=True, type="primary"
                )
                cancel_edit = False

        if cancel_edit:
            st.session_state.pending_reset = True
            st.rerun()

        if submitted:
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

            existing_count = len(list_batches())
            resolved_name = (
                batch_name.strip() or f"{order_type.title()} #{existing_count + 1}"
            )

            save_batch(
                {
                    "name": resolved_name,
                    "species": order_type,
                    "quantity": quantity,
                    "start_date": start_d.isoformat(),
                    "end_date": end_d.isoformat(),
                    "days_to_birth": days_to_birth,
                    "weeks_to_proc": weeks_to_proc,
                    "feed_total": feed_total,
                    "active": active_flag,
                    "notes": notes.strip(),
                },
                batch_id=st.session_state.editing_batch_id,
            )
            st.session_state.pending_reset = True
            st.rerun()

        st.divider()
        st.subheader("🛢️ Storage Settings")
        current_barrel_cap = get_barrel_capacity()
        new_barrel_cap = st.number_input(
            "Barrel Capacity (lbs per barrel)",
            min_value=10,
            max_value=5000,
            value=current_barrel_cap,
            step=25,
        )
        if int(new_barrel_cap) != current_barrel_cap:
            set_barrel_capacity(int(new_barrel_cap))
            st.rerun()

        st.divider()
        with st.expander("💾 Backup & Restore (JSON)", expanded=False):
            backup_str = export_backup_json()
            st.download_button(
                "⬇️ Download Herd Backup (.json)",
                data=backup_str,
                file_name=f"homestead_plan_{date.today().isoformat()}.json",
                mime="application/json",
                use_container_width=True,
            )
            uploaded_file = st.file_uploader("Restore from JSON", type=["json"])
            import_mode = st.radio(
                "Import Mode",
                ["Merge with existing batches", "Replace existing batches"],
                index=0,
            )
            if uploaded_file is not None and st.button(
                "📥 Import JSON Backup", use_container_width=True
            ):
                try:
                    raw_content = uploaded_file.read().decode("utf-8")
                    imported_n = import_backup_json(
                        raw_content,
                        replace_existing=(import_mode == "Replace existing batches"),
                    )
                    st.success(f"Imported {imported_n} batch(es) successfully!")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Failed to import backup: {exc}")

    # -----------------------------------------------------------------------
    # Main Dashboard: Multi-Batch Overview, Shared Timeline & Feed Schedule
    # -----------------------------------------------------------------------
    st.title("🚜 Homestead Planner: Poultry & Pigs")
    st.caption(
        "Plan, compare, and persist multiple livestock batches on a shared timeline and combined feed/storage schedule."
    )

    all_batches = list_batches()
    barrel_capacity = get_barrel_capacity()

    if not all_batches:
        st.info(
            "👋 **No livestock batches saved yet.** Use the **➕ Add Livestock Batch** panel in the sidebar to add your first batch of poultry or pigs!"
        )
        return

    all_schedules = [compute_batch_schedule(b, barrel_capacity) for b in all_batches]
    active_schedules = [s for s in all_schedules if s["batch"]["active"]]

    # Scenario / Filter Bar + Calendar Export
    top_col1, top_col2 = st.columns([3, 1])
    with top_col1:
        species_present = sorted({b["species"] for b in all_batches})
        selected_species_filter = st.multiselect(
            "Filter Active View by Species",
            options=species_present,
            default=species_present,
            format_func=lambda s: s.title(),
        )
    with top_col2:
        filtered_active_schedules = [
            s
            for s in active_schedules
            if s["batch"]["species"] in selected_species_filter
        ]
        if filtered_active_schedules:
            ics_content = generate_ics_calendar(filtered_active_schedules)
            st.download_button(
                "📅 Export Active Plan (.ics)",
                data=ics_content,
                file_name="homestead_schedule.ics",
                mime="text/calendar",
                use_container_width=True,
                help="Download an iCalendar (.ics) file with Arrival/Breeding, Birth/Hatch, and Processing dates for all active batches.",
            )

    weekly_df, monthly_df, feed_stats = compute_combined_feed_schedule(
        filtered_active_schedules, barrel_capacity
    )

    # Top Summary Metrics
    total_active_batches = len(filtered_active_schedules)
    total_animals = sum(s["batch"]["quantity"] for s in filtered_active_schedules)
    combined_feed_lbs = sum(s["total_feed"] for s in filtered_active_schedules)
    combined_barrels = math.ceil(combined_feed_lbs / max(1, barrel_capacity))

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Active Batches", f"{total_active_batches} / {len(all_batches)}")
    m2.metric("Total Livestock", f"{total_animals:,}")
    m3.metric("Combined Feed Needed", f"{combined_feed_lbs:,.0f} lbs")
    m4.metric(
        f"Total {barrel_capacity}lb Barrels",
        f"{combined_barrels}",
        help="Total barrels required over the full lifetime of all active batches.",
    )
    m5.metric(
        "Peak Concurrent Barrels",
        f"{feed_stats['peak_concurrent_barrels']}",
        delta=f"Peak {feed_stats['peak_weekly_lbs']:,.1f} lbs/wk",
        delta_color="off",
        help="Maximum barrels needed on hand at once for overlapping active batches.",
    )

    if not filtered_active_schedules:
        st.warning(
            "No active batches match the current filter. Enable a batch below or adjust the species filter."
        )
    else:
        # 1. Shared Multi-Batch Swimlane Timeline
        st.subheader("🗓️ Shared Herd Lifecycle Timeline")
        timeline_groups = []
        timeline_items = []
        for s in filtered_active_schedules:
            b = s["batch"]
            timeline_groups.append(
                {
                    "id": b["id"],
                    "content": f"<b>{b['name']}</b><br><small>{b['quantity']}x {b['species'].title()}</small>",
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
            height="340px",
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
                st.info(
                    f"🔎 **Selected Timeline Event:** `{mb['name']}` ({mb['quantity']}x {mb['species'].title()}) — "
                    f"**{matched_schedule['initial_label']}:** {matched_schedule['start_date']} to {matched_schedule['end_date']} | "
                    + (
                        f"**{matched_schedule['birth_label']} Window:** {matched_schedule['birth_start']} to {matched_schedule['birth_end']} | "
                        if matched_schedule["birth_start"]
                        else ""
                    )
                    + f"**Processing:** {matched_schedule['proc_date']} | "
                    f"**Feed:** {matched_schedule['total_feed']:,.0f} lbs ({matched_schedule['barrels_needed']} barrels)"
                )
            else:
                st.caption(f"Selected timeline item: `{selected_tl_item}`")

        # 2. Combined Feed & Barrel Storage Schedule
        st.subheader("📊 Combined Feed & Barrel Storage Schedule")
        if not weekly_df.empty:
            chart_tab1, chart_tab2, chart_tab3 = st.tabs(
                [
                    "🌾 Weekly Feed Burn by Batch",
                    "🛢️ Concurrent Barrel Storage Demand",
                    "📅 Monthly Feed & Barrel Summary Table",
                ]
            )
            batch_cols = [
                f"{s['batch']['name']} (#{s['batch']['id']})"
                for s in filtered_active_schedules
            ]

            with chart_tab1:
                st.caption(
                    "Uniform weekly feed consumption (lbs/week) across each batch's Growing phase."
                )
                weekly_chart_df = weekly_df.set_index("Week Starting")[batch_cols]
                st.bar_chart(weekly_chart_df, height=300)

            with chart_tab2:
                st.caption(
                    f"Number of {barrel_capacity}lb barrels required to hold remaining feed for all batches actively growing in a given week."
                )
                barrel_chart_df = weekly_df.set_index("Week Starting")[
                    ["Concurrent Barrels Needed (Remaining Active)"]
                ]
                st.area_chart(barrel_chart_df, height=300)

            with chart_tab3:
                st.dataframe(monthly_df, use_container_width=True, hide_index=True)
                with st.expander("View Detailed Weekly Feed Breakdown"):
                    st.dataframe(weekly_df, use_container_width=True, hide_index=True)

    # 3. Saved Batches Management & Per-Batch Details
    st.subheader("📋 Saved Livestock Batches")
    for s in all_schedules:
        b = s["batch"]
        status_badge = "🟢 Active" if b["active"] else "⚪ Inactive (Excluded)"
        header_label = (
            f"{status_badge} | **{b['name']}** — {b['quantity']}x {b['species'].title()} "
            f"({s['initial_label']}: {s['start_date']} → Processing: {s['proc_date']}) | "
            f"{s['total_feed']:,.0f} lbs ({s['barrels_needed']} barrels)"
        )

        with st.expander(header_label, expanded=False):
            info_c1, info_c2, info_c3 = st.columns(3)
            with info_c1:
                st.markdown(f"**📥 {s['initial_label']} Window:**")
                if s["start_date"] == s["end_date"]:
                    st.write(f"{s['start_date']}")
                else:
                    st.write(f"{s['start_date']} to {s['end_date']}")

                if s["birth_start"] and s["birth_end"]:
                    st.markdown(f"**🐣 Estimated {s['birth_label']} Window:**")
                    st.write(f"{s['birth_start']} to {s['birth_end']}")
                    birth_gcal = create_gcal_link(
                        f"{b['name']} - {s['birth_label']} Window",
                        s["birth_start"],
                        s["birth_end"],
                        f"Look out for {b['name']} ({b['quantity']}x {b['species']})!",
                    )
                    st.link_button(
                        f"📅 Add {s['birth_label']} Window to Google Calendar",
                        birth_gcal,
                    )
                else:
                    st.caption("📦 Immediate start (already hatched/born).")

            with info_c2:
                st.markdown("**🔪 Approximate Processing Date:**")
                if s["proc_window_start"] != s["proc_date"]:
                    st.write(
                        f"{s['proc_date']} *(Window: {s['proc_window_start']} to {s['proc_date']})*"
                    )
                else:
                    st.write(f"{s['proc_date']}")

                proc_gcal = create_gcal_link(
                    f"{b['name']} - Processing Date",
                    s["proc_date"],
                    s["proc_date"],
                    f"Processing date for {b['name']} ({b['quantity']}x {b['species']}).",
                )
                st.link_button(
                    "📅 Add Processing Date to Google Calendar",
                    proc_gcal,
                )

                if b.get("notes"):
                    st.markdown(f"**📝 Notes:** {b['notes']}")

            with info_c3:
                st.markdown("**🔍 Batch Assumptions & Feed:**")
                st.write(f"- **Gestation/Incubation:** {b['days_to_birth']} days")
                st.write(f"- **Growing Time:** {b['weeks_to_proc']} weeks")
                st.write(
                    f"- **Feed per Animal:** {b['feed_total']:,.1f} lbs ({s['weekly_feed']:,.1f} lbs/wk total)"
                )
                st.write(
                    f"- **Total Batch Feed:** {s['total_feed']:,.1f} lbs ({s['barrels_needed']} × {barrel_capacity}lb barrels)"
                )

            act_c1, act_c2, act_c3, act_c4 = st.columns(4)
            toggle_label = (
                "⏸️ Exclude from Plan" if b["active"] else "▶️ Include in Plan"
            )
            if act_c1.button(
                toggle_label, key=f"toggle_{b['id']}", use_container_width=True
            ):
                set_batch_active(b["id"], not bool(b["active"]))
                st.rerun()

            if act_c2.button(
                "✏️ Edit in Sidebar", key=f"edit_{b['id']}", use_container_width=True
            ):
                st.session_state.pending_edit_batch = b
                st.rerun()

            if act_c3.button(
                "📋 Duplicate", key=f"dup_{b['id']}", use_container_width=True
            ):
                save_batch(
                    {
                        "name": f"{b['name']} (Copy)",
                        "species": b["species"],
                        "quantity": b["quantity"],
                        "start_date": b["start_date"],
                        "end_date": b["end_date"],
                        "days_to_birth": b["days_to_birth"],
                        "weeks_to_proc": b["weeks_to_proc"],
                        "feed_total": b["feed_total"],
                        "active": b["active"],
                        "notes": b.get("notes", ""),
                    }
                )
                st.rerun()

            if act_c4.button(
                "🗑️ Delete", key=f"del_{b['id']}", use_container_width=True
            ):
                if st.session_state.editing_batch_id == b["id"]:
                    st.session_state.pending_reset = True
                delete_batch(b["id"])
                st.rerun()


if os.environ.get("FARM_APP_TESTING") != "1":
    render_app()
