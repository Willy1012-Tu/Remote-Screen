"""PIN authentication, session token management, and per-IP rate limiting."""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import time
from typing import Callable, Mapping


_LOOPBACK_ADDRS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def extract_client_ip(headers: Mapping[str, str], remote: str | None) -> str:
    """Extract client IP, trusting CF-Connecting-IP only over loopback tunnel proxies."""
    remote_ip = remote.strip() if remote and remote.strip() else ""
    if remote_ip in _LOOPBACK_ADDRS:
        for key, value in headers.items():
            if key.lower() == "cf-connecting-ip":
                stripped = value.strip()
                if stripped:
                    return stripped
    return remote_ip or "unknown"


class AuthManager:
    """Manages salted PBKDF2 PIN verification, rate limiting, and cookie sessions."""

    def __init__(
        self,
        pin: str,
        max_attempts: int = 5,
        lockout_seconds: float = 60.0,
        session_ttl: float = 43200.0,
        global_max_attempts: int = 20,
        global_lockout_seconds: float = 120.0,
        max_tracked_clients: int = 1024,
        pbkdf2_iterations: int = 100_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not pin:
            raise ValueError("PIN must not be empty")
        self._salt = secrets.token_bytes(16)
        self._pbkdf2_iterations = max(1, int(pbkdf2_iterations))
        self._pin_hash = hashlib.pbkdf2_hmac(
            "sha256",
            pin.encode("utf-8"),
            self._salt,
            self._pbkdf2_iterations,
        )
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self.session_ttl = session_ttl
        self.global_max_attempts = global_max_attempts
        self.global_lockout_seconds = global_lockout_seconds
        self.max_tracked_clients = max(1, int(max_tracked_clients))
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._lockouts: dict[str, float] = {}
        self._global_failures = 0
        self._global_lockout_until: float | None = None
        self._sessions: dict[str, float] = {}

    def _hash_pin(self, candidate_pin: str) -> bytes:
        return hashlib.pbkdf2_hmac(
            "sha256",
            candidate_pin.encode("utf-8"),
            self._salt,
            self._pbkdf2_iterations,
        )

    def _evict_if_needed(self, now: float, client_id: str) -> None:
        if client_id in self._failures:
            return
        expired = [cid for cid, until in self._lockouts.items() if until <= now]
        for cid in expired:
            self._lockouts.pop(cid, None)
            self._failures.pop(cid, None)
        while len(self._failures) >= self.max_tracked_clients:
            oldest = next(iter(self._failures))
            self._failures.pop(oldest, None)
            self._lockouts.pop(oldest, None)

    def check_rate_limit(self, client_id: str) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds) for a client identifier."""
        now = self._clock()
        global_retry = 0
        if self._global_lockout_until is not None:
            g_rem = self._global_lockout_until - now
            if g_rem > 0:
                global_retry = max(1, math.ceil(g_rem))
            else:
                self._global_lockout_until = None
                self._global_failures = 0

        client_retry = 0
        until = self._lockouts.get(client_id)
        if until is not None:
            c_rem = until - now
            if c_rem > 0:
                client_retry = max(1, math.ceil(c_rem))
            else:
                self._lockouts.pop(client_id, None)
                self._failures.pop(client_id, None)

        retry = max(global_retry, client_retry)
        if retry > 0:
            return False, retry
        return True, 0

    def authenticate(self, client_id: str, candidate_pin: str) -> tuple[bool, str | None, int]:
        """Verify candidate_pin for client_id.

        Returns (ok, session_token_or_None, retry_after_seconds).
        """
        allowed, retry_after = self.check_rate_limit(client_id)
        if not allowed:
            return False, None, retry_after

        now = self._clock()
        candidate_hash = self._hash_pin(candidate_pin)
        if hmac.compare_digest(candidate_hash, self._pin_hash):
            self._failures.pop(client_id, None)
            self._lockouts.pop(client_id, None)
            self._global_failures = 0
            self._global_lockout_until = None
            expired_sessions = [tok for tok, exp in self._sessions.items() if now >= exp]
            for tok in expired_sessions:
                self._sessions.pop(tok, None)
            token = secrets.token_urlsafe(32)
            self._sessions[token] = now + self.session_ttl
            return True, token, 0

        self._evict_if_needed(now, client_id)
        attempts = self._failures.get(client_id, 0) + 1
        self._failures[client_id] = attempts
        self._global_failures += 1

        client_retry = 0
        if attempts >= self.max_attempts:
            self._lockouts[client_id] = now + self.lockout_seconds
            client_retry = max(1, math.ceil(self.lockout_seconds))

        global_retry = 0
        if self._global_failures >= self.global_max_attempts:
            self._global_lockout_until = now + self.global_lockout_seconds
            global_retry = max(1, math.ceil(self.global_lockout_seconds))

        return False, None, max(client_retry, global_retry)

    def validate_session(self, token: str | None) -> bool:
        """Check whether a session token exists and has not expired."""
        if not token:
            return False
        expires_at = self._sessions.get(token)
        if expires_at is None:
            return False
        if self._clock() >= expires_at:
            self._sessions.pop(token, None)
            return False
        return True

    def revoke_session(self, token: str | None) -> None:
        """Invalidate a session token if present."""
        if token:
            self._sessions.pop(token, None)
