"""Farm expenses and Google Drive OAuth receipt-upload connection records."""

from datetime import datetime
import secrets

from homestead.db.core import get_db_connection
from homestead.db.eggs import normalize_feed_species_tags
from homestead.db.herds import (
    normalize_herd_feed_category,
    normalize_herd_feed_unit,
)


EXPENSE_CATEGORIES = (
    "Supplies & Equipment",
    "Capital Equipment / Depreciation",
    "Feed",
    "Livestock / Chicks",
    "Processing",
    "Repairs & Maintenance",
    "Utilities & Fuel",
    "Market / Selling Fees",
    "Other",
)

EXPENSE_CATEGORY_OPTIONS = (
    ("Supplies & Equipment", "Supplies (Line 28)"),
    ("Capital Equipment / Depreciation", "Capital Equipment / Depreciation (Line 14 · CPA check)"),
    ("Feed", "Feed (Line 16)"),
    ("Livestock / Chicks", "Livestock / Chicks (Part I Line 1b · CPA check)"),
    ("Processing", "Processing / Custom Hire (Line 15)"),
    ("Repairs & Maintenance", "Repairs & Maintenance (Line 25)"),
    ("Utilities & Fuel", "Utilities & Fuel (Line 30)"),
    ("Market / Selling Fees", "Market / Selling Fees (Line 32)"),
    ("Other", "Other Farm Expenses (Line 32)"),
)

SCHEDULE_F_LINE_MAP = {
    "Feed": ("Line 16", "Line 16 · Feed", 10),
    "Processing": ("Line 15", "Line 15 · Custom Hire / Processing", 20),
    "Supplies & Equipment": ("Line 28", "Line 28 · Supplies", 30),
    "Capital Equipment / Depreciation": ("Line 14", "Line 14 · Capital Equipment / Depreciation (CPA check)", 35),
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


def list_batch_expenses(user_id: int, batch_id: int) -> list[dict]:
    """List Financials receipts (`farm_expenses`) tagged to a specific batch."""
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM farm_expenses
            WHERE user_id = ? AND batch_id = ?
            ORDER BY expense_date DESC, id DESC
            """,
            (int(user_id), int(batch_id)),
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
                round(float(amount), 2),
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
        c4 = conn.execute(
            "DELETE FROM herd_feed_logs WHERE user_id = ? AND receipt_group_id = ?",
            (int(user_id), gid),
        )
        deleted += int(c4.rowcount or 0)
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
                UNION ALL
                SELECT vendor, feed_date AS d FROM herd_feed_logs WHERE user_id = ? AND COALESCE(vendor, '') != ''
            )
            GROUP BY vendor COLLATE NOCASE
            ORDER BY last_d DESC, vendor ASC
            LIMIT ?
            """,
            (int(user_id), int(user_id), int(user_id), int(user_id), int(limit)),
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


def get_receipt_for_edit(
    user_id: int,
    *,
    receipt_group_id: str = "",
    source_key: str = "",
    entry_id: int | None = None,
) -> dict | None:
    """Load an existing single entry or split receipt group into modal-ready form lines."""
    uid = int(user_id)
    gid = str(receipt_group_id or "").strip()
    key = str(source_key or "").strip()

    lines: list[dict] = []
    meta_dates: list[str] = []
    meta_vendors: list[str] = []
    meta_urls: list[str] = []
    meta_drives: list[str] = []

    def _collect_meta(d_val, v_val, u_val, df_val) -> None:
        if d_val:
            meta_dates.append(str(d_val)[:10])
        if v_val:
            meta_vendors.append(str(v_val).strip())
        if u_val:
            meta_urls.append(str(u_val).strip())
        if df_val:
            meta_drives.append(str(df_val).strip())

    def _append_gen(r) -> None:
        _collect_meta(r["expense_date"], r["vendor"], r["receipt_url"], r["drive_file_id"])
        lines.append(
            {
                "kind": "general",
                "category": str(r["category"] or "Other"),
                "batch_id": int(r["batch_id"]) if r["batch_id"] is not None else "",
                "amount": round(float(r["amount"] or 0.0), 2),
                "notes": str(r["notes"] or ""),
            }
        )

    def _append_batch_feed(r) -> None:
        _collect_meta(r["feed_date"], r["vendor"], r["receipt_url"], r["drive_file_id"])
        lines.append(
            {
                "kind": "batch_feed",
                "batch_id": int(r["batch_id"]),
                "feed_type": str(r["feed_type"] or "grower"),
                "bags": round(float(r["bags"] or 0.0), 2),
                "feed_lbs": round(float(r["feed_lbs"] or 0.0), 1),
                "amount": round(float(r["cost_total"] or 0.0), 2),
                "notes": str(r["notes"] or ""),
            }
        )

    def _append_layer_feed(r) -> None:
        _collect_meta(r["feed_date"], r["vendor"], r["receipt_url"], r["drive_file_id"])
        tags = [t for t in normalize_feed_species_tags(r["species_tags"]).split(",") if t]
        lines.append(
            {
                "kind": "layer_feed",
                "feed_type": str(r["feed_type"] or "layer"),
                "species_tags": tags or ["all"],
                "bags": round(float(r["bags"] or 0.0), 2),
                "feed_lbs": round(float(r["feed_lbs"] or 0.0), 1),
                "amount": round(float(r["cost_total"] or 0.0), 2),
                "notes": str(r["notes"] or ""),
            }
        )

    def _append_herd_feed(r) -> None:
        _collect_meta(r["feed_date"], r["vendor"], r["receipt_url"], r["drive_file_id"])
        lines.append(
            {
                "kind": "herd_feed",
                "herd_id": int(r["herd_id"]),
                "feed_category": str(r["feed_category"] or "grain"),
                "unit": str(r["unit"] or "bags"),
                "quantity": round(float(r["quantity"] or 0.0), 2),
                "weight_lbs": round(float(r["weight_lbs"] or 0.0), 1),
                "amount": round(float(r["cost_total"] or 0.0), 2),
                "notes": str(r["notes"] or ""),
            }
        )

    with get_db_connection() as conn:
        if gid:
            edit_mode = "group"
            for r in conn.execute(
                """
                SELECT id, batch_id, expense_date, vendor, category, amount, notes, receipt_url, drive_file_id
                FROM farm_expenses
                WHERE user_id = ? AND receipt_group_id = ?
                ORDER BY id ASC
                """,
                (uid, gid),
            ).fetchall():
                _append_gen(r)
            for r in conn.execute(
                """
                SELECT fl.id, fl.batch_id, fl.feed_date, fl.feed_type, fl.bags, fl.feed_lbs,
                       fl.cost_total, fl.notes, fl.receipt_url, fl.drive_file_id, fl.vendor
                FROM batch_feed_logs fl
                JOIN batches b ON b.id = fl.batch_id
                WHERE b.user_id = ? AND fl.receipt_group_id = ?
                ORDER BY fl.id ASC
                """,
                (uid, gid),
            ).fetchall():
                _append_batch_feed(r)
            for r in conn.execute(
                """
                SELECT id, feed_date, feed_type, species_tags, bags, feed_lbs,
                       cost_total, notes, receipt_url, drive_file_id, vendor
                FROM layer_feed_logs
                WHERE user_id = ? AND receipt_group_id = ?
                ORDER BY id ASC
                """,
                (uid, gid),
            ).fetchall():
                _append_layer_feed(r)
            for r in conn.execute(
                """
                SELECT id, herd_id, feed_date, feed_category, unit, quantity, weight_lbs,
                       cost_total, notes, receipt_url, drive_file_id, vendor
                FROM herd_feed_logs
                WHERE user_id = ? AND receipt_group_id = ?
                ORDER BY id ASC
                """,
                (uid, gid),
            ).fetchall():
                _append_herd_feed(r)
        elif key in ("general", "batch_feed", "layer_feed", "herd_feed") and entry_id is not None and int(entry_id) > 0:
            edit_mode = "single"
            eid = int(entry_id)
            if key == "general":
                r = conn.execute(
                    """
                    SELECT id, batch_id, expense_date, vendor, category, amount, notes, receipt_url, drive_file_id
                    FROM farm_expenses
                    WHERE id = ? AND user_id = ?
                    """,
                    (eid, uid),
                ).fetchone()
                if r:
                    _append_gen(r)
            elif key == "batch_feed":
                r = conn.execute(
                    """
                    SELECT fl.id, fl.batch_id, fl.feed_date, fl.feed_type, fl.bags, fl.feed_lbs,
                           fl.cost_total, fl.notes, fl.receipt_url, fl.drive_file_id, fl.vendor
                    FROM batch_feed_logs fl
                    JOIN batches b ON b.id = fl.batch_id
                    WHERE fl.id = ? AND b.user_id = ?
                    """,
                    (eid, uid),
                ).fetchone()
                if r:
                    _append_batch_feed(r)
            elif key == "layer_feed":
                r = conn.execute(
                    """
                    SELECT id, feed_date, feed_type, species_tags, bags, feed_lbs,
                           cost_total, notes, receipt_url, drive_file_id, vendor
                    FROM layer_feed_logs
                    WHERE id = ? AND user_id = ?
                    """,
                    (eid, uid),
                ).fetchone()
                if r:
                    _append_layer_feed(r)
            elif key == "herd_feed":
                r = conn.execute(
                    """
                    SELECT id, herd_id, feed_date, feed_category, unit, quantity, weight_lbs,
                           cost_total, notes, receipt_url, drive_file_id, vendor
                    FROM herd_feed_logs
                    WHERE id = ? AND user_id = ?
                    """,
                    (eid, uid),
                ).fetchone()
                if r:
                    _append_herd_feed(r)
        else:
            return None

    if not lines:
        return None

    return {
        "edit_mode": edit_mode,
        "receipt_group_id": gid,
        "source_key": key if edit_mode == "single" else "",
        "entry_id": int(entry_id) if edit_mode == "single" and entry_id is not None else None,
        "receipt_date": meta_dates[0] if meta_dates else "",
        "vendor": meta_vendors[0] if meta_vendors else "",
        "receipt_url": meta_urls[0] if meta_urls else "",
        "drive_file_id": meta_drives[0] if meta_drives else "",
        "total_amount": round(sum(float(item["amount"]) for item in lines), 2),
        "lines": lines,
    }


def replace_receipt_entries(
    user_id: int,
    *,
    edit_mode: str,
    orig_group_id: str,
    orig_source_key: str,
    orig_entry_id: int | None,
    receipt_date: str,
    vendor: str,
    receipt_url: str,
    drive_file_id: str,
    expanded_lines: list[dict],
) -> tuple[int, float]:
    """Atomically replace an existing receipt entry or group with expanded lines."""
    uid = int(user_id)
    if not expanded_lines:
        raise ValueError("Add at least one line item with a non-zero amount.")

    clean_vendor = str(vendor or "").strip()
    clean_url = str(receipt_url or "").strip()
    clean_drive = str(drive_file_id or "").strip()
    rdate = str(receipt_date or "").strip()[:10]
    now_iso = datetime.utcnow().isoformat(timespec="seconds")

    with get_db_connection() as conn:
        if edit_mode == "group":
            gid = str(orig_group_id or "").strip()
            if not gid:
                raise ValueError("Original receipt entry not found.")
            deleted = 0
            deleted += int(
                conn.execute(
                    "DELETE FROM farm_expenses WHERE user_id = ? AND receipt_group_id = ?",
                    (uid, gid),
                ).rowcount
                or 0
            )
            deleted += int(
                conn.execute(
                    "DELETE FROM layer_feed_logs WHERE user_id = ? AND receipt_group_id = ?",
                    (uid, gid),
                ).rowcount
                or 0
            )
            deleted += int(
                conn.execute(
                    """
                    DELETE FROM batch_feed_logs
                    WHERE receipt_group_id = ?
                      AND batch_id IN (SELECT id FROM batches WHERE user_id = ?)
                    """,
                    (gid, uid),
                ).rowcount
                or 0
            )
            deleted += int(
                conn.execute(
                    "DELETE FROM herd_feed_logs WHERE user_id = ? AND receipt_group_id = ?",
                    (uid, gid),
                ).rowcount
                or 0
            )
            if deleted == 0:
                raise ValueError("Original receipt entry not found.")
            new_group_id = gid if len(expanded_lines) > 1 else ""
        elif edit_mode == "single":
            key = str(orig_source_key or "").strip()
            if not orig_entry_id or int(orig_entry_id) <= 0:
                raise ValueError("Original receipt entry not found.")
            eid = int(orig_entry_id)
            if key == "general":
                cur = conn.execute(
                    "DELETE FROM farm_expenses WHERE id = ? AND user_id = ?",
                    (eid, uid),
                )
            elif key == "batch_feed":
                cur = conn.execute(
                    """
                    DELETE FROM batch_feed_logs
                    WHERE id = ?
                      AND batch_id IN (SELECT id FROM batches WHERE user_id = ?)
                    """,
                    (eid, uid),
                )
            elif key == "layer_feed":
                cur = conn.execute(
                    "DELETE FROM layer_feed_logs WHERE id = ? AND user_id = ?",
                    (eid, uid),
                )
            elif key == "herd_feed":
                cur = conn.execute(
                    "DELETE FROM herd_feed_logs WHERE id = ? AND user_id = ?",
                    (eid, uid),
                )
            else:
                raise ValueError("Original receipt entry not found.")
            if int(cur.rowcount or 0) == 0:
                raise ValueError("Original receipt entry not found.")
            new_group_id = secrets.token_hex(6) if len(expanded_lines) > 1 else ""
        else:
            raise ValueError("Invalid receipt edit mode.")

        for line in expanded_lines:
            kind = line["kind"]
            notes = str(line.get("notes") or "").strip()
            if kind == "batch_feed":
                ft = "starter" if str(line.get("feed_type") or "").strip().lower() == "starter" else "grower"
                conn.execute(
                    """
                    INSERT INTO batch_feed_logs (
                        batch_id, feed_date, feed_type, bags, feed_lbs, cost_total,
                        notes, receipt_url, drive_file_id, vendor, receipt_group_id, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(line["batch_id"]),
                        rdate,
                        ft,
                        round(float(line.get("bags") or 0.0), 2),
                        round(float(line.get("feed_lbs") or 0.0), 2),
                        round(float(line["amount"]), 2),
                        notes,
                        clean_url,
                        clean_drive,
                        clean_vendor,
                        new_group_id,
                        now_iso,
                    ),
                )
            elif kind == "layer_feed":
                ft_raw = str(line.get("feed_type") or "layer").strip().lower()
                ftype = ft_raw if ft_raw in ("layer", "scratch", "supplement") else "layer"
                norm_tags = normalize_feed_species_tags(line.get("species_tags") or "all")
                conn.execute(
                    """
                    INSERT INTO layer_feed_logs (
                        user_id, feed_date, feed_type, species_tags, bags, feed_lbs, cost_total,
                        notes, receipt_url, drive_file_id, vendor, receipt_group_id, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uid,
                        rdate,
                        ftype,
                        norm_tags,
                        round(float(line.get("bags") or 0.0), 2),
                        round(float(line.get("feed_lbs") or 0.0), 2),
                        round(float(line["amount"]), 2),
                        notes,
                        clean_url,
                        clean_drive,
                        clean_vendor,
                        new_group_id,
                        now_iso,
                    ),
                )
            elif kind == "herd_feed":
                fcat = normalize_herd_feed_category(line.get("feed_category"))
                funit = normalize_herd_feed_unit(line.get("unit"), fcat)
                conn.execute(
                    """
                    INSERT INTO herd_feed_logs (
                        user_id, herd_id, feed_date, feed_category, quantity, unit,
                        weight_lbs, cost_total, vendor, notes, receipt_url,
                        drive_file_id, receipt_group_id, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uid,
                        int(line["herd_id"]),
                        rdate,
                        fcat,
                        round(float(line.get("quantity") or 0.0), 2),
                        funit,
                        round(float(line.get("weight_lbs") or 0.0), 1),
                        round(float(line["amount"]), 2),
                        clean_vendor,
                        notes,
                        clean_url,
                        clean_drive,
                        new_group_id,
                        now_iso,
                    ),
                )
            else:
                cat = str(line.get("category") or "Other").strip() or "Other"
                bid = int(line["batch_id"]) if line.get("batch_id") else None
                conn.execute(
                    """
                    INSERT INTO farm_expenses (
                        user_id, batch_id, expense_date, vendor, category, amount,
                        notes, receipt_url, drive_file_id, receipt_group_id, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uid,
                        bid,
                        rdate,
                        clean_vendor,
                        cat,
                        round(float(line["amount"]), 2),
                        notes,
                        clean_url,
                        clean_drive,
                        new_group_id,
                        now_iso,
                    ),
                )
        conn.commit()

    total_amt = round(sum(float(item["amount"]) for item in expanded_lines), 2)
    return len(expanded_lines), total_amt
