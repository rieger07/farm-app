"""FastAPI APIRouter modules for Homestead Planner."""

from homestead.routes import (
    auth,
    batches,
    customers,
    eggs,
    farm_ops,
    harvest_picking,
    preorders_pickup,
    radar,
    settings,
)

__all__ = [
    "auth",
    "batches",
    "customers",
    "eggs",
    "farm_ops",
    "harvest_picking",
    "preorders_pickup",
    "radar",
    "settings",
]
