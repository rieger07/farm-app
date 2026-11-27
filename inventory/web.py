"""Shared FastAPI request context, templates, auth cookies, and date-range helpers."""

from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from inventory import billing, sync
from inventory.marketplaces import marketplace_utc_windows
from inventory.merchants import (
    can_access,
    get_membership,
    home_page,
    list_user_merchants,
)
from inventory.sales import money
from shared.auth import create_user_session, get_user_by_session_token


BASE = Path(__file__).parent
COOKIE = "inv_session"
MERCHANT_COOKIE = "inv_merchant"
SESSION_DAYS = 90  # long-lived so staff stay logged in at the stall


def asset(name: str) -> str:
    """URL for a static file with a version query so a new deploy busts every cache."""
    try:
        version = int((BASE / "static" / name).stat().st_mtime)
    except OSError:
        version = 0
    return f"/static/{name}?v={version}"


templates = Jinja2Templates(directory=BASE / "templates")
templates.env.filters["money"] = money
templates.env.globals["asset"] = asset


def local_tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("INVENTORY_TZ", "America/New_York"))


def utc_bounds(d0: date, d1: date) -> tuple[str, str]:
    """UTC ISO bounds [start, end) for the local dates d0 (inclusive) to d1 (exclusive)."""
    tz = local_tz()

    def conv(d: date) -> str:
        local = datetime(d.year, d.month, d.day, tzinfo=tz)
        return local.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")

    return conv(d0), conv(d1)


def to_local(occurred_at: str) -> datetime:
    return datetime.fromisoformat(occurred_at).replace(tzinfo=timezone.utc).astimezone(local_tz())


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


MAX_RANGE_DAYS = 366


def resolve_range(
    period: str, date_from: str = "", date_to: str = "", market_date: str = ""
) -> dict:
    """Turn a period choice into local date bounds plus the window the chart covers."""
    today = datetime.now(local_tz()).date()
    one = timedelta(days=1)
    if period == "day":
        d = _parse_date(market_date)
        if d:
            return {
                "period": "day",
                "label": d.strftime("%a %b %-d, %Y"),
                "start": d,
                "end": d + one,
                "chart_start": max(d - timedelta(days=6), d - timedelta(days=6)),
                "chart_end": d + one,
                "date_from": d.isoformat(),
                "date_to": d.isoformat(),
                "market_date": d.isoformat(),
            }
        period = "today"
    if period == "custom":
        d0, d1 = _parse_date(date_from), _parse_date(date_to)
        if d0 and d1:
            if d0 > d1:
                d0, d1 = d1, d0
            start, end = max(d0, d1 - timedelta(days=MAX_RANGE_DAYS - 1)), d1 + one
            label = (
                start.strftime("%b %-d, %Y") if start == d1
                else f"{start.strftime('%b %-d, %Y')} – {d1.strftime('%b %-d, %Y')}"
            )
            return {"period": "custom", "label": label, "start": start, "end": end,
                    "chart_start": start, "chart_end": end,
                    "date_from": start.isoformat(), "date_to": d1.isoformat(),
                    "market_date": ""}
        period = "week"  # incomplete custom range: fall back
    if period == "yesterday":
        start, end, label = today - one, today, "Yesterday"
    elif period == "month":
        start, end, label = today - timedelta(days=29), today + one, "Last 30 days"
    elif period == "quarter":
        start, end, label = today - timedelta(days=89), today + one, "Last 90 days"
    elif period == "all":
        start, end, label = date(2020, 1, 1), today + one, "All time"
    elif period == "week":
        start, end, label = today - timedelta(days=6), today + one, "Last 7 days"
    else:
        period, start, end, label = "today", today, today + one, "Today"
    if period in ("today", "yesterday"):
        chart_start, chart_end = today - timedelta(days=6), today + one
    elif period == "all":
        chart_start, chart_end = today - timedelta(days=89), today + one
    else:
        chart_start, chart_end = start, end
    return {"period": period, "label": label, "start": start, "end": end,
            "chart_start": chart_start, "chart_end": chart_end,
            "date_from": start.isoformat(), "date_to": (end - one).isoformat(),
            "market_date": ""}


def chart_buckets(chart_start: date, chart_end: date) -> list[dict]:
    """Daily buckets for up to 31 days, weekly buckets beyond that."""
    n = (chart_end - chart_start).days
    step = 1 if n <= 31 else 7
    out, d = [], chart_start
    while d < chart_end:
        nxt = min(d + timedelta(days=step), chart_end)
        if step == 7:
            label, title = d.strftime("%b %-d"), f"Week of {d.strftime('%b %-d')}"
        else:
            label = d.strftime("%a") if n <= 7 else d.strftime("%-d")
            title = d.strftime("%a %b %-d")
        out.append({"d0": d, "d1": nxt, "label": label, "title": title})
        d = nxt
    return out


class Ctx:
    def __init__(self, user: dict, membership: dict | None, workspaces: list[dict]):
        self.user = user
        self.membership = membership or {
            "merchant_id": 0,
            "merchant_name": "No Workspace",
            "role": "owner" if user.get("is_admin") else "staff",
        }
        self.workspaces = workspaces

    @property
    def is_admin(self) -> bool:
        return bool(self.user.get("is_admin"))

    @property
    def role(self) -> str:
        return self.membership["role"]

    @property
    def merchant_id(self) -> int:
        return int(self.membership["merchant_id"])


class NeedsLogin(Exception):
    pass


class Forbidden(Exception):
    def __init__(self, redirect: str):
        self.redirect = redirect


def current_ctx(request: Request) -> Ctx:
    user = get_user_by_session_token(request.cookies.get(COOKIE, ""))
    if not user:
        raise NeedsLogin()
    raw_mid = request.cookies.get(MERCHANT_COOKIE, "")
    preferred_mid = int(raw_mid) if raw_mid.isdigit() else None
    is_admin = bool(user.get("is_admin"))
    membership = get_membership(user["id"], merchant_id=preferred_mid, is_admin=is_admin)
    if not membership and not is_admin:
        raise NeedsLogin()
    workspaces = list_user_merchants(user["id"], is_admin=is_admin)
    return Ctx(user, membership, workspaces)


def require(page: str):
    def dep(ctx: Ctx = Depends(current_ctx)) -> Ctx:
        if page == "admin":
            if not ctx.is_admin:
                raise Forbidden("/" + home_page(ctx.role).replace("_", "-"))
            return ctx
        if ctx.merchant_id == 0 and ctx.is_admin:
            raise Forbidden("/admin")
        if page != "billing" and not ctx.is_admin:
            sub = billing.get_subscription(ctx.merchant_id)
            allowed, _ = billing.is_access_allowed(sub)
            if not allowed:
                raise Forbidden("/billing")
        if page != "billing" and not can_access(ctx.role, page):
            raise Forbidden("/" + home_page(ctx.role).replace("_", "-"))
        return ctx

    return dep


def render(request: Request, name: str, ctx: Ctx, active: str, **extra):
    nav = []
    if ctx.merchant_id != 0:
        nav.extend(
            (k, label, path)
            for k, label, path in (
                ("inventory", "Inventory", "/inventory"),
                ("sales", "Sales", "/sales"),
                ("insights", "Insights", "/insights"),
                ("market_day", "Market day", "/market-day"),
                ("quick_sale", "Quick sale", "/quick-sale"),
                ("settings", "Settings", "/settings"),
            )
            if can_access(ctx.role, k)
        )
    if ctx.is_admin:
        nav.append(("admin", "Admin", "/admin"))
    sub = billing.get_subscription(ctx.merchant_id) if ctx.merchant_id else None
    _, grace_flag = billing.is_access_allowed(sub) if sub else (True, "")
    return templates.TemplateResponse(
        request,
        name,
        {
            "ctx": ctx,
            "active": active,
            "zettle_connected": sync.is_connected(ctx.merchant_id) if ctx.merchant_id else False,
            "billing_sub": sub,
            "billing_grace": grace_flag == "grace",
            "nav": nav,
            **extra,
        },
    )


def _set_session_cookies(resp: RedirectResponse, user_id: int, merchant_id: int | None = None):
    token = create_user_session(user_id, days_valid=SESSION_DAYS)
    secure = os.environ.get("INVENTORY_COOKIE_SECURE", "0") == "1"
    resp.set_cookie(
        COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=secure
    )
    if merchant_id is not None:
        resp.set_cookie(
            MERCHANT_COOKIE,
            str(merchant_id),
            max_age=SESSION_DAYS * 86400,
            httponly=True,
            samesite="lax",
            secure=secure,
        )


def _sales_query(
    period: str, date_from: str, date_to: str, market_date: str = ""
) -> dict:
    rng = resolve_range(period, date_from, date_to, market_date)
    rng["since"], rng["until"] = utc_bounds(rng["start"], rng["end"])
    return rng


def _parse_dollars_to_cents(raw: str | None) -> int | None:
    text = (raw or "").strip().replace("$", "").replace(",", "")
    if not text:
        return None
    return max(0, round(float(text) * 100))


def _resolve_marketplace_windows(
    merchant_id: int, marketplace: str, rng: dict
) -> tuple[int | None, list[tuple[str, str]] | None]:
    mp_id = int(marketplace) if (marketplace or "").strip().isdigit() else None
    if not mp_id:
        return None, None
    return mp_id, marketplace_utc_windows(
        merchant_id, mp_id, rng["date_from"], rng["date_to"], local_tz()
    )


def _csv_safe(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text
