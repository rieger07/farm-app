"""Workspace settings, payment handles, marketplaces, members, and Zettle OAuth sync."""

import asyncio
import os
import secrets
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from inventory import billing, sync, zettle
from inventory.marketplaces import archive_marketplace, list_marketplaces, save_marketplace
from inventory.merchants import (
    ROLES,
    add_member,
    get_payment_handles,
    list_members,
    remove_member,
    rename_merchant,
    update_member_role,
    update_payment_handles,
)
from inventory.stock import ITEM_TYPES, list_stock, set_item_type
from inventory.web import (
    Ctx,
    _parse_dollars_to_cents,
    format_last_sync,
    render,
    require,
)
from shared.auth import create_user, get_user_by_username

router = APIRouter()


def _redirect_uri(request: Request) -> str:
    env_uri = os.environ.get("ZETTLE_REDIRECT_URI", "").strip()
    if env_uri:
        return env_uri
    base_url = os.environ.get("APP_BASE_URL", "").strip()
    if base_url:
        return base_url.rstrip("/") + "/zettle/callback"
    host = (request.url.hostname or "").lower()
    if host in ("", "localhost", "127.0.0.1", "0.0.0.0") or "rieger.page" in host:
        return "https://ledger.ironcladsystems.dev/zettle/callback"
    return str(request.url_for("zettle_callback"))


@router.get("/settings")
def settings_page(request: Request, msg: str = "", ctx: Ctx = Depends(require("settings"))):
    status = sync.connection_status(ctx.merchant_id)
    return render(
        request, "settings.html", ctx, "settings",
        status=status,
        last_sync_display=format_last_sync(status.get("last_sync")),
        configured=zettle.configured(),
        sub=billing.get_subscription(ctx.merchant_id),
        stripe_configured=billing.configured(),
        members=list_members(ctx.merchant_id),
        roles=ROLES,
        all_skus=list_stock(ctx.merchant_id, include_fees=True),
        marketplaces=list_marketplaces(ctx.merchant_id),
        payment_handles=get_payment_handles(ctx.merchant_id),
        redirect_uri=_redirect_uri(request),
        msg=msg,
    )


@router.post("/settings/payment-handles")
def settings_payment_handles(
    venmo_handle: str = Form(""),
    paypal_handle: str = Form(""),
    ctx: Ctx = Depends(require("settings")),
):
    update_payment_handles(ctx.merchant_id, venmo_handle=venmo_handle, paypal_handle=paypal_handle)
    return RedirectResponse(
        f"/settings?msg={quote_plus('Saved Venmo & PayPal.Me handles.')}",
        status_code=303,
    )


@router.post("/settings/marketplaces")
def settings_save_marketplace(
    marketplace_id: str = Form(""),
    name: str = Form(...),
    location: str = Form(""),
    booth_fee: str = Form("0"),
    travel_cost: str = Form("0"),
    ctx: Ctx = Depends(require("settings")),
):
    try:
        mp_id = int(marketplace_id) if marketplace_id.strip().isdigit() else None
        save_marketplace(
            ctx.merchant_id,
            name=name,
            location=location,
            booth_fee_cents=_parse_dollars_to_cents(booth_fee) or 0,
            travel_cost_cents=_parse_dollars_to_cents(travel_cost) or 0,
            marketplace_id=mp_id,
        )
        msg = f"Saved marketplace {name.strip()}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/settings?msg={quote_plus(msg)}", status_code=303)


@router.post("/settings/marketplaces/{marketplace_id}/archive")
def settings_archive_marketplace(
    marketplace_id: int,
    ctx: Ctx = Depends(require("settings")),
):
    archive_marketplace(ctx.merchant_id, marketplace_id)
    return RedirectResponse(
        f"/settings?msg={quote_plus('Archived marketplace.')}", status_code=303
    )


@router.post("/settings/item-type")
def settings_item_type(
    sku_id: int = Form(...),
    item_type: str = Form("product"),
    ctx: Ctx = Depends(require("settings")),
):
    if item_type not in ITEM_TYPES:
        item_type = "product"
    set_item_type(ctx.merchant_id, sku_id, item_type)
    label = "excluded fee / non-inventory" if item_type == "fee" else "regular inventory product"
    return RedirectResponse(
        f"/settings?msg={quote_plus(f'Updated item classification to {label}.')}",
        status_code=303,
    )


@router.post("/settings/merchant/rename")
def settings_rename_merchant(
    name: str = Form(...),
    ctx: Ctx = Depends(require("settings")),
):
    try:
        rename_merchant(ctx.merchant_id, name)
        msg = f"Renamed workspace to {name.strip()}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/settings?msg={quote_plus(msg)}", status_code=303)


@router.post("/settings/members")
def settings_add_member(
    username: str = Form(...),
    display_name: str = Form(""),
    password: str = Form(""),
    role: str = Form("staff"),
    ctx: Ctx = Depends(require("settings")),
):
    try:
        clean_user = username.strip()
        existing = get_user_by_username(clean_user)
        if existing:
            uid = int(existing["id"])
            current_ids = {int(m["user_id"]) for m in list_members(ctx.merchant_id)}
            if uid in current_ids:
                update_member_role(ctx.merchant_id, uid, role, allow_zero_owners=False)
            else:
                add_member(ctx.merchant_id, uid, role)
        else:
            user = create_user(
                clean_user,
                password,
                display_name=display_name,
                is_admin=False,
            )
            uid = int(user["id"])
            add_member(ctx.merchant_id, uid, role)
        msg = f"Saved member {clean_user} ({role})."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/settings?msg={quote_plus(msg)}", status_code=303)


@router.post("/settings/members/role")
def settings_update_member_role(
    user_id: int = Form(...),
    role: str = Form(...),
    ctx: Ctx = Depends(require("settings")),
):
    try:
        update_member_role(ctx.merchant_id, user_id, role, allow_zero_owners=False)
        msg = f"Updated member role to {role}."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/settings?msg={quote_plus(msg)}", status_code=303)


@router.post("/settings/members/remove")
def settings_remove_member(
    user_id: int = Form(...),
    ctx: Ctx = Depends(require("settings")),
):
    try:
        remove_member(ctx.merchant_id, user_id)
        msg = "Removed member."
    except ValueError as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/settings?msg={quote_plus(msg)}", status_code=303)


@router.get("/zettle/connect")
def zettle_connect(request: Request, ctx: Ctx = Depends(require("settings"))):
    if not zettle.configured():
        raise HTTPException(400, "ZETTLE_CLIENT_ID / ZETTLE_CLIENT_SECRET are not set.")
    state = secrets.token_urlsafe(24)
    resp = RedirectResponse(zettle.build_authorize_url(_redirect_uri(request), state), 303)
    resp.set_cookie("zettle_state", state, max_age=600, httponly=True, samesite="lax",
                    secure=os.environ.get("INVENTORY_COOKIE_SECURE", "0") == "1")
    return resp


@router.get("/zettle/callback", name="zettle_callback")
def zettle_callback(request: Request, code: str = "", state: str = "", error: str = "",
                    ctx: Ctx = Depends(require("settings"))):
    expected = request.cookies.get("zettle_state", "")
    if error or not code or not expected or not secrets.compare_digest(state, expected):
        return RedirectResponse("/settings?msg=Zettle+connection+was+not+completed.", 303)
    try:
        tokens = zettle.exchange_code(code, _redirect_uri(request))
    except zettle.ZettleError:
        return RedirectResponse("/settings?msg=Zettle+rejected+the+authorization.", 303)
    sync.save_connection(ctx.merchant_id, tokens)
    resp = RedirectResponse("/settings?msg=Connected.+First+sync+running.", 303)
    resp.delete_cookie("zettle_state")
    asyncio.get_running_loop().run_in_executor(None, sync.sync_merchant, ctx.merchant_id)
    return resp


@router.post("/zettle/sync")
def zettle_sync_now(ctx: Ctx = Depends(require("settings"))):
    result = sync.sync_merchant(ctx.merchant_id)
    msg = (f"Sync failed: {result['error']}" if result["error"] else
           f"Synced {result['products']} products, {result['purchases']} new sales.")
    return RedirectResponse(f"/settings?msg={quote_plus(msg)}", 303)
