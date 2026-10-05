"""Encrypted private memory: enable, unlock, lock, change passphrase, recover, disable (docs/SECURITY.md).

Everything here is for a signed-in person acting on their own memory. Passphrases arrive in POST bodies only, are never
logged or put in a URL, and every attempt is rate-limited per person.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from .. import keys
from ..access import AccessError
from ..records import ValidationFailed
from .deps import Ctx, notice_url, render, user_csrf

router = APIRouter()
BACK = "/settings"


def _go(message: str) -> RedirectResponse:
    return RedirectResponse(notice_url(BACK, message), status_code=303)


def _throttle(request: Request, ctx: Ctx) -> None:
    if not request.app.state.key_throttle.allow(ctx.user.id):
        raise AccessError("Too many passphrase attempts. Wait a few minutes and try again.")


def _unlock_session(request: Request, ctx: Ctx, dek: bytes) -> None:
    request.app.state.unlock_cache.put(ctx.ws.id, ctx.user.id, dek)


@router.post("/settings/memory-key/enable")
def enable(
    request: Request,
    passphrase: str = Form(""),
    confirm: str = Form(""),
    ctx: Ctx = Depends(user_csrf),
):  # noqa: ANN201
    if passphrase != confirm:
        return _go("The two passphrases don't match.")
    try:
        res = keys.enable(ctx.db, ctx.user, passphrase)
        ctx.db.commit()
        keys.scrub(ctx.db)
    except ValidationFailed as e:
        ctx.db.rollback()
        return _go(str(e))
    _unlock_session(request, ctx, res.dek)
    # The recovery key is shown once, in this response only (never in a URL), and is not stored anywhere.
    return render(
        request,
        "memory_key_recovery.html",
        ctx,
        recovery_key=res.recovery_key,
        records=res.records,
        again=False,
    )


@router.post("/settings/memory-key/unlock")
def unlock(request: Request, passphrase: str = Form(""), ctx: Ctx = Depends(user_csrf)) -> RedirectResponse:
    _throttle(request, ctx)
    dek = keys.unlock(ctx.db, ctx.user.id, passphrase)
    if dek is None:
        return _go("That isn't your memory passphrase.")
    _unlock_session(request, ctx, dek)
    return _go("Unlocked for this session. It locks again when you sign out or after a while idle.")


@router.post("/settings/memory-key/lock")
def lock(request: Request, ctx: Ctx = Depends(user_csrf)) -> RedirectResponse:
    request.app.state.unlock_cache.drop(ctx.ws.id)
    return _go("Locked.")


@router.post("/settings/memory-key/change")
def change(
    request: Request,
    current: str = Form(""),
    new: str = Form(""),
    confirm: str = Form(""),
    ctx: Ctx = Depends(user_csrf),
) -> RedirectResponse:
    _throttle(request, ctx)
    if new != confirm:
        return _go("The new passphrases don't match.")
    try:
        keys.change_passphrase(ctx.db, ctx.user, current, new)
        ctx.db.commit()
    except (AccessError, ValidationFailed) as e:
        ctx.db.rollback()
        return _go(str(e))
    return _go("Memory passphrase changed. Your recovery key still works.")


@router.post("/settings/memory-key/recovery")
def new_recovery(request: Request, passphrase: str = Form(""), ctx: Ctx = Depends(user_csrf)):  # noqa: ANN201
    _throttle(request, ctx)
    try:
        text = keys.regenerate_recovery_key(ctx.db, ctx.user, passphrase)
        ctx.db.commit()
    except AccessError as e:
        ctx.db.rollback()
        return _go(str(e))
    return render(request, "memory_key_recovery.html", ctx, recovery_key=text, records=0, again=True)


@router.post("/settings/memory-key/recover")
def recover(
    request: Request,
    recovery_key: str = Form(""),
    new: str = Form(""),
    confirm: str = Form(""),
    ctx: Ctx = Depends(user_csrf),
) -> RedirectResponse:
    _throttle(request, ctx)
    if new != confirm:
        return _go("The new passphrases don't match.")
    try:
        dek = keys.recover(ctx.db, ctx.user, recovery_key, new)
        ctx.db.commit()
    except (AccessError, ValidationFailed) as e:
        ctx.db.rollback()
        return _go(str(e))
    _unlock_session(request, ctx, dek)
    return _go("Recovered. Your new passphrase is set and this session is unlocked.")


@router.post("/settings/memory-key/disable")
def disable(
    request: Request,
    passphrase: str = Form(""),
    confirm: str = Form(""),
    ctx: Ctx = Depends(user_csrf),
) -> RedirectResponse:
    _throttle(request, ctx)
    if confirm != "decrypt":
        return _go("Type “decrypt” to confirm; this stores your private memory unencrypted again.")
    try:
        n = keys.disable(ctx.db, ctx.user, passphrase)
        ctx.db.commit()
    except (AccessError, ValidationFailed) as e:
        ctx.db.rollback()
        return _go(str(e))
    request.app.state.unlock_cache.drop_user(ctx.user.id)
    return _go(f"Encryption is off. {n} private record(s) are stored as plain text again.")


def status(db: Session, ctx: Ctx) -> dict:
    uk = keys.get_keys(db, ctx.user.id)
    return {
        "enabled": uk is not None,
        "unlocked": ctx.dek is not None,
        "since": uk.created_at if uk else None,
        "changed": uk.passphrase_changed_at if uk else None,
        "locked_count": 0 if uk is None or ctx.dek else _locked_count(db, ctx.user.id),
    }


def _locked_count(db: Session, user_id: str) -> int:
    from sqlmodel import col, func, select

    from ..models import MemoryRecord

    return db.exec(
        select(func.count())
        .select_from(MemoryRecord)
        .where(MemoryRecord.user_id == user_id, col(MemoryRecord.encrypted).is_(True))
    ).one()
