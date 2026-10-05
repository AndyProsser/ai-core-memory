"""Login, logout, first-run setup, and OIDC SSO."""

from __future__ import annotations

import re
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, col, select

from .. import oidc
from ..auth import (
    SESSION_COOKIE,
    check_setup_code,
    create_web_session,
    end_web_session,
    has_admin,
    lookup_web_session,
)
from ..models import AuthFlow, InstanceSettings, User, utcnow
from ..security import (
    check_password_policy,
    hash_password,
    hash_token,
    new_csrf_token,
    safe_equal,
    verify_password,
)
from .deps import Ctx, get_db, render, require_user, set_session_cookie, user_csrf

router = APIRouter()
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
OIDC_BINDING_COOKIE = "acm_oidc"


def _safe_next(nxt: str | None) -> str:
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt else "/memory"


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True  # non-browser clients; SameSite=Lax covers browsers that omit it
    from urllib.parse import urlsplit

    return origin != "null" and urlsplit(origin).netloc == request.headers.get("host", "")


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _login_page(request: Request, db: Session, *, error: str = "", next_url: str = "", status: int = 200):  # noqa: ANN202
    s = request.app.state.settings
    inst = db.get(InstanceSettings, 1)
    return render(
        request,
        "login.html",
        None,
        status=status,
        error=error,
        next_url=next_url,
        oidc_enabled=s.oidc_enabled,
        local_enabled=bool(inst.local_login_enabled) if inst else True,
    )


@router.get("/")
def home() -> RedirectResponse:
    return RedirectResponse("/memory", status_code=303)


@router.get("/login")
def login_form(request: Request, next: str = "", db: Session = Depends(get_db)):  # noqa: A002, ANN201
    if not has_admin(db):
        return RedirectResponse("/setup", status_code=303)
    if lookup_web_session(db, request.app.state.settings, request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse(_safe_next(next), status_code=303)
    return _login_page(request, db, next_url=_safe_next(next) if next else "")


@router.post("/login")
def login(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    next: str = Form(""),
    db: Session = Depends(get_db),
):  # noqa: A002, ANN201
    if not _same_origin(request):
        return _login_page(request, db, error="Request blocked (cross-origin).", status=403)
    throttle = request.app.state.login_throttle
    email_n = email.strip().lower()
    keys = (f"acct:{email_n}", f"ip:{_ip(request)}")
    if any(throttle.blocked(k) for k in keys):
        return _login_page(
            request, db, error="Too many failed attempts. Try again in a few minutes.", status=429
        )
    user = db.exec(select(User).where(User.email == email_n)).first()
    inst = db.get(InstanceSettings, 1)
    allowed = bool(user and user.is_active and (user.is_admin or (inst and inst.local_login_enabled)))
    ok = verify_password(
        user.password_hash if user and allowed else None, password
    )  # constant-ish time for unknown users
    if not (ok and user and allowed):
        for k in keys:
            throttle.failure(k)
        return _login_page(
            request,
            db,
            error="Wrong email or password.",
            next_url=_safe_next(next) if next else "",
            status=401,
        )
    for k in keys:
        throttle.success(k)
    raw, _ = create_web_session(db, user, request.app.state.settings)
    db.commit()
    resp = RedirectResponse(_safe_next(next), status_code=303)
    set_session_cookie(request, resp, raw)
    return resp


@router.post("/logout")
def logout(request: Request, ctx: Ctx = Depends(user_csrf)) -> RedirectResponse:
    request.app.state.unlock_cache.drop(ctx.ws.id)  # signing out locks encrypted memory
    end_web_session(ctx.db, request.cookies.get(SESSION_COOKIE))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# --- first-run setup ----------------------------------------------------------------------------------


@router.get("/setup")
def setup_form(request: Request, db: Session = Depends(get_db)):  # noqa: ANN201
    if has_admin(db):
        return RedirectResponse("/login", status_code=303)
    return render(request, "setup.html", None, admin_email=request.app.state.settings.admin_email, error="")


@router.post("/setup")
def setup(
    request: Request,
    setup_code: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    confirm: str = Form(""),
    deployment_mode: str = Form("solo"),
    db: Session = Depends(get_db),
):  # noqa: ANN201
    if has_admin(db):
        return RedirectResponse("/login", status_code=303)
    if not _same_origin(request):
        return render(
            request, "setup.html", None, status=403, admin_email="", error="Request blocked (cross-origin)."
        )
    throttle = request.app.state.login_throttle
    key = f"setup:{_ip(request)}"

    def fail(msg: str, status: int = 400):  # noqa: ANN202
        return render(request, "setup.html", None, status=status, admin_email=email, error=msg)

    if throttle.blocked(key):
        return fail("Too many attempts. Try again in a few minutes.", 429)
    if not check_setup_code(db, setup_code):
        throttle.failure(key)
        return fail(
            "That setup code isn't right. It's printed in the server log; run `acm setup-code` on the host for a new one."
        )
    email_n = email.strip().lower()
    if not EMAIL_RE.match(email_n):
        return fail("Enter a valid email address.")
    if password != confirm:
        return fail("The passwords don't match.")
    if problem := check_password_policy(password):
        return fail(problem)
    if deployment_mode not in {"solo", "team", "multi_team"}:
        return fail("Pick a deployment mode.")
    inst = db.get(InstanceSettings, 1) or InstanceSettings(id=1)
    inst.deployment_mode = deployment_mode
    inst.oidc_provisioning = "auto" if deployment_mode == "solo" else "invite"
    inst.setup_code_hash = None  # single use
    db.add(inst)
    user = User(email=email_n, password_hash=hash_password(password), is_admin=True)
    db.add(user)
    db.flush()
    raw, _ = create_web_session(db, user, request.app.state.settings)
    db.commit()
    resp = RedirectResponse("/memory?notice=Welcome.+Your+hub+is+ready.", status_code=303)
    set_session_cookie(request, resp, raw)
    return resp


# --- OIDC ---------------------------------------------------------------------------------------------


async def _start_oidc(request: Request, db: Session, *, reauth: bool):  # noqa: ANN202
    settings = request.app.state.settings
    if not settings.oidc_enabled:
        return _login_page(request, db, error="Single sign-on isn't configured.", status=404)
    async with request.app.state.http_client_factory() as client:
        try:
            disc = await oidc.discover(settings, client)
        except Exception:  # noqa: BLE001
            return _login_page(request, db, error="Couldn't reach the identity provider.", status=502)
    state, nonce, binding = new_csrf_token(), new_csrf_token(), new_csrf_token()
    verifier, challenge = oidc.pkce_pair()
    db.add(
        AuthFlow(
            state_hash=hash_token(state),
            nonce=nonce,
            code_verifier=verifier,
            binding_hash=hash_token(binding),
            reauth=reauth,
        )
    )
    db.commit()
    resp = RedirectResponse(
        oidc.authorization_url(settings, disc, state=state, nonce=nonce, challenge=challenge, reauth=reauth),
        status_code=303,
    )
    resp.set_cookie(
        OIDC_BINDING_COOKIE,
        binding,
        httponly=True,
        samesite="lax",
        secure=settings.public_url.startswith("https"),
        max_age=600,
        path="/auth/oidc",
    )
    return resp


@router.get("/login/oidc")
async def oidc_login(request: Request, db: Session = Depends(get_db)):  # noqa: ANN201
    if not has_admin(db):
        return RedirectResponse("/setup", status_code=303)
    return await _start_oidc(request, db, reauth=False)


@router.get("/auth/oidc/reauth")
async def oidc_reauth(request: Request, ctx: Ctx = Depends(require_user)):  # noqa: ANN201
    return await _start_oidc(request, ctx.db, reauth=True)


@router.get("/auth/oidc/callback")
async def oidc_callback(
    request: Request, code: str = "", state: str = "", error: str = "", db: Session = Depends(get_db)
):  # noqa: ANN201
    settings = request.app.state.settings

    def bad(msg: str, status: int = 400):  # noqa: ANN202
        resp = _login_page(request, db, error=msg, status=status)
        resp.delete_cookie(OIDC_BINDING_COOKIE, path="/auth/oidc")
        return resp

    if error or not code or not state:
        return bad("Single sign-on was cancelled or failed.")
    flow = db.get(AuthFlow, hash_token(state))
    binding = request.cookies.get(OIDC_BINDING_COOKIE, "")
    if (
        flow is None
        or utcnow() - flow.created_at > timedelta(minutes=10)
        or not safe_equal(flow.binding_hash, hash_token(binding))
    ):
        if flow:
            db.delete(flow)
            db.commit()
        return bad("This sign-in link is invalid or expired. Start again.")
    verifier, nonce, reauth = flow.code_verifier, flow.nonce, flow.reauth
    db.delete(flow)  # single use
    db.commit()
    async with request.app.state.http_client_factory() as client:
        try:
            claims = await oidc.exchange_and_verify(
                settings, client, code=code, verifier=verifier, nonce=nonce
            )
        except Exception as e:  # noqa: BLE001
            return bad(
                f"Single sign-on failed: {e}" if isinstance(e, oidc.OIDCError) else "Single sign-on failed.",
                401,
            )

    key = oidc.identity_key(claims)
    email = str(claims.get("email") or "").strip().lower()
    verified = claims.get("email_verified") is True
    inst = db.get(InstanceSettings, 1) or InstanceSettings(id=1)
    user = db.exec(select(User).where(User.external_id == key)).first()
    if user is None:
        if not email or not verified:
            return bad("Your identity provider didn't supply a verified email address.", 403)
        invited = db.exec(
            select(User).where(
                User.email == email, User.auth_provider == "oidc", col(User.external_id).is_(None)
            )
        ).first()
        if invited:
            invited.external_id = key
            user = invited
        elif inst.oidc_provisioning == "auto":
            if db.exec(select(User).where(User.email == email)).first():
                return bad(
                    "An account with this email already exists. Ask an admin to invite your SSO identity.",
                    403,
                )
            user = User(email=email, auth_provider="oidc", external_id=key)
            db.add(user)
        else:
            return bad("This hub is invite-only. Ask an admin to invite your email.", 403)
        db.flush()
    if not user.is_active:
        return bad("This account has been deactivated. Ask an admin.", 403)
    if reauth:
        found = lookup_web_session(db, settings, request.cookies.get(SESSION_COOKIE))
        if not found or found[0].id != user.id:
            return bad("Re-authentication didn't match the signed-in account.", 403)
        found[1].authenticated_at = utcnow()
        db.add(found[1])
        db.commit()
        resp = RedirectResponse("/settings?notice=Re-authenticated.", status_code=303)
    else:
        raw, _ = create_web_session(db, user, settings)
        db.commit()
        resp = RedirectResponse("/memory", status_code=303)
        set_session_cookie(request, resp, raw)
    resp.delete_cookie(OIDC_BINDING_COOKIE, path="/auth/oidc")
    return resp
