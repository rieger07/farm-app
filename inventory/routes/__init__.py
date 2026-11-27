"""FastAPI APIRouter modules for Iron Ledger."""

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

__all__ = [
    "admin",
    "auth",
    "billing",
    "inventory",
    "market_day",
    "quick_sale",
    "sales",
    "settings",
]
