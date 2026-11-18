"""Google Drive OAuth (drive.file scope) & receipt upload helpers."""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from homestead.database import (
    get_google_drive_connection,
    save_google_drive_connection,
    update_google_drive_tokens,
)
from inventory.crypto import decrypt_token, encrypt_token

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
GOOGLE_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
GOOGLE_DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/drive.file "
    "https://www.googleapis.com/auth/userinfo.email"
)
DEFAULT_FOLDER_NAME = "Homestead Planner Receipts"
MAX_RECEIPT_BYTES = 15 * 1024 * 1024


class GoogleDriveError(RuntimeError):
    """Raised when Google Drive OAuth or upload fails."""


def _client_id() -> str:
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()


def is_gdrive_configured() -> bool:
    return bool(_client_id() and _client_secret())


def get_redirect_uri(base_url: str = "") -> str:
    env_uri = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()
    if env_uri:
        return env_uri
    return f"{base_url.rstrip('/')}/settings/google-drive/callback"


def build_auth_url(state: str, redirect_uri: str) -> str:
    if not is_gdrive_configured():
        raise GoogleDriveError("Google Drive OAuth is not configured on this server.")
    params = {
        "client_id": _client_id(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GOOGLE_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def exchange_code_for_tokens(code: str, redirect_uri: str) -> dict:
    if not is_gdrive_configured():
        raise GoogleDriveError("Google Drive OAuth is not configured on this server.")
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": _client_id(),
                "client_secret": _client_secret(),
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if resp.status_code >= 400:
            raise GoogleDriveError(f"Google token exchange failed ({resp.status_code}).")
        data = resp.json()
        access_token = data.get("access_token", "")
        email = ""
        if access_token:
            u_resp = client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if u_resp.status_code == 200:
                email = str(u_resp.json().get("email") or "")
        data["email"] = email
        return data


def _expires_at_iso(expires_in: int | float | None) -> str:
    secs = max(60, int(expires_in or 3600))
    return (datetime.now(timezone.utc) + timedelta(seconds=secs)).isoformat()


def refresh_access_token(refresh_token: str) -> dict:
    if not is_gdrive_configured():
        raise GoogleDriveError("Google Drive OAuth is not configured on this server.")
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": _client_id(),
                "client_secret": _client_secret(),
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if resp.status_code >= 400:
            raise GoogleDriveError(f"Google token refresh failed ({resp.status_code}).")
        return resp.json()


def get_valid_access_token(user_id: int) -> tuple[str, dict]:
    conn = get_google_drive_connection(user_id)
    if not conn:
        raise GoogleDriveError("Google Drive is not connected.")
    access_token = decrypt_token(conn.get("access_token_enc"))
    refresh_token = decrypt_token(conn.get("refresh_token_enc"))
    expires_at_raw = conn.get("token_expires_at") or ""
    needs_refresh = not access_token
    if expires_at_raw:
        try:
            exp = datetime.fromisoformat(expires_at_raw)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp <= datetime.now(timezone.utc) + timedelta(seconds=60):
                needs_refresh = True
        except ValueError:
            needs_refresh = True
    if needs_refresh:
        if not refresh_token:
            raise GoogleDriveError("Google Drive session expired; reconnect in Settings.")
        refreshed = refresh_access_token(refresh_token)
        access_token = refreshed.get("access_token") or ""
        if not access_token:
            raise GoogleDriveError("Google Drive refresh returned no access token.")
        new_exp = _expires_at_iso(refreshed.get("expires_in"))
        update_google_drive_tokens(
            user_id=user_id,
            access_token_enc=encrypt_token(access_token),
            token_expires_at=new_exp,
        )
    return access_token, conn


def ensure_drive_folder(
    client: httpx.Client,
    access_token: str,
    folder_name: str,
    folder_id: str | None = None,
) -> str:
    headers = {"Authorization": f"Bearer {access_token}"}
    target_name = (folder_name or DEFAULT_FOLDER_NAME).strip() or DEFAULT_FOLDER_NAME
    if folder_id:
        check = client.get(
            f"{GOOGLE_DRIVE_FILES_URL}/{folder_id}",
            params={"fields": "id,name,trashed"},
            headers=headers,
        )
        if check.status_code == 200 and not check.json().get("trashed"):
            return folder_id

    safe_name = target_name.replace("'", "\\'")
    search = client.get(
        GOOGLE_DRIVE_FILES_URL,
        params={
            "q": (
                f"name='{safe_name}' and "
                "mimeType='application/vnd.google-apps.folder' and trashed=false"
            ),
            "fields": "files(id,name)",
            "pageSize": 1,
        },
        headers=headers,
    )
    if search.status_code == 200:
        files = search.json().get("files") or []
        if files:
            return str(files[0]["id"])

    create = client.post(
        GOOGLE_DRIVE_FILES_URL,
        params={"fields": "id"},
        headers={**headers, "Content-Type": "application/json"},
        json={
            "name": target_name,
            "mimeType": "application/vnd.google-apps.folder",
        },
    )
    if create.status_code >= 400:
        raise GoogleDriveError(f"Failed to create Drive folder ({create.status_code}).")
    return str(create.json()["id"])


def _clean_receipt_filename(original_name: str, receipt_date: str = "", label: str = "") -> str:
    ext = os.path.splitext(original_name or "receipt.jpg")[1].lower()
    if not ext or len(ext) > 8:
        ext = ".jpg"
    parts = []
    if receipt_date:
        parts.append(re.sub(r"[^0-9-]", "", receipt_date)[:10])
    if label:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", label.strip()).strip("-")[:40]
        if slug:
            parts.append(slug)
    if not parts:
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.splitext(original_name or "receipt")[0]).strip("-")
        parts.append(stem[:40] or "receipt")
    return "_".join(parts) + ext


def upload_receipt_to_drive(
    user_id: int,
    filename: str,
    content_type: str,
    data: bytes,
    *,
    receipt_date: str = "",
    label: str = "",
) -> tuple[str, str]:
    """Upload receipt bytes to the user's Google Drive folder and return (file_id, webViewLink)."""
    if not data:
        raise GoogleDriveError("Receipt file is empty.")
    if len(data) > MAX_RECEIPT_BYTES:
        raise GoogleDriveError("Receipt file exceeds 15 MB limit.")

    access_token, conn = get_valid_access_token(user_id)
    folder_name = conn.get("folder_name") or DEFAULT_FOLDER_NAME
    existing_folder_id = conn.get("folder_id") or None

    with httpx.Client(timeout=30.0) as client:
        folder_id = ensure_drive_folder(client, access_token, folder_name, existing_folder_id)
        if folder_id != existing_folder_id:
            save_google_drive_connection(
                user_id=user_id,
                google_email=conn.get("google_email") or "",
                access_token_enc=conn.get("access_token_enc") or "",
                refresh_token_enc=conn.get("refresh_token_enc") or "",
                token_expires_at=conn.get("token_expires_at") or "",
                folder_id=folder_id,
                folder_name=folder_name,
            )

        clean_name = _clean_receipt_filename(filename, receipt_date=receipt_date, label=label)
        boundary = f"homestead-{secrets.token_hex(12)}"
        meta_bytes = json.dumps({"name": clean_name, "parents": [folder_id]}).encode("utf-8")
        mime = (content_type or "application/octet-stream").split(";")[0].strip()
        body = (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        ).encode("utf-8") + meta_bytes + (
            f"\r\n--{boundary}\r\n"
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("utf-8")

        resp = client.post(
            GOOGLE_DRIVE_UPLOAD_URL,
            params={"uploadType": "multipart", "fields": "id,webViewLink"},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": f"multipart/related; boundary={boundary}",
            },
            content=body,
        )
        if resp.status_code >= 400:
            raise GoogleDriveError(f"Google Drive upload failed ({resp.status_code}).")
        payload = resp.json()
        file_id = str(payload.get("id") or "")
        view_url = str(payload.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view")
        return file_id, view_url
