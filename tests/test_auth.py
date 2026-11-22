"""Unit tests for config and auth modules."""

from __future__ import annotations

import json
from pathlib import Path

from remote_screen.auth import AuthManager, extract_client_ip
from remote_screen.config import generate_pin, load_config


class FakeClock:
    def __init__(self, initial: float = 1000.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_generate_pin_format() -> None:
    pins = {generate_pin() for _ in range(20)}
    for pin in pins:
        assert len(pin) == 6
        assert pin.isdigit()


def test_load_config_pin_precedence(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"pin": "111111"}), encoding="utf-8")

    # 1. CLI overrides env and file
    cfg = load_config(["--pin", "999999"], env={"REMOTE_PIN": "222222"}, config_path=cfg_file)
    assert cfg.pin == "999999"
    assert cfg.pin_is_random is False

    # 2. Env overrides file
    cfg = load_config([], env={"REMOTE_PIN": "222222"}, config_path=cfg_file)
    assert cfg.pin == "222222"
    assert cfg.pin_is_random is False

    # 3. File overrides random
    cfg = load_config([], env={}, config_path=cfg_file)
    assert cfg.pin == "111111"
    assert cfg.pin_is_random is False

    # 4. Fallback to random
    cfg = load_config([], env={}, config_path=tmp_path / "missing.json")
    assert len(cfg.pin) == 6
    assert cfg.pin.isdigit()
    assert cfg.pin_is_random is True


def test_extract_client_ip_prefers_cf_connecting_ip() -> None:
    headers = {"X-Forwarded-For": "10.0.0.1", "CF-Connecting-IP": "203.0.113.42"}
    assert extract_client_ip(headers, "127.0.0.1") == "203.0.113.42"
    assert extract_client_ip({"cf-connecting-ip": " 198.51.100.7 "}, "::1") == "198.51.100.7"
    # Non-loopback remote cannot spoof CF-Connecting-IP
    assert extract_client_ip({"CF-Connecting-IP": "1.2.3.4"}, "198.51.100.50") == "198.51.100.50"
    assert extract_client_ip({}, "127.0.0.1") == "127.0.0.1"
    assert extract_client_ip({}, None) == "unknown"


def test_session_lifecycle_and_expiration() -> None:
    clock = FakeClock()
    auth = AuthManager("654321", session_ttl=120.0, clock=clock)

    ok, token, retry = auth.authenticate("1.2.3.4", "654321")
    assert ok is True
    assert token is not None
    assert retry == 0
    assert auth.validate_session(token) is True
    assert auth.validate_session("wrong-token") is False
    assert auth.validate_session(None) is False

    # Expires after TTL
    clock.advance(119.0)
    assert auth.validate_session(token) is True
    clock.advance(2.0)
    assert auth.validate_session(token) is False

    # Revoke removes valid token immediately
    ok2, token2, _ = auth.authenticate("1.2.3.4", "654321")
    assert ok2 is True and token2 is not None
    assert auth.validate_session(token2) is True
    auth.revoke_session(token2)
    assert auth.validate_session(token2) is False


def test_rate_limit_lockout_isolation_and_reset() -> None:
    clock = FakeClock()
    auth = AuthManager("123456", max_attempts=5, lockout_seconds=60.0, clock=clock)

    ip_bad = extract_client_ip({"CF-Connecting-IP": "198.51.100.10"}, "127.0.0.1")
    ip_good = extract_client_ip({"CF-Connecting-IP": "198.51.100.20"}, "127.0.0.1")

    for _ in range(4):
        ok, token, retry = auth.authenticate(ip_bad, "000000")
        assert ok is False and token is None and retry == 0

    # 5th failure triggers lockout
    ok, token, retry = auth.authenticate(ip_bad, "000000")
    assert ok is False and token is None and retry > 0

    # Even valid PIN fails while locked out
    ok, token, retry = auth.authenticate(ip_bad, "123456")
    assert ok is False and token is None and retry > 0

    # Another IP is unaffected
    ok_other, token_other, retry_other = auth.authenticate(ip_good, "123456")
    assert ok_other is True and token_other is not None and retry_other == 0

    # After lockout expires, bad IP can log in again and failure counter resets
    clock.advance(61.0)
    ok, token, retry = auth.authenticate(ip_bad, "123456")
    assert ok is True and token is not None and retry == 0

    # Counter was reset: 1 wrong attempt doesn't re-lock
    ok, token, retry = auth.authenticate(ip_bad, "000000")
    assert ok is False and token is None and retry == 0


def test_pin_stored_as_salted_pbkdf2_hash() -> None:
    auth = AuthManager("654321")
    assert not hasattr(auth, "_pin_bytes")
    assert len(auth._salt) == 16
    assert len(auth._pin_hash) == 32
    assert b"654321" not in auth._pin_hash


def test_global_rate_limit_blocks_distributed_ips() -> None:
    clock = FakeClock()
    auth = AuthManager(
        "123456",
        max_attempts=5,
        lockout_seconds=60.0,
        global_max_attempts=10,
        global_lockout_seconds=90.0,
        clock=clock,
    )

    for _ in range(5):
        auth.authenticate("198.51.100.1", "000000")
    for _ in range(4):
        ok, token, retry = auth.authenticate("198.51.100.2", "000000")
        assert ok is False and token is None and retry == 0

    # 10th total failure triggers global lockout
    ok, token, retry = auth.authenticate("198.51.100.2", "000000")
    assert ok is False and token is None and retry == 90

    # Fresh third IP is blocked even with valid PIN while global lockout is active
    ok3, token3, retry3 = auth.authenticate("198.51.100.3", "123456")
    assert ok3 is False and token3 is None and retry3 == 90

    clock.advance(91.0)
    ok3_after, token3_after, retry3_after = auth.authenticate("198.51.100.3", "123456")
    assert ok3_after is True and token3_after is not None and retry3_after == 0


def test_tracked_client_map_is_bounded() -> None:
    auth = AuthManager("123456", max_tracked_clients=5, global_max_attempts=100)
    for idx in range(10):
        auth.authenticate(f"198.51.100.{idx}", "000000")
    assert len(auth._failures) <= 5
