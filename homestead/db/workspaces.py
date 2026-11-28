"""Homestead workspace resolution, role permissions, and member management wrappers."""

from datetime import datetime

from homestead.db.core import get_db_connection
from shared.merchants import (
    ROLES as HOMESTEAD_ROLES,
    add_member as _shared_add_member,
    create_merchant as _shared_create_merchant,
    get_merchant_primary_owner_user_id,
    list_members as list_homestead_merchant_members,
    list_user_merchants as _shared_list_user_merchants,
    remove_member as _shared_remove_member,
    rename_merchant as _shared_rename_merchant,
    update_member_role as _shared_update_member_role,
)


HOMESTEAD_ROLE_PAGES = {
    "owner": {"radar", "batches", "eggs", "herds", "customers", "notes", "feed", "financials", "settings"},
    "manager": {"radar", "batches", "eggs", "herds", "customers", "notes", "feed", "financials"},
    "staff": {"radar", "batches", "eggs", "herds", "notes"},
}


def can_access_homestead_page(role: str, page: str) -> bool:
    return page in HOMESTEAD_ROLE_PAGES.get(role or "staff", set())


def ensure_user_merchant_workspace(user: dict) -> None:
    """Ensure a user has at least one merchant membership (auto-linking or creating a default farm workspace)."""
    uid = int(user["id"])
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        existing = conn.execute(
            "SELECT merchant_id FROM merchant_members WHERE user_id = ? LIMIT 1",
            (uid,),
        ).fetchone()
        if existing:
            return
        # If this is user #1 or an admin and a merchant already exists (e.g. from Iron Ledger), link as owner
        if uid == 1 or user.get("is_admin"):
            first_m = conn.execute("SELECT id FROM merchants ORDER BY id ASC LIMIT 1").fetchone()
            if first_m:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO merchant_members (merchant_id, user_id, role, created_at)
                    VALUES (?, ?, 'owner', ?)
                    """,
                    (int(first_m["id"]), uid, now_iso),
                )
                conn.commit()
                return
        farm_row = conn.execute(
            "SELECT value FROM user_settings WHERE user_id = ? AND key = 'farm_name'",
            (uid,),
        ).fetchone()
        farm_name = (farm_row["value"] if farm_row and farm_row["value"] else "").strip()
        if not farm_name:
            disp = (user.get("display_name") or user.get("username") or "Homestead").strip()
            farm_name = f"{disp}'s Farm"
        cur = conn.execute(
            "INSERT INTO merchants (name, created_at) VALUES (?, ?)",
            (farm_name[:120], now_iso),
        )
        mid = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO merchant_members (merchant_id, user_id, role, created_at) VALUES (?, ?, 'owner', ?)",
            (mid, uid, now_iso),
        )
        conn.commit()


def list_homestead_user_merchants(user_id: int, is_admin: bool = False) -> list[dict]:
    uid = int(user_id)
    merchants = _shared_list_user_merchants(uid, is_admin=is_admin)
    with get_db_connection() as conn:
        for d in merchants:
            d["owner_user_id"] = get_merchant_primary_owner_user_id(
                int(d["merchant_id"]), uid, conn=conn
            )
    return merchants


def resolve_homestead_workspace(user: dict, preferred_merchant_id: int | None = None) -> dict:
    """Resolve the active Farm/Merchant workspace for an authenticated user.

    Sets ``user['id']`` to the workspace primary owner's ``user_id`` (so all
    existing farm tables query the shared workspace data without migration),
    while preserving ``user['actor_id']`` as the logged-in user's own ID.
    """
    ensure_user_merchant_workspace(user)
    actor_id = int(user["id"])
    is_admin = bool(user.get("is_admin"))
    merchants = list_homestead_user_merchants(actor_id, is_admin=is_admin)
    active = None
    if preferred_merchant_id is not None:
        active = next((m for m in merchants if int(m["merchant_id"]) == int(preferred_merchant_id)), None)
    if not active and merchants:
        active = merchants[0]
    if not active:
        return {
            **user,
            "id": actor_id,
            "actor_id": actor_id,
            "merchant_id": 1,
            "merchant_name": "Homestead",
            "role": "owner",
            "merchants": [],
        }
    return {
        **user,
        "id": int(active["owner_user_id"]),
        "actor_id": actor_id,
        "merchant_id": int(active["merchant_id"]),
        "merchant_name": active["merchant_name"],
        "role": active["role"],
        "merchants": merchants,
    }


def add_homestead_merchant_member(merchant_id: int, user_id: int, role: str = "manager") -> None:
    try:
        _shared_add_member(merchant_id, user_id, role)
    except ValueError as exc:
        raise ValueError(f"Invalid role: {role}") from exc


def remove_homestead_merchant_member(merchant_id: int, user_id: int) -> None:
    try:
        _shared_remove_member(merchant_id, user_id)
    except ValueError as exc:
        raise ValueError("Cannot remove the last owner of the farm workspace.") from exc


def update_homestead_merchant_member_role(
    merchant_id: int, user_id: int, role: str
) -> None:
    try:
        _shared_update_member_role(
            merchant_id, user_id, role, allow_zero_owners=False
        )
    except ValueError as exc:
        if "last owner" in str(exc).lower():
            raise ValueError(
                "Cannot demote the last owner of the farm workspace."
            ) from exc
        raise


def rename_homestead_merchant(merchant_id: int, new_name: str) -> None:
    try:
        _shared_rename_merchant(merchant_id, new_name)
    except ValueError as exc:
        raise ValueError("Farm workspace name cannot be empty.") from exc


def create_homestead_merchant(name: str, owner_user_id: int) -> int:
    try:
        return _shared_create_merchant(name, owner_user_id)
    except ValueError as exc:
        raise ValueError("Farm workspace name cannot be empty.") from exc
