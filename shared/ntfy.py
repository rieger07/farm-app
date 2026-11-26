"""Shared ntfy HTTP push notification transport helpers."""

from __future__ import annotations

import threading

import httpx

DEFAULT_NTFY_URL = "https://ntfy.sh"


def send_ntfy_message(
    url: str,
    topic: str,
    title: str,
    message: str,
    *,
    token: str = "",
    click_url: str = "",
    tags: str = "",
    default_title: str = "Ironclad Systems",
) -> tuple[bool, str]:
    """Post a push notification to an ntfy server. Returns ``(ok, detail)``."""
    clean_url = (url or DEFAULT_NTFY_URL).strip().rstrip("/") or DEFAULT_NTFY_URL
    clean_topic = (topic or "").strip().lstrip("/")
    if not clean_topic:
        return False, "ntfy topic is not configured."
    endpoint = f"{clean_url}/{clean_topic}"
    safe_title = (
        (title or default_title)
        .replace("\u00b7", "-")
        .encode("ascii", "ignore")
        .decode("ascii")
        .strip()[:120]
        or default_title
    )
    headers = {"Title": safe_title}
    if tags:
        headers["Tags"] = tags
    if click_url:
        headers["Click"] = click_url
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(endpoint, content=message.encode("utf-8"), headers=headers)
            if resp.status_code >= 400:
                return False, f"ntfy returned HTTP {resp.status_code}"
        return True, "Sent"
    except Exception as exc:
        return False, str(exc)


def send_ntfy_message_async(
    url: str,
    topic: str,
    title: str,
    message: str,
    *,
    token: str = "",
    click_url: str = "",
    tags: str = "",
    default_title: str = "Ironclad Systems",
) -> None:
    """Fire-and-forget ntfy notification in a daemon thread so web handlers never block."""
    t = threading.Thread(
        target=send_ntfy_message,
        args=(url, topic, title, message),
        kwargs={
            "token": token,
            "click_url": click_url,
            "tags": tags,
            "default_title": default_title,
        },
        daemon=True,
    )
    t.start()
