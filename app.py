"""Homestead Planner — FastAPI web app entrypoint."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from homestead.db.core import init_db
from homestead.routes import (
    auth,
    batches,
    customers,
    eggs,
    farm_ops,
    harvest_picking,
    herds,
    preorders_pickup,
    radar,
    settings,
)
from homestead.web import BASE, _LoginRedirect, templates
from shared.web import RevalidatingStatic, mount_shared_static


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Homestead Planner", lifespan=lifespan)
app.mount("/static", RevalidatingStatic(directory=BASE / "static"), name="static")
mount_shared_static(app, templates)


@app.exception_handler(_LoginRedirect)
def _on_login_redirect(_, __):
    return RedirectResponse("/login", status_code=303)


for _router in (
    auth.router,
    radar.router,
    batches.router,
    harvest_picking.router,
    customers.router,
    preorders_pickup.router,
    eggs.router,
    herds.router,
    farm_ops.router,
    settings.router,
):
    app.include_router(_router)
