"""aiohttp HTTP and WebSocket server for RemoteScreen."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from aiohttp import WSCloseCode, WSMsgType, web

from remote_screen.auth import AuthManager, extract_client_ip
from remote_screen.config import QUALITY_PRESETS, Config, get_static_dir
from remote_screen.screen import CapturedFrame, ScreenCapturer
from remote_screen.win_input import InputController


SESSION_COOKIE = "rs_session"

_SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "img-src 'self' blob:; "
        "connect-src 'self' ws: wss:; "
        "style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'self'"
    ),
}


@dataclass(eq=False)
class _ClientSession:
    ws: web.WebSocketResponse
    session_token: str = ""
    sending: bool = False
    last_dims: tuple[int, int] | None = None
    revoke_event: asyncio.Event = field(default_factory=asyncio.Event)


class RemoteServer:
    """Serves mobile web UI, PIN authentication, JPEG screen stream, and control WS."""

    def __init__(
        self,
        config: Config,
        auth: AuthManager,
        capturer: ScreenCapturer,
        controller: InputController,
        static_dir: Path | None = None,
        on_login: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.auth = auth
        self.capturer = capturer
        self.controller = controller
        self.static_dir = static_dir or get_static_dir()
        self._on_login = on_login

        self._clients: set[_ClientSession] = set()
        self._wake_event = asyncio.Event()
        self._force_next = False
        self._broadcaster_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

        self.connections_total = 0
        self.actions_total = 0
        self._app = self.create_app()

    @web.middleware
    async def _security_headers_middleware(
        self,
        request: web.Request,
        handler: Any,
    ) -> web.StreamResponse:
        try:
            resp = await handler(request)
        except web.HTTPException as exc:
            for key, value in _SECURITY_HEADERS.items():
                exc.headers.setdefault(key, value)
            raise
        if not resp.prepared:
            for key, value in _SECURITY_HEADERS.items():
                resp.headers.setdefault(key, value)
        return resp

    def create_app(self) -> web.Application:
        """Build the aiohttp application with all HTTP and WebSocket routes."""
        app = web.Application(
            client_max_size=16 * 1024,
            middlewares=[self._security_headers_middleware],
        )
        app.router.add_get("/api/health", self._handle_health)
        app.router.add_get("/login", self._handle_login_page)
        app.router.add_post("/api/login", self._handle_login)
        app.router.add_post("/api/logout", self._handle_logout)
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/ws", self._handle_ws)
        return app

    async def start(self) -> tuple[str, int]:
        """Start the local HTTP/WS server and return bound (host, port)."""
        self._stopping = False
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.config.host, self.config.port)
        await self._site.start()

        sockets = self._site._server.sockets if self._site._server else ()
        if sockets:
            bound_host, bound_port = sockets[0].getsockname()[:2]
        else:
            bound_host, bound_port = self.config.host, self.config.port
        return str(bound_host), int(bound_port)

    async def stop(self) -> None:
        """Stop broadcaster, disconnect active clients, and tear down HTTP runner."""
        self._stopping = True
        self._wake_event.set()
        if self._broadcaster_task is not None:
            self._broadcaster_task.cancel()
            try:
                await self._broadcaster_task
            except asyncio.CancelledError:
                pass
            self._broadcaster_task = None

        for client in list(self._clients):
            try:
                await client.ws.close(code=WSCloseCode.GOING_AWAY, message=b"Server shutting down")
            except Exception:
                pass
        self._clients.clear()

        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    def _is_authenticated(self, request: web.Request) -> bool:
        token = request.cookies.get(SESSION_COOKIE)
        return self.auth.validate_session(token)

    @staticmethod
    def _is_https_request(request: web.Request) -> bool:
        proto = request.headers.get("X-Forwarded-Proto", "").lower()
        return request.secure or proto == "https"

    @staticmethod
    def _normalize_host_port(netloc: str, scheme: str) -> str:
        host = netloc.strip().lower()
        if scheme == "https" and host.endswith(":443"):
            return host[:-4]
        if scheme == "http" and host.endswith(":80"):
            return host[:-3]
        return host

    @classmethod
    def _is_same_origin(cls, request: web.Request) -> bool:
        sec_fetch_site = request.headers.get("Sec-Fetch-Site", "").strip().lower()
        if sec_fetch_site == "cross-site":
            return False

        origin = request.headers.get("Origin", "").strip()
        if not origin:
            return True
        if origin.lower() == "null":
            return False

        parsed = urlparse(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False

        req_scheme = "https" if cls._is_https_request(request) else "http"
        origin_hp = cls._normalize_host_port(parsed.netloc, parsed.scheme)
        req_hp = cls._normalize_host_port(request.host, req_scheme)
        return origin_hp == req_hp

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def _handle_login_page(self, request: web.Request) -> web.Response:
        if self._is_authenticated(request):
            raise web.HTTPFound("/")
        html = (self.static_dir / "login.html").read_text(encoding="utf-8")
        return web.Response(
            text=html,
            content_type="text/html",
            charset="utf-8",
            headers={"Cache-Control": "no-store"},
        )

    async def _handle_login(self, request: web.Request) -> web.Response:
        if not self._is_same_origin(request):
            return web.json_response({"ok": False, "error": "forbidden_origin"}, status=403)
        try:
            body: Any = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

        pin = body.get("pin") if isinstance(body, dict) else None
        if not isinstance(pin, str) or not pin.strip():
            return web.json_response({"ok": False, "error": "missing_pin"}, status=400)

        client_ip = extract_client_ip(request.headers, request.remote)
        ok, token, retry_after = self.auth.authenticate(client_ip, pin.strip())
        if ok and token is not None:
            if self._on_login is not None:
                try:
                    self._on_login(client_ip)
                except Exception:
                    pass
            resp = web.json_response({"ok": True})
            resp.set_cookie(
                SESSION_COOKIE,
                token,
                httponly=True,
                samesite="Lax",
                path="/",
                secure=self._is_https_request(request),
            )
            return resp

        if retry_after > 0:
            return web.json_response(
                {"ok": False, "error": "locked", "retry_after": retry_after},
                status=429,
                headers={"Retry-After": str(retry_after)},
            )
        return web.json_response({"ok": False, "error": "invalid_pin"}, status=401)

    async def _handle_logout(self, request: web.Request) -> web.Response:
        if not self._is_same_origin(request):
            return web.json_response({"ok": False, "error": "forbidden_origin"}, status=403)
        token = request.cookies.get(SESSION_COOKIE)
        self.auth.revoke_session(token)
        if token:
            for client in list(self._clients):
                if client.session_token == token:
                    client.revoke_event.set()
        resp = web.json_response({"ok": True})
        resp.del_cookie(
            SESSION_COOKIE,
            path="/",
            httponly=True,
            samesite="Lax",
            secure=self._is_https_request(request),
        )
        return resp

    async def _handle_index(self, request: web.Request) -> web.Response:
        if not self._is_authenticated(request):
            raise web.HTTPFound("/login")
        html = (self.static_dir / "index.html").read_text(encoding="utf-8")
        return web.Response(
            text=html,
            content_type="text/html",
            charset="utf-8",
            headers={"Cache-Control": "no-store"},
        )

    async def _send_frame_to_client(self, client: _ClientSession, frame: CapturedFrame) -> bool:
        if client.ws.closed or client.sending:
            return False
        if not self.auth.validate_session(client.session_token):
            client.revoke_event.set()
            return False
        client.sending = True
        try:
            dims = (frame.screen_width, frame.screen_height)
            if client.last_dims != dims:
                await client.ws.send_json(
                    {
                        "type": "meta",
                        "width": frame.screen_width,
                        "height": frame.screen_height,
                        "unavailable": frame.unavailable,
                    }
                )
                client.last_dims = dims
            await client.ws.send_bytes(frame.jpeg)
            return True
        except Exception:
            return False
        finally:
            client.sending = False

    def _ensure_broadcaster(self) -> None:
        if self._broadcaster_task is None or self._broadcaster_task.done():
            self._broadcaster_task = asyncio.create_task(self._broadcaster_loop())

    async def _broadcaster_loop(self) -> None:
        interval = 1.0 / max(0.5, self.config.fps)
        try:
            while not self._stopping and self._clients:
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
                self._wake_event.clear()
                if self._stopping or not self._clients:
                    break

                force = self._force_next
                self._force_next = False
                frame = await self.capturer.capture_async(force=force)
                if frame is None:
                    continue
                await asyncio.gather(
                    *(self._send_frame_to_client(c, frame) for c in list(self._clients)),
                    return_exceptions=True,
                )
        except asyncio.CancelledError:
            raise

    def _schedule_post_action_refresh(self) -> None:
        loop = asyncio.get_running_loop()
        loop.call_later(0.08, self._wake_event.set)

    async def _handle_ws(self, request: web.Request) -> web.StreamResponse:
        if not self._is_same_origin(request):
            return web.Response(status=403, text="Forbidden origin")
        token = request.cookies.get(SESSION_COOKIE)
        if not self.auth.validate_session(token):
            return web.Response(status=401, text="Unauthorized")

        ws = web.WebSocketResponse(heartbeat=20.0, max_msg_size=64 * 1024)
        await ws.prepare(request)

        client = _ClientSession(ws=ws, session_token=token or "")
        self._clients.add(client)
        self.connections_total += 1
        self._ensure_broadcaster()

        revoke_task = asyncio.create_task(client.revoke_event.wait())
        try:
            initial = await self.capturer.capture_async(force=True)
            if initial is not None:
                await self._send_frame_to_client(client, initial)

            while not ws.closed:
                recv_task = asyncio.create_task(ws.receive())
                done, _ = await asyncio.wait(
                    {recv_task, revoke_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if revoke_task in done:
                    recv_task.cancel()
                    try:
                        await recv_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    await ws.close(code=4401, message=b"Session revoked")
                    break

                msg = recv_task.result()
                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
                if not self.auth.validate_session(client.session_token):
                    await ws.close(code=4401, message=b"Session expired")
                    break
                if msg.type == WSMsgType.TEXT:
                    try:
                        payload = json.loads(msg.data)
                    except json.JSONDecodeError:
                        await ws.send_json({"type": "error", "message": "Invalid JSON"})
                        continue
                    if not isinstance(payload, dict):
                        await ws.send_json({"type": "error", "message": "Invalid payload"})
                        continue

                    kind = payload.get("type")
                    if kind == "refresh":
                        self._force_next = True
                        self._wake_event.set()
                        continue
                    if kind == "quality":
                        preset = payload.get("preset")
                        if preset in QUALITY_PRESETS:
                            w, q = QUALITY_PRESETS[preset]
                            self.capturer.set_encoding(w, q)
                            self._force_next = True
                            self._wake_event.set()
                        else:
                            await ws.send_json({"type": "error", "message": "Unknown quality preset"})
                        continue

                    try:
                        self.controller.dispatch(payload)
                        self.actions_total += 1
                        if kind != "mouse_move":
                            self._schedule_post_action_refresh()
                    except ValueError as exc:
                        await ws.send_json({"type": "error", "message": str(exc)})
        finally:
            if not revoke_task.done():
                revoke_task.cancel()
                try:
                    await revoke_task
                except asyncio.CancelledError:
                    pass
            self._clients.discard(client)
            if not self._clients:
                self._wake_event.set()
        return ws
