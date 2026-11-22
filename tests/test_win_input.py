"""Unit tests for Win32 SendInput structures and InputController."""

from __future__ import annotations

import ctypes
import sys

import pytest

from remote_screen.win_input import (
    INPUT,
    INPUT_KEYBOARD,
    INPUT_MOUSE,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP,
    MOUSEEVENTF_WHEEL,
    VK_MAP,
    InputController,
    norm_to_absolute,
)


def _recorder() -> tuple[list[list[INPUT]], InputController]:
    batches: list[list[INPUT]] = []

    def fake_send(inputs: list[INPUT]) -> int:
        batches.append(list(inputs))
        return len(inputs)

    return batches, InputController(send_fn=fake_send)


def test_input_struct_size_on_64bit_windows() -> None:
    if sys.platform == "win32" and ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(INPUT) == 40


def test_norm_to_absolute_clamping() -> None:
    assert norm_to_absolute(-0.5, 1.5) == (0, 65535)
    assert norm_to_absolute(0.0, 0.0) == (0, 0)
    assert norm_to_absolute(0.5, 0.5) == (32768, 32768)
    assert norm_to_absolute(1.0, 1.0) == (65535, 65535)


def test_mouse_click_variants_and_drag() -> None:
    batches, ctrl = _recorder()

    ctrl.click(0.5, 0.5, button="left")
    assert len(batches) == 1
    assert [i.type for i in batches[0]] == [INPUT_MOUSE, INPUT_MOUSE, INPUT_MOUSE]
    assert batches[0][1].mi.dwFlags == (MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_LEFTDOWN)
    assert batches[0][2].mi.dwFlags == (MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_LEFTUP)

    ctrl.click(0.25, 0.75, button="right")
    assert batches[1][1].mi.dwFlags == (MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_RIGHTDOWN)
    assert batches[1][2].mi.dwFlags == (MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_RIGHTUP)

    ctrl.double_click(0.5, 0.5)
    assert len(batches[2]) == 5

    ctrl.mouse_down(0.1, 0.2, button="left")
    ctrl.mouse_up(0.8, 0.9, button="left")
    assert batches[3][1].mi.dwFlags & MOUSEEVENTF_LEFTDOWN
    assert batches[4][0].mi.dwFlags & MOUSEEVENTF_LEFTUP


def test_negative_scroll_wheel_encoding() -> None:
    batches, ctrl = _recorder()
    ctrl.scroll(0.5, 0.5, -3)
    assert len(batches) == 1
    wheel = batches[0][1]
    assert wheel.mi.dwFlags == MOUSEEVENTF_WHEEL
    assert wheel.mi.mouseData == (-360 & 0xFFFFFFFF)


def test_unicode_text_chinese_and_newline() -> None:
    batches, ctrl = _recorder()
    ctrl.type_text("你好\nA")
    events = batches[0]
    assert all(ev.type == INPUT_KEYBOARD for ev in events)
    # 2 events for 你, 2 for 好, 2 for VK_RETURN, 2 for A => 8 events
    assert len(events) == 8

    assert events[0].ki.wScan == ord("你")
    assert events[0].ki.dwFlags == KEYEVENTF_UNICODE
    assert events[1].ki.dwFlags == (KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)

    assert events[2].ki.wScan == ord("好")
    assert events[4].ki.wVk == VK_MAP["enter"]
    assert events[4].ki.dwFlags == 0
    assert events[5].ki.wVk == VK_MAP["enter"]
    assert events[5].ki.dwFlags == KEYEVENTF_KEYUP

    assert events[6].ki.wScan == ord("A")


def test_press_combo_and_validation() -> None:
    batches, ctrl = _recorder()
    ctrl.press_combo(["ctrl", "v"])
    events = batches[0]
    assert [(e.ki.wVk, e.ki.dwFlags) for e in events] == [
        (VK_MAP["ctrl"], 0),
        (VK_MAP["v"], 0),
        (VK_MAP["v"], KEYEVENTF_KEYUP),
        (VK_MAP["ctrl"], KEYEVENTF_KEYUP),
    ]

    ctrl.press_combo(["win"])
    assert batches[1][0].ki.dwFlags == KEYEVENTF_EXTENDEDKEY
    assert batches[1][1].ki.dwFlags == (KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP)

    with pytest.raises(ValueError):
        ctrl.press_combo(["nonexistent_key"])
    with pytest.raises(ValueError):
        ctrl.press_combo([])
    with pytest.raises(ValueError):
        ctrl.dispatch({"type": "click", "x": float("nan"), "y": 0.5})
