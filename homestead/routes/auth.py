"""Public pages, authentication, session switching, and health check routes."""

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from homestead.db.core import (
    authenticate_user,
    count_users,
    create_user,
    delete_user_session,
    get_user_by_session_token,
    is_public_registration_enabled,
)
from homestead.web import (
    COOKIE,
    MERCHANT_COOKIE,
    _cookie_secure,
    _set_session_cookie,
    current_user,
    templates,
)
from shared.web import safe_redirect

router = APIRouter()


@router.post("/switch-merchant")
def switch_merchant_submit(
    request: Request,
    merchant_id: int = Form(...),
    next_url: str = Form("/"),
    user: dict = Depends(current_user),
):
    allowed = {int(m["merchant_id"]) for m in user.get("merchants", [])}
    resp = safe_redirect(next_url, fallback="/")
    if int(merchant_id) in allowed:
        resp.set_cookie(
            MERCHANT_COOKIE,
            str(int(merchant_id)),
            httponly=True,
            samesite="lax",
            secure=_cookie_secure(request),
            max_age=30 * 86400,
        )
    return resp


@router.get("/healthz")
def healthz():
    return {"ok": True}


@router.get("/home")
def public_home_page(request: Request):
    return templates.TemplateResponse(request, "home.html", {"request": request})


@router.get("/privacy")
def privacy_page(request: Request):
    return templates.TemplateResponse(request, "privacy.html", {"request": request})


@router.get("/terms")
def terms_page(request: Request):
    return templates.TemplateResponse(request, "terms.html", {"request": request})


@router.get("/login")
def login_page(request: Request, msg: str = ""):
    tok = request.cookies.get(COOKIE)
    if tok and get_user_by_session_token(tok):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html",
        {
            "request": request,
            "msg": msg,
            "first_setup": count_users() == 0,
            "reg_open": is_public_registration_enabled(),
        },
    )


@router.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    user = authenticate_user(username, password)
    if not user:
        return RedirectResponse("/login?msg=" + quote_plus("Invalid username or password."), status_code=303)
    resp = RedirectResponse("/", status_code=303)
    _set_session_cookie(resp, request, user["id"])
    return resp


@router.post("/register")
def register_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(""),
):
    if count_users() > 0 and not is_public_registration_enabled():
        return RedirectResponse("/login?msg=" + quote_plus("Registration is closed."), status_code=303)
    try:
        user = create_user(username, password, display_name=display_name)
    except ValueError as exc:
        return RedirectResponse("/login?msg=" + quote_plus(str(exc)), status_code=303)
    resp = RedirectResponse("/", status_code=303)
    _set_session_cookie(resp, request, user["id"])
    return resp


@router.post("/logout")
def logout_submit(request: Request):
    tok = request.cookies.get(COOKIE)
    if tok:
        delete_user_session(tok)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp
