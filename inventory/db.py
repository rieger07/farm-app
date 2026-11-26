"""SQLite connection and schema entrypoint for the inventory service.

Delegates connection configuration and ordered migrations to ``shared.db``.
"""

from shared.db import get_connection, get_db_path, migrate

__all__ = ["get_connection", "get_db_path", "init_inventory_db"]


def init_inventory_db() -> None:
    """Initialize and migrate the shared SQLite database (WAL mode enabled)."""
    migrate()
