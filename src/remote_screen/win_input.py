"""Windows SendInput controller for normalized pointer and keyboard/Unicode input."""

from __future__ import annotations

import ctypes
import math
import sys
from typing import Any, Callable, Mapping, Sequence


ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("_union",)
    _fields_ = [
        ("type", ctypes.c_ulong),
        ("_union", INPUT_UNION),
    ]


VK_MAP: dict[str, int] = {
    "backspace": 0x08,
    "bs": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "menu": 0x12,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "delete": 0x2E,
    "del": 0x2E,
    "win": 0x5B,
    "lwin": 0x5B,
    "super": 0x5B,
    **{str(d): 0x30 + d for d in range(10)},
    **{chr(c): c - 32 for c in range(ord("a"), ord("z") + 1)},
    **{f"f{n}": 0x6F + n for n in range(1, 13)},
}

EXTENDED_KEYS: set[str] = {
    "win",
    "lwin",
    "super",
    "up",
    "down",
    "left",
    "right",
    "delete",
    "del",
    "home",
    "end",
    "pageup",
    "pgup",
    "pagedown",
    "pgdn",
}


def norm_to_absolute(x_norm: float, y_norm: float) -> tuple[int, int]:
    """Clamp normalized [0.0, 1.0] coordinates and map to Win32 absolute [0, 65535]."""
    cx = min(1.0, max(0.0, float(x_norm)))
    cy = min(1.0, max(0.0, float(y_norm)))
    return round(cx * 65535), round(cy * 65535)


def _win32_send_inputs(inputs: Sequence[INPUT]) -> int:
    if not inputs:
        return 0
    if sys.platform != "win32":
        return len(inputs)
    arr = (INPUT * len(inputs))(*inputs)
    sent = ctypes.windll.user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    return int(sent)


def _mouse_input(dx: int, dy: int, flags: int, mouse_data: int = 0) -> INPUT:
    return INPUT(
        type=INPUT_MOUSE,
        mi=MOUSEINPUT(
            dx=dx,
            dy=dy,
            mouseData=mouse_data & 0xFFFFFFFF,
            dwFlags=flags,
            time=0,
            dwExtraInfo=0,
        ),
    )


def _key_vk_input(vk: int, flags: int = 0) -> INPUT:
    return INPUT(
        type=INPUT_KEYBOARD,
        ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0),
    )


def _key_unicode_input(code_unit: int, key_up: bool = False) -> INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if key_up else 0)
    return INPUT(
        type=INPUT_KEYBOARD,
        ki=KEYBDINPUT(wVk=0, wScan=code_unit, dwFlags=flags, time=0, dwExtraInfo=0),
    )


def _require_finite_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    num = float(value)
    if not math.isfinite(num):
        raise ValueError(f"{name} must be finite")
    return num


class InputController:
    """Translates high-level pointer/keyboard events into Win32 SendInput batches."""

    def __init__(self, send_fn: Callable[[list[INPUT]], int] | None = None) -> None:
        self._send_fn = send_fn or _win32_send_inputs

    def _emit(self, inputs: list[INPUT]) -> None:
        if inputs:
            self._send_fn(inputs)

    def move(self, x_norm: float, y_norm: float) -> None:
        ax, ay = norm_to_absolute(x_norm, y_norm)
        self._emit([_mouse_input(ax, ay, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE)])

    def mouse_down(self, x_norm: float, y_norm: float, button: str = "left") -> None:
        ax, ay = norm_to_absolute(x_norm, y_norm)
        down_flag = self._button_flags(button)[0]
        self._emit([
            _mouse_input(ax, ay, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE),
            _mouse_input(ax, ay, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | down_flag),
        ])

    def mouse_up(self, x_norm: float, y_norm: float, button: str = "left") -> None:
        ax, ay = norm_to_absolute(x_norm, y_norm)
        up_flag = self._button_flags(button)[1]
        self._emit([
            _mouse_input(ax, ay, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | up_flag),
        ])

    def click(self, x_norm: float, y_norm: float, button: str = "left") -> None:
        ax, ay = norm_to_absolute(x_norm, y_norm)
        down_flag, up_flag = self._button_flags(button)
        self._emit([
            _mouse_input(ax, ay, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE),
            _mouse_input(ax, ay, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | down_flag),
            _mouse_input(ax, ay, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | up_flag),
        ])

    def double_click(self, x_norm: float, y_norm: float) -> None:
        ax, ay = norm_to_absolute(x_norm, y_norm)
        down_flag, up_flag = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
        self._emit([
            _mouse_input(ax, ay, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE),
            _mouse_input(ax, ay, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | down_flag),
            _mouse_input(ax, ay, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | up_flag),
            _mouse_input(ax, ay, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | down_flag),
            _mouse_input(ax, ay, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | up_flag),
        ])

    def scroll(self, x_norm: float, y_norm: float, delta: int) -> None:
        ax, ay = norm_to_absolute(x_norm, y_norm)
        wheel_delta = (int(delta) * 120) & 0xFFFFFFFF
        self._emit([
            _mouse_input(ax, ay, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE),
            _mouse_input(ax, ay, MOUSEEVENTF_WHEEL, mouse_data=wheel_delta),
        ])

    def type_text(self, text: str) -> None:
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")[:2000]
        events: list[INPUT] = []
        for ch in normalized:
            if ch == "\n":
                vk = VK_MAP["enter"]
                events.append(_key_vk_input(vk, 0))
                events.append(_key_vk_input(vk, KEYEVENTF_KEYUP))
            elif ch == "\t":
                vk = VK_MAP["tab"]
                events.append(_key_vk_input(vk, 0))
                events.append(_key_vk_input(vk, KEYEVENTF_KEYUP))
            else:
                encoded = ch.encode("utf-16-le")
                for offset in range(0, len(encoded), 2):
                    unit = int.from_bytes(encoded[offset : offset + 2], "little")
                    events.append(_key_unicode_input(unit, key_up=False))
                    events.append(_key_unicode_input(unit, key_up=True))
        self._emit(events)

    def press_combo(self, keys: Sequence[str]) -> None:
        if not keys:
            raise ValueError("keys must not be empty")
        resolved: list[tuple[int, int]] = []
        for raw_key in keys:
            if not isinstance(raw_key, str):
                raise ValueError("each key must be a string")
            name = raw_key.strip().lower()
            if name not in VK_MAP:
                raise ValueError(f"Unknown key: {raw_key!r}")
            ext_flag = KEYEVENTF_EXTENDEDKEY if name in EXTENDED_KEYS else 0
            resolved.append((VK_MAP[name], ext_flag))

        events: list[INPUT] = []
        for vk, ext_flag in resolved:
            events.append(_key_vk_input(vk, ext_flag))
        for vk, ext_flag in reversed(resolved):
            events.append(_key_vk_input(vk, ext_flag | KEYEVENTF_KEYUP))
        self._emit(events)

    def dispatch(self, payload: Mapping[str, Any]) -> None:
        """Validate and execute a JSON control message."""
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be a mapping")
        kind = payload.get("type")
        if kind in {"click", "dblclick", "mouse_down", "mouse_move", "mouse_up", "scroll"}:
            x = _require_finite_float(payload.get("x"), "x")
            y = _require_finite_float(payload.get("y"), "y")
            if kind == "mouse_move":
                self.move(x, y)
                return
            if kind == "dblclick":
                self.double_click(x, y)
                return
            if kind == "scroll":
                raw_delta = payload.get("delta")
                if isinstance(raw_delta, bool) or not isinstance(raw_delta, (int, float)):
                    raise ValueError("delta must be numeric")
                if not math.isfinite(float(raw_delta)):
                    raise ValueError("delta must be finite")
                clamped_delta = max(-50, min(50, int(raw_delta)))
                self.scroll(x, y, clamped_delta)
                return
            button = payload.get("button", "left")
            if not isinstance(button, str):
                raise ValueError("button must be a string")
            if kind == "click":
                self.click(x, y, button=button)
            elif kind == "mouse_down":
                self.mouse_down(x, y, button=button)
            else:
                self.mouse_up(x, y, button=button)
            return

        if kind == "text":
            text_val = payload.get("text")
            if text_val is None and "value" in payload:
                text_val = payload.get("value")
            self.type_text(text_val)  # type: ignore[arg-type]
            return

        if kind == "key":
            keys = payload.get("keys")
            if not isinstance(keys, list):
                raise ValueError("keys must be a list")
            self.press_combo(keys)
            return

        raise ValueError(f"Unsupported control event type: {kind!r}")

    @staticmethod
    def _button_flags(button: str) -> tuple[int, int]:
        norm = button.strip().lower()
        if norm == "left":
            return MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
        if norm == "right":
            return MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
        raise ValueError(f"Unsupported mouse button: {button!r}")
