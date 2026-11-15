"""Merchants, membership roles, invite tokens, and role-based permissions."""

from datetime import datetime, timedelta
import secrets

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


def create_merchant(name: str, owner_user_id: int | None = None) -> int:
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
        if owner_user_id is not None:
            conn.execute(
                "INSERT INTO merchant_members (merchant_id, user_id, role, created_at)"
                " VALUES (?, ?, ?, ?)",
                (merchant_id, int(owner_user_id), OWNER, _now()),
            )
        conn.commit()
        return merchant_id
    finally:
        conn.close()


def rename_merchant(merchant_id: int, new_name: str) -> None:
    clean = str(new_name).strip()
    if not clean:
        raise ValueError("Merchant name cannot be empty.")
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE merchants SET name = ? WHERE id = ?",
            (clean, int(merchant_id)),
        )
        conn.commit()
    finally:
        conn.close()


def delete_merchant(merchant_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM merchants WHERE id = ?", (int(merchant_id),))
        conn.commit()
    finally:
        conn.close()


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
                (SELECT value FROM sync_state ss WHERE ss.merchant_id = mr.id AND ss.key = 'last_error') AS last_error
            FROM merchants mr
            ORDER BY mr.id ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_user_merchants(user_id: int, is_admin: bool = False) -> list[dict]:
    """Return merchants available in the workspace switcher for a user."""
    conn = get_connection()
    try:
        if is_admin:
            rows = conn.execute(
                """
                SELECT mr.id AS merchant_id, mr.name AS merchant_name,
                       COALESCE(m.role, 'owner') AS role
                FROM merchants mr
                LEFT JOIN merchant_members m
                  ON m.merchant_id = mr.id AND m.user_id = ?
                ORDER BY mr.id ASC
                """,
                (int(user_id),),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT m.merchant_id, mr.name AS merchant_name, m.role
                FROM merchant_members m
                JOIN merchants mr ON mr.id = m.merchant_id
                WHERE m.user_id = ?
                ORDER BY m.merchant_id ASC
                """,
                (int(user_id),),
            ).fetchall()
        return [dict(r) for r in rows]
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


def remove_member(merchant_id: int, user_id: int, allow_zero_owners: bool = False) -> None:
    """Remove a member. The last owner cannot be removed unless allow_zero_owners=True."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT role FROM merchant_members WHERE merchant_id = ? AND user_id = ?",
            (int(merchant_id), int(user_id)),
        ).fetchone()
        if not row:
            return
        if row["role"] == OWNER and not allow_zero_owners:
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


def get_membership(
    user_id: int, merchant_id: int | None = None, is_admin: bool = False
) -> dict | None:
    """Return the user's membership (or synthesized owner access for platform admins)."""
    conn = get_connection()
    try:
        if merchant_id is not None:
            row = conn.execute(
                "SELECT m.merchant_id, m.role, mr.name AS merchant_name"
                " FROM merchant_members m JOIN merchants mr ON mr.id = m.merchant_id"
                " WHERE m.user_id = ? AND m.merchant_id = ?",
                (int(user_id), int(merchant_id)),
            ).fetchone()
            if row:
                return dict(row)
            if is_admin:
                mr = conn.execute(
                    "SELECT id AS merchant_id, name AS merchant_name FROM merchants WHERE id = ?",
                    (int(merchant_id),),
                ).fetchone()
                if mr:
                    return {
                        "merchant_id": int(mr["merchant_id"]),
                        "merchant_name": mr["merchant_name"],
                        "role": OWNER,
                    }

        row = conn.execute(
            "SELECT m.merchant_id, m.role, mr.name AS merchant_name"
            " FROM merchant_members m JOIN merchants mr ON mr.id = m.merchant_id"
            " WHERE m.user_id = ? ORDER BY m.merchant_id LIMIT 1",
            (int(user_id),),
        ).fetchone()
        if row:
            return dict(row)
        if is_admin:
            mr = conn.execute(
                "SELECT id AS merchant_id, name AS merchant_name FROM merchants ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if mr:
                return {
                    "merchant_id": int(mr["merchant_id"]),
                    "merchant_name": mr["merchant_name"],
                    "role": OWNER,
                }
        return None
    finally:
        conn.close()


def list_members(merchant_id: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT u.id AS user_id, u.username, u.display_name, u.is_admin, m.role"
            " FROM merchant_members m JOIN users u ON u.id = m.user_id"
            " WHERE m.merchant_id = ? ORDER BY m.created_at, u.id",
            (int(merchant_id),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_all_users_with_memberships() -> list[dict]:
    """List every user in the shared DB along with their merchant memberships."""
    conn = get_connection()
    try:
        users = conn.execute(
            "SELECT id, username, display_name, is_admin, created_at FROM users ORDER BY id ASC"
        ).fetchall()
        members = conn.execute(
            """
            SELECT m.user_id, m.merchant_id, m.role, mr.name AS merchant_name
            FROM merchant_members m
            JOIN merchants mr ON mr.id = m.merchant_id
            ORDER BY m.merchant_id ASC
            """
        ).fetchall()
        by_user: dict[int, list[dict]] = {}
        for m in members:
            by_user.setdefault(int(m["user_id"]), []).append(dict(m))
        out = []
        for u in users:
            d = dict(u)
            d["is_admin"] = bool(d["is_admin"])
            d["memberships"] = by_user.get(int(d["id"]), [])
            out.append(d)
        return out
    finally:
        conn.close()


def set_user_admin(user_id: int, is_admin: bool) -> None:
    conn = get_connection()
    try:
        if not is_admin:
            admins = conn.execute(
                "SELECT COUNT(*) FROM users WHERE is_admin = 1"
            ).fetchone()[0]
            target = conn.execute(
                "SELECT is_admin FROM users WHERE id = ?", (int(user_id),)
            ).fetchone()
            if target and target["is_admin"] and admins <= 1:
                raise ValueError("Cannot remove admin privileges from the last Super Admin.")
        conn.execute(
            "UPDATE users SET is_admin = ? WHERE id = ?",
            (1 if is_admin else 0, int(user_id)),
        )
        conn.commit()
    finally:
        conn.close()


# ---- One-time onboarding invites ------------------------------------------

def create_invite(
    merchant_id: int | None = None,
    role: str = OWNER,
    default_merchant_name: str = "",
    note: str = "",
    created_by: int | None = None,
    days_valid: int = 14,
) -> str:
    if role not in ROLES:
        raise ValueError(f"Unknown role: {role}")
    if merchant_id is None and role != OWNER:
        role = OWNER  # new merchant invites always create an owner
    token = secrets.token_urlsafe(20)
    now = datetime.utcnow()
    expires = now + timedelta(days=max(1, int(days_valid)))
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO merchant_invites
            (token, merchant_id, default_merchant_name, role, note, created_by, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token,
                int(merchant_id) if merchant_id is not None else None,
                default_merchant_name.strip(),
                role,
                note.strip(),
                int(created_by) if created_by is not None else None,
                expires.isoformat(timespec="seconds"),
                now.isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return token
    finally:
        conn.close()


def get_invite(token: str) -> dict | None:
    if not token:
        return None
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT i.*, mr.name AS merchant_name
            FROM merchant_invites i
            LEFT JOIN merchants mr ON mr.id = i.merchant_id
            WHERE i.token = ?
            """,
            (str(token).strip(),),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        now_iso = _now()
        d["expired"] = bool(d["expires_at"] and d["expires_at"] < now_iso)
        d["redeemed"] = bool(d["used_at"])
        d["valid"] = not d["expired"] and not d["redeemed"]
        return d
    finally:
        conn.close()


def redeem_invite(
    token: str, user_id: int, merchant_name: str = ""
) -> int:
    """Redeem a valid invite for user_id. Returns the merchant_id joined or created."""
    inv = get_invite(token)
    if not inv or not inv["valid"]:
        raise ValueError("This invite link is expired or has already been used.")
    now_iso = _now()
    if inv["merchant_id"] is not None:
        mid = int(inv["merchant_id"])
        add_member(mid, int(user_id), inv["role"])
    else:
        chosen_name = (
            merchant_name.strip()
            or inv["default_merchant_name"]
            or "My Business"
        )
        mid = create_merchant(chosen_name, int(user_id))
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE merchant_invites SET used_at = ?, used_by = ? WHERE token = ?",
            (now_iso, int(user_id), str(token).strip()),
        )
        conn.commit()
    finally:
        conn.close()
    return mid


def list_invites() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT i.*, mr.name AS merchant_name, u.username AS used_by_username
            FROM merchant_invites i
            LEFT JOIN merchants mr ON mr.id = i.merchant_id
            LEFT JOIN users u ON u.id = i.used_by
            ORDER BY i.created_at DESC
            """
        ).fetchall()
        now_iso = _now()
        out = []
        for r in rows:
            d = dict(r)
            d["expired"] = bool(d["expires_at"] and d["expires_at"] < now_iso)
            d["redeemed"] = bool(d["used_at"])
            out.append(d)
        return out
    finally:
        conn.close()


def delete_invite(token: str) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM merchant_invites WHERE token = ?", (str(token).strip(),))
        conn.commit()
    finally:
        conn.close()
