"""Unit tests for Windows system tray controller and helpers."""

from __future__ import annotations

from pathlib import Path
import sys

from PIL import Image

from remote_screen.tray import TrayController, TrayState, copy_to_clipboard, create_icon_image


class _FakeIcon:
    def __init__(self, name: str, icon: Image.Image, title: str, menu=None) -> None:
        self.name = name
        self.icon = icon
        self.title = title
        self.menu = menu
        self.visible = False
        self.stopped = False
        self.menu_updates = 0
        self.notifications: list[tuple[str, str]] = []

    def update_menu(self) -> None:
        self.menu_updates += 1

    def notify(self, message: str, title: str = "") -> None:
        self.notifications.append((title, message))

    def stop(self) -> None:
        self.stopped = True

    def run(self, setup=None) -> None:
        if setup is not None:
            setup(self)


def test_create_icon_image_distinct_states() -> None:
    starting = create_icon_image(ready=False, size=64)
    ready = create_icon_image(ready=True, size=64)

    assert starting.size == (64, 64)
    assert starting.mode == "RGBA"
    assert ready.size == (64, 64)
    assert ready.mode == "RGBA"
    assert starting.tobytes() != ready.tobytes()


def test_tray_controller_state_and_menu_transitions(tmp_path: Path) -> None:
    quit_called = False

    def on_quit() -> None:
        nonlocal quit_called
        quit_called = True

    state = TrayState(
        pin="246810",
        pin_is_random=False,
        log_path=tmp_path / "remote.log",
        app_name="RemoteScreen.exe",
        no_telegram=True,
    )
    ctrl = TrayController(state=state, on_quit=on_quit, icon_factory=_FakeIcon)

    assert state.ready is False
    assert state.url is None
    assert "啟動中" in ctrl.icon.title

    items = list(ctrl.icon.menu.items)
    assert items[0].text == "RemoteScreen - 啟動中..."
    assert items[2].enabled is False  # copy URL disabled before ready
    assert items[4].enabled is False  # resend telegram disabled when no_telegram=True

    ctrl.mark_ready("https://demo.trycloudflare.com")
    assert state.ready is True
    assert state.url == "https://demo.trycloudflare.com"
    assert "運行中" in ctrl.icon.title
    assert ctrl.icon.menu_updates == 1
    assert len(ctrl.icon.notifications) == 1
    assert "246810" in ctrl.icon.notifications[0][1]
    assert items[0].text == "RemoteScreen - 運行中"
    assert items[2].enabled is True

    ctrl._handle_quit()
    assert quit_called is True
    ctrl.stop()
    assert ctrl.icon.stopped is True


def test_copy_to_clipboard_does_not_crash() -> None:
    result = copy_to_clipboard("https://test.trycloudflare.com")
    assert isinstance(result, bool)
    if sys.platform != "win32":
        assert result is False
