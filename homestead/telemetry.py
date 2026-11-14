"""Self-hosted user click, navigation, and render timing telemetry backed by SQLite."""

from datetime import datetime
import json
import math
import time
import streamlit as st

from homestead.database import (
    get_active_user_id,
    get_db_connection,
    get_user_by_id,
)

MAX_TELEMETRY_ROWS = 5000


def _format_details(details: str | dict | None) -> str:
    if details is None:
        return ""
    if isinstance(details, dict):
        return json.dumps(details, separators=(",", ":"))
    return str(details)


def log_telemetry_event(
    event_type: str,
    action: str,
    duration_ms: float | None = None,
    workspace: str = "",
    details: str | dict | None = None,
    user_id: int | None = None,
    username: str | None = None,
) -> None:
    """Persist a telemetry event row in SQLite (with automatic rolling cap)."""
    try:
        resolved_uid = (
            int(user_id) if user_id is not None else get_active_user_id()
        )
        resolved_username = str(username or "").strip()
        if not resolved_username and resolved_uid is not None:
            u = get_user_by_id(resolved_uid)
            if u:
                resolved_username = str(u.get("username", ""))

        resolved_ws = str(workspace or "")
        if not resolved_ws:
            try:
                from streamlit.runtime.scriptrunner import get_script_run_ctx

                if get_script_run_ctx(suppress_warning=True) is not None:
                    resolved_ws = str(
                        st.session_state.get("workspace_nav", "")
                    )
            except Exception:
                resolved_ws = ""

        now_iso = datetime.utcnow().isoformat(timespec="milliseconds")
        clean_dur = (
            round(max(0.0, float(duration_ms)), 2)
            if duration_ms is not None
            else None
        )
        clean_details = _format_details(details)

        with get_db_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO telemetry_events (
                    user_id, username, event_type, action, workspace, duration_ms, details, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolved_uid,
                    resolved_username,
                    str(event_type),
                    str(action),
                    resolved_ws,
                    clean_dur,
                    clean_details,
                    now_iso,
                ),
            )
            last_id = int(cursor.lastrowid or 0)
            if last_id > MAX_TELEMETRY_ROWS and last_id % 100 == 0:
                conn.execute(
                    "DELETE FROM telemetry_events WHERE id <= ?",
                    (last_id - MAX_TELEMETRY_ROWS,),
                )
            conn.commit()
    except Exception:
        # Telemetry must never break user workflows
        pass


def record_ui_action(
    action: str,
    event_type: str = "click",
    details: str | dict | None = None,
) -> None:
    """Queue a UI click/mutation action in session_state so its full click-to-render time is measured."""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        if get_script_run_ctx(suppress_warning=True) is None:
            return
        st.session_state._pending_telemetry_action = {
            "t0": time.perf_counter(),
            "event_type": str(event_type),
            "action": str(action),
            "details": details if isinstance(details, dict) else ({"note": str(details)} if details else {}),
        }
    except Exception:
        pass


def finalize_rerun_telemetry(
    current_user: dict | None,
    active_workspace: str,
    rerun_start_t0: float,
    db_sched_ms: float,
    ui_render_ms: float,
) -> None:
    """Flush any pending UI action or workspace switch with end-to-end timing at the end of render_app()."""
    try:
        uid = int(current_user["id"]) if current_user and current_user.get("id") is not None else None
        uname = str(current_user.get("username", "")) if current_user else ""
        total_pass_ms = round((time.perf_counter() - rerun_start_t0) * 1000.0, 2)

        pending = st.session_state.pop("_pending_telemetry_action", None)
        if pending and isinstance(pending, dict):
            elapsed_ms = round(
                (time.perf_counter() - float(pending.get("t0", rerun_start_t0))) * 1000.0,
                2,
            )
            det = dict(pending.get("details") or {})
            det["db_sched_ms"] = round(db_sched_ms, 2)
            det["ui_render_ms"] = round(ui_render_ms, 2)
            det["pass_ms"] = total_pass_ms
            log_telemetry_event(
                event_type=str(pending.get("event_type", "click")),
                action=str(pending.get("action", "ui_action")),
                duration_ms=elapsed_ms,
                workspace=active_workspace,
                details=det,
                user_id=uid,
                username=uname,
            )
            st.session_state._last_telemetry_workspace = active_workspace
            return

        last_ws = st.session_state.get("_last_telemetry_workspace")
        if last_ws != active_workspace:
            st.session_state._last_telemetry_workspace = active_workspace
            log_telemetry_event(
                event_type="navigation",
                action=f"View {active_workspace}",
                duration_ms=total_pass_ms,
                workspace=active_workspace,
                details={
                    "from": last_ws or "initial_load",
                    "db_sched_ms": round(db_sched_ms, 2),
                    "ui_render_ms": round(ui_render_ms, 2),
                },
                user_id=uid,
                username=uname,
            )
    except Exception:
        pass


def list_telemetry_events(
    limit: int = 250,
    username_filter: str | None = None,
    event_type_filter: str | None = None,
) -> list[dict]:
    query = "SELECT * FROM telemetry_events WHERE 1=1"
    params: list = []
    if username_filter and username_filter != "All Users":
        query += " AND username = ?"
        params.append(str(username_filter))
    if event_type_filter and event_type_filter != "All Types":
        query += " AND event_type = ?"
        params.append(str(event_type_filter))
    query += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, int(limit)))

    with get_db_connection() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
        return [dict(r) for r in rows]


def get_telemetry_summary() -> dict:
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM telemetry_events ORDER BY id DESC LIMIT 2000"
        ).fetchall()

    events = [dict(r) for r in rows]
    durations = sorted(
        float(e["duration_ms"])
        for e in events
        if e.get("duration_ms") is not None
    )
    total_events = len(events)
    avg_ms = round(sum(durations) / len(durations), 2) if durations else 0.0
    p95_idx = (
        min(len(durations) - 1, max(0, math.ceil(len(durations) * 0.95) - 1))
        if durations
        else 0
    )
    p95_ms = round(durations[p95_idx], 2) if durations else 0.0
    max_ms = round(durations[-1], 2) if durations else 0.0

    # Group by action
    action_map: dict[tuple[str, str], list[float]] = {}
    action_counts: dict[tuple[str, str], int] = {}
    for e in events:
        k = (str(e.get("action", "")), str(e.get("event_type", "")))
        action_counts[k] = action_counts.get(k, 0) + 1
        if e.get("duration_ms") is not None:
            action_map.setdefault(k, []).append(float(e["duration_ms"]))

    by_action = []
    for (act, etype), cnt in sorted(
        action_counts.items(), key=lambda x: (-x[1], x[0][0])
    ):
        d_list = action_map.get((act, etype), [])
        by_action.append(
            {
                "Action": act,
                "Type": etype,
                "Count": cnt,
                "Avg (ms)": round(sum(d_list) / len(d_list), 2) if d_list else 0.0,
                "Max (ms)": round(max(d_list), 2) if d_list else 0.0,
            }
        )

    # Group by user
    user_counts: dict[str, int] = {}
    user_durs: dict[str, list[float]] = {}
    user_last: dict[str, str] = {}
    for e in events:
        u = str(e.get("username") or "anonymous")
        user_counts[u] = user_counts.get(u, 0) + 1
        if e.get("duration_ms") is not None:
            user_durs.setdefault(u, []).append(float(e["duration_ms"]))
        if u not in user_last:
            user_last[u] = str(e.get("created_at", ""))

    by_user = []
    for u, cnt in sorted(user_counts.items(), key=lambda x: (-x[1], x[0])):
        d_list = user_durs.get(u, [])
        by_user.append(
            {
                "User": f"@{u}",
                "Events": cnt,
                "Avg Timing (ms)": round(sum(d_list) / len(d_list), 2)
                if d_list
                else 0.0,
                "Last Active (UTC)": user_last.get(u, ""),
            }
        )

    # Group by workspace
    ws_counts: dict[str, int] = {}
    ws_durs: dict[str, list[float]] = {}
    for e in events:
        ws = str(e.get("workspace") or "(Modal / Auth)")
        ws_counts[ws] = ws_counts.get(ws, 0) + 1
        if e.get("duration_ms") is not None:
            ws_durs.setdefault(ws, []).append(float(e["duration_ms"]))

    by_workspace = []
    for ws, cnt in sorted(ws_counts.items(), key=lambda x: (-x[1], x[0])):
        d_list = ws_durs.get(ws, [])
        by_workspace.append(
            {
                "Workspace": ws,
                "Interactions": cnt,
                "Avg Timing (ms)": round(sum(d_list) / len(d_list), 2)
                if d_list
                else 0.0,
                "Max Timing (ms)": round(max(d_list), 2) if d_list else 0.0,
            }
        )

    return {
        "total_events": total_events,
        "avg_ms": avg_ms,
        "p95_ms": p95_ms,
        "max_ms": max_ms,
        "unique_users": len(user_counts),
        "by_action": by_action,
        "by_user": by_user,
        "by_workspace": by_workspace,
    }


def clear_telemetry_events() -> None:
    with get_db_connection() as conn:
        conn.execute("DELETE FROM telemetry_events")
        conn.commit()
