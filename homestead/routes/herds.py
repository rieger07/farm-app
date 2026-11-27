"""Perennial herds management (breeding pigs, fiber alpacas), named animals, and feed logs."""

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from homestead.db.core import get_bag_weight
from homestead.db.herds import (
    HERD_FEED_CATEGORIES,
    HERD_FEED_UNITS,
    HERD_SPECIES,
    HERD_TYPES,
    delete_herd,
    delete_herd_animal,
    delete_herd_feed_log,
    get_herd,
    list_herd_animals,
    list_herd_feed_logs,
    list_herds,
    save_herd,
    save_herd_animal,
    toggle_herd_animal_active,
)
from homestead.web import _render, current_user

router = APIRouter()


def _require_herd_manage(user: dict) -> None:
    if (user.get("role") or "owner") not in ("owner", "manager"):
        raise HTTPException(
            status_code=403, detail="Only owners and managers can modify herds."
        )


def _herd_redirect(herd_id: int | str | None, msg: str) -> RedirectResponse:
    hid = str(herd_id or "").strip()
    hq = f"herd_id={quote_plus(hid)}&" if hid else ""
    return RedirectResponse(f"/herds?{hq}msg={quote_plus(msg)}", status_code=303)


@router.get("/herds")
def herds_page(
    request: Request,
    herd_id: str = "",
    msg: str = "",
    user: dict = Depends(current_user),
):
    uid = user["id"]
    herds = list_herds(uid, include_inactive=True)
    selected_herd = None
    if herd_id.strip().isdigit():
        wanted = int(herd_id.strip())
        selected_herd = next((h for h in herds if int(h["id"]) == wanted), None)
    if selected_herd is None and herds:
        selected_herd = next((h for h in herds if h["active"]), herds[0])

    animals = (
        list_herd_animals(uid, herd_id=int(selected_herd["id"]), include_inactive=True)
        if selected_herd
        else []
    )
    feed_logs = (
        list_herd_feed_logs(uid, herd_id=int(selected_herd["id"]), limit=100)
        if selected_herd
        else []
    )
    can_manage = (user.get("role") or "owner") in ("owner", "manager")

    return _render(
        request,
        "herds.html",
        user,
        "herds",
        herds=herds,
        selected_herd=selected_herd,
        animals=animals,
        feed_logs=feed_logs,
        bag_weight=get_bag_weight(user_id=uid),
        herd_species_options=HERD_SPECIES,
        herd_type_options=HERD_TYPES,
        feed_category_options=HERD_FEED_CATEGORIES,
        feed_unit_options=HERD_FEED_UNITS,
        can_manage=can_manage,
        msg=msg,
    )


@router.post("/herds/save")
def herd_save_submit(
    herd_id: str = Form(""),
    name: str = Form(...),
    species: str = Form("pig"),
    herd_type: str = Form("breeding"),
    head_count: int = Form(0),
    notes: str = Form(""),
    active: str = Form("1"),
    user: dict = Depends(current_user),
):
    _require_herd_manage(user)
    hid = int(herd_id) if herd_id.strip().isdigit() else None
    if hid is not None and not get_herd(user["id"], hid):
        raise HTTPException(status_code=404, detail="Herd not found.")
    try:
        saved_id = save_herd(
            user["id"],
            name=name,
            species=species,
            herd_type=herd_type,
            head_count=head_count,
            notes=notes,
            herd_id=hid,
            active=active != "0",
        )
    except ValueError as exc:
        return _herd_redirect(hid, f"Error: {exc}")
    verb = "Updated" if hid else "Added"
    return _herd_redirect(saved_id, f"{verb} herd '{name.strip()}'.")


@router.post("/herds/{herd_id}/delete")
def herd_delete_submit(herd_id: int, user: dict = Depends(current_user)):
    _require_herd_manage(user)
    if not delete_herd(user["id"], herd_id):
        raise HTTPException(status_code=404, detail="Herd not found.")
    return _herd_redirect("", "Removed herd.")


@router.post("/herds/{herd_id}/animals/save")
def herd_animal_save_submit(
    herd_id: int,
    animal_id: str = Form(""),
    name: str = Form(...),
    sex_role: str = Form(""),
    color: str = Form(""),
    birth_year: str = Form(""),
    notes: str = Form(""),
    user: dict = Depends(current_user),
):
    _require_herd_manage(user)
    if not get_herd(user["id"], herd_id):
        raise HTTPException(status_code=404, detail="Herd not found.")
    aid = int(animal_id) if animal_id.strip().isdigit() else None
    byr = int(birth_year.strip()) if birth_year.strip().isdigit() else None
    try:
        save_herd_animal(
            user["id"],
            herd_id=herd_id,
            name=name,
            sex_role=sex_role,
            color=color,
            birth_year=byr,
            notes=notes,
            animal_id=aid,
        )
    except ValueError as exc:
        return _herd_redirect(herd_id, f"Error: {exc}")
    verb = "Updated" if aid else "Added"
    return _herd_redirect(herd_id, f"{verb} {name.strip()}.")


@router.post("/herds/{herd_id}/animals/{animal_id}/toggle")
def herd_animal_toggle_submit(
    herd_id: int,
    animal_id: int,
    active: int = Form(...),
    user: dict = Depends(current_user),
):
    _require_herd_manage(user)
    if not get_herd(user["id"], herd_id):
        raise HTTPException(status_code=404, detail="Herd not found.")
    toggle_herd_animal_active(user["id"], animal_id, bool(active))
    return _herd_redirect(
        herd_id, "Marked animal active." if active else "Retired animal from active count."
    )


@router.post("/herds/{herd_id}/animals/{animal_id}/delete")
def herd_animal_delete_submit(
    herd_id: int, animal_id: int, user: dict = Depends(current_user)
):
    _require_herd_manage(user)
    if not get_herd(user["id"], herd_id):
        raise HTTPException(status_code=404, detail="Herd not found.")
    delete_herd_animal(user["id"], animal_id)
    return _herd_redirect(herd_id, "Removed animal from roster.")


@router.post("/herds/{herd_id}/feed/{log_id}/delete")
def herd_feed_delete_submit(
    herd_id: int, log_id: int, user: dict = Depends(current_user)
):
    _require_herd_manage(user)
    if not get_herd(user["id"], herd_id):
        raise HTTPException(status_code=404, detail="Herd not found.")
    delete_herd_feed_log(user["id"], log_id)
    return _herd_redirect(herd_id, "Removed herd feed entry.")
