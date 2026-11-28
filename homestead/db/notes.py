"""Shared farm team notes, living SOPs, interactive checklists, and threaded comments."""

from __future__ import annotations

from datetime import datetime
import re
from urllib.parse import urlparse

from homestead.db.core import get_db_connection

NOTE_CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("sop", "SOP / Reference", "blue"),
    ("chores", "Chores & Handoff", "green"),
    ("vet", "Vet & Health", "red"),
    ("maintenance", "Maintenance", "amber"),
    ("supplies", "Feed & Supplies", "amber"),
    ("general", "General", "gray"),
)
_CATEGORY_META = {
    k: {"key": k, "label": label, "pill": pill} for k, label, pill in NOTE_CATEGORIES
}

_CHECK_LINE_RE = re.compile(r"^(\s*[-*]\s+\[)([ xX])(\]\s+)(.*)$")
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_INLINE_TOKEN_RE = re.compile(
    r"\[(?P<link_label>[^\]\n]+)\]\((?P<link_url>[^)\s]+)\)"
    r"|(?P<bare_url>https?://[^\s<>\"'\)\]]+)"
    r"|\*\*(?P<bold>[^*\n]+)\*\*"
    r"|\*(?P<italic>[^*\n]+)\*"
    r"|`(?P<code>[^`\n]+)`"
)


def normalize_note_category(raw: str | None) -> str:
    clean = str(raw or "").strip().lower()
    return clean if clean in _CATEGORY_META else "general"


def note_category_meta(raw: str | None) -> dict[str, str]:
    return dict(_CATEGORY_META[normalize_note_category(raw)])


def sanitize_note_url(raw: str | None) -> str:
    """Allow only http:// or https:// URLs without control characters."""
    clean = str(raw or "").strip()
    if not clean or "\r" in clean or "\n" in clean:
        return ""
    parsed = urlparse(clean)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return ""
    return clean


def _parse_inline(text: str) -> list[dict[str, str]]:
    """Parse safe inline Markdown tokens without emitting any raw HTML."""
    runs: list[dict[str, str]] = []
    pos = 0
    for m in _INLINE_TOKEN_RE.finditer(text):
        if m.start() > pos:
            runs.append({"type": "text", "text": text[pos : m.start()]})
        if m.group("link_url") is not None:
            href = sanitize_note_url(m.group("link_url"))
            if href:
                runs.append({"type": "link", "text": m.group("link_label"), "href": href})
            else:
                runs.append({"type": "text", "text": m.group("link_label")})
        elif m.group("bare_url") is not None:
            raw_url = m.group("bare_url")
            trimmed = raw_url.rstrip(".,;:!?")
            suffix = raw_url[len(trimmed) :]
            href = sanitize_note_url(trimmed)
            if href:
                runs.append({"type": "link", "text": trimmed, "href": href})
            else:
                runs.append({"type": "text", "text": trimmed})
            if suffix:
                runs.append({"type": "text", "text": suffix})
        elif m.group("bold") is not None:
            runs.append({"type": "bold", "text": m.group("bold")})
        elif m.group("italic") is not None:
            runs.append({"type": "italic", "text": m.group("italic")})
        elif m.group("code") is not None:
            runs.append({"type": "code", "text": m.group("code")})
        pos = m.end()
    if pos < len(text):
        runs.append({"type": "text", "text": text[pos:]})
    return runs


def parse_note_body(body: str | None) -> tuple[list[dict], int, int]:
    """Parse note Markdown into safe structured blocks plus checklist counts."""
    raw = str(body or "")
    blocks: list[dict] = []
    check_idx = 0
    check_done = 0

    for line in raw.splitlines():
        if not line.strip():
            if blocks and blocks[-1]["type"] in ("p", "ul", "checklist"):
                blocks[-1]["_closed"] = True
            continue

        hm = _HEADING_RE.match(line)
        if hm:
            blocks.append(
                {
                    "type": "heading",
                    "level": min(3, len(hm.group(1))),
                    "runs": _parse_inline(hm.group(2).strip()),
                }
            )
            continue

        cm = _CHECK_LINE_RE.match(line)
        if cm:
            checked = cm.group(2).lower() == "x"
            item = {
                "idx": check_idx,
                "checked": checked,
                "runs": _parse_inline(cm.group(4)),
            }
            check_idx += 1
            if checked:
                check_done += 1
            if blocks and blocks[-1]["type"] == "checklist" and not blocks[-1].get("_closed"):
                blocks[-1]["items"].append(item)
            else:
                blocks.append({"type": "checklist", "items": [item]})
            continue

        bm = _BULLET_RE.match(line)
        if bm:
            item_runs = _parse_inline(bm.group(1).strip())
            if blocks and blocks[-1]["type"] == "ul" and not blocks[-1].get("_closed"):
                blocks[-1]["items"].append(item_runs)
            else:
                blocks.append({"type": "ul", "items": [item_runs]})
            continue

        line_runs = _parse_inline(line)
        if blocks and blocks[-1]["type"] == "p" and not blocks[-1].get("_closed"):
            blocks[-1]["lines"].append(line_runs)
        else:
            blocks.append({"type": "p", "lines": [line_runs]})

    for blk in blocks:
        blk.pop("_closed", None)
    return blocks, check_done, check_idx


def plain_note_preview(body: str | None, max_len: int = 140) -> str:
    """Build a compact plain-text snippet for Radar / ntfy previews."""
    parts: list[str] = []
    for line in str(body or "").splitlines():
        clean = line.strip()
        if not clean:
            continue
        clean = re.sub(r"^#{1,3}\s+", "", clean)
        clean = re.sub(r"^[-*]\s+\[[ xX]\]\s+", "", clean)
        clean = re.sub(r"^[-*]\s+", "", clean)
        clean = re.sub(r"\*\*([^*]+)\*\*", r"\1", clean)
        clean = re.sub(r"\*([^*]+)\*", r"\1", clean)
        clean = re.sub(r"`([^`]+)`", r"\1", clean)
        clean = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", clean)
        parts.append(clean)
    joined = " ".join(parts).strip()
    if len(joined) <= max_len:
        return joined
    return joined[: max(1, max_len - 1)].rstrip() + "…"


def toggle_checklist_item_in_body(body: str, item_index: int) -> tuple[str, bool, int, int]:
    """Toggle a 0-based `- [ ]` / `- [x]` item inside note Markdown text."""
    lines = str(body or "").splitlines()
    trailing_nl = "\n" if str(body or "").endswith("\n") else ""
    out_lines: list[str] = []
    idx = 0
    done = 0
    found = False
    new_checked = False

    for line in lines:
        m = _CHECK_LINE_RE.match(line)
        if m:
            cur_checked = m.group(2).lower() == "x"
            if idx == item_index:
                found = True
                new_checked = not cur_checked
                mark = "x" if new_checked else " "
                out_lines.append(f"{m.group(1)}{mark}{m.group(3)}{m.group(4)}")
                if new_checked:
                    done += 1
            else:
                out_lines.append(line)
                if cur_checked:
                    done += 1
            idx += 1
        else:
            out_lines.append(line)

    if not found:
        raise ValueError("Checklist item not found.")
    return "\n".join(out_lines) + trailing_nl, new_checked, done, idx


def reset_checklist_in_body(body: str) -> tuple[str, int]:
    """Reset all checked `- [x]` items in a note back to unchecked `- [ ]`."""
    lines = str(body or "").splitlines()
    trailing_nl = "\n" if str(body or "").endswith("\n") else ""
    out_lines: list[str] = []
    reset_count = 0

    for line in lines:
        m = _CHECK_LINE_RE.match(line)
        if m and m.group(2).lower() == "x":
            out_lines.append(f"{m.group(1)} {m.group(3)}{m.group(4)}")
            reset_count += 1
        else:
            out_lines.append(line)

    return "\n".join(out_lines) + trailing_nl, reset_count


def _fmt_ts(iso_str: str | None) -> str:
    clean = str(iso_str or "").strip()
    if len(clean) >= 16:
        return f"{clean[:10]} {clean[11:16]}"
    return clean


def _now_utc_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _format_comment_row(cr) -> dict:
    cd = dict(cr)
    return {
        "id": int(cd["id"]),
        "note_id": int(cd["note_id"]),
        "author_id": int(cd["author_id"]) if cd.get("author_id") else None,
        "author_name": str(
            cd.get("author_display_name") or cd.get("author_username") or "Team"
        ).strip(),
        "body": str(cd.get("body") or ""),
        "created_at": str(cd.get("created_at") or ""),
        "created_at_short": _fmt_ts(cd.get("created_at")),
    }


def _enrich_note_row(row_dict: dict, comments: list[dict]) -> dict:
    meta = note_category_meta(row_dict.get("category"))
    blocks, done, total = parse_note_body(row_dict.get("body"))
    created_at = str(row_dict.get("created_at") or "")
    updated_at = str(row_dict.get("updated_at") or "")
    author_name = str(
        row_dict.get("author_display_name")
        or row_dict.get("author_username")
        or "Team"
    ).strip()
    updated_by_name = str(
        row_dict.get("updated_display_name")
        or row_dict.get("updated_username")
        or author_name
    ).strip()
    att_url = sanitize_note_url(row_dict.get("attachment_url"))

    return {
        **row_dict,
        "id": int(row_dict["id"]),
        "category": meta["key"],
        "category_label": meta["label"],
        "category_pill": meta["pill"],
        "pinned": bool(int(row_dict.get("pinned") or 0)),
        "archived": bool(int(row_dict.get("archived") or 0)),
        "batch_id": int(row_dict["batch_id"]) if row_dict.get("batch_id") else None,
        "herd_id": int(row_dict["herd_id"]) if row_dict.get("herd_id") else None,
        "batch_name": str(row_dict.get("batch_name") or "").strip(),
        "herd_name": str(row_dict.get("herd_name") or "").strip(),
        "attachment_url": att_url,
        "author_name": author_name,
        "updated_by_name": updated_by_name,
        "created_at_short": _fmt_ts(created_at),
        "updated_at_short": _fmt_ts(updated_at),
        "was_edited": bool(updated_at and created_at and updated_at != created_at),
        "blocks": blocks,
        "check_done": done,
        "check_total": total,
        "preview": plain_note_preview(row_dict.get("body")),
        "comments": comments,
    }


def list_farm_notes(
    user_id: int,
    *,
    category: str = "",
    query: str = "",
    include_archived: bool = False,
    pinned_only: bool = False,
    include_comments: bool = True,
) -> list[dict]:
    uid = int(user_id)
    clauses = ["n.user_id = ?"]
    params: list[object] = [uid]
    if not include_archived:
        clauses.append("n.archived = 0")
    if pinned_only:
        clauses.append("n.pinned = 1")
    clean_cat = str(category or "").strip().lower()
    if clean_cat and clean_cat in _CATEGORY_META:
        clauses.append("n.category = ?")
        params.append(clean_cat)
    where_sql = " AND ".join(clauses)

    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT n.*,
                   uc.display_name AS author_display_name,
                   uc.username AS author_username,
                   uu.display_name AS updated_display_name,
                   uu.username AS updated_username,
                   b.name AS batch_name,
                   h.name AS herd_name
            FROM farm_notes n
            LEFT JOIN users uc ON uc.id = n.created_by
            LEFT JOIN users uu ON uu.id = n.updated_by
            LEFT JOIN batches b ON b.id = n.batch_id
            LEFT JOIN herds h ON h.id = n.herd_id
            WHERE {where_sql}
            ORDER BY n.archived ASC, n.pinned DESC, n.updated_at DESC, n.id DESC
            """,
            tuple(params),
        ).fetchall()
        if not rows:
            return []

        comments_by_note: dict[int, list[dict]] = {}
        if include_comments:
            note_ids = [int(r["id"]) for r in rows]
            marks = ",".join("?" * len(note_ids))
            c_rows = conn.execute(
                f"""
                SELECT c.*, u.display_name AS author_display_name, u.username AS author_username
                FROM farm_note_comments c
                LEFT JOIN users u ON u.id = c.author_id
                WHERE c.user_id = ? AND c.note_id IN ({marks})
                ORDER BY c.id ASC
                """,
                (uid, *note_ids),
            ).fetchall()
            for cr in c_rows:
                item = _format_comment_row(cr)
                comments_by_note.setdefault(item["note_id"], []).append(item)

    clean_q = str(query or "").strip().lower()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        nid = int(d["id"])
        note_comments = comments_by_note.get(nid, [])
        enriched = _enrich_note_row(d, note_comments)
        if clean_q:
            haystack = " ".join(
                [
                    enriched["title"],
                    enriched["body"],
                    enriched["batch_name"],
                    enriched["herd_name"],
                    enriched["author_name"],
                    *(c["body"] for c in note_comments),
                ]
            ).lower()
            if clean_q not in haystack:
                continue
        out.append(enriched)
    return out


def count_farm_notes_by_category(user_id: int, *, include_archived: bool = False) -> dict[str, int]:
    uid = int(user_id)
    arch_clause = "" if include_archived else " AND archived = 0"
    counts = {k: 0 for k, _, _ in NOTE_CATEGORIES}
    counts["all"] = 0
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT category, COUNT(*) AS cnt
            FROM farm_notes
            WHERE user_id = ?{arch_clause}
            GROUP BY category
            """,
            (uid,),
        ).fetchall()
    for r in rows:
        cat = normalize_note_category(r["category"])
        cnt = int(r["cnt"] or 0)
        counts[cat] += cnt
        counts["all"] += cnt
    return counts


def get_farm_note(user_id: int, note_id: int) -> dict | None:
    uid = int(user_id)
    nid = int(note_id)
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT n.*,
                   uc.display_name AS author_display_name,
                   uc.username AS author_username,
                   uu.display_name AS updated_display_name,
                   uu.username AS updated_username,
                   b.name AS batch_name,
                   h.name AS herd_name
            FROM farm_notes n
            LEFT JOIN users uc ON uc.id = n.created_by
            LEFT JOIN users uu ON uu.id = n.updated_by
            LEFT JOIN batches b ON b.id = n.batch_id
            LEFT JOIN herds h ON h.id = n.herd_id
            WHERE n.id = ? AND n.user_id = ?
            """,
            (nid, uid),
        ).fetchone()
        if not row:
            return None
        c_rows = conn.execute(
            """
            SELECT c.*, u.display_name AS author_display_name, u.username AS author_username
            FROM farm_note_comments c
            LEFT JOIN users u ON u.id = c.author_id
            WHERE c.note_id = ? AND c.user_id = ?
            ORDER BY c.id ASC
            """,
            (nid, uid),
        ).fetchall()
    comments = [_format_comment_row(cr) for cr in c_rows]
    return _enrich_note_row(dict(row), comments)


def save_farm_note(
    user_id: int,
    *,
    title: str,
    body: str = "",
    category: str = "general",
    pinned: bool = False,
    batch_id: int | None = None,
    herd_id: int | None = None,
    attachment_url: str = "",
    drive_file_id: str = "",
    clear_attachment: bool = False,
    actor_id: int | None = None,
    note_id: int | None = None,
    allow_pin_change: bool = True,
) -> int:
    uid = int(user_id)
    clean_title = str(title or "").strip()[:140]
    if not clean_title:
        raise ValueError("Note title is required.")
    clean_body = str(body or "").rstrip()
    clean_cat = normalize_note_category(category)
    raw_url = str(attachment_url or "").strip()
    clean_url = sanitize_note_url(raw_url)
    if raw_url and not clean_url:
        raise ValueError("Attachment URL must start with http:// or https://.")
    clean_drive = str(drive_file_id or "").strip()
    bid = int(batch_id) if batch_id else None
    hid = int(herd_id) if herd_id else None
    actor = int(actor_id if actor_id is not None else uid)
    nid = int(note_id) if note_id else None
    now_iso = _now_utc_iso()

    with get_db_connection() as conn:
        if bid is not None:
            b_row = conn.execute(
                "SELECT id FROM batches WHERE id = ? AND user_id = ?", (bid, uid)
            ).fetchone()
            if not b_row:
                raise ValueError("Selected batch was not found.")
        if hid is not None:
            h_row = conn.execute(
                "SELECT id FROM herds WHERE id = ? AND user_id = ?", (hid, uid)
            ).fetchone()
            if not h_row:
                raise ValueError("Selected herd was not found.")

        if nid is not None:
            existing = conn.execute(
                "SELECT id, attachment_url, drive_file_id, pinned FROM farm_notes"
                " WHERE id = ? AND user_id = ?",
                (nid, uid),
            ).fetchone()
            if not existing:
                raise ValueError("Note not found.")
            if clear_attachment:
                final_url = ""
                final_drive = ""
            elif clean_url:
                final_url = clean_url
                final_drive = clean_drive
            else:
                final_url = sanitize_note_url(existing["attachment_url"])
                final_drive = str(existing["drive_file_id"] or "")
            final_pinned = (1 if pinned else 0) if allow_pin_change else int(existing["pinned"] or 0)
            conn.execute(
                """
                UPDATE farm_notes
                SET title = ?, body = ?, category = ?, pinned = ?,
                    batch_id = ?, herd_id = ?, attachment_url = ?, drive_file_id = ?,
                    updated_by = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    clean_title,
                    clean_body,
                    clean_cat,
                    final_pinned,
                    bid,
                    hid,
                    final_url,
                    final_drive,
                    actor,
                    now_iso,
                    nid,
                    uid,
                ),
            )
            conn.commit()
            return nid

        final_pinned = 1 if (pinned and allow_pin_change) else 0
        cur = conn.execute(
            """
            INSERT INTO farm_notes (
                user_id, title, body, category, pinned, archived,
                batch_id, herd_id, attachment_url, drive_file_id,
                created_by, updated_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uid,
                clean_title,
                clean_body,
                clean_cat,
                final_pinned,
                bid,
                hid,
                clean_url,
                clean_drive if clean_url else "",
                actor,
                actor,
                now_iso,
                now_iso,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def set_farm_note_pinned(user_id: int, note_id: int, pinned: bool) -> bool:
    with get_db_connection() as conn:
        cur = conn.execute(
            "UPDATE farm_notes SET pinned = ? WHERE id = ? AND user_id = ?",
            (1 if pinned else 0, int(note_id), int(user_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def set_farm_note_archived(user_id: int, note_id: int, archived: bool) -> bool:
    with get_db_connection() as conn:
        cur = conn.execute(
            """
            UPDATE farm_notes
            SET archived = ?,
                pinned = CASE WHEN ? = 1 THEN 0 ELSE pinned END
            WHERE id = ? AND user_id = ?
            """,
            (1 if archived else 0, 1 if archived else 0, int(note_id), int(user_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_farm_note(user_id: int, note_id: int) -> bool:
    with get_db_connection() as conn:
        cur = conn.execute(
            "DELETE FROM farm_notes WHERE id = ? AND user_id = ?",
            (int(note_id), int(user_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def toggle_farm_note_checklist(
    user_id: int,
    note_id: int,
    item_index: int,
    *,
    actor_id: int | None = None,
) -> dict:
    uid = int(user_id)
    nid = int(note_id)
    actor = int(actor_id if actor_id is not None else uid)
    now_iso = _now_utc_iso()

    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT id, archived, body FROM farm_notes WHERE id = ? AND user_id = ?",
            (nid, uid),
        ).fetchone()
        if not row:
            raise LookupError("Note not found.")
        if int(row["archived"] or 0):
            raise ValueError("Restore this archived note before updating its checklist.")
        new_body, new_checked, done, total = toggle_checklist_item_in_body(
            str(row["body"] or ""), int(item_index)
        )
        conn.execute(
            """
            UPDATE farm_notes
            SET body = ?, updated_by = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (new_body, actor, now_iso, nid, uid),
        )
        conn.commit()
        u_row = conn.execute(
            "SELECT display_name, username FROM users WHERE id = ?", (actor,)
        ).fetchone()
    actor_name = (
        str(u_row["display_name"] or u_row["username"] or "Team").strip() if u_row else "Team"
    )
    return {
        "ok": True,
        "checked": new_checked,
        "done": done,
        "total": total,
        "updated_at_short": _fmt_ts(now_iso),
        "updated_by_name": actor_name,
    }


def reset_farm_note_checklist(
    user_id: int,
    note_id: int,
    *,
    actor_id: int | None = None,
) -> int:
    uid = int(user_id)
    nid = int(note_id)
    actor = int(actor_id if actor_id is not None else uid)
    now_iso = _now_utc_iso()

    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT id, archived, body FROM farm_notes WHERE id = ? AND user_id = ?",
            (nid, uid),
        ).fetchone()
        if not row:
            raise LookupError("Note not found.")
        if int(row["archived"] or 0):
            raise ValueError("Restore this archived note before resetting its checklist.")
        new_body, reset_count = reset_checklist_in_body(str(row["body"] or ""))
        if reset_count > 0:
            conn.execute(
                """
                UPDATE farm_notes
                SET body = ?, updated_by = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (new_body, actor, now_iso, nid, uid),
            )
            conn.commit()
        return reset_count


def add_farm_note_comment(
    user_id: int,
    note_id: int,
    body: str,
    *,
    author_id: int | None = None,
) -> int:
    uid = int(user_id)
    nid = int(note_id)
    clean_body = str(body or "").strip()[:2000]
    if not clean_body:
        raise ValueError("Reply cannot be empty.")
    author = int(author_id if author_id is not None else uid)
    now_iso = _now_utc_iso()

    with get_db_connection() as conn:
        note = conn.execute(
            "SELECT id FROM farm_notes WHERE id = ? AND user_id = ?", (nid, uid)
        ).fetchone()
        if not note:
            raise LookupError("Note not found.")
        cur = conn.execute(
            """
            INSERT INTO farm_note_comments (note_id, user_id, author_id, body, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (nid, uid, author, clean_body, now_iso),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_farm_note_comment(user_id: int, note_id: int, comment_id: int) -> dict | None:
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM farm_note_comments WHERE id = ? AND note_id = ? AND user_id = ?",
            (int(comment_id), int(note_id), int(user_id)),
        ).fetchone()
        return dict(row) if row else None


def delete_farm_note_comment(user_id: int, note_id: int, comment_id: int) -> bool:
    with get_db_connection() as conn:
        cur = conn.execute(
            "DELETE FROM farm_note_comments WHERE id = ? AND note_id = ? AND user_id = ?",
            (int(comment_id), int(note_id), int(user_id)),
        )
        conn.commit()
        return cur.rowcount > 0
