"""PIN authentication, session token management, and per-IP rate limiting."""

from __future__ import annotations

import hmac
import math
import secrets
import time
from typing import Callable, Mapping


def extract_client_ip(headers: Mapping[str, str], remote: str | None) -> str:
    """Extract client IP, preferring Cloudflare's CF-Connecting-IP header."""
    for key, value in headers.items():
        if key.lower() == "cf-connecting-ip":
            stripped = value.strip()
            if stripped:
                return stripped
    if remote and remote.strip():
        return remote.strip()
    return "unknown"


class AuthManager:
    """Manages PIN verification, per-client lockout, and bearer/cookie sessions."""

    def __init__(
        self,
        pin: str,
        max_attempts: int = 5,
        lockout_seconds: float = 60.0,
        session_ttl: float = 43200.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not pin:
            raise ValueError("PIN must not be empty")
        self._pin_bytes = pin.encode("utf-8")
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self.session_ttl = session_ttl
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._lockouts: dict[str, float] = {}
        self._sessions: dict[str, float] = {}

    def check_rate_limit(self, client_id: str) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds) for a client identifier."""
        now = self._clock()
        until = self._lockouts.get(client_id)
        if until is None:
            return True, 0
        remaining = until - now
        if remaining > 0:
            return False, max(1, math.ceil(remaining))
        self._lockouts.pop(client_id, None)
        self._failures.pop(client_id, None)
        return True, 0

    def authenticate(self, client_id: str, candidate_pin: str) -> tuple[bool, str | None, int]:
        """Verify candidate_pin for client_id.

        Returns (ok, session_token_or_None, retry_after_seconds).
        """
        allowed, retry_after = self.check_rate_limit(client_id)
        if not allowed:
            return False, None, retry_after

        candidate_bytes = candidate_pin.encode("utf-8")
        if hmac.compare_digest(candidate_bytes, self._pin_bytes):
            self._failures.pop(client_id, None)
            self._lockouts.pop(client_id, None)
            token = secrets.token_urlsafe(32)
            self._sessions[token] = self._clock() + self.session_ttl
            return True, token, 0

        attempts = self._failures.get(client_id, 0) + 1
        self._failures[client_id] = attempts
        if attempts >= self.max_attempts:
            self._lockouts[client_id] = self._clock() + self.lockout_seconds
            return False, None, max(1, math.ceil(self.lockout_seconds))
        return False, None, 0

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
