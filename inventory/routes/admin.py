"""Super Admin tenant, member, onboarding invite, and user administration routes."""

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from inventory import billing, sync
from inventory.merchants import (
    ROLES,
    add_member,
    create_invite,
    create_merchant,
    delete_invite,
    delete_merchant,
    list_all_merchants,
    list_all_users_with_memberships,
    list_invites,
    list_members,
    remove_member,
    rename_merchant,
)
from inventory.web import Ctx, render, require
from shared.auth import (
    create_user,
    get_user_by_username,
    set_user_admin,
    update_user_password,
)

router = APIRouter()


@router.get("/admin")
def admin_page(
    request: Request,
    msg: str = "",
    invite_token: str = "",
    checkout_url: str = "",
    ctx: Ctx = Depends(require("admin")),
):
    merchants = list_all_merchants()
    for m in merchants:
        m["members"] = list_members(m["id"])
    base_url = str(request.base_url).rstrip("/")
    invite_url = f"{base_url}/invite/{invite_token}" if invite_token else ""
    return render(
        request,
        "admin.html",
        ctx,
        "admin",
        merchants=merchants,
        users=list_all_users_with_memberships(),
        invites=list_invites(),
        roles=ROLES,
        base_url=base_url,
        invite_url=invite_url,
        checkout_url=checkout_url,
        stripe_configured=billing.configured(),
        stripe_webhook_configured=billing.webhook_configured(),
        price_catalog=billing.price_catalog(),
        msg=msg,
    )


@router.post("/admin/merchants/create")
def admin_create_merchant(
    merchant_name: str = Form(...),
    owner_username: str = Form(""),
    owner_display_name: str = Form(""),
    owner_password: str = Form(""),
    ctx: Ctx = Depends(require("admin")),
):
    try:
        owner_id = None
        clean_user = owner_username.strip()
        if clean_user:
            existing = get_user_by_username(clean_user)
            if existing:
                owner_id = existing["id"]
            else:
                created = create_user(
                    clean_user,
                    owner_password,
                    display_name=owner_display_name,
                    is_admin=False,
                )
                owner_id = created["id"]
        mid = create_merchant(merchant_name, owner_user_id=owner_id)
        msg = f"Created workspace '{merchant_name.strip()}' (#{mid})."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/admin?msg={quote_plus(msg)}", status_code=303)


@router.post("/admin/merchants/rename")
def admin_rename_merchant(
    merchant_id: int = Form(...),
    name: str = Form(...),
    ctx: Ctx = Depends(require("admin")),
):
    try:
        rename_merchant(merchant_id, name)
        msg = f"Renamed workspace to '{name.strip()}'."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/admin?msg={quote_plus(msg)}", status_code=303)


@router.post("/admin/merchants/delete")
def admin_delete_merchant(
    merchant_id: int = Form(...),
    ctx: Ctx = Depends(require("admin")),
):
    delete_merchant(merchant_id)
    return RedirectResponse(
        f"/admin?msg={quote_plus('Deleted workspace.')}", status_code=303
    )


@router.post("/admin/merchants/sync")
def admin_sync_merchant(
    merchant_id: int = Form(...),
    ctx: Ctx = Depends(require("admin")),
):
    result = sync.sync_merchant(merchant_id)
    msg = (
        f"Sync failed: {result['error']}"
        if result["error"]
        else f"Synced {result['products']} products, {result['purchases']} new sales."
    )
    return RedirectResponse(f"/admin?msg={quote_plus(msg)}", status_code=303)


@router.post("/admin/members/save")
def admin_save_member(
    merchant_id: int = Form(...),
    username: str = Form(...),
    display_name: str = Form(""),
    password: str = Form(""),
    role: str = Form("owner"),
    ctx: Ctx = Depends(require("admin")),
):
    try:
        clean_user = username.strip()
        existing = get_user_by_username(clean_user)
        if existing:
            uid = existing["id"]
        else:
            created = create_user(
                clean_user,
                password,
                display_name=display_name,
                is_admin=False,
            )
            uid = created["id"]
        add_member(merchant_id, uid, role)
        msg = f"Assigned {clean_user} as {role}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/admin?msg={quote_plus(msg)}", status_code=303)


@router.post("/admin/members/remove")
def admin_remove_member(
    merchant_id: int = Form(...),
    user_id: int = Form(...),
    ctx: Ctx = Depends(require("admin")),
):
    try:
        remove_member(merchant_id, user_id, allow_zero_owners=True)
        msg = "Removed member from workspace."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/admin?msg={quote_plus(msg)}", status_code=303)


@router.post("/admin/invites/create")
def admin_create_invite(
    target_merchant_id: str = Form(""),
    role: str = Form("owner"),
    default_merchant_name: str = Form(""),
    note: str = Form(""),
    days_valid: int = Form(14),
    ctx: Ctx = Depends(require("admin")),
):
    try:
        mid = int(target_merchant_id) if target_merchant_id.strip().isdigit() else None
        token = create_invite(
            merchant_id=mid,
            role=role,
            default_merchant_name=default_merchant_name,
            note=note,
            created_by=ctx.user["id"],
            days_valid=days_valid,
        )
        return RedirectResponse(
            f"/admin?msg={quote_plus('Created one-time invite link.')}&invite_token={quote_plus(token)}",
            status_code=303,
        )
    except ValueError as exc:
        return RedirectResponse(f"/admin?msg={quote_plus(f'Error: {exc}')}", status_code=303)


@router.post("/admin/invites/delete")
def admin_delete_invite(
    token: str = Form(...),
    ctx: Ctx = Depends(require("admin")),
):
    delete_invite(token)
    return RedirectResponse(f"/admin?msg={quote_plus('Deleted invite.')}", status_code=303)


@router.post("/admin/users/password")
def admin_reset_password(
    user_id: int = Form(...),
    new_password: str = Form(...),
    ctx: Ctx = Depends(require("admin")),
):
    try:
        update_user_password(user_id, new_password)
        msg = "Updated user password."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/admin?msg={quote_plus(msg)}", status_code=303)


@router.post("/admin/users/admin")
def admin_toggle_user_admin(
    user_id: int = Form(...),
    is_admin: int = Form(0),
    ctx: Ctx = Depends(require("admin")),
):
    try:
        set_user_admin(user_id, bool(is_admin))
        msg = "Updated Super Admin status."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/admin?msg={quote_plus(msg)}", status_code=303)
