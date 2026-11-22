"""Configuration loading and path resolution for RemoteScreen."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import sys
from typing import Mapping, Sequence


QUALITY_PRESETS: dict[str, tuple[int, int]] = {
    "low": (960, 45),
    "medium": (1280, 60),
    "high": (1920, 75),
}


@dataclass(frozen=True)
class Config:
    """Runtime configuration for RemoteScreen."""

    pin: str
    pin_is_random: bool
    host: str = "127.0.0.1"
    port: int = 0
    fps: float = 4.0
    quality: int = 60
    max_width: int = 1280
    monitor_index: int = 1
    local_only: bool = False
    no_telegram: bool = False
    no_tray: bool = False
    log_file: Path | None = None


def default_log_path() -> Path:
    """Return per-user default log file path for windowed/tray execution."""
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home()
    return root / "RemoteScreen" / "remote_screen.log"


def get_app_dir() -> Path:
    """Return directory containing the executable or project root."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def get_static_dir() -> Path:
    """Return directory containing static HTML assets."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "remote_screen" / "static"
    return Path(__file__).resolve().parent / "static"


def generate_pin() -> str:
    """Generate a cryptographically random 6-digit PIN string."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _read_pin_from_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    raw_pin = data.get("pin")
    if isinstance(raw_pin, str) and raw_pin.strip():
        return raw_pin.strip()
    return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="RemoteScreen",
        description="Remote desktop screen viewer and controller via Cloudflare Tunnel.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Local bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=0, help="Local bind port (default: 0 for auto)")
    parser.add_argument("--pin", default=None, help="Access PIN/password (default: random 6 digits)")
    parser.add_argument("--fps", type=float, default=4.0, help="Max capture FPS (default: 4.0)")
    parser.add_argument("--quality", type=int, default=60, help="JPEG quality 20-95 (default: 60)")
    parser.add_argument(
        "--max-width",
        type=int,
        default=1280,
        help="Max stream width in pixels (default: 1280)",
    )
    parser.add_argument(
        "--monitor",
        type=int,
        default=1,
        help="Monitor index for mss capture (default: 1)",
    )
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Run local HTTP server without starting Cloudflare Tunnel",
    )
    parser.add_argument(
        "--no-telegram",
        action="store_true",
        help="Disable Telegram startup/shutdown notifications",
    )
    parser.add_argument(
        "--no-tray",
        action="store_true",
        help="Run without Windows system tray icon (headless/console mode)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Optional path to log file (default: %%LOCALAPPDATA%%\\RemoteScreen\\remote_screen.log)",
    )
    return parser


def load_config(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    config_path: Path | None = None,
) -> Config:
    """Load configuration with precedence: CLI > env > config.json > random PIN."""
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    env_map = os.environ if env is None else env

    cli_pin = args.pin.strip() if isinstance(args.pin, str) and args.pin.strip() else None
    env_raw = env_map.get("REMOTE_PIN", "")
    env_pin = env_raw.strip() if isinstance(env_raw, str) and env_raw.strip() else None

    resolved_config_path = config_path if config_path is not None else (get_app_dir() / "config.json")
    file_pin = _read_pin_from_file(resolved_config_path)

    if cli_pin is not None:
        pin = cli_pin
        pin_is_random = False
    elif env_pin is not None:
        pin = env_pin
        pin_is_random = False
    elif file_pin is not None:
        pin = file_pin
        pin_is_random = False
    else:
        pin = generate_pin()
        pin_is_random = True

    fps = max(0.5, min(30.0, float(args.fps)))
    quality = max(20, min(95, int(args.quality)))
    max_width = max(320, min(3840, int(args.max_width)))
    monitor_index = max(1, int(args.monitor))

    raw_log_file = args.log_file.strip() if isinstance(args.log_file, str) and args.log_file.strip() else None
    log_file = Path(raw_log_file).expanduser() if raw_log_file is not None else None

    return Config(
        pin=pin,
        pin_is_random=pin_is_random,
        host=args.host,
        port=int(args.port),
        fps=fps,
        quality=quality,
        max_width=max_width,
        monitor_index=monitor_index,
        local_only=bool(args.local_only),
        no_telegram=bool(args.no_telegram),
        no_tray=bool(args.no_tray),
        log_file=log_file,
    )
