"""Shared FastAPI static asset helpers for Homestead Planner and Iron Ledger."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

SHARED_STATIC_DIR = Path(__file__).resolve().parent / "static"


class RevalidatingStatic(StaticFiles):
    """Static files that browsers and CDNs must revalidate so CSS/JS updates ship."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


def shared_asset(name: str) -> str:
    """URL for a shared static file with an mtime cache-buster query string."""
    try:
        version = int((SHARED_STATIC_DIR / name).stat().st_mtime)
    except OSError:
        return f"/shared-static/{name}"
    return f"/shared-static/{name}?v={version}"


def mount_shared_static(app: FastAPI, templates: Jinja2Templates) -> None:
    """Mount ``/shared-static`` on an app and expose ``shared_asset()`` in Jinja templates."""
    app.mount(
        "/shared-static",
        RevalidatingStatic(directory=SHARED_STATIC_DIR),
        name="shared_static",
    )
    templates.env.globals["shared_asset"] = shared_asset


def safe_redirect_path(raw: str | None, fallback: str = "/") -> str:
    """Validate an internal redirect target, blocking scheme, host, backslash, and control-char bypasses."""
    candidate = str(raw or "").strip()
    if (
        not candidate
        or "\\" in candidate
        or any(ord(ch) < 32 for ch in candidate)
        or not candidate.startswith("/")
        or candidate.startswith("//")
    ):
        return fallback
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        return fallback
    path = parsed.path or fallback
    if not path.startswith("/") or path.startswith("//"):
        return fallback
    return urlunsplit(("", "", path, parsed.query, ""))


def safe_redirect(
    next_url: str | None,
    *,
    fallback: str = "/",
    msg: str | None = None,
    status_code: int = 303,
) -> RedirectResponse:
    """Return a 303 RedirectResponse strictly to a validated relative path, optionally updating ?msg=."""
    target = safe_redirect_path(next_url, fallback=fallback)
    if msg is not None:
        parts = urlsplit(target)
        params = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k != "msg"
        ]
        params.append(("msg", str(msg)))
        target = urlunsplit(("", "", parts.path or fallback, urlencode(params), ""))
    return RedirectResponse(target, status_code=status_code)
