"""Iron Ledger permissions, merchant queries, and re-exported shared workspace helpers."""

from __future__ import annotations

from inventory.db import get_connection
from shared.auth import set_user_admin
from shared.merchants import (
    MANAGER,
    OWNER,
    ROLES,
    STAFF,
    add_member,
    clean_payment_handle,
    count_merchants,
    create_invite,
    create_merchant,
    delete_invite,
    delete_merchant,
    get_invite,
    get_membership,
    get_payment_handles,
    list_all_users_with_memberships,
    list_invites,
    list_members,
    list_user_merchants,
    redeem_invite,
    remove_member,
    rename_merchant,
    update_payment_handles,
)

__all__ = [
    "HOME_PAGE",
    "MANAGER",
    "OWNER",
    "PAGE_ACCESS",
    "ROLES",
    "STAFF",
    "add_member",
    "can_access",
    "clean_payment_handle",
    "count_merchants",
    "create_invite",
    "create_merchant",
    "delete_invite",
    "delete_merchant",
    "get_invite",
    "get_membership",
    "get_payment_handles",
    "home_page",
    "list_all_merchants",
    "list_all_users_with_memberships",
    "list_invites",
    "list_members",
    "list_user_merchants",
    "redeem_invite",
    "remove_member",
    "rename_merchant",
    "set_user_admin",
    "update_payment_handles",
]

# Pages each role may open. Staff see Quick sale and Market day.
PAGE_ACCESS = {
    OWNER: {"inventory", "sales", "insights", "market_day", "quick_sale", "settings"},
    MANAGER: {"inventory", "sales", "insights", "market_day", "quick_sale"},
    STAFF: {"market_day", "quick_sale"},
}
HOME_PAGE = {OWNER: "inventory", MANAGER: "inventory", STAFF: "quick_sale"}


def can_access(role: str, page: str) -> bool:
    return page in PAGE_ACCESS.get(role, set())


def home_page(role: str, preferred: str | None = None) -> str:
    """Landing page for a role. A preferred page is honoured if permitted."""
    if preferred and can_access(role, preferred):
        return preferred
    return HOME_PAGE[role]


def list_all_merchants() -> list[dict]:
    """List every merchant on the platform with member/SKU counts and Zettle sync status."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT
                mr.id,
                mr.name,
                mr.created_at,
                (SELECT COUNT(*) FROM merchant_members m WHERE m.merchant_id = mr.id) AS member_count,
                (SELECT COUNT(*) FROM skus s WHERE s.merchant_id = mr.id AND s.active = 1) AS sku_count,
                (SELECT COUNT(*) FROM sales sl WHERE sl.merchant_id = mr.id) AS sale_count,
                EXISTS(SELECT 1 FROM zettle_connections z WHERE z.merchant_id = mr.id) AS zettle_connected,
                (SELECT value FROM sync_state ss WHERE ss.merchant_id = mr.id AND ss.key = 'last_sync_at') AS last_sync_at,
                (SELECT value FROM sync_state ss WHERE ss.merchant_id = mr.id AND ss.key = 'last_error') AS last_error,
                COALESCE(ms.status, 'comp') AS billing_status,
                COALESCE(ms.plan_key, 'lifetime') AS plan_key,
                ms.stripe_customer_id,
                ms.current_period_end,
                ms.grace_until
            FROM merchants mr
            LEFT JOIN merchant_subscriptions ms ON ms.merchant_id = mr.id
            ORDER BY mr.id ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
