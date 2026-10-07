"""Authentication: bearer API tokens (AI clients) and server-side web sessions (people).

The two credential types are deliberately separate (docs/SECURITY.md): sessions never authenticate
the MCP/token routes and tokens never authenticate the web UI, so one can't be mistaken for the other.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from sqlmodel import Session, col, select

from . import crypto
from .access import Principal, principal_for_user
from .config import Settings
from .models import ApiToken, InstanceSettings, User, WebSession, utcnow
from .security import (
    SlidingWindowLimiter,
    hash_token,
    is_local_or_private,
    looks_like_token,
    new_csrf_token,
    new_session_secret,
    safe_equal,
)

log = logging.getLogger("acm_hub.auth")
_LAST_USED_GRANULARITY = timedelta(seconds=60)


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# --- request helpers --------------------------------------------------------------------------------


def request_is_https(scheme: str) -> bool:
    """Only the ASGI scheme counts. Behind a proxy, uvicorn rewrites it from X-Forwarded-Proto *for the peers it was
    told to trust* (MEMORY_HUB_TRUSTED_PROXIES); reading that header here as well would let any direct caller claim https."""
    return scheme == "https"


def cookie_should_be_secure(scheme: str, client_host: str | None, settings: Settings) -> bool:
    if settings.cookie_secure is not None:
        return settings.cookie_secure
    # Served over https: never send the cookie in the clear, whatever the last hop looks like (a proxy's is plain http).
    if settings.public_https:
        return True
    return request_is_https(scheme) or not is_local_or_private(client_host)


# --- API tokens -------------------------------------------------------------------------------------


def authenticate_bearer(
    session: Session,
    settings: Settings,
    limiter: SlidingWindowLimiter,
    authorization: str | None,
    *,
    scheme: str,
    client_host: str | None,
) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError(401, "Missing bearer token.")
    raw = authorization[7:].strip()
    # Tokens are never accepted over plaintext HTTP, except on loopback / a private LAN.
    if not request_is_https(scheme) and not is_local_or_private(client_host):
        raise AuthError(
            400,
            "API tokens are only accepted over HTTPS (plain HTTP is allowed from localhost and private networks).",
        )
    if not looks_like_token(raw):
        raise AuthError(401, "Invalid token.")
    tok = session.exec(select(ApiToken).where(ApiToken.token_hash == hash_token(raw))).first()
    now = utcnow()
    # revoked_at / expires_at are checked on every request: revocation is immediate, no cache.
    if tok is None or tok.revoked_at is not None or (tok.expires_at is not None and tok.expires_at <= now):
        raise AuthError(401, "Invalid, expired, or revoked token.")
    if not limiter.allow(tok.id):
        raise AuthError(429, "Rate limit exceeded for this token.")
    user = session.get(User, tok.user_id)
    if (
        user is None or not user.is_active
    ):  # a deactivated person's tokens stop working at once, even if not yet revoked
        raise AuthError(401, "Invalid token.")
    if tok.last_used_at is None or now - tok.last_used_at > _LAST_USED_GRANULARITY:
        tok.last_used_at = now
        session.add(tok)
        session.commit()
    p = principal_for_user(session, user, kind="token")
    p.is_admin = False  # admin is a human platform role; a token never carries it
    p.token_id = tok.id
    p.token_project_ids = list(tok.project_ids or [])
    p.token_include_user_scope = tok.include_user_scope
    p.read_only = tok.access_level != "read_write"
    # Only a token the person explicitly allowed carries a copy of their data key, wrapped under this token's own secret.
    p.dek = crypto.unwrap_from_secret(raw, tok.wrapped_dek, "token")
    return p


def mint_token(
    session: Session,
    user: User,
    *,
    label: str,
    project_ids: list[str],
    access_level: str,
    expires_days: int | None,
    include_user_scope: bool,
    dek: bytes | None = None,
) -> tuple[str, ApiToken]:
    from .security import generate_api_token

    inst = session.get(InstanceSettings, 1) or InstanceSettings(id=1)
    max_days = inst.max_token_expiry_days
    days = expires_days if expires_days is not None else inst.default_token_expiry_days
    if days < 1 or days > max_days:
        raise ValueError(f"Expiry must be between 1 and {max_days} days.")
    if access_level not in {"read_only", "read_write"}:
        raise ValueError("access_level must be read_only or read_write.")
    label = label.strip()
    if not label or len(label) > 80:
        raise ValueError("Give the token a label (max 80 characters).")
    raw, digest, prefix = generate_api_token()
    tok = ApiToken(
        user_id=user.id,
        token_hash=digest,
        prefix=prefix,
        label=label,
        project_ids=project_ids,
        access_level=access_level,
        include_user_scope=include_user_scope,
        expires_at=utcnow() + timedelta(days=days),
        wrapped_dek=crypto.wrap_for_secret(raw, dek, "token") if dek else None,
    )
    session.add(tok)
    session.flush()
    return raw, tok


# --- web sessions -----------------------------------------------------------------------------------


def create_web_session(session: Session, user: User, settings: Settings) -> tuple[str, WebSession]:
    raw, digest = new_session_secret()  # new id on every login (rotation)
    ws = WebSession(
        id=digest,
        user_id=user.id,
        csrf_token=new_csrf_token(),
        expires_at=utcnow() + timedelta(days=settings.session_absolute_days),
    )
    session.add(ws)
    session.flush()
    return raw, ws


def lookup_web_session(
    session: Session, settings: Settings, raw_cookie: str | None
) -> tuple[User, WebSession] | None:
    if not raw_cookie:
        return None
    ws = session.get(WebSession, hash_token(raw_cookie))
    if ws is None:
        return None
    now = utcnow()
    idle = timedelta(hours=settings.session_idle_hours)
    if ws.expires_at <= now or now - ws.last_seen_at > idle:
        session.delete(ws)
        session.commit()
        return None
    user = session.get(User, ws.user_id)
    if user is None or not user.is_active:
        return None
    if now - ws.last_seen_at > timedelta(minutes=1):
        ws.last_seen_at = now
        session.add(ws)
        session.commit()
    return user, ws


def end_web_session(session: Session, raw_cookie: str | None) -> None:
    if raw_cookie:
        ws = session.get(WebSession, hash_token(raw_cookie))
        if ws:
            session.delete(ws)
            session.commit()


def purge_expired(session: Session) -> None:
    from sqlmodel import delete

    from .models import AuthFlow

    now = utcnow()
    session.exec(delete(WebSession).where(WebSession.expires_at <= now))  # type: ignore[call-overload]
    session.exec(delete(AuthFlow).where(AuthFlow.created_at <= now - timedelta(minutes=15)))  # type: ignore[call-overload]
    session.commit()


def recently_authenticated(ws: WebSession, minutes: int = 10) -> bool:
    return utcnow() - ws.authenticated_at <= timedelta(minutes=minutes)


def fetch_site_ok(fetch_site: str | None) -> bool:
    """Browsers say where a request came from in Sec-Fetch-Site. Only our own pages (`same-origin`) or the person typing
    the address (`none`) may submit; `same-site` (a sibling subdomain) and `cross-site` may not. Absent = an old browser
    or a script, which the other checks still cover."""
    return fetch_site is None or fetch_site.lower() in {"same-origin", "none"}


def csrf_ok(
    ws: WebSession,
    supplied: str | None,
    origin: str | None,
    host: str | None,
    fetch_site: str | None = None,
) -> bool:
    if not fetch_site_ok(fetch_site):
        return False
    if origin and origin != "null":
        if urlsplit(origin).netloc != (host or ""):
            return False
    elif origin == "null":
        return False
    return bool(supplied) and safe_equal(ws.csrf_token, supplied or "")


# --- first-run setup code ---------------------------------------------------------------------------


def issue_setup_code(session: Session) -> str:
    """Generate (and store only the hash of) a one-time code that must be presented to claim first-run setup."""
    code = "-".join(secrets.token_hex(2) for _ in range(3))
    inst = session.get(InstanceSettings, 1)
    if inst is None:
        inst = InstanceSettings(id=1)
    inst.setup_code_hash = hash_token(code)
    session.add(inst)
    session.commit()
    return code


def check_setup_code(session: Session, supplied: str) -> bool:
    inst = session.get(InstanceSettings, 1)
    if inst is None or not inst.setup_code_hash:
        return False
    return safe_equal(inst.setup_code_hash, hash_token(supplied.strip().lower()))


def has_admin(session: Session) -> bool:
    return session.exec(select(User).where(col(User.is_admin).is_(True))).first() is not None
