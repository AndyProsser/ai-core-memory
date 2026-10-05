"""Key management for encrypted private memory (docs/SECURITY.md § Encrypted private memory).

Every operation that needs the data key takes the passphrase (or recovery key) and derives it for that call only. The hub
never stores a passphrase, a recovery key or an unwrapped data key; `UnlockCache` holds a data key in server memory for
the life of an unlocked web session and nowhere else.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, col, select

from . import crypto, crypto_store
from .access import AccessError
from .models import ApiToken, MemoryRecord, MemoryRevision, OAuthCode, OAuthGrant, User, UserKey, utcnow
from .records import ValidationFailed, _fts_sync
from .security import check_password_policy

AAD_PASSPHRASE = b"acm-dek:passphrase"
AAD_RECOVERY = b"acm-dek:recovery"


def get_keys(session: Session, user_id: str) -> UserKey | None:
    return session.get(UserKey, user_id)


def is_enabled(session: Session, user_id: str) -> bool:
    return get_keys(session, user_id) is not None


def _kek(
    passphrase: str, uk_or_params: UserKey | None, salt: bytes | None = None
) -> tuple[bytes, bytes, dict]:
    if uk_or_params is None:
        salt = salt or crypto.new_salt()
        params = {
            "time": crypto.KDF_TIME,
            "memory_kib": crypto.KDF_MEMORY_KIB,
            "parallelism": crypto.KDF_PARALLELISM,
        }
    else:
        salt = crypto.b64d(uk_or_params.kdf_salt)
        params = {
            "time": uk_or_params.kdf_time,
            "memory_kib": uk_or_params.kdf_memory_kib,
            "parallelism": uk_or_params.kdf_parallelism,
        }
    return crypto.derive_kek(passphrase, salt, **params), salt, params


def unlock(session: Session, user_id: str, passphrase: str) -> bytes | None:
    """The data key, or None for a wrong passphrase. (A GCM tag is the check: there's no separate verifier to attack.)"""
    uk = get_keys(session, user_id)
    if uk is None or not passphrase:
        return None
    kek, _, _ = _kek(passphrase, uk)
    try:
        return crypto.open_(kek, uk.wrapped_by_passphrase, AAD_PASSPHRASE)
    except crypto.DecryptionFailed:
        return None


def _check_new_passphrase(passphrase: str) -> None:
    if problem := check_password_policy(passphrase):
        raise ValidationFailed(f"Memory passphrase: {problem}")


def _wrap(dek: bytes, passphrase: str) -> tuple[str, str, dict]:
    kek, salt, params = _kek(passphrase, None)
    return crypto.seal(kek, dek, AAD_PASSPHRASE), crypto.b64e(salt), params


# --- enabling and disabling -----------------------------------------------------------------------------------------------


@dataclass
class Enabled:
    recovery_key: str  # shown to the person exactly once
    dek: bytes  # to unlock the enabling session
    records: int


def scrub(session: Session) -> None:
    """Rewrite the database file so plaintext that existed before encryption was switched on doesn't linger in freed
    pages or the write-ahead log. Call after the enabling commit; best effort (a busy database just skips it)."""
    try:
        bind = session.get_bind()
        with bind.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            c.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
            c.execute(text("VACUUM"))
            c.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    except Exception:  # noqa: BLE001 — hygiene, never a reason to fail the enable that already succeeded
        pass


def enable(session: Session, user: User, passphrase: str) -> Enabled:
    if is_enabled(session, user.id):
        raise ValidationFailed("Encrypted private memory is already on.")
    _check_new_passphrase(passphrase)
    dek = crypto.new_key()
    wrapped, salt_b64, params = _wrap(dek, passphrase)
    rec_raw, rec_text = crypto.new_recovery_key()
    session.add(
        UserKey(
            user_id=user.id,
            kdf_salt=salt_b64,
            kdf_time=params["time"],
            kdf_memory_kib=params["memory_kib"],
            kdf_parallelism=params["parallelism"],
            wrapped_by_passphrase=wrapped,
            wrapped_by_recovery=crypto.seal(rec_raw, dek, AAD_RECOVERY),
        )
    )
    crypto_store.attach_keys(session, user.id, dek)
    n = _encrypt_existing(session, user)
    session.flush()
    return Enabled(rec_text, dek, n)


def _encrypt_existing(session: Session, user: User) -> int:
    """Seal what is already there. Plaintext bodies are loaded (not yet flagged encrypted), flagged, and re-saved: the
    ORM layer seals them in the flush. The search index drops the body in the same step."""
    recs = session.exec(
        select(MemoryRecord).where(
            MemoryRecord.scope == "user",
            MemoryRecord.user_id == user.id,
            col(MemoryRecord.encrypted).is_(False),
        )
    ).all()
    ids = [r.id for r in recs]
    for r in recs:
        r.encrypted = True
        flag_modified(r, "body")
    if ids:
        for rev in session.exec(
            select(MemoryRevision).where(
                col(MemoryRevision.memory_record_id).in_(ids), col(MemoryRevision.encrypted).is_(False)
            )
        ).all():
            rev.encrypted, rev.key_user_id = True, user.id
            flag_modified(rev, "body")
    session.flush()
    for r in recs:
        _fts_sync(session, r)  # now without the body
    return len(recs)


def disable(session: Session, user: User, passphrase: str) -> int:
    dek = unlock(session, user.id, passphrase)
    if dek is None:
        raise AccessError("That isn't your memory passphrase.")
    crypto_store.attach_keys(session, user.id, dek)
    recs = session.exec(
        select(MemoryRecord).where(MemoryRecord.user_id == user.id, col(MemoryRecord.encrypted).is_(True))
    ).all()
    revs = session.exec(
        select(MemoryRevision).where(
            MemoryRevision.key_user_id == user.id, col(MemoryRevision.encrypted).is_(True)
        )
    ).all()
    # Refuse to write the placeholder over anything: if any body can't be opened, stop with nothing changed.
    if any(r.body == crypto.LOCKED for r in recs) or any(v.body == crypto.LOCKED for v in revs):
        raise ValidationFailed(
            "Some encrypted text couldn't be opened with this key, so nothing was changed."
        )
    for r in recs:
        r.encrypted = False
        flag_modified(r, "body")
    for v in revs:
        v.encrypted, v.key_user_id = False, None
        flag_modified(v, "body")
    session.flush()
    for r in recs:
        _fts_sync(session, r)  # back in the index, with its body
    drop_token_wraps(session, user.id)
    session.delete(get_keys(session, user.id))
    session.flush()
    return len(recs)


# --- passphrase and recovery ----------------------------------------------------------------------------------------------------


def change_passphrase(session: Session, user: User, old: str, new: str) -> None:
    dek = unlock(session, user.id, old)
    if dek is None:
        raise AccessError("That isn't your current memory passphrase.")
    _check_new_passphrase(new)
    _rewrap(session, user.id, dek, new)


def _rewrap(session: Session, user_id: str, dek: bytes, passphrase: str) -> None:
    uk = get_keys(session, user_id)
    assert uk is not None
    wrapped, salt_b64, params = _wrap(dek, passphrase)
    uk.wrapped_by_passphrase, uk.kdf_salt = wrapped, salt_b64
    uk.kdf_time, uk.kdf_memory_kib, uk.kdf_parallelism = (
        params["time"],
        params["memory_kib"],
        params["parallelism"],
    )
    uk.passphrase_changed_at = utcnow()
    session.add(uk)
    session.flush()


def regenerate_recovery_key(session: Session, user: User, passphrase: str) -> str:
    dek = unlock(session, user.id, passphrase)
    if dek is None:
        raise AccessError("That isn't your memory passphrase.")
    raw, text = crypto.new_recovery_key()
    uk = get_keys(session, user.id)
    assert uk is not None
    uk.wrapped_by_recovery = crypto.seal(raw, dek, AAD_RECOVERY)
    session.add(uk)
    session.flush()
    return text


def recover(session: Session, user: User, recovery_key: str, new_passphrase: str) -> bytes:
    """Forgotten passphrase: the recovery key opens the data key, and a new passphrase takes over."""
    uk = get_keys(session, user.id)
    raw = crypto.parse_recovery_key(recovery_key)
    if uk is None or raw is None:
        raise AccessError("That isn't a valid recovery key.")
    try:
        dek = crypto.open_(raw, uk.wrapped_by_recovery, AAD_RECOVERY)
    except crypto.DecryptionFailed:
        raise AccessError("That recovery key doesn't match.") from None
    _check_new_passphrase(new_passphrase)
    _rewrap(session, user.id, dek, new_passphrase)
    return dek


# --- handing the key to a token, and taking it back ----------------------------------------------------------------------------


def wrap_for_token(raw_token: str, dek: bytes) -> str:
    return crypto.wrap_for_secret(raw_token, dek, "token")


def unwrap_for_token(raw_token: str, wrapped: str | None) -> bytes | None:
    return crypto.unwrap_from_secret(raw_token, wrapped, "token")


def drop_token_wraps(session: Session, user_id: str) -> None:
    """Everything that could open this person's memory without their passphrase loses its copy of the key."""
    for t in session.exec(
        select(ApiToken).where(ApiToken.user_id == user_id, col(ApiToken.wrapped_dek).is_not(None))
    ).all():
        t.wrapped_dek = None
        session.add(t)
    for g in session.exec(
        select(OAuthGrant).where(OAuthGrant.user_id == user_id, col(OAuthGrant.wrapped_dek).is_not(None))
    ).all():
        g.wrapped_dek = None
        session.add(g)
    for c in session.exec(
        select(OAuthCode).where(OAuthCode.user_id == user_id, col(OAuthCode.wrapped_dek).is_not(None))
    ).all():
        c.wrapped_dek = None
        session.add(c)


# --- the unlocked-session cache (server memory only) ---------------------------------------------------------------------------


class UnlockCache:
    """Data keys for unlocked web sessions. In memory, never persisted: a restart locks everyone, which is intended."""

    IDLE_SECONDS = 30 * 60

    def __init__(self, idle: float | None = None):
        self.idle = self.IDLE_SECONDS if idle is None else idle
        self._d: dict[str, tuple[str, bytes, float]] = {}

    def put(self, session_id: str, user_id: str, dek: bytes) -> None:
        self.sweep()
        self._d[session_id] = (user_id, dek, time.monotonic() + self.idle)

    def get(self, session_id: str, user_id: str) -> bytes | None:
        entry = self._d.get(session_id)
        if entry is None:
            return None
        owner, dek, expires = entry
        if owner != user_id or time.monotonic() > expires:
            self._d.pop(session_id, None)
            return None
        self._d[session_id] = (owner, dek, time.monotonic() + self.idle)  # activity keeps it open
        return dek

    def drop(self, session_id: str) -> None:
        self._d.pop(session_id, None)

    def drop_user(self, user_id: str) -> None:
        for k in [k for k, (u, _, _) in self._d.items() if u == user_id]:
            self._d.pop(k, None)

    def sweep(self) -> None:
        now = time.monotonic()
        for k in [k for k, (_, _, e) in self._d.items() if now > e]:
            self._d.pop(k, None)
