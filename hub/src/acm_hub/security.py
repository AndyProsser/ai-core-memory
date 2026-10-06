"""Credential primitives: password hashing, API token format, throttling.

Implements docs/SECURITY.md: Argon2id for (low-entropy) passwords, SHA-256 for (256-bit random)
API tokens, recognisable token prefix, constant-time comparisons, plaintext-HTTP policy.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import threading
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

TOKEN_PREFIX = "acm_live_"
_hasher = PasswordHasher()  # Argon2id by default


# --- passwords -----------------------------------------------------------------------------------

MIN_PASSWORD_LENGTH = 12


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    """Always spends comparable time, even for unknown users (pass a dummy hash)."""
    try:
        return _hasher.verify(stored_hash or _DUMMY_HASH, password) and stored_hash is not None
    except (VerificationError, InvalidHashError):
        return False


_DUMMY_HASH = _hasher.hash("not-a-real-password")


def check_password_policy(password: str) -> str | None:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    return None


# --- API tokens ----------------------------------------------------------------------------------


def generate_api_token() -> tuple[str, str, str]:
    """Return (raw_token, sha256_hex, display_prefix). The raw token is shown once and never stored."""
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)  # 256 bits of CSPRNG entropy
    return raw, hash_token(raw), raw[: len(TOKEN_PREFIX) + 4]


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def looks_like_token(value: str) -> bool:
    return value.startswith(TOKEN_PREFIX)


def new_session_secret() -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def safe_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# --- short-lived signed blobs ----------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _blob_mac(secret: str, purpose: str, body: str) -> str:
    # The purpose is part of what is signed, so a blob minted for one use can't be replayed for another.
    return _b64(hmac.new(secret.encode(), f"acm-blob:{purpose}:{body}".encode(), hashlib.sha256).digest())


def sign_blob(secret: str, purpose: str, payload: dict, *, ttl: int) -> str:
    """`<body>.<mac>`: tamper-evident and expiring, not encrypted. Keep secrets out of the payload."""
    body = _b64(json.dumps({**payload, "exp": int(time.time()) + ttl}, separators=(",", ":")).encode())
    return f"{body}.{_blob_mac(secret, purpose, body)}"


def verify_blob(secret: str, purpose: str, blob: str) -> dict | None:
    body, _, mac = (blob or "").partition(".")
    if not body or not safe_equal(mac, _blob_mac(secret, purpose, body)):
        return None
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or int(payload.get("exp", 0)) <= time.time():
        return None
    return payload


# --- network policy ------------------------------------------------------------------------------


_LAN_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "127.0.0.0/8",
        "::1/128",  # loopback
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",  # RFC1918
        "169.254.0.0/16",
        "fe80::/10",
        "fc00::/7",  # link-local, IPv6 unique-local
    )
)


def is_local_or_private(host: str | None) -> bool:
    """Loopback or an RFC1918/link-local address: where plaintext HTTP is tolerated (LAN self-hosting).

    Deliberately an explicit list: `ipaddress.is_private` also covers documentation and other reserved
    ranges that aren't "your own network"."""
    if not host:
        return False
    if host in {"localhost", "testclient"}:  # "testclient" is Starlette's TestClient peer name
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _LAN_NETWORKS)


# --- throttling ----------------------------------------------------------------------------------


class SlidingWindowLimiter:
    """Per-key requests in the last `window` seconds. In-process (the hub is a single process)."""

    def __init__(self, limit: int, window: float = 60.0):
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


class LoginThrottle:
    """Failed-login lockout per key (account or client address)."""

    def __init__(self, max_failures: int = 8, window: float = 900.0):
        self.max_failures = max_failures
        self.window = window
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, q: deque[float], now: float) -> None:
        while q and now - q[0] > self.window:
            q.popleft()

    def blocked(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._fails[key]
            self._prune(q, now)
            return len(q) >= self.max_failures

    def failure(self, key: str) -> None:
        with self._lock:
            self._fails[key].append(time.monotonic())

    def success(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)
