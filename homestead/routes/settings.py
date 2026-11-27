"""Farm settings, species catalog, ntfy, Google Drive OAuth, backups, workspaces & admin."""

from datetime import datetime, timedelta, timezone
import secrets
import time
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse

from homestead.db.backup import import_backup_json
from homestead.db.core import (
    create_user,
    delete_species_preset,
    delete_user,
    get_species_catalog,
    get_user_by_username,
    is_public_registration_enabled,
    list_users,
    save_species_preset,
    set_public_registration_enabled,
    set_user_admin,
    update_user_display_name,
    update_user_password,
    verify_password,
)
from homestead.db.customers import (
    get_farm_pickup_settings,
    save_farm_pickup_settings,
)
from homestead.db.eggs import (
    EGG_SPECIES_ORDER,
    get_layer_settings,
    save_egg_species_settings,
)
from homestead.db.expenses import (
    disconnect_google_drive,
    get_google_drive_connection,
    save_google_drive_connection,
    update_google_drive_folder,
)
from homestead.db.workspaces import (
    HOMESTEAD_ROLES,
    add_homestead_merchant_member,
    create_homestead_merchant,
    list_homestead_merchant_members,
    remove_homestead_merchant_member,
    rename_homestead_merchant,
    update_homestead_merchant_member_role,
)
from homestead.gdrive import (
    GoogleDriveError,
    build_auth_url,
    exchange_code_for_tokens,
    get_redirect_uri,
    is_gdrive_configured,
)
from homestead.ntfy import (
    get_ntfy_config,
    save_ntfy_config,
    send_ntfy_notification,
)
from homestead.web import (
    MERCHANT_COOKIE,
    _cookie_secure,
    _render,
    current_user,
)
from shared.crypto import encrypt_token

router = APIRouter()
_GDRIVE_OAUTH_STATES: dict[str, tuple[int, float]] = {}


@router.get("/settings")
def settings_page(request: Request, msg: str = "", user: dict = Depends(current_user)):
    return _render(
        request, "settings.html", user, "settings",
        species_catalog=get_species_catalog(user_id=user["id"]),
        layer_cfg=get_layer_settings(user["id"]),
        egg_species_order=EGG_SPECIES_ORDER,
        reg_open=is_public_registration_enabled(),
        users=list_users() if user.get("is_admin") else [],
        members=list_homestead_merchant_members(user["merchant_id"]),
        roles=HOMESTEAD_ROLES,
        gdrive_configured=is_gdrive_configured(),
        gdrive_conn=get_google_drive_connection(user["id"]),
        ntfy_cfg=get_ntfy_config(user["id"]),
        pickup_cfg=get_farm_pickup_settings(user["id"]),
        actor_id=user.get("actor_id", user["id"]),
        msg=msg,
    )


@router.post("/settings/egg-species")
def settings_egg_species_save(
    enabled_species: list[str] = Form([]),
    goose_pack_size: str = Form("4"),
    user: dict = Depends(current_user),
):
    if (user.get("role") or "owner") != "owner":
        raise HTTPException(status_code=403, detail="Only farm owners can update farm settings.")
    gp = 1 if str(goose_pack_size or "4").strip() == "1" else 4
    save_egg_species_settings(user["id"], enabled_species, goose_pack_size=gp)
    return RedirectResponse(
        "/settings?msg=" + quote_plus("Saved active laying species & egg carton defaults."),
        status_code=303,
    )


@router.post("/settings/members")
def settings_members_save(
    username: str = Form(...),
    display_name: str = Form(""),
    password: str = Form(""),
    role: str = Form("manager"),
    user: dict = Depends(current_user),
):
    if (user.get("role") or "owner") != "owner":
        raise HTTPException(status_code=403, detail="Only farm owners can manage members.")
    try:
        clean_user = username.strip()
        existing = get_user_by_username(clean_user)
        if existing:
            uid = int(existing["id"])
            current_ids = {
                int(m["user_id"])
                for m in list_homestead_merchant_members(user["merchant_id"])
            }
            if uid in current_ids:
                update_homestead_merchant_member_role(
                    user["merchant_id"], uid, role=role
                )
            else:
                add_homestead_merchant_member(user["merchant_id"], uid, role=role)
        else:
            if not password:
                raise ValueError("Password is required when creating a new family/member account.")
            created = create_user(
                clean_user,
                password,
                display_name=display_name.strip() or clean_user,
                is_admin=False,
                claim_unowned_data=False,
            )
            uid = int(created["id"])
            add_homestead_merchant_member(user["merchant_id"], uid, role=role)
        msg = f"Saved {display_name.strip() or clean_user} ({role}) on {user['merchant_name']}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse("/settings?msg=" + quote_plus(msg), status_code=303)


@router.post("/settings/members/role")
def settings_members_role(
    user_id: int = Form(...),
    role: str = Form(...),
    user: dict = Depends(current_user),
):
    if (user.get("role") or "owner") != "owner":
        raise HTTPException(status_code=403, detail="Only farm owners can manage members.")
    try:
        update_homestead_merchant_member_role(user["merchant_id"], user_id, role=role)
        msg = f"Updated member role to {role}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse("/settings?msg=" + quote_plus(msg), status_code=303)


@router.post("/settings/members/remove")
def settings_members_remove(
    user_id: int = Form(...),
    user: dict = Depends(current_user),
):
    if (user.get("role") or "owner") != "owner":
        raise HTTPException(status_code=403, detail="Only farm owners can manage members.")
    try:
        remove_homestead_merchant_member(user["merchant_id"], user_id)
        msg = "Removed member from farm workspace."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse("/settings?msg=" + quote_plus(msg), status_code=303)


@router.post("/settings/merchant/rename")
def settings_merchant_rename(
    name: str = Form(...),
    user: dict = Depends(current_user),
):
    if (user.get("role") or "owner") != "owner":
        raise HTTPException(status_code=403, detail="Only farm owners can rename the farm workspace.")
    try:
        rename_homestead_merchant(user["merchant_id"], name)
        msg = f"Renamed farm workspace to {name.strip()}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse("/settings?msg=" + quote_plus(msg), status_code=303)


@router.post("/settings/merchant/create")
def settings_merchant_create(
    request: Request,
    name: str = Form(...),
    user: dict = Depends(current_user),
):
    if (user.get("role") or "owner") != "owner":
        raise HTTPException(status_code=403)
    try:
        mid = create_homestead_merchant(name, owner_user_id=user.get("actor_id", user["id"]))
        resp = RedirectResponse("/settings?msg=" + quote_plus(f"Created farm workspace {name.strip()}."), status_code=303)
        resp.set_cookie(
            MERCHANT_COOKIE,
            str(mid),
            httponly=True,
            samesite="lax",
            secure=_cookie_secure(request),
            max_age=30 * 86400,
        )
        return resp
    except ValueError as exc:
        return RedirectResponse("/settings?msg=" + quote_plus(f"Error: {exc}"), status_code=303)


@router.post("/settings/pickup-payments")
def settings_pickup_payments_save(
    farm_name: str = Form(""),
    pickup_address: str = Form(""),
    pickup_instructions: str = Form(""),
    venmo_handle: str = Form(""),
    paypal_handle: str = Form(""),
    user: dict = Depends(current_user),
):
    save_farm_pickup_settings(
        user["id"],
        farm_name=farm_name,
        pickup_address=pickup_address,
        pickup_instructions=pickup_instructions,
        venmo_handle=venmo_handle,
        paypal_handle=paypal_handle,
    )
    return RedirectResponse(
        "/settings?msg=" + quote_plus("Saved farm pickup & digital payment settings."),
        status_code=303,
    )


@router.post("/settings/ntfy")
def settings_ntfy_save(
    ntfy_url: str = Form("https://ntfy.sh"),
    ntfy_topic: str = Form(""),
    ntfy_token: str = Form(""),
    clear_token: str = Form(""),
    user: dict = Depends(current_user),
):
    save_ntfy_config(
        user["id"],
        url=ntfy_url,
        topic=ntfy_topic,
        token=ntfy_token,
        clear_token=bool(clear_token),
    )
    return RedirectResponse(
        "/settings?msg=" + quote_plus("Saved ntfy push notification settings."),
        status_code=303,
    )


@router.post("/settings/ntfy/test")
def settings_ntfy_test(request: Request, user: dict = Depends(current_user)):
    ok, detail = send_ntfy_notification(
        user["id"],
        "Homestead Planner - Test Notification",
        "ntfy push notifications are connected! You'll get alerts when customers claim birds.",
        click_url=f"{str(request.base_url).rstrip('/')}/batches",
        tags="white_check_mark,turkey",
    )
    msg = "Test notification sent to ntfy!" if ok else f"ntfy test failed: {detail}"
    return RedirectResponse("/settings?msg=" + quote_plus(msg), status_code=303)


@router.get("/settings/google-drive/connect")
def gdrive_connect(request: Request, user: dict = Depends(current_user)):
    if not is_gdrive_configured():
        return RedirectResponse(
            "/settings?msg=" + quote_plus("Google Drive OAuth is not configured on this server."),
            status_code=303,
        )
    now = time.time()
    for k, (_, exp) in list(_GDRIVE_OAUTH_STATES.items()):
        if exp < now:
            _GDRIVE_OAUTH_STATES.pop(k, None)
    state = secrets.token_urlsafe(24)
    _GDRIVE_OAUTH_STATES[state] = (user["id"], now + 600)
    redirect_uri = get_redirect_uri(str(request.base_url))
    return RedirectResponse(build_auth_url(state, redirect_uri), status_code=302)


@router.get("/settings/google-drive/callback")
def gdrive_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    user: dict = Depends(current_user),
):
    if error:
        return RedirectResponse("/settings?msg=" + quote_plus(f"Google Drive error: {error}"), status_code=303)
    entry = _GDRIVE_OAUTH_STATES.pop(state, None)
    if not entry or entry[0] != user["id"] or entry[1] < time.time():
        return RedirectResponse("/settings?msg=" + quote_plus("Google Drive authorization expired; try again."), status_code=303)
    redirect_uri = get_redirect_uri(str(request.base_url))
    try:
        tokens = exchange_code_for_tokens(code, redirect_uri)
    except GoogleDriveError as exc:
        return RedirectResponse("/settings?msg=" + quote_plus(str(exc)), status_code=303)
    secs = max(60, int(tokens.get("expires_in") or 3600))
    exp_iso = (datetime.now(timezone.utc) + timedelta(seconds=secs)).isoformat()
    save_google_drive_connection(
        user_id=user["id"],
        google_email=str(tokens.get("email") or ""),
        access_token_enc=encrypt_token(tokens.get("access_token") or ""),
        refresh_token_enc=encrypt_token(tokens.get("refresh_token") or ""),
        token_expires_at=exp_iso,
    )
    return RedirectResponse("/settings?msg=" + quote_plus("Google Drive connected for receipts."), status_code=303)


@router.post("/settings/google-drive/folder")
def gdrive_folder_save(folder_name: str = Form(...), user: dict = Depends(current_user)):
    update_google_drive_folder(user["id"], folder_name)
    return RedirectResponse("/settings?msg=" + quote_plus("Updated Google Drive receipt folder."), status_code=303)


@router.post("/settings/google-drive/disconnect")
def gdrive_disconnect(user: dict = Depends(current_user)):
    disconnect_google_drive(user["id"])
    return RedirectResponse("/settings?msg=" + quote_plus("Disconnected Google Drive."), status_code=303)


@router.post("/settings/species")
def settings_species_save(
    species: str = Form(...),
    lifecycle_type: str = Form("direct_arrival"),
    days_to_birth: int = Form(0),
    starter_weeks: int = Form(3),
    grower_weeks: int = Form(5),
    starter_feed_lbs: float = Form(3.0),
    grower_feed_lbs: float = Form(12.0),
    unit_cost: float = Form(3.5),
    expected_yield_lbs: float = Form(4.5),
    sale_price_per_lb: float = Form(5.5),
    processing_cost: float = Form(5.0),
    starter_bag_price: float = Form(24.0),
    grower_bag_price: float = Form(22.0),
    apply_active: str = Form(""),
    user: dict = Depends(current_user),
):
    save_species_preset(
        species,
        {
            "lifecycle_type": lifecycle_type,
            "days_to_birth": days_to_birth,
            "starter_weeks": starter_weeks,
            "grower_weeks": grower_weeks,
            "starter_feed_lbs": starter_feed_lbs,
            "grower_feed_lbs": grower_feed_lbs,
            "unit_cost": unit_cost,
            "expected_yield_lbs": expected_yield_lbs,
            "sale_price_per_lb": sale_price_per_lb,
            "processing_cost": processing_cost,
            "starter_bag_price": starter_bag_price,
            "grower_bag_price": grower_bag_price,
        },
        user_id=user["id"],
        apply_to_active_batches=bool(apply_active),
    )
    return RedirectResponse("/settings?msg=" + quote_plus(f"Saved {species} preset."), status_code=303)


@router.post("/settings/species/delete")
def settings_species_delete(species: str = Form(...), user: dict = Depends(current_user)):
    delete_species_preset(species, user_id=user["id"])
    return RedirectResponse("/settings?msg=" + quote_plus(f"Deleted {species} preset."), status_code=303)


@router.post("/settings/restore")
async def settings_restore(
    backup_file: UploadFile = File(...),
    replace_existing: str = Form(""),
    user: dict = Depends(current_user),
):
    raw = (await backup_file.read()).decode("utf-8")
    try:
        import_backup_json(raw, user_id=user["id"], replace_existing=bool(replace_existing))
        msg = "Backup restored."
    except Exception as exc:
        msg = f"Restore failed: {exc}"
    return RedirectResponse("/settings?msg=" + quote_plus(msg), status_code=303)


@router.post("/settings/password")
def settings_password(
    current_password: str = Form(...),
    new_password: str = Form(...),
    user: dict = Depends(current_user),
):
    if not verify_password(current_password, user["password_hash"], user["password_salt"]):
        return RedirectResponse("/settings?msg=" + quote_plus("Current password is incorrect."), status_code=303)
    try:
        update_user_password(user.get("actor_id", user["id"]), new_password)
        msg = "Password updated."
    except ValueError as exc:
        msg = str(exc)
    return RedirectResponse("/settings?msg=" + quote_plus(msg), status_code=303)


@router.post("/settings/admin/registration")
def settings_admin_registration(enabled: str = Form(...), user: dict = Depends(current_user)):
    if not user.get("is_admin"):
        raise HTTPException(status_code=403)
    set_public_registration_enabled(enabled == "1")
    return RedirectResponse("/settings?msg=" + quote_plus("Updated registration setting."), status_code=303)


@router.post("/settings/admin/users")
def settings_admin_add_user(
    username: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(""),
    is_admin: str = Form(""),
    user: dict = Depends(current_user),
):
    if not user.get("is_admin"):
        raise HTTPException(status_code=403)
    try:
        create_user(
            username,
            password,
            display_name=display_name,
            is_admin=(is_admin == "1"),
            claim_unowned_data=False,
        )
        msg = f"Created user {username}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse("/settings?msg=" + quote_plus(msg), status_code=303)


@router.post("/settings/admin/users/{uid}/update")
def settings_admin_update_user(
    uid: int,
    display_name: str = Form(...),
    new_password: str = Form(""),
    is_admin: str = Form(""),
    user: dict = Depends(current_user),
):
    if not user.get("is_admin"):
        raise HTTPException(status_code=403)
    try:
        clean_name = update_user_display_name(uid, display_name)
        if new_password:
            update_user_password(uid, new_password)
        set_user_admin(uid, is_admin == "1")
        msg = f"Updated user {clean_name}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse("/settings?msg=" + quote_plus(msg), status_code=303)


@router.post("/settings/admin/users/{uid}/delete")
def settings_admin_delete_user(uid: int, user: dict = Depends(current_user)):
    if not user.get("is_admin"):
        raise HTTPException(status_code=403)
    try:
        delete_user(uid, acting_user_id=user.get("actor_id", user["id"]))
        msg = "User deleted."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse("/settings?msg=" + quote_plus(msg), status_code=303)
