"""Consent screen and connected-app management for MCP OAuth (docs/SECURITY.md § MCP OAuth)."""

from __future__ import annotations

from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from .. import oauth
from ..access import AccessError, NotFound, principal_for_user
from ..auth import SESSION_COOKIE, lookup_web_session
from ..models import OAuthClient, OAuthGrant, OAuthRequest, utcnow
from ..orgs import visible_projects
from .deps import Ctx, get_db, notice_url, render, user_csrf

router = APIRouter()


def _enabled(request: Request) -> None:
    if not request.app.state.settings.oauth_enabled:
        raise NotFound("OAuth sign-in isn't switched on for this hub.")


def _load_request(db: Session, rid: str) -> OAuthRequest:
    req = db.get(OAuthRequest, rid)
    if req is None or req.expires_at <= utcnow():
        raise NotFound("This sign-in request expired. Start the connection again from the app.")
    return req


def _origin(uri: str) -> str:
    p = urlsplit(uri)
    return f"{p.scheme}://{p.netloc}"


def _consent_page(
    request: Request, ctx: Ctx, req: OAuthRequest, *, error: str = "", status: int = 200
) -> HTMLResponse:
    client = ctx.db.get(OAuthClient, req.client_id)
    page = render(
        request,
        "consent.html",
        ctx,
        status=status,
        req=req,
        client_name=(client.client_name if client else None) or "An app that didn't give a name",
        redirect_host=urlsplit(req.redirect_uri).netloc,
        loopback=oauth.is_loopback_redirect(req.redirect_uri),
        projects=visible_projects(ctx.db, ctx.principal),
        wants_write=oauth.SCOPE_WRITE in req.scopes,
        offer_encrypted=ctx.dek is not None,  # they've turned encryption on AND unlocked this session
        error=error,
    )
    # The page's form-action decides where the approval redirect may go; allow only this request's own origin
    # (never anything taken from the form), on top of the hub itself.
    csp = page.headers.get("content-security-policy") or ""
    page.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
        f"connect-src 'self'; form-action 'self' {_origin(req.redirect_uri)}; base-uri 'none'; frame-ancestors 'none'"
        if not csp
        else csp.replace("form-action 'self'", f"form-action 'self' {_origin(req.redirect_uri)}")
    )
    return page


@router.get("/oauth/consent")
def consent_form(request: Request, db: Session = Depends(get_db)):  # noqa: ANN201
    _enabled(request)
    rid = request.query_params.get("request", "")
    found = lookup_web_session(db, request.app.state.settings, request.cookies.get(SESSION_COOKIE))
    if found is None:
        return RedirectResponse(
            f"/login?next={quote('/oauth/consent?request=' + quote(rid))}", status_code=303
        )
    user, ws = found
    principal = principal_for_user(db, user, kind="session")
    principal.dek = request.app.state.unlock_cache.get(ws.id, user.id)
    ctx = Ctx(db, user, ws, principal, request, principal.dek)
    return _consent_page(request, ctx, _load_request(db, rid))


@router.post("/oauth/consent")
def consent_decide(  # noqa: ANN201
    request: Request,
    request_id: str = Form("", alias="request"),
    decision: str = Form(""),
    scope_mode: str = Form("some"),
    projects: list[str] = Form(default_factory=list),
    user_scope: str = Form(""),
    access: str = Form("read_only"),
    include_encrypted: str = Form(""),
    ctx: Ctx = Depends(user_csrf),
):
    _enabled(request)
    req = _load_request(ctx.db, request_id)
    iss = request.app.state.settings.public_url
    if decision != "approve":
        ctx.db.delete(req)
        ctx.db.commit()
        return RedirectResponse(
            oauth.redirect_with(req.redirect_uri, {"error": "access_denied", "state": req.state, "iss": iss}),
            status_code=303,
        )
    if access not in ("read_only", "read_write") or (
        access == "read_write" and oauth.SCOPE_WRITE not in req.scopes
    ):
        return _consent_page(
            request, ctx, req, error="That access level wasn't requested by the app.", status=422
        )
    readable = {p.id for p in visible_projects(ctx.db, ctx.principal)}
    if scope_mode == "all":
        project_ids: list[str] = []  # empty = every project the person can read, as with any token
    else:
        project_ids = [p for p in dict.fromkeys(projects) if p in readable]
        if len(project_ids) != len(set(projects)):
            return _consent_page(
                request, ctx, req, error="One of those projects isn't available to you.", status=422
            )
        if not project_ids and not user_scope:
            return _consent_page(
                request, ctx, req, error="Pick at least one project, or choose all of them.", status=422
            )
    if include_encrypted and (ctx.dek is None or not user_scope):
        return _consent_page(
            request,
            ctx,
            req,
            error="Opening encrypted memory needs it unlocked in this session and personal memory included.",
            status=422,
        )
    code = oauth.approve(
        ctx.db,
        ctx.user,
        req,
        project_ids=project_ids,
        include_user_scope=bool(user_scope),
        access_level=access,
        dek=ctx.dek if include_encrypted else None,  # only on an explicit, informed choice
    )
    ctx.db.commit()
    return RedirectResponse(
        oauth.redirect_with(req.redirect_uri, {"code": code, "state": req.state, "iss": iss}), status_code=303
    )


@router.post("/settings/apps/{grant_id}/disconnect")
def disconnect_app(grant_id: str, ctx: Ctx = Depends(user_csrf)) -> RedirectResponse:
    grant = ctx.db.get(OAuthGrant, grant_id)
    if grant is None or grant.user_id != ctx.user.id:
        raise AccessError("No such connected app.")
    oauth.revoke_grant(ctx.db, grant)
    ctx.db.commit()
    return RedirectResponse(
        notice_url("/settings", "Disconnected. The app can't read your memory any more."), status_code=303
    )
