"""Farm expenses and Google Drive OAuth receipt-upload connection records."""

from datetime import datetime

from homestead.db.core import get_db_connection


EXPENSE_CATEGORIES = (
    "Feed",
    "Livestock / Chicks",
    "Processing",
    "Supplies & Equipment",
    "Utilities & Fuel",
    "Repairs & Maintenance",
    "Other",
)


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
) -> int:
    cat = str(category or "Other").strip() or "Other"
    with get_db_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO farm_expenses (
                user_id, batch_id, expense_date, vendor, category, amount,
                notes, receipt_url, drive_file_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def delete_farm_expense(expense_id: int, user_id: int) -> None:
    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM farm_expenses WHERE id = ? AND user_id = ?",
            (int(expense_id), int(user_id)),
        )
        conn.commit()
