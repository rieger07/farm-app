"""Google Calendar link builder and RFC 5545 .ics calendar export."""

from datetime import date, datetime, timedelta
import hashlib
import urllib.parse


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
        uid = (
            hashlib.md5(uid_seed.encode("utf-8")).hexdigest()
            + "@homesteadplanner"
        )
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
        b_title = f"{b['name']} ({s['surviving_quantity']}/{s['initial_quantity']}x {b['species'].title()})"
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
        if s["starter_weeks"] > 0 and s["grower_weeks"] > 0:
            add_event(
                f"batch-{b['id']}-transition-{s['transition_date']}",
                f"🌾 {b['name']}: Switch to Grower/Finisher Feed",
                s["transition_date"],
                s["transition_date"],
                (
                    f"Transition {b_title} from Starter ration to Grower/Finisher ration. "
                    f"Grower feed needed: {s['grower_feed']:,.1f} lbs ({s['grower_bags']} bags)."
                ),
            )
        add_event(
            f"batch-{b['id']}-proc-{s['proc_date']}",
            f"🔪 {b['name']}: Processing Date",
            s["proc_date"],
            s["proc_date"],
            (
                f"Approximate processing date for {b_title}. "
                f"Total feed: {s['total_feed']:,.1f} lbs ({s['barrels_needed']} barrels). "
                f"Expected meat yield: {s['expected_meat_lbs']:,.1f} lbs (${s['total_cost_per_lb_meat']:,.2f}/lb)."
            ),
        )

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)
