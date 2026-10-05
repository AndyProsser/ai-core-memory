"""MCP OAuth: the hub as its own authorization server (docs/SECURITY.md § MCP OAuth).

Lets a connector (Claude.ai, Claude Code, …) sign a person in instead of having them paste a token. The hard parts —
PKCE verification, exact redirect matching, metadata, form parsing — come from the MCP SDK's handlers; this module is
the policy and the storage behind them.

The central design choice: **an OAuth access token is an ordinary hub API token** (an `ApiToken` row), so every
guarantee tokens already have — project scoping, read-only, no `established`, no team writes, immediate revocation,
dead on deactivation — applies unchanged and can't drift. What OAuth adds is who chooses the scope (the person, on
a consent screen), a short access-token lifetime, and rotating refresh tokens with reuse detection.
"""

from __future__ import annotations

import ipaddress
import re
import secrets
from collections.abc import Callable
from datetime import UTC, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

import anyio.to_thread
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from sqlalchemy import update
from sqlmodel import Session, col, select

from . import crypto
from .config import Settings
from .models import ApiToken, OAuthClient, OAuthCode, OAuthGrant, OAuthRequest, User, utcnow
from .security import generate_api_token, hash_token

SCOPE_READ, SCOPE_WRITE, SCOPE_OFFLINE = "memory:read", "memory:write", "offline_access"
SUPPORTED_SCOPES = [SCOPE_READ, SCOPE_WRITE, SCOPE_OFFLINE]
DEFAULT_SCOPES = [SCOPE_READ]

# The only redirect URIs a self-registering client may use, unless the operator adds more. A registered client can
# never send an authorization code anywhere else, which is what makes open dynamic registration tolerable.
#  - Claude's hosted apps (claude.ai web, Desktop, mobile): the one fixed callback its documentation names.
#  - RFC 8252 loopback, for local tools such as Claude Code: http://localhost|127.0.0.1|[::1]:<any port>/<path>.
CLAUDE_CALLBACKS = frozenset({"https://claude.ai/api/mcp/auth_callback"})
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})

CODE_TTL = timedelta(seconds=60)
REQUEST_TTL = timedelta(minutes=10)
MAX_CLIENTS = 500  # unauthenticated registration must not be able to fill the database
UNUSED_CLIENT_TTL = timedelta(days=1)  # a registration that never led to a grant is swept
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def extra_redirects(settings: Settings) -> frozenset[str]:
    return frozenset(u.strip() for u in settings.oauth_extra_redirect_uris.split(",") if u.strip())


def is_loopback_redirect(uri: str) -> bool:
    try:
        p = urlsplit(uri)
        host = p.hostname or ""
    except ValueError:
        return False
    return p.scheme == "http" and host in LOOPBACK_HOSTS and not p.fragment and not p.username


def redirect_allowed(uri: str, settings: Settings) -> bool:
    if "#" in uri:  # RFC 6749 §3.1.2: no fragments
        return False
    return uri in CLAUDE_CALLBACKS or uri in extra_redirects(settings) or is_loopback_redirect(uri)


def issuer_url(settings: Settings) -> str:
    return settings.public_url


def resource_urls(settings: Settings) -> tuple[str, str]:
    """The MCP endpoint as a person might enter it: with and without the trailing slash."""
    base = f"{settings.public_url}/mcp"
    return base, base + "/"


def check_enabled_config(settings: Settings) -> None:
    """Refuse to switch OAuth on where it can't be safe: tokens and codes must travel over TLS."""
    p = urlsplit(settings.public_url)
    host = p.hostname or ""
    local = host in LOOPBACK_HOSTS
    if not local:
        try:
            local = ipaddress.ip_address(host).is_private
        except ValueError:
            local = False
    if p.scheme != "https" and not local:
        raise RuntimeError(
            "MEMORY_HUB_OAUTH_ENABLED needs MEMORY_HUB_PUBLIC_URL to be https:// (plain http is only allowed for "
            "localhost or a private address). OAuth moves authorization codes and tokens through the browser."
        )


def canonical_url(value: str):  # noqa: ANN201
    """An AnyHttpUrl that keeps a path-less URL exactly as written. Plain `AnyHttpUrl(...)` appends a "/", and RFC
    8414 compares the issuer as an exact string, so `https://hub` and `https://hub/` are different issuers."""
    from pydantic import AnyHttpUrl, BaseModel, ConfigDict

    class _U(BaseModel):
        model_config = ConfigDict(url_preserve_empty_path=True)
        u: AnyHttpUrl

    return _U(u=value).u


def clean_client_name(name: str | None) -> str | None:
    if not name:
        return None
    name = _CTRL.sub("", name).strip()[:100]
    return name or None


# --- the provider the SDK's handlers call ----------------------------------------------------------------------


class HubOAuthProvider:
    """Implements the SDK's OAuthAuthorizationServerProvider on the hub's database."""

    def __init__(self, engine, settings: Settings):  # noqa: ANN001
        self.engine = engine
        self.settings = settings

    async def _db(self, fn: Callable[[Session], Any]) -> Any:
        def run() -> Any:
            with Session(self.engine) as s:
                out = fn(s)
                s.commit()
                return out

        return await anyio.to_thread.run_sync(run)

    # --- clients -------------------------------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        def go(s: Session) -> OAuthClientInformationFull | None:
            c = s.get(OAuthClient, client_id)
            if c is None:
                return None
            return OAuthClientInformationFull.model_validate(
                {
                    "client_id": c.id,
                    "client_name": c.client_name,
                    "redirect_uris": c.redirect_uris,
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "scope": " ".join(SUPPORTED_SCOPES),
                }
            )

        return await self._db(go)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in (client_info.redirect_uris or [])]
        bad = [u for u in uris if not redirect_allowed(u, self.settings)]
        if bad:
            raise RegistrationError(
                "invalid_redirect_uri",
                "This hub only accepts Claude's callback, loopback addresses, or URIs its operator allowed.",
            )

        def go(s: Session) -> None:
            now = utcnow()
            stale = s.exec(select(OAuthClient).where(OAuthClient.created_at < now - UNUSED_CLIENT_TTL)).all()
            used = {g.client_id for g in s.exec(select(OAuthGrant)).all()}
            for c in stale:
                if c.id not in used:
                    s.delete(c)
            s.flush()
            if len(s.exec(select(OAuthClient)).all()) >= MAX_CLIENTS:
                raise RegistrationError(
                    "invalid_client_metadata", "Too many registered clients; try again later."
                )
            s.add(
                OAuthClient(
                    id=client_info.client_id,
                    client_name=clean_client_name(client_info.client_name),
                    redirect_uris=uris,
                )
            )

        await self._db(go)
        # Every client is public (PKCE, no secret): there is nothing here a leaked database could replay.
        client_info.client_secret = None
        client_info.client_secret_expires_at = None
        client_info.token_endpoint_auth_method = "none"
        client_info.grant_types = ["authorization_code", "refresh_token"]
        client_info.client_name = clean_client_name(client_info.client_name)
        client_info.scope = " ".join(
            SUPPORTED_SCOPES
        )  # what it may *ask* for; the person decides what it *gets*

    # --- authorization (consent happens in the web UI) -----------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource is not None and params.resource.rstrip("/") != resource_urls(self.settings)[0]:
            raise AuthorizeError("invalid_target", "This server only issues tokens for its own MCP endpoint.")
        scopes = [sc for sc in (params.scopes or []) if sc in SUPPORTED_SCOPES] or list(DEFAULT_SCOPES)
        rid = secrets.token_urlsafe(32)

        def go(s: Session) -> None:
            now = utcnow()
            for old in s.exec(select(OAuthRequest).where(OAuthRequest.expires_at < now)).all():
                s.delete(old)
            s.add(
                OAuthRequest(
                    id=rid,
                    client_id=client.client_id or "",
                    redirect_uri=str(params.redirect_uri),
                    redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                    state=params.state,
                    scopes=scopes,
                    code_challenge=params.code_challenge,
                    resource=params.resource,
                    expires_at=now + REQUEST_TTL,
                )
            )
            c = s.get(OAuthClient, client.client_id)
            if c:
                c.last_used_at = now
                s.add(c)

        await self._db(go)
        return f"{self.settings.public_url}/oauth/consent?{urlencode({'request': rid})}"

    # --- codes -----------------------------------------------------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        def go(s: Session) -> AuthorizationCode | None:
            row = s.get(OAuthCode, hash_token(authorization_code))
            if row is None or row.client_id != client.client_id:
                return None
            if row.used_at is not None:
                # A code presented twice means it leaked (RFC 6749 §4.1.2): kill what it already produced.
                if row.grant_id and (g := s.get(OAuthGrant, row.grant_id)):
                    revoke_grant(s, g)
                return None
            return AuthorizationCode(
                code=authorization_code,
                scopes=row.scopes,
                expires_at=_epoch(row.expires_at),
                client_id=row.client_id,
                code_challenge=row.code_challenge,
                redirect_uri=row.redirect_uri,
                redirect_uri_provided_explicitly=row.redirect_uri_provided_explicitly,
                resource=row.resource,
                subject=row.user_id,
            )

        return await self._db(go)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        def go(s: Session) -> OAuthToken:
            h = hash_token(authorization_code.code)
            # Atomic claim: of two concurrent exchanges exactly one gets rowcount 1.
            claimed = s.exec(  # type: ignore[call-overload]
                update(OAuthCode)
                .where(col(OAuthCode.code_hash) == h, col(OAuthCode.used_at).is_(None))
                .values(used_at=utcnow())
            )
            if claimed.rowcount != 1:
                raise TokenError("invalid_grant", "This authorization code was already used.")
            row = s.get(OAuthCode, h)
            user = s.get(User, row.user_id)
            if user is None or not user.is_active:
                raise TokenError("invalid_grant", "The account that approved this is no longer active.")
            raw_refresh, refresh_hash, _ = _new_secret()
            grant = OAuthGrant(
                user_id=user.id,
                client_id=row.client_id,
                project_ids=row.project_ids,
                include_user_scope=row.include_user_scope,
                access_level=row.access_level,
                scopes=row.scopes,
                resource=row.resource,
                expires_at=utcnow() + timedelta(days=self.settings.oauth_refresh_days),
                refresh_hash=refresh_hash,
            )
            s.add(grant)
            s.flush()
            row.grant_id = grant.id
            dek = crypto.unwrap_from_secret(authorization_code.code, row.wrapped_dek, "oauth-code")
            row.wrapped_dek = None  # the code's copy of the key is single-use too
            s.add(row)
            return _token_response(s, self.settings, grant, user, raw_refresh, dek=dek)

        return await self._db(go)

    # --- refresh (rotating, with reuse detection) ------------------------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        def go(s: Session) -> RefreshToken | None:
            h = hash_token(refresh_token)
            grant = s.exec(select(OAuthGrant).where(OAuthGrant.refresh_hash == h)).first()
            if grant is None:
                stale = s.exec(select(OAuthGrant).where(OAuthGrant.prev_refresh_hash == h)).first()
                if stale is not None and stale.client_id == client.client_id:
                    # An already-rotated refresh token is being replayed: either the client or a thief has a
                    # copy. We can't tell which, so end the whole grant; the person simply reconnects.
                    revoke_grant(s, stale)
                return None
            user = s.get(User, grant.user_id)
            if (
                grant.client_id != client.client_id
                or grant.revoked_at is not None
                or grant.expires_at <= utcnow()
                or user is None
                or not user.is_active
            ):
                return None
            return RefreshToken(
                token=refresh_token,
                client_id=grant.client_id,
                scopes=grant.scopes,
                expires_at=_epoch(grant.expires_at),
                resource=grant.resource,
                subject=grant.user_id,
            )

        return await self._db(go)

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        def go(s: Session) -> OAuthToken:
            grant = s.exec(
                select(OAuthGrant).where(OAuthGrant.refresh_hash == hash_token(refresh_token.token))
            ).first()
            if grant is None or grant.revoked_at is not None:
                raise TokenError("invalid_grant", "This refresh token is no longer valid.")
            if scopes and not set(scopes) <= set(grant.scopes):
                raise TokenError("invalid_scope", "A refresh can narrow what was granted, never widen it.")
            user = s.get(User, grant.user_id)
            if user is None or not user.is_active:
                raise TokenError("invalid_grant", "The account that approved this is no longer active.")
            # The key travels with the refresh chain: opened with the token being presented, re-wrapped for the next one.
            dek = crypto.unwrap_from_secret(refresh_token.token, grant.wrapped_dek, "oauth-refresh")
            raw_refresh, new_hash, _ = _new_secret()
            grant.prev_refresh_hash, grant.refresh_hash = grant.refresh_hash, new_hash
            grant.last_refreshed_at = utcnow()
            s.add(grant)
            return _token_response(s, self.settings, grant, user, raw_refresh, dek=dek)

        return await self._db(go)

    # --- bearer lookups used by revocation ------------------------------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        def go(s: Session) -> AccessToken | None:
            tok = s.exec(select(ApiToken).where(ApiToken.token_hash == hash_token(token))).first()
            if tok is None or not tok.grant_id or tok.revoked_at is not None:
                return None
            grant = s.get(OAuthGrant, tok.grant_id)
            if grant is None:
                return None
            return AccessToken(
                token=token,
                client_id=grant.client_id,
                scopes=grant.scopes,
                expires_at=_epoch(tok.expires_at) if tok.expires_at else None,
                subject=grant.user_id,
            )

        return await self._db(go)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        def go(s: Session) -> None:
            h = hash_token(token.token)
            grant = s.exec(select(OAuthGrant).where(OAuthGrant.refresh_hash == h)).first()
            if grant is None:
                tok = s.exec(select(ApiToken).where(ApiToken.token_hash == h)).first()
                grant = s.get(OAuthGrant, tok.grant_id) if tok and tok.grant_id else None
            if grant is not None:
                revoke_grant(s, grant)  # revoking either half ends the grant (RFC 7009 SHOULD)

        await self._db(go)


# --- helpers -----------------------------------------------------------------------------------------------------


def _epoch(dt) -> int:  # noqa: ANN001
    """Stored datetimes are naive UTC."""
    return int(dt.replace(tzinfo=UTC).timestamp())


def _new_secret() -> tuple[str, str, str]:
    raw = secrets.token_urlsafe(48)
    return raw, hash_token(raw), raw[:6]


def _token_response(
    s: Session, settings: Settings, grant: OAuthGrant, user: User, raw_refresh: str, dek: bytes | None = None
) -> OAuthToken:
    """Mint the access token as a normal ApiToken, replacing any earlier one for this grant."""
    for old in s.exec(
        select(ApiToken).where(ApiToken.grant_id == grant.id, col(ApiToken.revoked_at).is_(None))
    ).all():
        old.revoked_at = utcnow()
        old.wrapped_dek = None  # a retired access token keeps no copy of the data key
        s.add(old)
    client = s.get(OAuthClient, grant.client_id)
    raw, digest, prefix = generate_api_token()
    ttl = timedelta(minutes=settings.oauth_access_token_minutes)
    s.add(
        ApiToken(
            user_id=user.id,
            token_hash=digest,
            prefix=prefix,
            label=f"OAuth · {(client.client_name if client and client.client_name else 'connected app')}"[
                :80
            ],
            project_ids=list(grant.project_ids),
            access_level=grant.access_level,
            include_user_scope=grant.include_user_scope,
            expires_at=utcnow() + ttl,
            grant_id=grant.id,
            wrapped_dek=crypto.wrap_for_secret(raw, dek, "token") if dek else None,
        )
    )
    grant.wrapped_dek = crypto.wrap_for_secret(raw_refresh, dek, "oauth-refresh") if dek else None
    s.add(grant)
    scopes = [sc for sc in grant.scopes if grant.access_level == "read_write" or sc != SCOPE_WRITE]
    return OAuthToken(
        access_token=raw,
        token_type="Bearer",
        expires_in=int(ttl.total_seconds()),
        refresh_token=raw_refresh,
        scope=" ".join(scopes),
    )


def revoke_grant(s: Session, grant: OAuthGrant) -> None:
    now = utcnow()
    if grant.revoked_at is None:
        grant.revoked_at = now
    grant.wrapped_dek = None  # revoking ends the app's ability to open encrypted memory, not just to ask
    s.add(grant)
    for tok in s.exec(select(ApiToken).where(ApiToken.grant_id == grant.id)).all():
        # every token of the grant, including ones already retired by a refresh: none may keep a copy of the data key
        if tok.revoked_at is None:
            tok.revoked_at = now
        tok.wrapped_dek = None
        s.add(tok)


def revoke_api_token(s: Session, tok: ApiToken) -> None:
    """Revoke a token by any route. If it belongs to an OAuth grant, the grant goes too: otherwise its refresh token
    would quietly mint a replacement."""
    if tok.revoked_at is None:
        tok.revoked_at = utcnow()
    tok.wrapped_dek = None  # and with it this token's copy of the data key
    s.add(tok)
    if tok.grant_id and (grant := s.get(OAuthGrant, tok.grant_id)):
        revoke_grant(s, grant)


def grants_for(s: Session, user: User, *, include_revoked: bool = False) -> list[OAuthGrant]:
    q = select(OAuthGrant).where(OAuthGrant.user_id == user.id).order_by(col(OAuthGrant.created_at).desc())
    rows = list(s.exec(q).all())
    return rows if include_revoked else [g for g in rows if g.revoked_at is None and g.expires_at > utcnow()]


def approve(
    s: Session,
    user: User,
    req: OAuthRequest,
    *,
    project_ids: list[str],
    include_user_scope: bool,
    access_level: str,
    dek: bytes | None = None,
) -> str:
    """Record the person's decision and return the one-time authorization code. Caller validates the choices.
    `dek` is passed only when they chose to let this app open their encrypted memory (and their session is unlocked)."""
    raw = secrets.token_urlsafe(32)
    scopes = [sc for sc in req.scopes if access_level == "read_write" or sc != SCOPE_WRITE]
    s.add(
        OAuthCode(
            code_hash=hash_token(raw),
            client_id=req.client_id,
            user_id=user.id,
            redirect_uri=req.redirect_uri,
            redirect_uri_provided_explicitly=req.redirect_uri_provided_explicitly,
            code_challenge=req.code_challenge,
            resource=req.resource,
            scopes=scopes,
            project_ids=project_ids,
            include_user_scope=include_user_scope,
            access_level=access_level,
            expires_at=utcnow() + CODE_TTL,
            wrapped_dek=crypto.wrap_for_secret(raw, dek, "oauth-code") if dek else None,
        )
    )
    s.delete(req)
    return raw


def redirect_with(uri: str, params: dict[str, str | None]) -> str:
    sep = "&" if "?" in uri else "?"
    return uri + sep + urlencode({k: v for k, v in params.items() if v is not None})


# --- per-IP limits on the unauthenticated endpoints ----------------------------------------------------------------


class OAuthRateLimit:
    """ASGI guard: registration, authorization and token endpoints take unauthenticated traffic."""

    LIMITS = {
        "/register": 20,
        "/authorize": 60,
        "/token": 120,
        "/revoke": 60,
    }  # requests per minute, per client IP

    def __init__(self, app):  # noqa: ANN001
        from .security import SlidingWindowLimiter

        self.app = app
        self.limiters = {path: SlidingWindowLimiter(n) for path, n in self.LIMITS.items()}

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope["type"] == "http" and (lim := self.limiters.get(scope["path"])):
            host = (scope.get("client") or ("unknown", 0))[0]
            if not lim.allow(host):
                body = b'{"error":"temporarily_unavailable","error_description":"Too many requests."}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 429,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"retry-after", b"60"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


# --- wiring -------------------------------------------------------------------------------------------------------


def _revoke_handler(provider: HubOAuthProvider):  # noqa: ANN202
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response

    async def handle(request: Request) -> Response:
        form = await request.form()
        token, client_id = str(form.get("token") or ""), str(form.get("client_id") or "")
        client = await provider.get_client(client_id) if client_id else None
        if client is None:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        if not token:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "token is required"}, status_code=400
            )
        found = await provider.load_access_token(token) or await provider.load_refresh_token(client, token)
        if found is not None and found.client_id == client.client_id:  # never revoke another client's token
            await provider.revoke_token(found)
        return Response(
            status_code=200, headers={"Cache-Control": "no-store", "Pragma": "no-cache"}
        )  # RFC 7009 §2.2

    return handle


def metadata_url(settings: Settings, mcp_path: str) -> str:
    """The RFC 9728 document to point an unauthenticated client at; matches the URL form the client used."""
    suffix = "/mcp/" if mcp_path.endswith("/") and mcp_path != "/mcp" else "/mcp"
    return f"{settings.public_url}/.well-known/oauth-protected-resource{suffix}"


def install(app, engine, settings: Settings) -> None:  # noqa: ANN001
    """Mount the authorization-server and protected-resource endpoints on the hub app. No-op unless enabled."""
    from mcp.server.auth.handlers.metadata import MetadataHandler
    from mcp.server.auth.routes import (
        build_metadata,
        cors_middleware,
        create_auth_routes,
        create_protected_resource_routes,
    )
    from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
    from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE, RequestBodyLimitMiddleware
    from starlette.routing import Route

    check_enabled_config(settings)
    provider = HubOAuthProvider(engine, settings)
    issuer = canonical_url(issuer_url(settings))
    reg = ClientRegistrationOptions(
        enabled=True, valid_scopes=SUPPORTED_SCOPES, default_scopes=DEFAULT_SCOPES
    )
    rev = RevocationOptions(enabled=True)
    routes = create_auth_routes(provider, issuer, client_registration_options=reg, revocation_options=rev)

    # Claude registers as a public client, and (per its docs) selects Client ID Metadata Documents only when "none" is
    # advertised; ours are all public/PKCE, so say so instead of the SDK's secret-based defaults.
    meta = build_metadata(issuer, None, reg, rev)
    meta.token_endpoint_auth_methods_supported = ["none"]
    meta.revocation_endpoint_auth_methods_supported = ["none"]
    meta.grant_types_supported = ["authorization_code", "refresh_token"]
    well_known = "/.well-known/oauth-authorization-server"
    # The SDK's /revoke insists on a client_secret field, which a public client (all of ours) never has; RFC 7009
    # doesn't ask for one. Same behaviour, minus that: authenticate by client_id, revoke only the client's own tokens.
    routes = [r for r in routes if r.path not in (well_known, "/revoke")]
    routes.append(
        Route(
            "/revoke",
            endpoint=RequestBodyLimitMiddleware(
                cors_middleware(_revoke_handler(provider), ["POST", "OPTIONS"]), DEFAULT_MAX_REQUEST_BODY_SIZE
            ),
            methods=["POST", "OPTIONS"],
        )
    )
    routes.append(
        Route(
            well_known,
            endpoint=cors_middleware(MetadataHandler(meta).handle, ["GET", "OPTIONS"]),
            methods=["GET", "OPTIONS"],
        )
    )
    for res in resource_urls(settings):
        routes += create_protected_resource_routes(
            canonical_url(res), [issuer], scopes_supported=SUPPORTED_SCOPES, resource_name="Memory hub"
        )
    # Some clients probe the bare well-known path first (Claude does, when the 401 carries no pointer).
    from mcp.server.auth.handlers.metadata import ProtectedResourceMetadataHandler
    from mcp.shared.auth import ProtectedResourceMetadata

    bare = ProtectedResourceMetadata(
        resource=canonical_url(resource_urls(settings)[0]),
        authorization_servers=[issuer],
        scopes_supported=SUPPORTED_SCOPES,
        resource_name="Memory hub",
    )
    routes.append(
        Route(
            "/.well-known/oauth-protected-resource",
            endpoint=cors_middleware(ProtectedResourceMetadataHandler(bare).handle, ["GET", "OPTIONS"]),
            methods=["GET", "OPTIONS"],
        )
    )
    seen: set[str] = set()
    for r in routes:
        if r.path not in seen:
            seen.add(r.path)
            app.router.routes.append(r)
    app.add_middleware(OAuthRateLimit)
