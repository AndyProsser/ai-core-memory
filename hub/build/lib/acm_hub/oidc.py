"""OpenID Connect login (authorization code + PKCE). See docs/SECURITY.md § OIDC details."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from .config import Settings
from .security import is_local_or_private

_ALGS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"]  # asymmetric only


class OIDCError(Exception):
    pass


@dataclass
class Discovery:
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    issuer: str
    fetched: float = field(default_factory=time.monotonic)


_cache: dict[str, Discovery] = {}


def _check_https(url: str, what: str) -> None:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme == "https" or (parts.scheme == "http" and is_local_or_private(parts.hostname)):
        return
    raise OIDCError(f"{what} must be an https URL.")


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def discover(settings: Settings, client: httpx.AsyncClient) -> Discovery:
    cached = _cache.get(settings.oidc_issuer)
    if cached and time.monotonic() - cached.fetched < 3600:
        return cached
    _check_https(settings.oidc_issuer, "OIDC issuer")
    r = await client.get(f"{settings.oidc_issuer}/.well-known/openid-configuration")
    r.raise_for_status()
    doc = r.json()
    if doc.get("issuer", "").rstrip("/") != settings.oidc_issuer:
        raise OIDCError("Issuer in discovery document doesn't match the configured issuer.")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        _check_https(doc[key], key)
    d = Discovery(
        doc["authorization_endpoint"], doc["token_endpoint"], doc["jwks_uri"], doc["issuer"].rstrip("/")
    )
    _cache[settings.oidc_issuer] = d
    return d


def authorization_url(
    settings: Settings, disc: Discovery, *, state: str, nonce: str, challenge: str, reauth: bool
) -> str:
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": f"{settings.public_url}/auth/oidc/callback",
        "scope": settings.oidc_scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if reauth:
        params["prompt"] = "login"
        params["max_age"] = "0"
    return f"{disc.authorization_endpoint}?{urlencode(params)}"


async def exchange_and_verify(
    settings: Settings, client: httpx.AsyncClient, *, code: str, verifier: str, nonce: str
) -> dict:
    disc = await discover(settings, client)
    resp = await client.post(
        disc.token_endpoint,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": f"{settings.public_url}/auth/oidc/callback",
            "code_verifier": verifier,
            "client_id": settings.oidc_client_id,
            "client_secret": settings.oidc_client_secret,
        },
        headers={"Accept": "application/json"},
    )
    if resp.status_code != 200:
        raise OIDCError("The identity provider rejected the login.")
    id_token = resp.json().get("id_token")
    if not id_token:
        raise OIDCError("The identity provider returned no ID token.")
    jwks = (await client.get(disc.jwks_uri)).json()
    try:
        token = jwt.decode(id_token, KeySet.import_key_set(jwks), algorithms=_ALGS)
        registry = jwt.JWTClaimsRegistry(
            leeway=60,
            iss={"essential": True, "value": disc.issuer},
            aud={"essential": True, "value": settings.oidc_client_id},
            exp={"essential": True},
            sub={"essential": True},
            nonce={"essential": True, "value": nonce},
        )
        registry.validate(token.claims)
        claims = token.claims
    except (JoseError, ValueError, KeyError) as e:
        raise OIDCError(f"ID token verification failed: {e}") from e
    return dict(claims)


def identity_key(claims: dict) -> str:
    """Identity is (issuer, sub) — never email, which can change or be reused at the provider."""
    return f"{claims['iss'].rstrip('/')}|{claims['sub']}"
