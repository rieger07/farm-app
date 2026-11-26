"""Shared FastAPI static asset helpers for Homestead Planner and Iron Ledger."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
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
