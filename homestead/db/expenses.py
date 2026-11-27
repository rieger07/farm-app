"""Farm expenses and Google Drive OAuth receipt-upload connection records."""

from datetime import datetime

from homestead.db.core import get_db_connection


EXPENSE_CATEGORIES = (
    "Supplies & Equipment",
    "Feed",
    "Livestock / Chicks",
    "Processing",
    "Repairs & Maintenance",
    "Utilities & Fuel",
    "Market / Selling Fees",
    "Other",
)

SCHEDULE_F_LINE_MAP = {
    "Feed": ("Line 16", "Line 16 · Feed", 10),
    "Processing": ("Line 15", "Line 15 · Custom Hire / Processing", 20),
    "Supplies & Equipment": ("Line 28", "Line 28 · Supplies", 30),
    "Repairs & Maintenance": ("Line 25", "Line 25 · Repairs & Maintenance", 40),
    "Utilities & Fuel": ("Line 30", "Line 30 · Utilities & Fuel", 50),
    "Market / Selling Fees": ("Line 32", "Line 32 · Market Booth & Travel Fees", 60),
    "Livestock / Chicks": ("Part I / 1b", "Part I Line 1b · Livestock / Chicks (CPA check)", 70),
    "Other": ("Line 32", "Line 32 · Other Farm Expenses", 80),
}


def schedule_f_line_for(category: str) -> tuple[str, str, int]:
    """Return (line_code, label, sort_order) on IRS Schedule F for an expense category."""
    return SCHEDULE_F_LINE_MAP.get(category, SCHEDULE_F_LINE_MAP["Other"])


def get_google_drive_connection(user_id: int) -> dict | None:
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM google_drive_tokens WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
        return dict(row) if row else None


def save_google_drive_connection(
    user_id: int,
    google_email: str,
    access_token_enc: str,
    refresh_token_enc: str,
    token_expires_at: str,
    folder_id: str = "",
    folder_name: str = "Homestead Planner Receipts",
) -> None:
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO google_drive_tokens (
                user_id, google_email, access_token_enc, refresh_token_enc,
                token_expires_at, folder_id, folder_name, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                google_email = COALESCE(NULLIF(excluded.google_email, ''), google_drive_tokens.google_email),
                access_token_enc = excluded.access_token_enc,
                refresh_token_enc = COALESCE(NULLIF(excluded.refresh_token_enc, ''), google_drive_tokens.refresh_token_enc),
                token_expires_at = excluded.token_expires_at,
                folder_id = COALESCE(NULLIF(excluded.folder_id, ''), google_drive_tokens.folder_id),
                folder_name = COALESCE(NULLIF(excluded.folder_name, ''), google_drive_tokens.folder_name),
                updated_at = excluded.updated_at
            """,
            (
                int(user_id),
                str(google_email or "").strip(),
                str(access_token_enc or ""),
                str(refresh_token_enc or ""),
                str(token_expires_at or ""),
                str(folder_id or ""),
                str(folder_name or "Homestead Planner Receipts").strip() or "Homestead Planner Receipts",
                now_iso,
            ),
        )
        conn.commit()


def update_google_drive_tokens(
    user_id: int,
    access_token_enc: str,
    token_expires_at: str,
) -> None:
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE google_drive_tokens
            SET access_token_enc = ?, token_expires_at = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (
                str(access_token_enc or ""),
                str(token_expires_at or ""),
                datetime.utcnow().isoformat(timespec="seconds"),
                int(user_id),
            ),
        )
        conn.commit()


def update_google_drive_folder(user_id: int, folder_name: str) -> None:
    clean = str(folder_name or "").strip() or "Homestead Planner Receipts"
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE google_drive_tokens
            SET folder_name = ?, folder_id = '', updated_at = ?
            WHERE user_id = ?
            """,
            (clean, datetime.utcnow().isoformat(timespec="seconds"), int(user_id)),
        )
        conn.commit()


def disconnect_google_drive(user_id: int) -> None:
    with get_db_connection() as conn:
        conn.execute("DELETE FROM google_drive_tokens WHERE user_id = ?", (int(user_id),))
        conn.commit()


def list_farm_expenses(user_id: int) -> list[dict]:
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT e.*, b.name AS batch_name
            FROM farm_expenses e
            LEFT JOIN batches b ON b.id = e.batch_id
            WHERE e.user_id = ?
            ORDER BY e.expense_date DESC, e.id DESC
            """,
            (int(user_id),),
        ).fetchall()
        return [dict(r) for r in rows]


def add_farm_expense(
    user_id: int,
    expense_date: str,
    vendor: str,
    category: str,
    amount: float,
    batch_id: int | None = None,
    notes: str = "",
    receipt_url: str = "",
    drive_file_id: str = "",
    receipt_group_id: str = "",
) -> int:
    cat = str(category or "Other").strip() or "Other"
    with get_db_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO farm_expenses (
                user_id, batch_id, expense_date, vendor, category, amount,
                notes, receipt_url, drive_file_id, receipt_group_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(user_id),
                int(batch_id) if batch_id else None,
                str(expense_date),
                str(vendor or "").strip(),
                cat,
                max(0.0, float(amount)),
                str(notes or "").strip(),
                str(receipt_url or "").strip(),
                str(drive_file_id or "").strip(),
                str(receipt_group_id or "").strip(),
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def attach_farm_expense_receipt(
    expense_id: int,
    user_id: int,
    receipt_url: str,
    drive_file_id: str = "",
) -> bool:
    clean_url = str(receipt_url or "").strip()
    if not clean_url:
        return False
    with get_db_connection() as conn:
        cur = conn.execute(
            """
            UPDATE farm_expenses
            SET receipt_url = ?,
                drive_file_id = COALESCE(NULLIF(?, ''), drive_file_id)
            WHERE id = ? AND user_id = ?
            """,
            (clean_url, str(drive_file_id or "").strip(), int(expense_id), int(user_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_farm_expense(expense_id: int, user_id: int) -> None:
    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM farm_expenses WHERE id = ? AND user_id = ?",
            (int(expense_id), int(user_id)),
        )
        conn.commit()


def delete_receipt_group(user_id: int, receipt_group_id: str) -> int:
    """Delete all lines sharing a split receipt_group_id across general, batch feed, and layer feed."""
    gid = str(receipt_group_id or "").strip()
    if not gid:
        return 0
    with get_db_connection() as conn:
        deleted = 0
        c1 = conn.execute(
            "DELETE FROM farm_expenses WHERE user_id = ? AND receipt_group_id = ?",
            (int(user_id), gid),
        )
        deleted += int(c1.rowcount or 0)
        c2 = conn.execute(
            "DELETE FROM layer_feed_logs WHERE user_id = ? AND receipt_group_id = ?",
            (int(user_id), gid),
        )
        deleted += int(c2.rowcount or 0)
        c3 = conn.execute(
            """
            DELETE FROM batch_feed_logs
            WHERE receipt_group_id = ?
              AND batch_id IN (SELECT id FROM batches WHERE user_id = ?)
            """,
            (gid, int(user_id)),
        )
        deleted += int(c3.rowcount or 0)
        conn.commit()
        return deleted


def list_recent_vendors(user_id: int, limit: int = 10) -> list[str]:
    """Return distinct recent vendor names topped up with common farm defaults."""
    defaults = ("Tractor Supply", "Runnings", "Local Feed Mill", "Butcher")
    seen_low: set[str] = set()
    vendors: list[str] = []
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT vendor, MAX(d) AS last_d FROM (
                SELECT vendor, expense_date AS d FROM farm_expenses WHERE user_id = ? AND vendor != ''
                UNION ALL
                SELECT fl.vendor, fl.feed_date AS d
                FROM batch_feed_logs fl
                JOIN batches b ON b.id = fl.batch_id
                WHERE b.user_id = ? AND COALESCE(fl.vendor, '') != ''
                UNION ALL
                SELECT vendor, feed_date AS d FROM layer_feed_logs WHERE user_id = ? AND COALESCE(vendor, '') != ''
            )
            GROUP BY vendor COLLATE NOCASE
            ORDER BY last_d DESC, vendor ASC
            LIMIT ?
            """,
            (int(user_id), int(user_id), int(user_id), int(limit)),
        ).fetchall()
    for r in rows:
        v = str(r["vendor"] or "").strip()
        if v and v.lower() not in seen_low:
            seen_low.add(v.lower())
            vendors.append(v)
    for d in defaults:
        if len(vendors) >= limit:
            break
        if d.lower() not in seen_low:
            seen_low.add(d.lower())
            vendors.append(d)
    return vendors[:limit]
