"""Windows system tray icon and native Win32 helpers for RemoteScreen."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import threading
from typing import Any, Callable

from PIL import Image, ImageDraw
import pystray

from remote_screen.notify import notify_startup


_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002
_MB_OK = 0x00000000
_MB_ICONERROR = 0x00000010
_MB_ICONINFORMATION = 0x00000040
_MB_SETFOREGROUND = 0x00010000


def create_icon_image(ready: bool = False, size: int = 64) -> Image.Image:
    """Create a crisp RGBA tray icon indicating starting (amber) or ready (green)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Outer monitor frame
    pad = max(3, size // 16)
    monitor_bottom = int(size * 0.72)
    draw.rounded_rectangle(
        (pad, pad, size - pad, monitor_bottom),
        radius=max(4, size // 10),
        fill=(24, 32, 46, 255),
        outline=(88, 166, 255, 255),
        width=max(2, size // 16),
    )

    # Inner screen area
    inner_pad = pad + max(4, size // 10)
    draw.rectangle(
        (inner_pad, inner_pad, size - inner_pad, monitor_bottom - max(4, size // 10)),
        fill=(34, 48, 70, 255),
    )

    # Monitor stand
    neck_w = max(4, size // 6)
    cx = size // 2
    draw.rectangle(
        (cx - neck_w // 2, monitor_bottom, cx + neck_w // 2, int(size * 0.86)),
        fill=(139, 148, 158, 255),
    )
    base_w = int(size * 0.44)
    draw.rounded_rectangle(
        (cx - base_w // 2, int(size * 0.84), cx + base_w // 2, size - pad),
        radius=max(2, size // 24),
        fill=(139, 148, 158, 255),
    )

    # Status indicator badge at bottom-right
    dot_color = (63, 185, 80, 255) if ready else (210, 153, 34, 255)
    r = max(6, size // 5)
    dx = size - pad - r
    dy = monitor_bottom - r // 2
    draw.ellipse((dx - r - 2, dy - r - 2, dx + r + 2, dy + r + 2), fill=(13, 17, 23, 255))
    draw.ellipse((dx - r, dy - r, dx + r, dy + r), fill=dot_color)
    return img


def copy_to_clipboard(text: str) -> bool:
    """Copy Unicode text to the Windows clipboard using 64-bit-safe ctypes."""
    if sys.platform != "win32":
        return False

    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL

    raw = (text + "\0").encode("utf-16-le")
    hmem = None
    opened = False
    try:
        if not user32.OpenClipboard(None):
            return False
        opened = True
        if not user32.EmptyClipboard():
            return False

        hmem = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(raw))
        if not hmem:
            return False

        ptr = kernel32.GlobalLock(hmem)
        if not ptr:
            kernel32.GlobalFree(hmem)
            hmem = None
            return False

        try:
            ctypes.memmove(ptr, raw, len(raw))
        finally:
            kernel32.GlobalUnlock(hmem)

        if not user32.SetClipboardData(_CF_UNICODETEXT, hmem):
            kernel32.GlobalFree(hmem)
            hmem = None
            return False

        # Clipboard now owns hmem.
        hmem = None
        return True
    except Exception:
        if hmem:
            try:
                kernel32.GlobalFree(hmem)
            except Exception:
                pass
        return False
    finally:
        if opened:
            try:
                user32.CloseClipboard()
            except Exception:
                pass


def show_message_box(title: str, message: str, is_error: bool = False) -> None:
    """Display a native Windows message box without opening a console."""
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
        user32.MessageBoxW.restype = ctypes.c_int
        flags = _MB_OK | _MB_SETFOREGROUND | (_MB_ICONERROR if is_error else _MB_ICONINFORMATION)
        user32.MessageBoxW(None, message, title, flags)
    except Exception:
        pass


@dataclass
class TrayState:
    """Mutable status shared between the background server and tray menu."""

    pin: str
    pin_is_random: bool
    log_path: Path
    app_name: str
    no_telegram: bool = False
    ready: bool = False
    url: str | None = None
    error_message: str | None = None


class TrayController:
    """Manages the Windows notification-area icon and user actions."""

    def __init__(
        self,
        state: TrayState,
        on_quit: Callable[[], None],
        icon_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.state = state
        self._on_quit = on_quit
        factory = icon_factory or pystray.Icon
        self.icon = factory(
            "RemoteScreen",
            create_icon_image(ready=False),
            "RemoteScreen - 啟動中...",
            menu=self._build_menu(),
        )

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(
                lambda _item: "RemoteScreen - 運行中" if self.state.ready else "RemoteScreen - 啟動中...",
                None,
                enabled=False,
            ),
            pystray.MenuItem(
                "顯示連線資訊 (網址 / PIN)",
                self._handle_show_info,
                default=True,
            ),
            pystray.MenuItem(
                "複製連線網址",
                self._handle_copy_url,
                enabled=lambda _item: bool(self.state.url),
            ),
            pystray.MenuItem(
                "複製 PIN",
                self._handle_copy_pin,
            ),
            pystray.MenuItem(
                "重發 Telegram 通知",
                self._handle_resend_telegram,
                enabled=lambda _item: bool(self.state.url) and not self.state.no_telegram,
            ),
            pystray.MenuItem(
                "開啟記錄檔",
                self._handle_open_log,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "停止並退出",
                self._handle_quit,
            ),
        )

    def _safe_notify(self, message: str, title: str = "RemoteScreen") -> None:
        try:
            self.icon.notify(message, title=title)
        except Exception:
            pass

    def _handle_show_info(self, _icon: Any = None, _item: Any = None) -> None:
        pin_note = "(隨機產生)" if self.state.pin_is_random else "(固定設定)"
        url_line = self.state.url or "隧道建立中，請稍候..."
        lines = [
            f"連線網址：{url_line}",
            f"存取 PIN：{self.state.pin} {pin_note}",
            f"記錄檔：{self.state.log_path}",
        ]
        if not self.state.no_telegram and self.state.url:
            lines.append("\n提示：已發送連線網址與 PIN 到 Telegram。")
        threading.Thread(
            target=show_message_box,
            args=("RemoteScreen 連線資訊", "\n".join(lines), False),
            daemon=True,
        ).start()

    def _handle_copy_url(self, _icon: Any = None, _item: Any = None) -> None:
        if not self.state.url:
            return
        if copy_to_clipboard(self.state.url):
            self._safe_notify(f"已複製連線網址：\n{self.state.url}")

    def _handle_copy_pin(self, _icon: Any = None, _item: Any = None) -> None:
        if copy_to_clipboard(self.state.pin):
            self._safe_notify(f"已複製存取 PIN：{self.state.pin}")

    def _handle_resend_telegram(self, _icon: Any = None, _item: Any = None) -> None:
        if not self.state.url or self.state.no_telegram:
            return
        url = self.state.url
        pin = self.state.pin
        app_name = self.state.app_name

        def _send() -> None:
            ok = notify_startup(url, pin, app_name, include_pin=self.state.pin_is_random)
            if ok:
                self._safe_notify("已重發連線網址與 PIN 到 Telegram。")
            else:
                self._safe_notify("Telegram 發送失敗，請確認 Bot Token、Chat ID 環境變數與網路。")

        threading.Thread(target=_send, daemon=True).start()

    def _handle_open_log(self, _icon: Any = None, _item: Any = None) -> None:
        try:
            if not self.state.log_path.exists():
                self.state.log_path.parent.mkdir(parents=True, exist_ok=True)
                self.state.log_path.touch()
            if hasattr(os, "startfile"):
                os.startfile(str(self.state.log_path))  # type: ignore[attr-defined]
        except Exception:
            pass

    def _handle_quit(self, _icon: Any = None, _item: Any = None) -> None:
        self._on_quit()

    def mark_ready(self, url: str) -> None:
        """Update tray state, icon, title, and balloon notification once tunnel is live."""
        self.state.ready = True
        self.state.url = url
        try:
            self.icon.icon = create_icon_image(ready=True)
            self.icon.title = "RemoteScreen - 運行中"
            self.icon.update_menu()
        except Exception:
            pass
        self._safe_notify(f"已就緒！PIN：{self.state.pin}\n{url}")

    def notify_error(self, message: str) -> None:
        """Stop the tray icon and display an error dialog."""
        self.state.error_message = message
        self.stop()
        show_message_box("RemoteScreen 啟動失敗", message, is_error=True)

    def stop(self) -> None:
        """Stop the tray icon loop safely."""
        try:
            self.icon.stop()
        except Exception:
            pass

    def run(self, setup_fn: Callable[[Any], None] | None = None) -> None:
        """Run the system tray event loop on the current thread."""
        self.icon.run(setup=setup_fn)
