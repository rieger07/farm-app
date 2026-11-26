"""ntfy push notification helpers for Homestead Planner."""

from __future__ import annotations

import os

from homestead.database import get_text_setting, set_text_setting
from shared.crypto import decrypt_token, encrypt_token
from shared.ntfy import (
    DEFAULT_NTFY_URL,
    send_ntfy_message,
    send_ntfy_message_async,
)


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
    return send_ntfy_message(
        cfg["url"],
        cfg["topic"],
        title,
        message,
        token=cfg["token"],
        click_url=click_url,
        tags=tags,
        default_title="Homestead Planner",
    )


def send_ntfy_async(
    user_id: int,
    title: str,
    message: str,
    *,
    click_url: str = "",
    tags: str = "turkey,moneybag",
) -> None:
    """Fire-and-forget ntfy notification in a daemon thread so claims never block."""
    cfg = get_ntfy_config(user_id)
    if not cfg["configured"]:
        return
    send_ntfy_message_async(
        cfg["url"],
        cfg["topic"],
        title,
        message,
        token=cfg["token"],
        click_url=click_url,
        tags=tags,
        default_title="Homestead Planner",
    )
