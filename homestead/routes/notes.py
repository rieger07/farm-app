"""Farm -> Team Notes routes (living SOPs, bulletin board, tappable checklists, replies)."""

from datetime import date
from urllib.parse import quote_plus, urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from homestead import ntfy as ntfy_mod
from homestead.db.batches import list_batches
from homestead.db.expenses import get_google_drive_connection
from homestead.db.herds import list_herds
from homestead.db.notes import (
    NOTE_CATEGORIES,
    add_farm_note_comment,
    count_farm_notes_by_category,
    delete_farm_note,
    delete_farm_note_comment,
    get_farm_note,
    get_farm_note_comment,
    list_farm_notes,
    note_category_meta,
    plain_note_preview,
    reset_farm_note_checklist,
    save_farm_note,
    set_farm_note_archived,
    set_farm_note_pinned,
    toggle_farm_note_checklist,
)
from homestead.db.workspaces import can_access_homestead_page
from homestead.web import _render, _resolve_receipt_attachment, current_user

router = APIRouter()


def _is_manager(user: dict) -> bool:
    return (user.get("role") or "owner") in ("owner", "manager")


def _actor_id(user: dict) -> int:
    return int(user.get("actor_id") or user["id"])


def _require_notes_access(user: dict) -> None:
    if not can_access_homestead_page(user.get("role") or "staff", "notes"):
        raise HTTPException(
            status_code=403, detail="Your role does not have access to Team Notes."
        )


def _can_modify_note(user: dict, note: dict) -> bool:
    if _is_manager(user):
        return True
    owner_actor = note.get("created_by")
    return owner_actor is not None and int(owner_actor) == _actor_id(user)


def _can_delete_comment(user: dict, comment: dict) -> bool:
    if _is_manager(user):
        return True
    author = comment.get("author_id")
    return author is not None and int(author) == _actor_id(user)


def _notes_redirect(msg: str, note_id: int | None = None) -> RedirectResponse:
    frag = f"#note-{int(note_id)}" if note_id is not None else ""
    target = f"/notes?msg={quote_plus(msg)}{frag}".replace("\\", "")
    if not urlparse(target).netloc and not urlparse(target).scheme:
        return RedirectResponse(target, status_code=303)
    return RedirectResponse("/notes", status_code=303)


@router.get("/notes")
def notes_page(
    request: Request,
    category: str = "",
    q: str = "",
    archived: str = "",
    msg: str = "",
    user: dict = Depends(current_user),
):
    _require_notes_access(user)
    uid = user["id"]
    show_archived = archived == "1"
    notes = list_farm_notes(
        uid,
        category=category,
        query=q,
        include_archived=show_archived,
    )
    counts = count_farm_notes_by_category(uid, include_archived=show_archived)
    active_batches = [b for b in list_batches(user_id=uid) if b.get("active", 1)]
    active_herds = list_herds(uid)
    return _render(
        request,
        "notes.html",
        user,
        "notes",
        notes=notes,
        note_categories=NOTE_CATEGORIES,
        category_counts=counts,
        filter_category=category.strip().lower(),
        filter_query=q,
        show_archived=show_archived,
        active_batches=active_batches,
        active_herds=active_herds,
        can_manage=_is_manager(user),
        actor_id=_actor_id(user),
        gdrive_connected=bool(get_google_drive_connection(uid)),
        ntfy_configured=bool(ntfy_mod.get_ntfy_config(uid).get("configured")),
        msg=msg,
    )


@router.post("/notes/save")
async def note_save_submit(request: Request, user: dict = Depends(current_user)):
    _require_notes_access(user)
    uid = user["id"]
    actor = _actor_id(user)
    can_manage = _is_manager(user)
    form = await request.form()

    raw_nid = str(form.get("note_id") or "").strip()
    nid = int(raw_nid) if raw_nid.isdigit() else None
    if nid is not None:
        existing = get_farm_note(uid, nid)
        if not existing:
            raise HTTPException(status_code=404, detail="Note not found.")
        if not _can_modify_note(user, existing):
            raise HTTPException(
                status_code=403, detail="Only managers or the author can edit this note."
            )

    title = str(form.get("title") or "").strip()
    body = str(form.get("body") or "")
    category = str(form.get("category") or "general").strip()
    pinned = str(form.get("pinned") or "") == "1"
    clear_att = str(form.get("clear_attachment") or "") == "1"
    notify_team = str(form.get("notify_team") or "") == "1"

    raw_bid = str(form.get("batch_id") or "").strip()
    bid = int(raw_bid) if raw_bid.isdigit() else None
    raw_hid = str(form.get("herd_id") or "").strip()
    hid = int(raw_hid) if raw_hid.isdigit() else None

    manual_url = "" if clear_att else str(form.get("attachment_url") or "").strip()
    att_file = form.get("attachment_file")
    upload = None
    if not clear_att and att_file is not None and getattr(att_file, "filename", ""):
        upload = att_file

    r_url, drive_id, warn = await _resolve_receipt_attachment(
        uid,
        upload,
        manual_url,
        date.today().isoformat(),
        f"Team Note ({title[:40] or 'Note'})",
    )

    try:
        saved_id = save_farm_note(
            uid,
            title=title,
            body=body,
            category=category,
            pinned=pinned,
            batch_id=bid,
            herd_id=hid,
            attachment_url=r_url,
            drive_file_id=drive_id,
            clear_attachment=clear_att,
            actor_id=actor,
            note_id=nid,
            allow_pin_change=can_manage,
        )
    except ValueError as exc:
        return _notes_redirect(f"Error: {exc}")

    if nid is None and notify_team and ntfy_mod.get_ntfy_config(uid).get("configured"):
        author_lbl = str(user.get("display_name") or user.get("username") or "Team")
        cat_lbl = note_category_meta(category)["label"]
        preview = plain_note_preview(body, max_len=160) or "Open Farm -> Team Notes for details."
        base_url = str(request.base_url).rstrip("/")
        ntfy_mod.send_ntfy_async(
            uid,
            f"Team Note ({cat_lbl}): {title[:80]}",
            f"{author_lbl}: {preview}",
            click_url=f"{base_url}/notes#note-{saved_id}",
            tags="memo",
        )

    msg = "Updated team note." if nid else f"Posted team note '{title[:60]}'."
    if warn:
        msg += f" (Attachment note: {warn})"
    return _notes_redirect(msg, note_id=saved_id)


@router.post("/notes/{note_id}/pin")
def note_pin_submit(
    note_id: int,
    pinned: str = Form("1"),
    user: dict = Depends(current_user),
):
    _require_notes_access(user)
    if not _is_manager(user):
        raise HTTPException(status_code=403, detail="Only owners and managers can pin team notes.")
    pin_val = pinned == "1"
    if not set_farm_note_pinned(user["id"], note_id, pin_val):
        raise HTTPException(status_code=404, detail="Note not found.")
    return _notes_redirect(
        "Pinned note to top & Today Radar." if pin_val else "Unpinned note.",
        note_id=note_id,
    )


@router.post("/notes/{note_id}/archive")
def note_archive_submit(
    note_id: int,
    archived: str = Form("1"),
    user: dict = Depends(current_user),
):
    _require_notes_access(user)
    note = get_farm_note(user["id"], note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found.")
    if not _can_modify_note(user, note):
        raise HTTPException(
            status_code=403, detail="Only managers or the author can archive this note."
        )
    arch_val = archived == "1"
    set_farm_note_archived(user["id"], note_id, arch_val)
    return _notes_redirect(
        "Archived team note." if arch_val else "Restored team note.",
        note_id=None if arch_val else note_id,
    )


@router.post("/notes/{note_id}/delete")
def note_delete_submit(note_id: int, user: dict = Depends(current_user)):
    _require_notes_access(user)
    note = get_farm_note(user["id"], note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found.")
    if not _can_modify_note(user, note):
        raise HTTPException(
            status_code=403, detail="Only managers or the author can delete this note."
        )
    delete_farm_note(user["id"], note_id)
    return _notes_redirect("Removed team note.")


@router.post("/api/notes/{note_id}/checklist/{item_idx}/toggle")
def api_note_checklist_toggle(
    note_id: int,
    item_idx: int,
    user: dict = Depends(current_user),
):
    _require_notes_access(user)
    try:
        res = toggle_farm_note_checklist(
            user["id"], note_id, item_idx, actor_id=_actor_id(user)
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(res)


@router.post("/notes/{note_id}/reset-checklist")
def note_reset_checklist_submit(note_id: int, user: dict = Depends(current_user)):
    _require_notes_access(user)
    try:
        reset_count = reset_farm_note_checklist(
            user["id"], note_id, actor_id=_actor_id(user)
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        return _notes_redirect(f"Error: {exc}", note_id=note_id)
    msg = (
        f"Reset {reset_count} checklist item{'s' if reset_count != 1 else ''}."
        if reset_count
        else "Checklist is already unchecked."
    )
    return _notes_redirect(msg, note_id=note_id)


@router.post("/notes/{note_id}/comments/add")
def note_comment_add_submit(
    request: Request,
    note_id: int,
    body: str = Form(...),
    notify_team: str = Form(""),
    user: dict = Depends(current_user),
):
    _require_notes_access(user)
    uid = user["id"]
    note = get_farm_note(uid, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found.")
    try:
        add_farm_note_comment(uid, note_id, body, author_id=_actor_id(user))
    except ValueError as exc:
        return _notes_redirect(f"Error: {exc}", note_id=note_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if notify_team == "1" and ntfy_mod.get_ntfy_config(uid).get("configured"):
        author_lbl = str(user.get("display_name") or user.get("username") or "Team")
        base_url = str(request.base_url).rstrip("/")
        ntfy_mod.send_ntfy_async(
            uid,
            f"Reply on: {note['title'][:80]}",
            f"{author_lbl}: {plain_note_preview(body, max_len=160)}",
            click_url=f"{base_url}/notes#note-{int(note_id)}",
            tags="speech_balloon",
        )
    return _notes_redirect("Added reply.", note_id=note_id)


@router.post("/notes/{note_id}/comments/{comment_id}/delete")
def note_comment_delete_submit(
    note_id: int,
    comment_id: int,
    user: dict = Depends(current_user),
):
    _require_notes_access(user)
    comment = get_farm_note_comment(user["id"], note_id, comment_id)
    if not comment:
        raise HTTPException(status_code=404, detail="Reply not found.")
    if not _can_delete_comment(user, comment):
        raise HTTPException(
            status_code=403, detail="Only managers or the author can remove this reply."
        )
    delete_farm_note_comment(user["id"], note_id, comment_id)
    return _notes_redirect("Removed reply.", note_id=note_id)
