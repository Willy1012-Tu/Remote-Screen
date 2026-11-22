"""Tests for ScreenCapturer and JPEG encoding/diffing."""

from __future__ import annotations

from io import BytesIO

from PIL import Image
import pytest

from remote_screen.screen import ScreenCapturer, enable_dpi_awareness


def _make_solid_bgra(width: int, height: int, b: int, g: int, r: int) -> bytes:
    pixel = bytes((b, g, r, 255))
    return pixel * (width * height)


def test_synthetic_capture_resize_and_diff() -> None:
    color = [10, 80, 200]

    def grabber() -> tuple[int, int, bytes]:
        return 1920, 1080, _make_solid_bgra(1920, 1080, *color)

    cap = ScreenCapturer(max_width=1280, quality=60, grabber=grabber)
    try:
        first = cap.capture()
        assert first is not None
        assert first.jpeg.startswith(b"\xff\xd8")
        assert first.screen_width == 1920
        assert first.screen_height == 1080
        assert first.changed is True
        assert first.unavailable is False
        assert first.seq == 1

        decoded = Image.open(BytesIO(first.jpeg))
        assert decoded.size == (1280, 720)

        # Identical frame skipped when force=False
        assert cap.capture(force=False) is None

        # Force returns a frame even when pixels are identical
        forced = cap.capture(force=True)
        assert forced is not None
        assert forced.seq == 2
        assert forced.changed is False

        # Changing pixel content triggers new frame
        color[0] = 250
        updated = cap.capture(force=False)
        assert updated is not None
        assert updated.seq == 3
        assert updated.changed is True

        # Changing encoding invalidates cache so identical pixels still emit
        cap.set_encoding(960, 45)
        reencoded = cap.capture(force=False)
        assert reencoded is not None
        assert reencoded.seq == 4
        decoded2 = Image.open(BytesIO(reencoded.jpeg))
        assert decoded2.size == (960, 540)
    finally:
        cap.close()


def test_capture_error_fallback_and_recovery() -> None:
    should_fail = True

    def grabber() -> tuple[int, int, bytes]:
        if should_fail:
            raise OSError("BitBlt failed")
        return 1920, 1080, _make_solid_bgra(1920, 1080, 20, 40, 60)

    cap = ScreenCapturer(max_width=1280, quality=60, grabber=grabber)
    try:
        fallback = cap.capture(force=False)
        assert fallback is not None
        assert fallback.jpeg.startswith(b"\xff\xd8")
        assert fallback.unavailable is True
        assert fallback.changed is True

        # Repeated failure is suppressed when force=False
        assert cap.capture(force=False) is None

        # Forced capture while unavailable still returns placeholder JPEG
        forced = cap.capture(force=True)
        assert forced is not None
        assert forced.unavailable is True
        assert forced.changed is False

        # Once display becomes available again, recovery emits real frame
        should_fail = False
        recovered = cap.capture(force=False)
        assert recovered is not None
        assert recovered.unavailable is False
        assert recovered.changed is True
        assert recovered.screen_width == 1920
        assert recovered.screen_height == 1080
    finally:
        cap.close()


@pytest.mark.asyncio
async def test_capture_async_and_real_display_smoke() -> None:
    enable_dpi_awareness()
    cap = ScreenCapturer(monitor_index=1, max_width=1280, quality=60)
    try:
        frame = await cap.capture_async(force=True)
        assert frame is not None
        assert frame.jpeg.startswith(b"\xff\xd8")
        assert frame.screen_width > 0
        assert frame.screen_height > 0
        assert cap.last_frame is frame
    finally:
        cap.close()
