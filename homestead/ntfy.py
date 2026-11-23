"""ntfy push notification helpers for Homestead Planner."""

from __future__ import annotations

import os
import threading

import httpx

from homestead.database import get_text_setting, set_text_setting
from inventory.crypto import decrypt_token, encrypt_token

DEFAULT_NTFY_URL = "https://ntfy.sh"


def get_ntfy_config(user_id: int) -> dict:
    url = (
        get_text_setting("ntfy_url", "", user_id=user_id)
        or os.environ.get("NTFY_URL", "").strip()
        or DEFAULT_NTFY_URL
    )
    topic = (
        get_text_setting("ntfy_topic", "", user_id=user_id)
        or os.environ.get("NTFY_TOPIC", "").strip()
    )
    token_enc = get_text_setting("ntfy_token_enc", "", user_id=user_id)
    token = (
        decrypt_token(token_enc)
        if token_enc
        else os.environ.get("NTFY_TOKEN", "").strip()
    )
    return {
        "url": url.rstrip("/"),
        "topic": topic.strip().lstrip("/"),
        "token": token or "",
        "has_token": bool(token),
        "configured": bool(topic.strip()),
    }


def save_ntfy_config(
    user_id: int, url: str, topic: str, token: str | None = None, clear_token: bool = False
) -> None:
    clean_url = (url or DEFAULT_NTFY_URL).strip().rstrip("/") or DEFAULT_NTFY_URL
    clean_topic = (topic or "").strip().lstrip("/")
    set_text_setting("ntfy_url", clean_url, user_id=user_id)
    set_text_setting("ntfy_topic", clean_topic, user_id=user_id)
    if clear_token:
        set_text_setting("ntfy_token_enc", "", user_id=user_id)
    elif token is not None and token.strip():
        set_text_setting("ntfy_token_enc", encrypt_token(token.strip()) or "", user_id=user_id)


def send_ntfy_notification(
    user_id: int,
    title: str,
    message: str,
    *,
    click_url: str = "",
    tags: str = "turkey,moneybag",
) -> tuple[bool, str]:
    """Send a push notification via ntfy. Returns (ok, detail)."""
    cfg = get_ntfy_config(user_id)
    if not cfg["configured"]:
        return False, "ntfy topic is not configured."
    endpoint = f"{cfg['url']}/{cfg['topic']}"
    headers = {"Title": title[:120], "Tags": tags}
    if click_url:
        headers["Click"] = click_url
    if cfg["token"]:
        headers["Authorization"] = f"Bearer {cfg['token']}"
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(endpoint, content=message.encode("utf-8"), headers=headers)
            if resp.status_code >= 400:
                return False, f"ntfy returned HTTP {resp.status_code}"
        return True, "Sent"
    except Exception as exc:
        return False, str(exc)


def send_ntfy_async(
    user_id: int,
    title: str,
    message: str,
    *,
    click_url: str = "",
    tags: str = "turkey,moneybag",
) -> None:
    """Fire-and-forget ntfy notification in a daemon thread so claims never block."""
    t = threading.Thread(
        target=send_ntfy_notification,
        args=(user_id, title, message),
        kwargs={"click_url": click_url, "tags": tags},
        daemon=True,
    )
    t.start()
