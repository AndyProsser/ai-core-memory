"""Settings: account & theme, password, API tokens, instance."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlmodel import col, select

from ..access import AccessError, readable_project_ids
from ..auth import mint_token, recently_authenticated
from ..models import ApiToken, InstanceSettings, Project, WebSession, utcnow
from ..records import ValidationFailed
from ..security import check_password_policy, hash_password, verify_password
from .deps import Ctx, notice_url, render, require_user, user_csrf

router = APIRouter()


def _settings_page(
    request: Request, ctx: Ctx, *, new_token: str | None = None, error: str = "", status: int = 200
):  # noqa: ANN202
    tokens = ctx.db.exec(
        select(ApiToken).where(ApiToken.user_id == ctx.user.id).order_by(col(ApiToken.created_at).desc())
    ).all()
    pids = readable_project_ids(ctx.db, ctx.principal)
    projects = ctx.db.exec(select(Project).where(Project.id.in_(pids))).all() if pids else []  # type: ignore[attr-defined]
    names = {p.id: p.slug for p in ctx.db.exec(select(Project)).all()}
    inst = ctx.db.get(InstanceSettings, 1) or InstanceSettings()
    s = request.app.state.settings
    return render(
        request,
        "settings.html",
        ctx,
        status=status,
        tokens=tokens,
        projects=sorted(projects, key=lambda p: p.slug),
        project_names=names,
        new_token=new_token,
        error=error,
        inst=inst,
        now=utcnow(),
        hub_url=s.public_url,
        oidc_enabled=s.oidc_enabled,
    )


@router.get("/settings")
def settings_page(request: Request, ctx: Ctx = Depends(require_user)):  # noqa: ANN201
    return _settings_page(request, ctx)


@router.post("/settings/theme")
def set_theme(theme: str = Form("system"), ctx: Ctx = Depends(user_csrf)) -> RedirectResponse:
    if theme not in {"system", "light", "dark"}:
        raise ValidationFailed("Unknown theme.")
    ctx.user.theme_preference = theme
    ctx.db.add(ctx.user)
    ctx.db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/password")
def set_password(
    current: str = Form(""), new: str = Form(""), confirm: str = Form(""), ctx: Ctx = Depends(user_csrf)
) -> RedirectResponse:
    if ctx.user.auth_provider != "local":
        raise AccessError(
            "This account signs in through single sign-on; there's no local password to change."
        )
    if not verify_password(ctx.user.password_hash, current):
        return RedirectResponse(notice_url("/settings", "Your current password is wrong."), status_code=303)
    if new != confirm:
        return RedirectResponse(notice_url("/settings", "The new passwords don't match."), status_code=303)
    if problem := check_password_policy(new):
        return RedirectResponse(notice_url("/settings", problem), status_code=303)
    ctx.user.password_hash = hash_password(new)
    ctx.db.add(ctx.user)
    # A password change should end every other login (a stolen session shouldn't survive it).
    for ws in ctx.db.exec(
        select(WebSession).where(WebSession.user_id == ctx.user.id, WebSession.id != ctx.ws.id)
    ).all():
        ctx.db.delete(ws)
    ctx.db.commit()
    return RedirectResponse(notice_url("/settings", "Password changed."), status_code=303)


@router.post("/settings/tokens")
async def create_token(request: Request, ctx: Ctx = Depends(user_csrf)):  # noqa: ANN201
    form = await request.form()
    # Minting a credential needs a fresh proof of identity, not just a live session.
    if ctx.user.auth_provider == "local":
        if not verify_password(ctx.user.password_hash, str(form.get("password") or "")):
            return _settings_page(request, ctx, error="Enter your password to create a token.", status=403)
    elif not recently_authenticated(ctx.ws):
        return RedirectResponse("/auth/oidc/reauth", status_code=303)
    chosen = [str(x) for x in form.getlist("projects")]
    allowed = readable_project_ids(ctx.db, ctx.principal)
    if any(c not in allowed for c in chosen):
        raise AccessError("You can't scope a token to a project you can't see.")
    try:
        raw, _tok = mint_token(
            ctx.db,
            ctx.user,
            label=str(form.get("label") or ""),
            project_ids=chosen,
            access_level=str(form.get("access_level") or "read_only"),
            expires_days=int(str(form.get("expires_days") or "") or 0) or None,
            include_user_scope=form.get("include_user_scope") == "on",
        )
        ctx.db.commit()
    except (ValueError, ValidationFailed) as e:
        ctx.db.rollback()
        return _settings_page(request, ctx, error=str(e), status=422)
    return _settings_page(request, ctx, new_token=raw)  # shown once; this response is never cached


@router.post("/settings/tokens/{token_id}/revoke")
def revoke_token(token_id: str, ctx: Ctx = Depends(user_csrf)) -> RedirectResponse:
    tok = ctx.db.get(ApiToken, token_id)
    if tok is None or tok.user_id != ctx.user.id:
        raise AccessError("No such token.")
    if tok.revoked_at is None:
        tok.revoked_at = utcnow()
        ctx.db.add(tok)
        ctx.db.commit()
    return RedirectResponse(
        notice_url("/settings", "Token revoked. It stops working immediately."), status_code=303
    )


@router.post("/settings/instance")
def update_instance(
    deployment_mode: str = Form("solo"),
    core_token_budget: int = Form(2000),
    default_token_expiry_days: int = Form(90),
    max_token_expiry_days: int = Form(365),
    ctx: Ctx = Depends(user_csrf),
) -> RedirectResponse:
    if not ctx.user.is_admin:
        raise AccessError("Only an admin can change instance settings.")
    if deployment_mode not in {"solo", "team", "multi_team"}:
        raise ValidationFailed("Unknown deployment mode.")
    if not (200 <= core_token_budget <= 20000) or not (
        1 <= default_token_expiry_days <= max_token_expiry_days <= 3650
    ):
        raise ValidationFailed(
            "Core budget must be 200–20000 tokens; token expiry must be 1+ days and no more than the maximum."
        )
    inst = ctx.db.get(InstanceSettings, 1) or InstanceSettings(id=1)
    inst.deployment_mode = deployment_mode
    inst.core_token_budget = core_token_budget
    inst.default_token_expiry_days = default_token_expiry_days
    inst.max_token_expiry_days = max_token_expiry_days
    ctx.db.add(inst)
    ctx.db.commit()
    return RedirectResponse(notice_url("/settings", "Instance settings saved."), status_code=303)
