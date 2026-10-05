"""Encryption primitives for private memory (docs/SECURITY.md § Encrypted private memory).

Nothing here is novel: AES-256-GCM for sealing, Argon2id for turning a passphrase into a key, HKDF for deriving a
wrapping key from a high-entropy token. All of it comes from the `cryptography` and `argon2-cffi` libraries.
"""

from __future__ import annotations

import base64
import secrets

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEY_BYTES = 32
NONCE_BYTES = 12
MIN_PASSPHRASE_LENGTH = 12

# Argon2id defaults (OWASP's second recommended profile). Stored per user, so they can be raised later without
# locking anyone out; tests lower them for speed.
KDF_TIME = 3
KDF_MEMORY_KIB = 64 * 1024
KDF_PARALLELISM = 4

# What every consumer sees in place of a body it cannot read. Never ciphertext, never empty text that might be
# mistaken for content, and a value `write_record` refuses to store.
LOCKED = (
    "🔒 This private memory is encrypted and locked. Unlock it in the web UI (Settings → Account) to read it."
)


class EncryptionLocked(Exception):
    """A credential without the key tried to write encrypted memory. Callers turn this into a clear refusal."""


class DecryptionFailed(Exception):
    """Wrong key, or the ciphertext was altered or moved to a different row."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_key() -> bytes:
    return secrets.token_bytes(KEY_BYTES)


def seal(key: bytes, plaintext: bytes, aad: bytes) -> str:
    """AES-256-GCM with a fresh random 96-bit nonce; `aad` binds the ciphertext to its purpose or row."""
    nonce = secrets.token_bytes(NONCE_BYTES)
    return "v1." + _b64e(nonce + AESGCM(key).encrypt(nonce, plaintext, aad))


def open_(key: bytes, sealed: str, aad: bytes) -> bytes:
    try:
        if not sealed.startswith("v1."):
            raise ValueError("unknown format")
        raw = _b64d(sealed[3:])
        return AESGCM(key).decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], aad)
    except (InvalidTag, ValueError) as e:
        raise DecryptionFailed("could not decrypt") from e


def derive_kek(passphrase: str, salt: bytes, *, time: int, memory_kib: int, parallelism: int) -> bytes:
    return hash_secret_raw(
        passphrase.encode(),
        salt,
        time_cost=time,
        memory_cost=memory_kib,
        parallelism=parallelism,
        hash_len=KEY_BYTES,
        type=Type.ID,
    )


def new_salt() -> bytes:
    return secrets.token_bytes(16)


def b64e(raw: bytes) -> str:
    return _b64e(raw)


def b64d(text: str) -> bytes:
    return _b64d(text)


# --- recovery key: 256 random bits in a form a person can write down ----------------------------------------------


def new_recovery_key() -> tuple[bytes, str]:
    raw = secrets.token_bytes(KEY_BYTES)
    text = base64.b32encode(raw).decode().rstrip("=")
    return raw, "-".join(text[i : i + 8] for i in range(0, len(text), 8))


def parse_recovery_key(text: str) -> bytes | None:
    cleaned = "".join(text.upper().split()).replace("-", "")
    try:
        raw = base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))
    except ValueError:
        return None
    return raw if len(raw) == KEY_BYTES else None


# --- wrapping the DEK under a token's own secret ------------------------------------------------------------------------


def _token_key(raw_token: str) -> bytes:
    """Derived from the raw token (256 bits of entropy), which the hub never stores — only a SHA-256 of it. The stored
    hash can't produce this key, so a stolen database can't unwrap a token's copy of the DEK."""
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=b"acm-token-wrap-v1").derive(
        raw_token.encode()
    )


def wrap_for_secret(raw_secret: str, dek: bytes, purpose: str) -> str:
    return seal(_token_key(raw_secret), dek, f"acm-wrap:{purpose}".encode())


def unwrap_from_secret(raw_secret: str, wrapped: str | None, purpose: str) -> bytes | None:
    if not wrapped:
        return None
    try:
        return open_(_token_key(raw_secret), wrapped, f"acm-wrap:{purpose}".encode())
    except DecryptionFailed:
        return None
