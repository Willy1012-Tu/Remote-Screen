"""Integration tests for RemoteServer HTTP auth and WebSocket streaming/control."""

from __future__ import annotations

import asyncio

from aiohttp.test_utils import TestClient, TestServer
import pytest

from remote_screen.auth import AuthManager
from remote_screen.config import Config
from remote_screen.screen import ScreenCapturer
from remote_screen.server import RemoteServer
from remote_screen.win_input import INPUT, InputController


def _make_test_server(on_login=None) -> tuple[RemoteServer, list[dict]]:
    calls: list[list[INPUT]] = []
    dispatched: list[dict] = []

    class RecordingController(InputController):
        def dispatch(self, payload: dict) -> None:
            dispatched.append(dict(payload))
            super().dispatch(payload)

    def fake_send(inputs: list[INPUT]) -> int:
        calls.append(list(inputs))
        return len(inputs)

    frame_counter = {"n": 0}

    def fake_grab() -> tuple[int, int, bytes]:
        frame_counter["n"] += 1
        c = (frame_counter["n"] * 40) % 255
        return 640, 360, bytes((c, 80, 120, 255)) * (640 * 360)

    cfg = Config(pin="246810", pin_is_random=False, fps=10.0, local_only=True, no_telegram=True)
    auth = AuthManager(cfg.pin, max_attempts=5, lockout_seconds=60.0)
    capturer = ScreenCapturer(max_width=640, quality=60, grabber=fake_grab)
    controller = RecordingController(send_fn=fake_send)
    srv = RemoteServer(cfg, auth, capturer, controller, on_login=on_login)
    return srv, dispatched


@pytest.mark.asyncio
async def test_http_auth_flow_lockout_and_logout() -> None:
    logged_in_ips: list[str] = []
    srv, _ = _make_test_server(on_login=logged_in_ips.append)
    try:
        async with TestClient(TestServer(srv.create_app())) as client:
            health = await client.get("/api/health")
            assert health.status == 200
            assert await health.json() == {"ok": True}

            # Unauthenticated / redirects to /login; /ws returns 401
            idx = await client.get("/", allow_redirects=False)
            assert idx.status == 302
            assert idx.headers["Location"] == "/login"

            ws_unauth = await client.get("/ws")
            assert ws_unauth.status == 401

            # 4 wrong attempts for CF-Connecting-IP -> 401, 5th -> 429
            cf_headers = {"CF-Connecting-IP": "198.51.100.77"}
            for _ in range(4):
                r = await client.post("/api/login", json={"pin": "000000"}, headers=cf_headers)
                assert r.status == 401
            locked = await client.post("/api/login", json={"pin": "000000"}, headers=cf_headers)
            assert locked.status == 429
            locked_body = await locked.json()
            assert locked_body["retry_after"] > 0
            assert logged_in_ips == []

            # Valid login from another IP succeeds, fires on_login, and sets HttpOnly cookie
            ok = await client.post(
                "/api/login",
                json={"pin": "246810"},
                headers={"CF-Connecting-IP": "198.51.100.88"},
            )
            assert ok.status == 200
            assert ok.cookies["rs_session"]["httponly"] is True
            assert logged_in_ips == ["198.51.100.88"]

            authed_idx = await client.get("/", allow_redirects=False)
            assert authed_idx.status == 200
            html = await authed_idx.text()
            assert "RemoteScreen" in html

            # Logout invalidates session
            out = await client.post("/api/logout")
            assert out.status == 200
            idx_after = await client.get("/", allow_redirects=False)
            assert idx_after.status == 302
    finally:
        await srv.stop()
        srv.capturer.close()


@pytest.mark.asyncio
async def test_websocket_stream_and_input_dispatch() -> None:
    srv, dispatched = _make_test_server()
    try:
        async with TestClient(TestServer(srv.create_app())) as client:
            login = await client.post("/api/login", json={"pin": "246810"})
            assert login.status == 200

            async with client.ws_connect("/ws") as ws:
                meta = await ws.receive_json(timeout=2.0)
                assert meta["type"] == "meta"
                assert meta["width"] == 640
                assert meta["height"] == 360

                jpeg = await ws.receive_bytes(timeout=2.0)
                assert jpeg.startswith(b"\xff\xd8")

                await ws.send_json({"type": "click", "x": 0.5, "y": 0.5, "button": "left"})
                await ws.send_json({"type": "text", "text": "你好"})
                await ws.send_json({"type": "key", "keys": ["ctrl", "c"]})
                await ws.send_json({"type": "quality", "preset": "low"})
                await ws.send_json({"type": "refresh"})

                # Receive forced refreshed frame
                next_bytes = await ws.receive_bytes(timeout=2.0)
                assert next_bytes.startswith(b"\xff\xd8")
                await asyncio.sleep(0.05)

            assert [d["type"] for d in dispatched] == ["click", "text", "key"]
            assert srv.capturer.max_width == 960
            assert srv.capturer.quality == 45
            assert srv.connections_total == 1
            assert srv.actions_total == 3
    finally:
        await srv.stop()
        srv.capturer.close()
