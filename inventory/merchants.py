"""Merchants, membership roles and role-based permissions."""

from datetime import datetime

from inventory.db import get_connection

OWNER, MANAGER, STAFF = "owner", "manager", "staff"
ROLES = (OWNER, MANAGER, STAFF)

# Pages each role may open. Staff only ever see Quick sale.
PAGE_ACCESS = {
    OWNER: {"inventory", "sales", "quick_sale", "settings"},
    MANAGER: {"inventory", "sales", "quick_sale"},
    STAFF: {"quick_sale"},
}
HOME_PAGE = {OWNER: "inventory", MANAGER: "inventory", STAFF: "quick_sale"}


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def can_access(role: str, page: str) -> bool:
    return page in PAGE_ACCESS.get(role, set())


def home_page(role: str, preferred: str | None = None) -> str:
    """Landing page for a role. A preferred page is honoured if permitted."""
    if preferred and can_access(role, preferred):
        return preferred
    return HOME_PAGE[role]


def count_merchants() -> int:
    conn = get_connection()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM merchants").fetchone()[0])
    finally:
        conn.close()


def create_merchant(name: str, owner_user_id: int) -> int:
    clean = str(name).strip()
    if not clean:
        raise ValueError("Merchant name cannot be empty.")
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO merchants (name, created_at) VALUES (?, ?)",
            (clean, _now()),
        )
        merchant_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO merchant_members (merchant_id, user_id, role, created_at)"
            " VALUES (?, ?, ?, ?)",
            (merchant_id, int(owner_user_id), OWNER, _now()),
        )
        conn.commit()
        return merchant_id
    finally:
        conn.close()


def add_member(merchant_id: int, user_id: int, role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"Unknown role: {role}")
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO merchant_members (merchant_id, user_id, role, created_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(merchant_id, user_id) DO UPDATE SET role = excluded.role",
            (int(merchant_id), int(user_id), role, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def remove_member(merchant_id: int, user_id: int) -> None:
    """Remove a member. The last owner cannot be removed."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT role FROM merchant_members WHERE merchant_id = ? AND user_id = ?",
            (int(merchant_id), int(user_id)),
        ).fetchone()
        if not row:
            return
        if row["role"] == OWNER:
            owners = conn.execute(
                "SELECT COUNT(*) FROM merchant_members"
                " WHERE merchant_id = ? AND role = ?",
                (int(merchant_id), OWNER),
            ).fetchone()[0]
            if owners <= 1:
                raise ValueError("Cannot remove the last owner.")
        conn.execute(
            "DELETE FROM merchant_members WHERE merchant_id = ? AND user_id = ?",
            (int(merchant_id), int(user_id)),
        )
        conn.commit()
    finally:
        conn.close()


def get_membership(user_id: int, merchant_id: int | None = None) -> dict | None:
    """Return the user's membership (first one if merchant_id is omitted)."""
    conn = get_connection()
    try:
        sql = (
            "SELECT m.merchant_id, m.role, mr.name AS merchant_name"
            " FROM merchant_members m JOIN merchants mr ON mr.id = m.merchant_id"
            " WHERE m.user_id = ?"
        )
        args: list = [int(user_id)]
        if merchant_id is not None:
            sql += " AND m.merchant_id = ?"
            args.append(int(merchant_id))
        row = conn.execute(sql + " ORDER BY m.merchant_id LIMIT 1", args).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_members(merchant_id: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT u.id AS user_id, u.username, u.display_name, m.role"
            " FROM merchant_members m JOIN users u ON u.id = m.user_id"
            " WHERE m.merchant_id = ? ORDER BY m.created_at, u.id",
            (int(merchant_id),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
