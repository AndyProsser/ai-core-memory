"""Transparent encryption of private-memory bodies at the ORM boundary (docs/SECURITY.md § Encrypted private memory).

The invariant this module exists to keep: **a record's `body` attribute is only ever plaintext (the session holds the
owner's key) or `crypto.LOCKED` (it doesn't) — never ciphertext.** Every consumer that reads `rec.body` therefore
either gets the real text, which only a credential holding the key could cause, or a harmless placeholder. Writing is
the mirror image: a body is sealed in `before_insert`/`before_update`, and a write that would need a key the session
doesn't hold fails closed with `EncryptionLocked` instead of storing plaintext.

Keys live only in `session.info["keys"]` ({user_id: dek}), attached per request by whoever authenticated it. Background
work (the scheduler, plugins, consolidation, search sync) never attaches any, so it only ever sees placeholders.
"""

from __future__ import annotations

import logging

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import object_session
from sqlalchemy.orm.attributes import set_committed_value
from sqlmodel import Session, select

from . import crypto
from .models import MemoryRecord, MemoryRevision, UserKey

log = logging.getLogger("acm_hub.crypto_store")
KEYS = "keys"


def attach_keys(session: Session, user_id: str | None, dek: bytes | None) -> None:
    if user_id and dek:
        session.info.setdefault(KEYS, {})[user_id] = dek


def key_for(session: OrmSession | None, user_id: str | None) -> bytes | None:
    if session is None or not user_id:
        return None
    return session.info.get(KEYS, {}).get(user_id)


def has_encryption(session: Session, user_id: str | None) -> bool:
    """Has this person turned encrypted private memory on? (A row in user_keys.)"""
    if not user_id:
        return False
    return session.exec(select(UserKey.user_id).where(UserKey.user_id == user_id)).first() is not None


def is_locked(rec: MemoryRecord) -> bool:
    return bool(rec.encrypted) and rec.body == crypto.LOCKED


def aad_record(rec_id: str) -> bytes:
    return f"rec:{rec_id}".encode()


def aad_revision(rev_id: str) -> bytes:
    return f"rev:{rev_id}".encode()


# --- reading: decrypt on load, or show the placeholder ---------------------------------------------------------------------


def _open_or_locked(session: OrmSession | None, owner: str | None, raw: str, aad: bytes) -> str:
    if raw == "":
        return ""  # an empty body is stored as empty (a revision made while locked, or a record with no body)
    key = key_for(session, owner)
    if key is None:
        return crypto.LOCKED
    try:
        return crypto.open_(key, raw, aad).decode()
    except crypto.DecryptionFailed:
        log.warning("could not decrypt a record for %s: wrong key or altered data", owner)
        return crypto.LOCKED


def _on_load_record(target: MemoryRecord, context, *args) -> None:  # noqa: ANN001, ANN002
    attrs = args[0] if args else None  # the "refresh" event passes which attributes were refreshed
    if not target.encrypted or (attrs is not None and "body" not in attrs):
        return
    raw = target.__dict__.get("body", "")
    set_committed_value(
        target, "body", _open_or_locked(object_session(target), target.user_id, raw, aad_record(target.id))
    )


def _on_load_revision(target: MemoryRevision, context, *args) -> None:  # noqa: ANN001, ANN002
    attrs = args[0] if args else None
    if not target.encrypted or (attrs is not None and "body" not in attrs):
        return
    raw = target.__dict__.get("body", "")
    set_committed_value(
        target,
        "body",
        _open_or_locked(object_session(target), target.key_user_id, raw, aad_revision(target.id)),
    )


# --- writing: seal in the flush, restore plaintext after ---------------------------------------------------------------------


def _seal(target, owner: str | None, aad: bytes) -> None:  # noqa: ANN001
    history = inspect(target).attrs.body.history
    if not history.has_changes():
        return  # the body isn't part of this write: leave the stored ciphertext alone
    body = target.body
    if body == crypto.LOCKED:
        raise crypto.EncryptionLocked("refusing to store the locked placeholder as a body")
    key = key_for(object_session(target), owner)
    if key is None:
        raise crypto.EncryptionLocked("this credential can't encrypt private memory")
    target.__dict__["_plain"] = body
    target.body = crypto.seal(key, body.encode(), aad)


def _before_write_record(mapper, connection, target: MemoryRecord) -> None:  # noqa: ANN001
    if target.encrypted:
        _seal(target, target.user_id, aad_record(target.id))


def _before_write_revision(mapper, connection, target: MemoryRevision) -> None:  # noqa: ANN001
    if target.encrypted and target.body != "":
        _seal(target, target.key_user_id, aad_revision(target.id))


def _after_write(mapper, connection, target) -> None:  # noqa: ANN001
    if "_plain" in target.__dict__:
        set_committed_value(target, "body", target.__dict__.pop("_plain"))


for _evt in ("load", "refresh"):
    event.listen(MemoryRecord, _evt, _on_load_record)
    event.listen(MemoryRevision, _evt, _on_load_revision)
event.listen(MemoryRecord, "before_insert", _before_write_record)
event.listen(MemoryRecord, "before_update", _before_write_record)
event.listen(MemoryRevision, "before_insert", _before_write_revision)
event.listen(MemoryRevision, "before_update", _before_write_revision)
for _evt in ("after_insert", "after_update"):
    event.listen(MemoryRecord, _evt, _after_write)
    event.listen(MemoryRevision, _evt, _after_write)
