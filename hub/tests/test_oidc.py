import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey
from sqlmodel import Session, select

from acm_hub import oidc
from acm_hub.app import create_app
from acm_hub.auth import issue_setup_code
from acm_hub.models import InstanceSettings, User

ISSUER = "https://idp.example"
CLIENT_ID = "memory-hub"
PASSWORD = "correct horse battery staple"


class FakeIdP:
    def __init__(self):
        self.key = RSAKey.generate_key(2048, parameters={"kid": "k1"})
        self.claims: dict = {}
        self.code_ok = True
        self.last_verifier = None

    def token(self, nonce: str, **over) -> str:
        now = int(time.time())
        claims = (
            {
                "iss": ISSUER,
                "aud": CLIENT_ID,
                "sub": "user-123",
                "email": "sso@example.com",
                "email_verified": True,
                "nonce": nonce,
                "iat": now,
                "exp": now + 300,
            }
            | self.claims
            | over
        )
        return jwt.encode({"alg": "RS256", "kid": "k1"}, claims, self.key)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                },
            )
        if path == "/jwks":
            return httpx.Response(200, json=KeySet([self.key]).as_dict(private=False))
        if path == "/token":
            form = parse_qs(request.content.decode())
            self.last_verifier = form["code_verifier"][0]
            if not self.code_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"id_token": self.pending_token()})
        return httpx.Response(404)


@pytest.fixture()
def sso(settings, monkeypatch):
    monkeypatch.setenv("MEMORY_HUB_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("MEMORY_HUB_OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("MEMORY_HUB_OIDC_CLIENT_SECRET", "shh")
    oidc._cache.clear()
    from acm_hub.config import Settings

    idp = FakeIdP()
    root = create_app(
        Settings(), http_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(idp.handler))
    )
    with TestClient(root) as client:
        app = root.fastapi
        with Session(app.state.engine) as s:
            code = issue_setup_code(s)
        client.post(
            "/setup",
            data={
                "setup_code": code,
                "email": "admin@example.com",
                "password": PASSWORD,
                "confirm": PASSWORD,
            },
        )
        client.cookies.clear()
        yield client, app, idp


def begin(client, idp, **token_over):
    r = client.get("/login/oidc", follow_redirects=False)
    assert r.status_code == 303, r.text
    loc = urlsplit(r.headers["location"])
    q = parse_qs(loc.query)
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == f"{ISSUER}/authorize"
    assert (
        q["code_challenge_method"] == ["S256"]
        and q["response_type"] == ["code"]
        and q["client_id"] == [CLIENT_ID]
    )
    idp.pending_token = lambda: idp.token(q["nonce"][0], **token_over)
    return q


def finish(client, state, code="abc"):
    return client.get(f"/auth/oidc/callback?code={code}&state={state}", follow_redirects=False)


def test_login_button_shows_only_when_configured(sso):
    client, *_ = sso
    assert "single sign-on" in client.get("/login").text


def test_happy_path_auto_provisions_and_signs_in(sso):
    client, app, idp = sso
    q = begin(client, idp)
    r = finish(client, q["state"][0])
    assert r.status_code == 303 and r.headers["location"] == "/memory"
    assert client.get("/memory").status_code == 200
    with Session(app.state.engine) as s:
        u = s.exec(select(User).where(User.email == "sso@example.com")).one()
        assert (
            u.auth_provider == "oidc"
            and u.external_id == f"{ISSUER}|user-123"
            and u.password_hash is None
            and not u.is_admin
        )
    assert idp.last_verifier and len(idp.last_verifier) >= 43  # PKCE verifier was actually sent


def test_identity_is_issuer_plus_sub_not_email(sso):
    client, app, idp = sso
    finish(client, begin(client, idp)["state"][0])
    client.cookies.clear()
    idp.claims = {"email": "renamed@example.com"}  # same sub, new email at the IdP
    finish(client, begin(client, idp)["state"][0])
    with Session(app.state.engine) as s:
        users = s.exec(select(User).where(User.auth_provider == "oidc")).all()
        assert len(users) == 1 and users[0].email == "sso@example.com"


def test_state_is_single_use_and_bound_to_the_browser(sso):
    client, app, idp = sso
    q = begin(client, idp)
    other = TestClient(client.app)  # a different browser replaying the link: no binding cookie
    assert finish(other, q["state"][0]).status_code == 400
    assert finish(client, "not-the-state").status_code == 400
    q = begin(client, idp)
    assert finish(client, q["state"][0]).status_code == 303
    client.cookies.clear()
    assert finish(client, q["state"][0]).status_code == 400  # replay


@pytest.mark.parametrize(
    "over",
    [
        {"aud": "someone-else"},
        {"iss": "https://evil.example"},
        {"exp": int(time.time()) - 600},
        {"nonce": "wrong"},
    ],
)
def test_bad_id_tokens_are_rejected(sso, over):
    client, app, idp = sso
    q = begin(client, idp, **over)
    r = finish(client, q["state"][0])
    assert r.status_code == 401 and "memory" not in r.headers.get("location", "")
    assert client.get("/memory", follow_redirects=False).status_code == 303  # not signed in


def test_unsigned_or_wrongly_signed_tokens_are_rejected(sso):
    client, app, idp = sso
    q = begin(client, idp)
    forged = RSAKey.generate_key(2048, parameters={"kid": "k1"})
    idp.pending_token = lambda: jwt.encode(
        {"alg": "RS256", "kid": "k1"},
        {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "x",
            "nonce": q["nonce"][0],
            "exp": int(time.time()) + 300,
            "email": "a@b.co",
            "email_verified": True,
        },
        forged,
    )
    assert finish(client, q["state"][0]).status_code == 401


def test_unverified_email_is_refused(sso):
    client, app, idp = sso
    q = begin(client, idp, email_verified=False)
    assert finish(client, q["state"][0]).status_code == 403


def test_existing_local_account_cannot_be_taken_over_by_matching_email(sso):
    """A matching verified email alone never links: the callback only parks the identity for a password check."""
    client, app, idp = sso
    q = begin(client, idp, email="admin@example.com", sub="attacker")
    r = finish(client, q["state"][0])
    assert r.status_code == 303 and r.headers["location"] == "/auth/oidc/link"
    assert client.get("/memory", follow_redirects=False).status_code == 303  # still signed out
    with Session(app.state.engine) as s:
        admin = s.exec(select(User).where(User.email == "admin@example.com")).one()
        assert admin.external_id is None and admin.auth_provider == "local"


def start_link(client, idp, email="admin@example.com", sub="andy-sso"):
    q = begin(client, idp, email=email, sub=sub)
    r = finish(client, q["state"][0])
    assert r.status_code == 303 and r.headers["location"] == "/auth/oidc/link", r.text
    return r


def link(client, password=PASSWORD, **headers):
    return client.post(
        "/auth/oidc/link", data={"password": password}, headers=headers, follow_redirects=False
    )


def test_link_page_needs_a_pending_link(sso):
    client, *_ = sso
    r = client.get("/auth/oidc/link", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert link(client).status_code == 303  # POST with no pending identity: bounced, nothing linked


def test_correct_password_links_the_sso_identity_and_signs_in(sso):
    client, app, idp = sso
    start_link(client, idp)
    page = client.get("/auth/oidc/link")
    assert page.status_code == 200 and "admin@example.com" in page.text
    r = link(client)
    assert r.status_code == 303 and r.headers["location"] == "/memory"
    assert client.get("/memory").status_code == 200
    with Session(app.state.engine) as s:
        u = s.exec(select(User).where(User.email == "admin@example.com")).one()
        assert u.external_id == f"{ISSUER}|andy-sso"
        assert u.is_admin and u.auth_provider == "local" and u.password_hash  # still the same local admin
        assert s.exec(select(User)).all().__len__() == 1  # no second account was created
    client.cookies.clear()  # next time SSO alone is enough, and lands on the same account
    q = begin(client, idp, email="admin@example.com", sub="andy-sso")
    assert finish(client, q["state"][0]).headers["location"] == "/memory"
    client.cookies.clear()
    r = client.post(
        "/login", data={"email": "admin@example.com", "password": PASSWORD}, follow_redirects=False
    )
    assert r.status_code == 303  # and the local password still works


def test_wrong_password_does_not_link(sso):
    client, app, idp = sso
    start_link(client, idp)
    r = link(client, password="not the password at all")
    assert r.status_code == 401
    assert client.get("/memory", follow_redirects=False).status_code == 303
    with Session(app.state.engine) as s:
        assert s.exec(select(User).where(User.email == "admin@example.com")).one().external_id is None


def test_link_attempts_share_the_login_lockout(sso):
    client, app, idp = sso
    start_link(client, idp)
    for _ in range(8):
        assert link(client, password="wrong wrong wrong").status_code == 401
    assert link(client).status_code == 429  # even the right password is refused once locked out
    with Session(app.state.engine) as s:
        assert s.exec(select(User).where(User.email == "admin@example.com")).one().external_id is None


def test_link_is_refused_cross_origin(sso):
    client, app, idp = sso
    start_link(client, idp)
    assert link(client, origin="https://evil.example").status_code == 403
    with Session(app.state.engine) as s:
        assert s.exec(select(User).where(User.email == "admin@example.com")).one().external_id is None


def test_tampered_or_expired_link_cookie_is_rejected(sso):
    from acm_hub.web.routes_auth import LINK_COOKIE

    client, app, idp = sso
    start_link(client, idp)
    good = client.cookies.get(LINK_COOKIE)
    payload, _, mac = good.rpartition(".")
    client.cookies.set(LINK_COOKIE, f"{payload}.{'0' * len(mac)}", path="/auth/oidc/link")
    assert link(client).status_code == 303 and link(client).headers["location"] == "/login"
    from acm_hub.security import sign_blob

    admin_id = None
    with Session(app.state.engine) as s:
        admin_id = s.exec(select(User).where(User.email == "admin@example.com")).one().id
    expired = sign_blob(app.state.settings.secret_key, "oidc-link", {"uid": admin_id, "key": "x|y"}, ttl=-1)
    client.cookies.set(LINK_COOKIE, expired, path="/auth/oidc/link")
    assert link(client).headers["location"] == "/login"
    wrong_purpose = sign_blob(
        app.state.settings.secret_key, "other", {"uid": admin_id, "key": "x|y"}, ttl=300
    )
    client.cookies.set(LINK_COOKIE, wrong_purpose, path="/auth/oidc/link")
    assert link(client).headers["location"] == "/login"
    with Session(app.state.engine) as s:
        assert s.exec(select(User).where(User.email == "admin@example.com")).one().external_id is None


def test_account_already_linked_to_another_identity_is_not_relinked(sso):
    client, app, idp = sso
    with Session(app.state.engine) as s:
        a = s.exec(select(User).where(User.email == "admin@example.com")).one()
        a.external_id = f"{ISSUER}|someone-else"
        s.add(a)
        s.commit()
    q = begin(client, idp, email="admin@example.com", sub="attacker")
    assert finish(client, q["state"][0]).status_code == 403


def test_deactivated_account_cannot_be_linked(sso):
    client, app, idp = sso
    start_link(client, idp)
    with Session(app.state.engine) as s:  # deactivated between the SSO round trip and the password step
        a = s.exec(select(User).where(User.email == "admin@example.com")).one()
        a.is_active = False
        s.add(a)
        s.commit()
    assert link(client).status_code == 403
    with Session(app.state.engine) as s:
        assert s.exec(select(User).where(User.email == "admin@example.com")).one().external_id is None


def test_link_also_works_in_invite_only_mode(sso):
    client, app, idp = sso
    with Session(app.state.engine) as s:
        inst = s.get(InstanceSettings, 1)
        inst.oidc_provisioning = "invite"
        s.add(inst)
        s.commit()
    start_link(client, idp)
    assert link(client).headers["location"] == "/memory"


def test_signed_blobs_round_trip_and_reject_tampering():
    from acm_hub.security import sign_blob, verify_blob

    blob = sign_blob("k" * 32, "p", {"a": 1}, ttl=60)
    assert (verify_blob("k" * 32, "p", blob) or {}).get("a") == 1
    assert verify_blob("j" * 32, "p", blob) is None  # wrong key
    assert verify_blob("k" * 32, "q", blob) is None  # wrong purpose
    assert verify_blob("k" * 32, "p", blob + "x") is None  # tampered
    assert verify_blob("k" * 32, "p", "garbage") is None
    assert verify_blob("k" * 32, "p", sign_blob("k" * 32, "p", {"a": 1}, ttl=-1)) is None  # expired


def test_invite_only_mode(sso):
    client, app, idp = sso
    with Session(app.state.engine) as s:
        inst = s.get(InstanceSettings, 1)
        inst.oidc_provisioning = "invite"
        s.add(inst)
        s.add(User(email="invited@example.com", auth_provider="oidc"))  # pre-invited by an admin
        s.commit()
    assert finish(client, begin(client, idp)["state"][0]).status_code == 403  # stranger: refused
    client.cookies.clear()
    q = begin(client, idp, email="invited@example.com", sub="inv-1")
    assert finish(client, q["state"][0]).status_code == 303
    with Session(app.state.engine) as s:
        assert (
            s.exec(select(User).where(User.email == "invited@example.com")).one().external_id
            == f"{ISSUER}|inv-1"
        )


def test_provider_rejecting_the_code_fails_cleanly(sso):
    client, app, idp = sso
    q = begin(client, idp)
    idp.code_ok = False
    assert finish(client, q["state"][0]).status_code == 401


def test_https_issuer_is_required():
    from acm_hub.oidc import OIDCError, _check_https

    with pytest.raises(OIDCError):
        _check_https("http://idp.public.example", "issuer")
    _check_https("https://idp.example", "issuer")
    _check_https("http://localhost:8080", "issuer")
