"""Today radar dashboard, timeline builder, milestone acks, and ICS calendar export."""

from datetime import date
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from homestead.calendar_export import generate_ics_calendar
from homestead.db.batches import acknowledge_milestone, list_acknowledged_milestones
from homestead.db.core import get_bag_weight, get_species_catalog
from homestead.db.eggs import compute_egg_dashboard
from homestead.db.notes import list_farm_notes
from homestead.scheduling import compute_upcoming_milestones
from homestead.web import (
    _get_owned_batch,
    _render,
    _schedules_for_user,
    current_user,
    optional_user,
    templates,
)

router = APIRouter()


def _build_timeline(schedules: list[dict]) -> dict | None:
    if not schedules:
        return None
    min_d = min(s["start_date"] for s in schedules)
    max_d = max(s["proc_date"] for s in schedules)
    span = max(1, (max_d - min_d).days)
    pct = lambda d: round(max(0.0, min(100.0, (d - min_d).days / span * 100)), 2)

    today = date.today()
    today_pct = pct(today) if min_d <= today <= max_d else None

    months = []
    cur = min_d.replace(day=1)
    while cur <= max_d:
        if cur >= min_d:
            months.append({"label": cur.strftime("%b '%y"), "pct": pct(cur)})
        cur = date(cur.year + (cur.month // 12), (cur.month % 12) + 1, 1)

    rows = []
    for s in schedules:
        b = s["batch"]
        rows.append({
            "id": b["id"],
            "name": b["name"],
            "span_label": f"{s['start_date'].strftime('%b %-d')} → {s['proc_date'].strftime('%b %-d')}",
            "incub_left": pct(s["start_date"]),
            "incub_w": round(max(0.0, pct(s["proc_base"]) - pct(s["start_date"])), 2),
            "start_left": pct(s["proc_base"]),
            "start_w": round(max(0.0, pct(s["transition_date"]) - pct(s["proc_base"])), 2),
            "grow_left": pct(s["transition_date"]),
            "grow_w": round(max(0.0, pct(s["proc_date"]) - pct(s["transition_date"])), 2),
        })
    return {"rows": rows, "months": months, "today_pct": today_pct}


@router.get("/")
def radar_page(request: Request, msg: str = "", user: dict | None = Depends(optional_user)):
    if user is None:
        return templates.TemplateResponse(request, "home.html", {"request": request})
    schedules = _schedules_for_user(user["id"], include_archived=False)
    active_schedules = [s for s in schedules if not s["is_planned"]]
    planned_schedules = [s for s in schedules if s["is_planned"]]
    acked = list_acknowledged_milestones([s["batch"]["id"] for s in schedules])
    milestones = [
        m for m in compute_upcoming_milestones(schedules)
        if m["days_away"] <= 14 and (int(m["batch_id"]), str(m["task"])) not in acked
    ]
    return _render(
        request, "radar.html", user, "radar",
        schedules=schedules,
        active_schedules=active_schedules,
        planned_schedules=planned_schedules,
        milestones=milestones,
        timeline=_build_timeline(schedules),
        total_head=sum(s["on_farm_head"] for s in active_schedules),
        total_losses=sum(s["initial_quantity"] - s["surviving_quantity"] for s in active_schedules),
        bag_weight=get_bag_weight(user_id=user["id"]),
        egg_dash=compute_egg_dashboard(user["id"], bag_weight=get_bag_weight(user_id=user["id"])),
        species_catalog=get_species_catalog(user_id=user["id"]),
        pinned_notes=list_farm_notes(
            user["id"], pinned_only=True, include_archived=False, include_comments=False
        ),
        msg=msg,
    )


@router.post("/milestones/ack")
def milestone_ack_submit(
    batch_id: int = Form(...),
    task: str = Form(...),
    user: dict = Depends(current_user),
):
    _get_owned_batch(batch_id, user)
    acknowledge_milestone(batch_id, task, done=True)
    return RedirectResponse("/?msg=" + quote_plus(f"Checked off: {task}"), status_code=303)


@router.get("/calendar.ics")
def calendar_ics(user: dict = Depends(current_user)):
    schedules = _schedules_for_user(user["id"], include_archived=False)
    ics = generate_ics_calendar(schedules)
    return Response(
        ics, media_type="text/calendar",
        headers={"Content-Disposition": 'attachment; filename="homestead-schedule.ics"'},
    )
