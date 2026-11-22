"""Thread-safe screen capture, change detection, and JPEG encoding."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import ctypes
from dataclasses import dataclass
import hashlib
from io import BytesIO
import sys
import threading
from typing import Callable

from PIL import Image, ImageDraw


GrabberFn = Callable[[], tuple[int, int, bytes]]

_UNAVAILABLE_HASH_KEY = b"__UNAVAILABLE__"
_PLACEHOLDER_WIDTH = 960
_PLACEHOLDER_HEIGHT = 540


def enable_dpi_awareness() -> None:
    """Enable per-monitor DPI awareness on Windows so physical screen metrics match input."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


@dataclass(frozen=True)
class CapturedFrame:
    """Encoded JPEG screen frame and metadata."""

    jpeg: bytes
    screen_width: int
    screen_height: int
    changed: bool
    seq: int
    unavailable: bool = False


def _render_unavailable_jpeg(quality: int = 60) -> bytes:
    """Render a clean placeholder JPEG when desktop capture is temporarily blocked."""
    img = Image.new("RGB", (_PLACEHOLDER_WIDTH, _PLACEHOLDER_HEIGHT), (20, 24, 32))
    draw = ImageDraw.Draw(img)
    lines = [
        "Desktop temporarily unavailable",
        "(UAC prompt, lock screen, or restricted desktop)",
        "Auto-reconnecting...",
    ]
    y = _PLACEHOLDER_HEIGHT // 2 - 36
    for idx, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line)
        text_w = bbox[2] - bbox[0]
        x = max(16, (_PLACEHOLDER_WIDTH - text_w) // 2)
        color = (232, 236, 244) if idx == 0 else (154, 166, 184)
        draw.text((x, y), line, fill=color)
        y += 28
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=False)
    return buf.getvalue()


class ScreenCapturer:
    """Captures the desktop on a dedicated single thread to keep GDI handles thread-affine."""

    def __init__(
        self,
        monitor_index: int = 1,
        max_width: int = 1280,
        quality: int = 60,
        grabber: GrabberFn | None = None,
    ) -> None:
        self.monitor_index = max(1, int(monitor_index))
        self.max_width = max(320, min(3840, int(max_width)))
        self.quality = max(20, min(95, int(quality)))
        self._custom_grabber = grabber
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="screen-capture")
        self._thread_local = threading.local()
        self._lock = threading.Lock()
        self._last_hash: bytes | None = None
        self._last_frame: CapturedFrame | None = None
        self._seq = 0
        self._closed = False

    @property
    def last_frame(self) -> CapturedFrame | None:
        with self._lock:
            return self._last_frame

    def set_encoding(self, max_width: int, quality: int) -> None:
        """Update output dimensions/JPEG quality and invalidate frame diff cache."""
        with self._lock:
            self.max_width = max(320, min(3840, int(max_width)))
            self.quality = max(20, min(95, int(quality)))
            self._last_hash = None

    def _grab_with_mss(self) -> tuple[int, int, bytes]:
        import mss

        sct = getattr(self._thread_local, "sct", None)
        if sct is None:
            factory = getattr(mss, "MSS", None) or mss.mss
            sct = factory()
            self._thread_local.sct = sct
        monitors = sct.monitors
        idx = self.monitor_index if self.monitor_index < len(monitors) else (1 if len(monitors) > 1 else 0)
        shot = sct.grab(monitors[idx])
        return shot.width, shot.height, bytes(shot.bgra)

    def _close_thread_local_mss(self) -> None:
        sct = getattr(self._thread_local, "sct", None)
        if sct is not None:
            try:
                sct.close()
            except Exception:
                pass
            self._thread_local.sct = None

    def capture(self, force: bool = False) -> CapturedFrame | None:
        """Capture and JPEG-encode the screen; return None if unchanged and force is False."""
        with self._lock:
            if self._closed:
                return None
            max_width = self.max_width
            quality = self.quality
            last_hash = self._last_hash

        try:
            import mss.exception

            capture_errors: tuple[type[BaseException], ...] = (OSError, mss.exception.ScreenShotError)
        except Exception:
            capture_errors = (OSError,)

        raw_grabber = self._custom_grabber or self._grab_with_mss
        try:
            width, height, bgra = raw_grabber()
        except capture_errors:
            if self._custom_grabber is None:
                self._close_thread_local_mss()
            frame_hash = hashlib.blake2b(_UNAVAILABLE_HASH_KEY, digest_size=16).digest()
            if not force and last_hash is not None and frame_hash == last_hash:
                return None
            jpeg_bytes = _render_unavailable_jpeg(quality=quality)
            with self._lock:
                changed = self._last_hash is None or frame_hash != self._last_hash
                self._last_hash = frame_hash
                self._seq += 1
                prev = self._last_frame
                screen_w = prev.screen_width if prev is not None else 1280
                screen_h = prev.screen_height if prev is not None else 720
                frame = CapturedFrame(
                    jpeg=jpeg_bytes,
                    screen_width=screen_w,
                    screen_height=screen_h,
                    changed=changed,
                    seq=self._seq,
                    unavailable=True,
                )
                self._last_frame = frame
                return frame

        frame_hash = hashlib.blake2b(bgra, digest_size=16).digest()

        if not force and last_hash is not None and frame_hash == last_hash:
            return None

        img = Image.frombytes("RGB", (width, height), bgra, "raw", "BGRX")
        if width > max_width:
            target_h = max(1, round(height * (max_width / width)))
            img = img.resize((max_width, target_h), Image.Resampling.BILINEAR)

        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=False)
        jpeg_bytes = buf.getvalue()

        with self._lock:
            changed = self._last_hash is None or frame_hash != self._last_hash
            self._last_hash = frame_hash
            self._seq += 1
            frame = CapturedFrame(
                jpeg=jpeg_bytes,
                screen_width=width,
                screen_height=height,
                changed=changed,
                seq=self._seq,
                unavailable=False,
            )
            self._last_frame = frame
            return frame

    async def capture_async(self, force: bool = False) -> CapturedFrame | None:
        """Run capture() on the dedicated capture thread."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self.capture, force)

    def close(self) -> None:
        """Release mss resources on the capture thread and shut down executor."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            fut = self._executor.submit(self._close_thread_local_mss)
            fut.result(timeout=2.0)
        except Exception:
            pass
        self._executor.shutdown(wait=False)
