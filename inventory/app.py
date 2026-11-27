"""Iron Ledger — FastAPI web app entrypoint."""

import asyncio
from contextlib import asynccontextmanager
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from inventory import sync, zettle
from inventory.db import init_inventory_db
from inventory.routes import (
    admin,
    auth,
    billing,
    inventory,
    market_day,
    quick_sale,
    sales,
    settings,
)
from inventory.web import (
    BASE,
    Forbidden,
    NeedsLogin,
    local_tz,
    templates,
)
from shared.web import RevalidatingStatic, mount_shared_static

__all__ = ["app", "local_tz"]


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_inventory_db()
    task = None
    interval = int(os.environ.get("ZETTLE_POLL_SECONDS", "300"))
    if zettle.configured() and interval > 0:
        async def poll():
            await asyncio.sleep(5)
            while True:
                await asyncio.to_thread(sync.sync_all)
                await asyncio.sleep(interval)

        task = asyncio.create_task(poll())
    yield
    if task:
        task.cancel()


app = FastAPI(title="iron ledger", lifespan=lifespan)
app.mount("/static", RevalidatingStatic(directory=BASE / "static"), name="static")
mount_shared_static(app, templates)


@app.exception_handler(NeedsLogin)
async def _needs_login(request: Request, exc: NeedsLogin):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "login_required"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(Forbidden)
async def _forbidden(request: Request, exc: Forbidden):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return RedirectResponse(exc.redirect, status_code=303)


for _router in (
    auth.router,
    inventory.router,
    sales.router,
    market_day.router,
    quick_sale.router,
    settings.router,
    admin.router,
    billing.router,
):
    app.include_router(_router)
