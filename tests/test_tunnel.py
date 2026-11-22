"""Unit tests for CloudflareTunnel process management and URL parsing."""

from __future__ import annotations

from pathlib import Path
import sys
import time

import pytest

from remote_screen.tunnel import (
    CloudflareTunnel,
    build_ssl_context,
    ensure_cloudflared,
    extract_trycloudflare_url,
    find_cloudflared,
)


def test_build_ssl_context_loads_ca_bundle() -> None:
    ctx = build_ssl_context()
    assert ctx.cert_store_stats()["x509_ca"] >= 100


def test_extract_trycloudflare_url() -> None:
    sample = (
        "2026-11-22T05:00:00Z INF +------------------------------------------------------------+\n"
        "2026-11-22T05:00:00Z INF |  https://bright-ocean-42.trycloudflare.com               |\n"
    )
    assert extract_trycloudflare_url(sample) == "https://bright-ocean-42.trycloudflare.com"
    assert extract_trycloudflare_url("no url here") is None


def test_find_and_ensure_cloudflared_atomic(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    bin_path = app_dir / "bin" / "cloudflared.exe"
    bin_path.parent.mkdir(parents=True)
    bin_path.write_bytes(b"fake-exe")

    assert find_cloudflared(app_dir=app_dir) == bin_path

    download_target = tmp_path / "cache" / "cloudflared.exe"

    def fake_download(url: str, dest: Path) -> None:
        assert "cloudflared-windows-amd64.exe" in url
        dest.write_bytes(b"downloaded")

    import hashlib

    expected = hashlib.sha256(b"downloaded").hexdigest()
    result = ensure_cloudflared(
        target_path=download_target,
        download_fn=fake_download,
        expected_sha256=expected,
    )
    assert result == download_target
    assert download_target.read_bytes() == b"downloaded"


def test_ensure_cloudflared_rejects_bad_sha256(tmp_path: Path) -> None:
    target = tmp_path / "cache" / "cloudflared.exe"

    def bad_download(_url: str, dest: Path) -> None:
        dest.write_bytes(b"tampered-binary")

    with pytest.raises(RuntimeError, match="SHA-256"):
        ensure_cloudflared(
            target_path=target,
            download_fn=bad_download,
            expected_sha256="0" * 64,
        )
    assert not target.exists()


def test_tunnel_start_drain_and_stop_lifecycle() -> None:
    script = (
        "import sys, time\n"
        "print('starting tunnel', flush=True)\n"
        "print('INF | https://quick-fox-99.trycloudflare.com |', file=sys.stderr, flush=True)\n"
        "while True:\n"
        "    print('heartbeat log line', flush=True)\n"
        "    time.sleep(0.1)\n"
    )
    tunnel = CloudflareTunnel(Path(sys.executable), command_args=["-u", "-c", script])
    try:
        url = tunnel.start("http://127.0.0.1:8080", timeout=5.0)
        assert url == "https://quick-fox-99.trycloudflare.com"
        assert tunnel.is_running() is True
        time.sleep(0.25)
        assert any("heartbeat log line" in line for line in tunnel.recent_logs)
    finally:
        tunnel.stop()
    assert tunnel.is_running() is False
    # Idempotent stop
    tunnel.stop()


def test_tunnel_early_exit_raises_runtime_error() -> None:
    script = "import sys; print('ERR failed to connect', file=sys.stderr, flush=True); sys.exit(2)"
    tunnel = CloudflareTunnel(Path(sys.executable), command_args=["-u", "-c", script])
    with pytest.raises(RuntimeError, match="failed to connect"):
        tunnel.start("http://127.0.0.1:8080", timeout=5.0)
    assert tunnel.is_running() is False


def test_tunnel_timeout_terminates_process() -> None:
    script = "import time; print('waiting...', flush=True); time.sleep(30)"
    tunnel = CloudflareTunnel(Path(sys.executable), command_args=["-u", "-c", script])
    with pytest.raises(RuntimeError, match="Timed out"):
        tunnel.start("http://127.0.0.1:8080", timeout=0.4)
    assert tunnel.is_running() is False
