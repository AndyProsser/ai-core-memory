"""Sealed storage for a person's connection credentials (docs/SECURITY.md § Per-user connections).

A connection's token has to be usable by a background job with nobody signed in, so it can't be tied to a person's
passphrase. It is sealed under a key derived from MEMORY_HUB_SECRET_KEY instead: a copy of the database alone is not
enough to use it. The connection id and the secret's name are bound in as associated data, so a sealed value can't be
moved to another connection or field. Changing the hub key makes every sealed value unreadable (`open_` returns None)."""

from __future__ import annotations

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import crypto
from .config import get_settings

_INFO = b"acm-connection-secrets-v1"


def _key() -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO).derive(
        get_settings().secret_key.encode()
    )


def _aad(instance_id: str, name: str) -> bytes:
    return f"conn:{instance_id}:{name}".encode()


def seal(instance_id: str, name: str, value: str) -> str:
    return crypto.seal(_key(), value.encode(), _aad(instance_id, name))


def open_(instance_id: str, name: str, sealed: str) -> str | None:
    """The plaintext, or None if it can't be opened (wrong hub key, tampered, moved to another row)."""
    try:
        return crypto.open_(_key(), sealed, _aad(instance_id, name)).decode()
    except Exception:  # noqa: BLE001 — a secret that won't open is "not set", never a crash
        return None
