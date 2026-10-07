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
from collections import deque

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


# The one hostname that counts as "this machine". Real peers are always IP addresses, so production code names nothing
# else; the tests widen this with Starlette's "testclient" peer name (tests/conftest.py).
_LOCAL_HOSTNAMES = frozenset({"localhost"})

_LINK_LOCAL = (ipaddress.ip_network("169.254.0.0/16"), ipaddress.ip_network("fe80::/10"))


def is_link_local(host: str | None) -> bool:
    """169.254.0.0/16 and fe80::/10 — where cloud metadata services live. Fine for a LAN check, never an egress target."""
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _LINK_LOCAL)


def is_local_or_private(host: str | None) -> bool:
    """Loopback or an RFC1918/link-local address: where plaintext HTTP is tolerated (LAN self-hosting).

    Deliberately an explicit list: `ipaddress.is_private` also covers documentation and other reserved
    ranges that aren't "your own network"."""
    if not host:
        return False
    if host in _LOCAL_HOSTNAMES:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _LAN_NETWORKS)


# --- throttling ----------------------------------------------------------------------------------

_MAX_KEYS = (
    10_000  # keys are caller-chosen (an email address, an IP), so the tables must not grow without bound
)


def _sweep(table: dict[str, deque[float]], now: float, window: float) -> None:
    """Drop keys with nothing left in their window; if an attacker still fills the table, drop the oldest."""
    for k in [k for k, q in table.items() if not q or now - q[-1] > window]:
        del table[k]
    while len(table) > _MAX_KEYS:
        del table[next(iter(table))]


class SlidingWindowLimiter:
    """Per-key requests in the last `window` seconds. In-process (the hub is a single process)."""

    def __init__(self, limit: int, window: float = 60.0):
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            if len(self._hits) > _MAX_KEYS // 2:
                _sweep(self._hits, now, self.window)
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


class LoginThrottle:
    """Failed-login lockout. Each key (account+address, account, address) has its own budget, so one address can't
    lock a person out of their own account: it runs out of its own budget first."""

    def __init__(self, max_failures: int = 8, window: float = 900.0):
        self.max_failures = max_failures
        self.window = window
        self._fails: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, key: str, limit: int | None = None) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._fails.get(key)  # never creates an entry: a lookup mustn't cost memory
            if q is None:
                return False
            while q and now - q[0] > self.window:
                q.popleft()
            return len(q) >= (limit or self.max_failures)

    def failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            if len(self._fails) > _MAX_KEYS // 2:
                _sweep(self._fails, now, self.window)
            self._fails.setdefault(key, deque()).append(now)

    def success(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)

    # --- a sign-in attempt, counted against three keys (keys are length-capped: they're caller-chosen) ---------

    ACCOUNT_LIMIT = (
        40  # across every address: well above the per-address budget, so a lone guesser is stopped first
    )

    @staticmethod
    def _attempt_keys(email: str, ip: str) -> tuple[str, str, str]:
        e = email.strip().lower()[:254]
        return f"acct-ip:{e}|{ip}", f"ip:{ip}", f"acct:{e}"

    def attempt_blocked(self, email: str, ip: str) -> bool:
        pair, by_ip, acct = self._attempt_keys(email, ip)
        return self.blocked(pair) or self.blocked(by_ip) or self.blocked(acct, self.ACCOUNT_LIMIT)

    def attempt_failed(self, email: str, ip: str) -> None:
        for k in self._attempt_keys(email, ip):
            self.failure(k)

    def attempt_succeeded(self, email: str, ip: str) -> None:
        """Clears this address's counts. The account-wide count is left to age out, so a success from one address
        can't be used to reset the budget while another address keeps guessing."""
        pair, by_ip, _ = self._attempt_keys(email, ip)
        self.success(pair)
        self.success(by_ip)
