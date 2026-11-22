"""Telegram notification helper using Python stdlib only."""

from __future__ import annotations

import os
from typing import Any, Callable
import urllib.parse
import urllib.request

from remote_screen.tunnel import build_ssl_context


def format_elapsed(seconds: float) -> str:
    """Format elapsed seconds into a compact human-readable string."""
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes > 0:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def send_telegram(
    message: str,
    timeout: float = 5.0,
    opener: Callable[..., Any] | None = None,
) -> bool:
    """Send a Telegram message via Bot API if bot token and chat ID env vars are set.

    Checks REMOTE_SCREEN_TG_BOT_TOKEN / REMOTE_SCREEN_TG_CHAT_ID first,
    then falls back to TG_BOT_TOKEN / TG_CHAT_ID.
    Never raises if env vars are missing or the network call fails.
    """
    token = (
        os.environ.get("REMOTE_SCREEN_TG_BOT_TOKEN", "").strip()
        or os.environ.get("TG_BOT_TOKEN", "").strip()
    )
    chat_id = (
        os.environ.get("REMOTE_SCREEN_TG_CHAT_ID", "").strip()
        or os.environ.get("TG_CHAT_ID", "").strip()
    )
    if not token or not chat_id:
        return False

    try:
        endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode("utf-8")
        req = urllib.request.Request(endpoint, data=payload, method="POST")
        if opener is None:
            resp_ctx = urllib.request.urlopen(req, timeout=timeout, context=build_ssl_context())
        else:
            resp_ctx = opener(req, timeout=timeout)
        with resp_ctx:
            pass
        return True
    except Exception:
        return False


def notify_startup(
    url: str,
    pin: str,
    app_name: str = "RemoteScreen.exe",
    opener: Callable[..., Any] | None = None,
) -> bool:
    """Send startup connection link and PIN to Telegram."""
    message = (
        f"[RemoteScreen] 已啟動\n"
        f"程式：{app_name}\n"
        f"連線網址：{url}\n"
        f"存取 PIN：{pin}"
    )
    return send_telegram(message, timeout=5.0, opener=opener)


def notify_shutdown(
    app_name: str,
    status: str,
    elapsed_seconds: float,
    summary: str,
    opener: Callable[..., Any] | None = None,
) -> bool:
    """Send completion or error summary notification to Telegram."""
    duration = format_elapsed(elapsed_seconds)
    message = (
        f"[RemoteScreen] {status}\n"
        f"程式：{app_name}\n"
        f"運行時間：{duration}\n"
        f"摘要：{summary}"
    )
    return send_telegram(message, timeout=5.0, opener=opener)
