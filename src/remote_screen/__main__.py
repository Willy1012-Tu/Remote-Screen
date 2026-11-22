"""CLI, windowed tray, and executable entrypoint for RemoteScreen."""

from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
import logging
import os
from pathlib import Path
import signal
import sys
import threading
import time
import traceback
from typing import Any, Callable, Sequence, TextIO

import qrcode

from remote_screen.auth import AuthManager
from remote_screen.config import Config, default_log_path, get_app_dir, load_config
from remote_screen.notify import notify_login, notify_shutdown, notify_startup
from remote_screen.screen import ScreenCapturer, enable_dpi_awareness
from remote_screen.server import RemoteServer
from remote_screen.tunnel import CloudflareTunnel, ensure_cloudflared
from remote_screen.win_input import InputController


_CTRL_HANDLER_REF = None


def _ensure_std_streams() -> TextIO | None:
    """Ensure stdout/stderr exist in --windowed builds; return real stdout if present."""
    real_stdout = sys.stdout
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    return real_stdout


def _setup_logging(log_path: Path, console_stream: TextIO | None) -> logging.Logger:
    """Configure UTF-8 file logging (and console logging when a console exists)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("remote_screen")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
        try:
            existing.close()
        except Exception:
            pass

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if console_stream is not None:
        stream_handler = logging.StreamHandler(console_stream)
        stream_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(stream_handler)

    return logger


def render_qr_lines(url: str, utf8_safe: bool) -> list[str]:
    """Render a QR code as terminal lines with safe ASCII fallback for legacy codepages."""
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    if utf8_safe:
        from io import StringIO

        buf = StringIO()
        qr.print_ascii(out=buf, invert=True)
        return buf.getvalue().splitlines()

    matrix = qr.get_matrix()
    return ["  " + "".join("##" if cell else "  " for cell in row) for row in matrix]


def _is_utf8_stream(stream: object) -> bool:
    enc = getattr(stream, "encoding", None) or ""
    return enc.lower().replace("-", "") == "utf8"


def _log_banner(display_url: str, cfg: Config, logger: logging.Logger, console_stream: TextIO | None) -> None:
    pin_note = "(隨機產生)" if cfg.pin_is_random else "(固定設定)"
    logger.info("========================================================")
    logger.info("  RemoteScreen 遠端螢幕控制已就緒")
    logger.info("========================================================")
    logger.info("  連線網址 : %s", display_url)
    logger.info("  存取 PIN : ****** %s（已省略明文，請由系統匣或 Telegram 查看）", pin_note)
    logger.info("  畫質設定 : max_width=%s, quality=%s, fps=%s", cfg.max_width, cfg.quality, cfg.fps)
    if console_stream is not None:
        print(f"  存取 PIN : {cfg.pin} {pin_note}", file=console_stream, flush=True)
        print("", file=console_stream, flush=True)
        print("  手機掃描下方 QR Code 或開啟上方網址：", file=console_stream, flush=True)
        for line in render_qr_lines(display_url, utf8_safe=_is_utf8_stream(console_stream)):
            print(line, file=console_stream, flush=True)
        print("", file=console_stream, flush=True)
        print("  按 Ctrl+C 或關閉視窗即可停止服務。", file=console_stream, flush=True)
        print("========================================================", file=console_stream, flush=True)


def _install_stop_handlers(
    trigger_stop: Callable[[], None],
    shutdown_done: threading.Event,
) -> None:
    if threading.current_thread() is threading.main_thread():
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    signal.signal(sig, lambda _s, _f: trigger_stop())
                except Exception:
                    pass

    if sys.platform == "win32":
        global _CTRL_HANDLER_REF
        handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        @handler_type
        def _win_ctrl_handler(ctrl_type: int) -> bool:
            if ctrl_type in (0, 1, 2, 5, 6):
                trigger_stop()
                if ctrl_type in (2, 5, 6):
                    shutdown_done.wait(timeout=4.0)
                return True
            return False

        _CTRL_HANDLER_REF = _win_ctrl_handler
        try:
            ctypes.windll.kernel32.SetConsoleCtrlHandler(_CTRL_HANDLER_REF, True)
        except Exception:
            pass


def _app_name() -> str:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).name
    return "RemoteScreen.py"


async def _run_async(
    cfg: Config,
    app_name: str,
    logger: logging.Logger,
    console_stream: TextIO | None,
    external_stop: threading.Event | None = None,
    register_stop: Callable[[Callable[[], None]], None] | None = None,
    on_ready: Callable[[str], None] | None = None,
) -> tuple[int, int]:
    enable_dpi_awareness()
    auth = AuthManager(cfg.pin)
    capturer = ScreenCapturer(
        monitor_index=cfg.monitor_index,
        max_width=cfg.max_width,
        quality=cfg.quality,
    )
    controller = InputController()
    bg_tasks: set[asyncio.Task[Any]] = set()
    loop = asyncio.get_running_loop()

    def _on_login(client_ip: str) -> None:
        logger.info("裝置已登入，來源 IP：%s", client_ip)
        if not cfg.no_telegram:
            task = loop.create_task(asyncio.to_thread(notify_login, client_ip, app_name))
            bg_tasks.add(task)
            task.add_done_callback(bg_tasks.discard)

    server = RemoteServer(cfg, auth, capturer, controller, on_login=_on_login)
    tunnel: CloudflareTunnel | None = None

    stop_event = asyncio.Event()
    shutdown_done = threading.Event()

    def _trigger_stop() -> None:
        if not loop.is_closed():
            loop.call_soon_threadsafe(stop_event.set)

    if register_stop is not None:
        register_stop(_trigger_stop)
    if external_stop is not None and external_stop.is_set():
        stop_event.set()
    _install_stop_handlers(_trigger_stop, shutdown_done)

    try:
        bound_host, bound_port = await server.start()
        local_url = f"http://{bound_host}:{bound_port}"

        if cfg.local_only:
            display_url = local_url
        else:
            binary = await asyncio.to_thread(ensure_cloudflared, get_app_dir())
            if external_stop is not None and external_stop.is_set():
                return server.connections_total, server.actions_total
            tunnel = CloudflareTunnel(binary)
            logger.info("正在建立 Cloudflare 加密隧道...")
            display_url = await asyncio.to_thread(tunnel.start, local_url, 30.0)

        if external_stop is not None and external_stop.is_set():
            return server.connections_total, server.actions_total

        _log_banner(display_url, cfg, logger, console_stream)
        if on_ready is not None:
            on_ready(display_url)

        if not cfg.no_telegram:
            sent = await asyncio.to_thread(
                notify_startup,
                display_url,
                cfg.pin,
                app_name,
                cfg.pin_is_random,
            )
            if sent:
                logger.info("  [Telegram] 已發送連線通知。")

        await stop_event.wait()
        return server.connections_total, server.actions_total
    finally:
        logger.info("正在停止服務與隧道...")
        if tunnel is not None:
            tunnel.stop()
        await server.stop()
        capturer.close()
        shutdown_done.set()


def _run_headless(
    cfg: Config,
    app_name: str,
    started_at: float,
    logger: logging.Logger,
    console_stream: TextIO | None,
) -> int:
    try:
        connections_total, actions_total = asyncio.run(
            _run_async(cfg, app_name, logger, console_stream)
        )
        if not cfg.no_telegram:
            summary = f"連線 {connections_total} 次，操作 {actions_total} 次"
            notify_shutdown(app_name, "已停止", time.monotonic() - started_at, summary)
        return 0
    except KeyboardInterrupt:
        if not cfg.no_telegram:
            notify_shutdown(app_name, "已停止", time.monotonic() - started_at, "使用者手動中止")
        return 0
    except Exception as exc:
        logger.error("執行失敗：%s\n%s", exc, traceback.format_exc())
        if not cfg.no_telegram:
            summary = f"{type(exc).__name__}: {exc}"[:200]
            notify_shutdown(app_name, "失敗", time.monotonic() - started_at, summary)
        if getattr(sys, "frozen", False) and console_stream is not None and sys.stdin and sys.stdin.isatty():
            try:
                input("\n發生錯誤，按 Enter 鍵退出...")
            except Exception:
                pass
        return 1


def _run_with_tray(
    cfg: Config,
    app_name: str,
    started_at: float,
    log_path: Path,
    logger: logging.Logger,
    console_stream: TextIO | None,
) -> int:
    from remote_screen.tray import TrayController, TrayState

    external_stop = threading.Event()
    stop_lock = threading.Lock()
    stop_callback: list[Callable[[], None]] = []
    exit_code = [0]

    def _register_stop(fn: Callable[[], None]) -> None:
        with stop_lock:
            stop_callback.append(fn)
        if external_stop.is_set():
            fn()

    def _request_stop() -> None:
        external_stop.set()
        with stop_lock:
            cb = stop_callback[0] if stop_callback else None
        if cb is not None:
            cb()

    state = TrayState(
        pin=cfg.pin,
        pin_is_random=cfg.pin_is_random,
        log_path=log_path,
        app_name=app_name,
        no_telegram=cfg.no_telegram,
    )

    tray_holder: list[TrayController] = []

    def _on_quit() -> None:
        _request_stop()
        if tray_holder:
            tray_holder[0].stop()

    tray = TrayController(state=state, on_quit=_on_quit)
    tray_holder.append(tray)

    def _worker() -> None:
        try:
            connections_total, actions_total = asyncio.run(
                _run_async(
                    cfg,
                    app_name,
                    logger,
                    console_stream,
                    external_stop=external_stop,
                    register_stop=_register_stop,
                    on_ready=tray.mark_ready,
                )
            )
            if not cfg.no_telegram:
                summary = f"連線 {connections_total} 次，操作 {actions_total} 次"
                notify_shutdown(app_name, "已停止", time.monotonic() - started_at, summary)
        except Exception as exc:
            exit_code[0] = 1
            logger.error("執行失敗：%s\n%s", exc, traceback.format_exc())
            if not cfg.no_telegram:
                summary = f"{type(exc).__name__}: {exc}"[:200]
                notify_shutdown(app_name, "失敗", time.monotonic() - started_at, summary)
            tray.notify_error(f"{exc}\n\n詳細記錄：{log_path}")
        finally:
            tray.stop()

    worker = threading.Thread(target=_worker, name="remote-screen-server", daemon=False)

    def _setup(icon: Any) -> None:
        icon.visible = True
        worker.start()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, lambda _s, _f: _on_quit())
            except Exception:
                pass

    tray.run(_setup)
    _request_stop()
    if worker.is_alive():
        worker.join(timeout=8.0)
    return exit_code[0]


def main(argv: Sequence[str] | None = None) -> int:
    """Program entrypoint."""
    console_stream = _ensure_std_streams()
    cfg = load_config(argv)
    log_path = cfg.log_file or default_log_path()
    logger = _setup_logging(log_path, console_stream)
    app_name = _app_name()
    started_at = time.monotonic()

    if sys.platform == "win32" and not cfg.no_tray:
        return _run_with_tray(cfg, app_name, started_at, log_path, logger, console_stream)
    return _run_headless(cfg, app_name, started_at, logger, console_stream)


if __name__ == "__main__":
    raise SystemExit(main())
