"""Tests for CLI helpers, logging, and no-console safety."""

from __future__ import annotations

from pathlib import Path
import sys

from remote_screen.__main__ import _setup_logging, render_qr_lines
from remote_screen.config import default_log_path, load_config


def test_render_qr_lines_utf8_and_ascii_fallback() -> None:
    utf8_lines = render_qr_lines("https://example.trycloudflare.com", utf8_safe=True)
    ascii_lines = render_qr_lines("https://example.trycloudflare.com", utf8_safe=False)

    assert len(utf8_lines) > 5
    assert len(ascii_lines) > 10
    assert all(set(line) <= {"#", " "} for line in ascii_lines)
    # Encodable on legacy cp950/gbk Windows consoles without UnicodeEncodeError
    "\n".join(ascii_lines).encode("cp950")
    "\n".join(ascii_lines).encode("gbk")


def test_config_tray_and_log_flags(tmp_path: Path) -> None:
    log_path = tmp_path / "custom.log"
    cfg = load_config(["--no-tray", "--log-file", str(log_path)], env={})
    assert cfg.no_tray is True
    assert cfg.log_file == log_path
    assert default_log_path().name == "remote_screen.log"


def test_setup_logging_writes_utf8_without_console(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "app.log"
    logger = _setup_logging(log_path, console_stream=None)
    logger.info("連線網址 : https://example.trycloudflare.com")
    for h in list(logger.handlers):
        h.flush()
        h.close()
        logger.removeHandler(h)

    content = log_path.read_text(encoding="utf-8")
    assert "連線網址 : https://example.trycloudflare.com" in content
    assert sys.stdout is not None
