"""Unit tests for Telegram notification module."""

from __future__ import annotations

import urllib.error
import urllib.parse

import pytest

from remote_screen.notify import format_elapsed, notify_login, notify_shutdown, notify_startup, send_telegram


class _DummyResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_send_telegram_skips_when_token_or_chat_id_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REMOTE_SCREEN_TG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("REMOTE_SCREEN_TG_CHAT_ID", raising=False)
    monkeypatch.delenv("TG_CHAT_ID", raising=False)
    called = False

    def opener(*args, **kwargs):
        nonlocal called
        called = True
        return _DummyResponse()

    assert send_telegram("hello", opener=opener) is False
    assert called is False

    # Token present but chat ID missing -> still skips
    monkeypatch.setenv("TG_BOT_TOKEN", "12345:FAKE_TOKEN")
    assert send_telegram("hello", opener=opener) is False
    assert called is False


def test_notify_startup_and_shutdown_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REMOTE_SCREEN_TG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("REMOTE_SCREEN_TG_CHAT_ID", raising=False)
    monkeypatch.setenv("TG_BOT_TOKEN", "12345:FAKE_TOKEN")
    monkeypatch.setenv("TG_CHAT_ID", "999999999")
    captured: list[tuple[str, dict[str, list[str]], float]] = []

    def fake_opener(req, timeout=None):
        body = urllib.parse.parse_qs(req.data.decode("utf-8"))
        captured.append((req.full_url, body, timeout))
        return _DummyResponse()

    assert notify_startup("https://abc.trycloudflare.com", "246810", opener=fake_opener) is True
    url, body, timeout = captured[0]
    assert url == "https://api.telegram.org/bot12345:FAKE_TOKEN/sendMessage"
    assert body["chat_id"] == ["999999999"]
    assert "https://abc.trycloudflare.com" in body["text"][0]
    assert "246810" in body["text"][0]
    assert timeout == 5.0

    assert notify_shutdown("RemoteScreen.exe", "已停止", 125.0, "連線 2 次", opener=fake_opener) is True
    _, body2, timeout2 = captured[1]
    assert "RemoteScreen.exe" in body2["text"][0]
    assert "已停止" in body2["text"][0]
    assert "2m 05s" in body2["text"][0]
    assert "連線 2 次" in body2["text"][0]
    assert timeout2 == 5.0


def test_send_telegram_swallows_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_BOT_TOKEN", "12345:FAKE_TOKEN")
    monkeypatch.setenv("TG_CHAT_ID", "999999999")

    def failing_opener(*args, **kwargs):
        raise urllib.error.URLError("timeout")

    assert send_telegram("hello", opener=failing_opener) is False
    assert format_elapsed(12) == "12s"
    assert format_elapsed(3910) == "1h 05m 10s"


def test_remote_screen_bot_token_and_chat_id_take_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TG_BOT_TOKEN", "old:TOKEN")
    monkeypatch.setenv("TG_CHAT_ID", "222222222")
    monkeypatch.setenv("REMOTE_SCREEN_TG_BOT_TOKEN", "new:TOKEN")
    monkeypatch.setenv("REMOTE_SCREEN_TG_CHAT_ID", "111111111")
    captured: list[tuple[str, dict[str, list[str]]]] = []

    def fake_opener(req, timeout=None):
        body = urllib.parse.parse_qs(req.data.decode("utf-8"))
        captured.append((req.full_url, body))
        return _DummyResponse()

    assert send_telegram("hi", opener=fake_opener) is True
    assert captured == [
        (
            "https://api.telegram.org/botnew:TOKEN/sendMessage",
            {"chat_id": ["111111111"], "text": ["hi"]},
        )
    ]


def test_notify_startup_hides_fixed_pin_and_notify_login_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REMOTE_SCREEN_TG_BOT_TOKEN", "new:TOKEN")
    monkeypatch.setenv("REMOTE_SCREEN_TG_CHAT_ID", "111111111")
    sent_texts: list[str] = []

    def fake_opener(req, timeout=None):
        body = urllib.parse.parse_qs(req.data.decode("utf-8"))
        sent_texts.append(body["text"][0])
        return _DummyResponse()

    assert (
        notify_startup(
            "https://abc.trycloudflare.com",
            "SecretPin99",
            include_pin=False,
            opener=fake_opener,
        )
        is True
    )
    assert "SecretPin99" not in sent_texts[0]
    assert "固定密碼" in sent_texts[0]

    assert notify_login("203.0.113.9", opener=fake_opener) is True
    assert "新裝置已登入" in sent_texts[1]
    assert "203.0.113.9" in sent_texts[1]
