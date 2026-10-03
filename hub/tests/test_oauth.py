import base64
import hashlib
import json
import secrets
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from acm_hub import oauth
from acm_hub.app import create_app
from acm_hub.config import Settings
from acm_hub.models import (
    ApiToken,
    MemoryRecord,
    OAuthClient,
    OAuthCode,
    OAuthGrant,
    OAuthRequest,
    Project,
    User,
    utcnow,
)

from .conftest import PASSWORD, csrf_of, setup_admin

LOOPBACK = "http://127.0.0.1:53682/callback"
CLAUDE = "https://claude.ai/api/mcp/auth_callback"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture()
def ohub(settings, monkeypatch):
    monkeypatch.setenv("MEMORY_HUB_OAUTH_ENABLED", "true")
    root = create_app(Settings())
    with TestClient(root) as client:
        yield client, root.fastapi


@pytest.fixture()
def oauthed(ohub):
    client, app = ohub
    setup_admin(client, app)
    with Session(app.state.engine) as s:
        owner = s.exec(select(User)).one()
        s.add(Project(slug="alpha", owner_user_id=owner.id, visibility="private"))
        s.add(Project(slug="beta", owner_user_id=owner.id, visibility="private"))
        s.commit()
    return client, app


# --- a minimal OAuth client, written from the spec rather than from our server -----------------------------------------


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def register(client, redirect=LOOPBACK, name="Test app", **extra):
    r = client.post(
        "/register",
        json={
            "redirect_uris": [redirect],
            "client_name": name,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            **extra,
        },
    )
    return r


def start(client, cid, challenge, redirect=LOOPBACK, scope="memory:read", **extra):
    params = {
        "response_type": "code",
        "client_id": cid,
        "redirect_uri": redirect,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "xyz",
        "scope": scope,
    } | extra
    return client.get("/authorize", params=params, follow_redirects=False)


def request_id(location):
    return parse_qs(urlsplit(location).query)["request"][0]


def consent(client, rid, *, decision="approve", **form):
    page = client.get(f"/oauth/consent?request={rid}")
    assert page.status_code == 200, page.text
    data = {
        "csrf_token": csrf_of(page.text),
        "request": rid,
        "decision": decision,
        "scope_mode": "some",
        "access": "read_only",
    } | form
    return client.post("/oauth/consent", data=data, follow_redirects=False)


def code_from(resp):
    assert resp.status_code == 303, resp.text
    q = parse_qs(urlsplit(resp.headers["location"]).query)
    return q["code"][0], q


def exchange(client, cid, code, verifier, redirect=LOOPBACK, **extra):
    return client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": cid,
            "code": code,
            "redirect_uri": redirect,
            "code_verifier": verifier,
        }
        | extra,
    )


def refresh(client, cid, token, **extra):
    return client.post(
        "/token", data={"grant_type": "refresh_token", "client_id": cid, "refresh_token": token} | extra
    )


def connect(client, *, redirect=LOOPBACK, scope="memory:read", **form):
    """Run the whole flow, returning (client_id, token response json)."""
    cid = register(client, redirect).json()["client_id"]
    verifier, challenge = pkce()
    rid = request_id(start(client, cid, challenge, redirect, scope).headers["location"])
    form = {"projects": [], "user_scope": ""} | form
    code, _ = code_from(consent(client, rid, **form))
    r = exchange(client, cid, code, verifier, redirect)
    assert r.status_code == 200, r.text
    return cid, r.json()


def mcp(client, token, method="tools/list", params=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return client.post("/mcp", headers=MCP_HEADERS | {"Authorization": f"Bearer {token}"}, json=body)


def call_tool(client, token, name, args):
    return mcp(client, token, "tools/call", {"name": name, "arguments": args})


def db_rows(app, model):
    with Session(app.state.engine) as s:
        return s.exec(select(model)).all()


# --- off by default --------------------------------------------------------------------------------------------------


def test_nothing_is_exposed_unless_switched_on(hub):
    client, app = hub
    for path in (
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource/mcp",
        "/authorize",
        "/token",
    ):
        assert client.get(path).status_code in (404, 405), path
    assert client.post("/register", json={"redirect_uris": [LOOPBACK]}).status_code in (404, 405)
    assert client.get("/oauth/consent?request=x").status_code == 404
    r = client.post("/mcp", json={})
    assert (
        r.status_code == 401 and "resource_metadata" not in r.headers["www-authenticate"]
    )  # nothing to advertise


def test_refuses_to_enable_over_plain_http_on_a_public_host(settings, monkeypatch):
    monkeypatch.setenv("MEMORY_HUB_OAUTH_ENABLED", "true")
    monkeypatch.setenv("MEMORY_HUB_PUBLIC_URL", "http://memory.example.com")
    with pytest.raises(RuntimeError, match="https"):
        create_app(Settings())
    monkeypatch.setenv("MEMORY_HUB_PUBLIC_URL", "https://memory.example.com")
    create_app(Settings())  # fine


# --- discovery --------------------------------------------------------------------------------------------------------


def test_discovery_documents_match_what_claude_requires(ohub):
    client, app = ohub
    issuer = Settings().public_url
    as_meta = client.get("/.well-known/oauth-authorization-server").json()
    assert as_meta["issuer"] == issuer  # exact: RFC 8414 compares the issuer as a string
    assert as_meta["code_challenge_methods_supported"] == ["S256"]
    assert as_meta["token_endpoint_auth_methods_supported"] == ["none"]  # public clients only
    assert {"authorization_code", "refresh_token"} == set(as_meta["grant_types_supported"])
    assert (
        as_meta["registration_endpoint"].endswith("/register")
        and "offline_access" in as_meta["scopes_supported"]
    )
    for suffix, resource in (("/mcp", issuer + "/mcp"), ("/mcp/", issuer + "/mcp/")):
        doc = client.get(f"/.well-known/oauth-protected-resource{suffix}").json()
        assert doc["resource"] == resource  # exactly what the person types into Claude
        assert (
            doc["authorization_servers"][0] == issuer == as_meta["issuer"]
        )  # Claude uses only the first entry
    assert client.get("/.well-known/oauth-protected-resource").status_code == 200  # bare path probe


def test_unauthenticated_mcp_points_clients_at_the_metadata(ohub):
    client, app = ohub
    for path, expected in (
        ("/mcp", "/.well-known/oauth-protected-resource/mcp"),
        ("/mcp/", "/.well-known/oauth-protected-resource/mcp/"),
    ):
        r = client.post(path, json={})
        assert r.status_code == 401
        assert f'resource_metadata="{Settings().public_url}{expected}"' in r.headers["www-authenticate"]


# --- registration policy ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("uri", [LOOPBACK, CLAUDE, "http://localhost:3118/callback", "http://[::1]:9/cb"])
def test_registration_accepts_only_known_safe_redirects(ohub, uri):
    r = register(ohub[0], uri)
    assert r.status_code == 201 and not r.json().get("client_secret")  # public client: nothing to steal
    assert r.json()["token_endpoint_auth_method"] == "none"


@pytest.mark.parametrize(
    "uri",
    [
        "https://evil.example/callback",
        "http://claude.ai/api/mcp/auth_callback",  # right host, wrong scheme
        "https://claude.ai.evil.example/api/mcp/auth_callback",
        "https://claude.ai/api/mcp/auth_callback/../x",
        "https://claude.ai/other",
        "http://localhost.evil.example/cb",
        "http://127.0.0.1.evil.example/cb",
        "http://user@127.0.0.1:1/cb",
        "https://127.0.0.1/cb",  # loopback is for plain http (RFC 8252)
        "myapp://callback",
        CLAUDE + "#frag",
        "http://192.168.1.5/cb",
    ],
)
def test_registration_refuses_everything_else(ohub, uri):
    client, app = ohub
    r = register(client, uri)
    assert r.status_code == 400 and r.json()["error"] in ("invalid_redirect_uri", "invalid_client_metadata")
    assert db_rows(app, OAuthClient) == []  # and nothing was stored


def test_operator_can_allow_an_extra_redirect_exactly(settings, monkeypatch):
    monkeypatch.setenv("MEMORY_HUB_OAUTH_ENABLED", "true")
    monkeypatch.setenv("MEMORY_HUB_OAUTH_EXTRA_REDIRECT_URIS", "https://tools.example/cb")
    with TestClient(create_app(Settings())) as client:
        assert register(client, "https://tools.example/cb").status_code == 201
        assert register(client, "https://tools.example/cb2").status_code == 400  # exact match only
        assert register(client, "https://tools.example/").status_code == 400


def test_client_names_are_cleaned_and_registration_is_capped(ohub, monkeypatch):
    client, app = ohub
    r = register(client, name="Nice\x00\x1bApp" + "x" * 300)
    assert "\x00" not in r.json()["client_name"] and len(r.json()["client_name"]) <= 100
    monkeypatch.setattr(oauth, "MAX_CLIENTS", 2)
    register(client)
    assert register(client).status_code == 400  # unauthenticated registration can't fill the database


def test_registration_is_rate_limited_per_ip(ohub):
    client, app = ohub
    codes = [register(client).status_code for _ in range(22)]
    assert codes[:20] == [201] * 20 and 429 in codes[20:]


# --- the happy path -----------------------------------------------------------------------------------------------------


def test_full_flow_issues_a_scoped_working_token(oauthed):
    client, app = oauthed
    with Session(app.state.engine) as s:
        alpha = s.exec(select(Project).where(Project.slug == "alpha")).one().id
    cid, tok = connect(
        client, scope="memory:read memory:write offline_access", projects=[alpha], access="read_write"
    )
    assert tok["token_type"] == "Bearer" and tok["expires_in"] == 3600 and tok["refresh_token"]
    assert mcp(client, tok["access_token"]).status_code == 200
    row = db_rows(app, ApiToken)[0]
    assert (
        row.project_ids == [alpha] and row.access_level == "read_write" and row.grant_id
    )  # an ordinary token
    assert row.label.startswith("OAuth · Test app") and not row.include_user_scope
    # it can write inside what was approved...
    ok = call_tool(
        client,
        tok["access_token"],
        "memory_write",
        {"name": "alpha-fact", "description": "d", "type": "project", "scope": "project", "project": "alpha"},
    )
    assert ok.status_code == 200 and [r.name for r in db_rows(app, MemoryRecord)] == ["alpha-fact"]
    # ...but not outside it
    call_tool(
        client,
        tok["access_token"],
        "memory_write",
        {"name": "beta-fact", "description": "d", "type": "project", "scope": "project", "project": "beta"},
    )
    assert [r.name for r in db_rows(app, MemoryRecord)] == ["alpha-fact"]


def test_read_only_grant_cannot_write(oauthed):
    client, app = oauthed
    cid, tok = connect(client, scope="memory:read memory:write", scope_mode="all", access="read_only")
    assert tok["scope"] == "memory:read"  # what the response says it can do matches what it can do
    call_tool(
        client,
        tok["access_token"],
        "memory_write",
        {"name": "x", "description": "d", "type": "user", "scope": "user"},
    )
    assert db_rows(app, MemoryRecord) == []


def test_oauth_tokens_get_no_extra_powers_over_the_session_api(oauthed):
    client, app = oauthed
    cid, tok = connect(client, scope_mode="all")
    fresh = TestClient(app)
    r = fresh.get("/api/v1/me", headers={"Authorization": f"Bearer {tok['access_token']}"})
    assert r.status_code == 401  # still tokens-never-here: OAuth doesn't reopen the human API to clients


# --- PKCE, redirects, resource ------------------------------------------------------------------------------------------


def test_pkce_is_mandatory_and_must_be_s256(oauthed):
    client, app = oauthed
    cid = register(client).json()["client_id"]
    base = {"response_type": "code", "client_id": cid, "redirect_uri": LOOPBACK, "state": "s"}
    r = client.get("/authorize", params=base, follow_redirects=False)  # no challenge at all
    assert (
        "code" not in parse_qs(urlsplit(r.headers.get("location", "")).query)
        and db_rows(app, OAuthRequest) == []
    )
    _, challenge = pkce()
    r = client.get(
        "/authorize",
        params=base | {"code_challenge": challenge, "code_challenge_method": "plain"},
        follow_redirects=False,
    )
    assert "/oauth/consent" not in r.headers.get("location", "") and db_rows(app, OAuthRequest) == []


def test_wrong_verifier_fails_and_burns_nothing_it_shouldnt(oauthed):
    client, app = oauthed
    cid = register(client).json()["client_id"]
    verifier, challenge = pkce()
    rid = request_id(start(client, cid, challenge).headers["location"])
    code, _ = code_from(consent(client, rid, scope_mode="all"))
    bad = exchange(client, cid, code, secrets.token_urlsafe(48))
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
    assert db_rows(app, ApiToken) == []  # no token was minted for the guess


def test_unregistered_redirect_never_receives_anything(oauthed):
    client, app = oauthed
    cid = register(client).json()["client_id"]
    _, challenge = pkce()
    r = start(client, cid, challenge, redirect="http://127.0.0.1:9999/other")
    assert (
        r.status_code == 400 and "location" not in r.headers
    )  # an error page, not a redirect to the attacker's URI
    assert db_rows(app, OAuthRequest) == []


def test_resource_indicator_must_name_this_server(oauthed):
    client, app = oauthed
    cid = register(client).json()["client_id"]
    _, challenge = pkce()
    bad = start(client, cid, challenge, resource="https://other.example/mcp")
    assert "invalid_target" in bad.headers["location"] and "oauth/consent" not in bad.headers["location"]
    base = Settings().public_url + "/mcp"
    for ok in (base, base + "/"):
        assert "/oauth/consent" in start(client, cid, challenge, resource=ok).headers["location"]


# --- codes -----------------------------------------------------------------------------------------------------------


def make_code(client, **form):
    cid = register(client).json()["client_id"]
    verifier, challenge = pkce()
    rid = request_id(start(client, cid, challenge).headers["location"])
    code, q = code_from(consent(client, rid, **({"scope_mode": "all"} | form)))
    return cid, verifier, code, q


def test_a_code_works_once_and_a_replay_kills_what_it_made(oauthed):
    client, app = oauthed
    cid, verifier, code, _ = make_code(client)
    first = exchange(client, cid, code, verifier)
    assert first.status_code == 200
    token = first.json()["access_token"]
    assert mcp(client, token).status_code == 200
    second = exchange(client, cid, code, verifier)
    assert second.status_code == 400 and second.json()["error"] == "invalid_grant"
    assert (
        mcp(client, token).status_code == 401
    )  # RFC 6749 §4.1.2: the replay revokes the first exchange's token
    assert refresh(client, cid, first.json()["refresh_token"]).status_code == 400  # and its refresh token


def test_codes_expire_and_belong_to_one_client(oauthed):
    client, app = oauthed
    cid, verifier, code, _ = make_code(client)
    other = register(client).json()["client_id"]
    assert exchange(client, other, code, verifier).status_code == 400  # someone else's code
    with Session(app.state.engine) as s:
        row = s.exec(select(OAuthCode)).one()
        row.expires_at = utcnow() - timedelta(seconds=1)
        s.add(row)
        s.commit()
    r = exchange(client, cid, code, verifier)
    assert r.status_code == 400 and db_rows(app, ApiToken) == []


def test_only_the_hash_of_secrets_is_stored(oauthed):
    client, app = oauthed
    cid, tok = connect(client, scope_mode="all")
    dump = json.dumps(
        [r.model_dump(mode="json") for m in (OAuthCode, OAuthGrant, ApiToken) for r in db_rows(app, m)]
    )
    assert tok["access_token"] not in dump and tok["refresh_token"] not in dump


# --- the consent screen --------------------------------------------------------------------------------------------------


def test_consent_requires_a_signed_in_person(oauthed):
    client, app = oauthed
    cid = register(client).json()["client_id"]
    _, challenge = pkce()
    rid = request_id(start(client, cid, challenge).headers["location"])
    anon = TestClient(app)
    r = anon.get(f"/oauth/consent?request={rid}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")
    r = anon.post(
        "/oauth/consent",
        data={"request": rid, "decision": "approve", "scope_mode": "all"},
        follow_redirects=False,
    )
    assert r.status_code in (303, 403) and db_rows(app, OAuthCode) == []  # no session, no code


def test_consent_post_needs_csrf(oauthed):
    client, app = oauthed
    cid = register(client).json()["client_id"]
    _, challenge = pkce()
    rid = request_id(start(client, cid, challenge).headers["location"])
    r = client.post(
        "/oauth/consent",
        data={"request": rid, "decision": "approve", "scope_mode": "all"},
        follow_redirects=False,
    )
    assert r.status_code == 403 and db_rows(app, OAuthCode) == []  # a drive-by page can't approve for you


def test_deny_returns_access_denied_and_no_code(oauthed):
    client, app = oauthed
    cid = register(client).json()["client_id"]
    _, challenge = pkce()
    rid = request_id(start(client, cid, challenge).headers["location"])
    r = consent(client, rid, decision="deny")
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert (
        r.headers["location"].startswith(LOOPBACK)
        and q["error"] == ["access_denied"]
        and q["state"] == ["xyz"]
        and "code" not in q
    )
    assert db_rows(app, OAuthCode) == [] and db_rows(app, OAuthRequest) == []


def test_consent_choices_are_validated(oauthed):
    client, app = oauthed
    cid = register(client).json()["client_id"]
    _, challenge = pkce()
    rid = request_id(start(client, cid, challenge, scope="memory:read").headers["location"])
    assert (
        consent(client, rid, scope_mode="some").status_code == 422
    )  # nothing picked: not silently "everything"
    assert consent(client, rid, scope_mode="some", projects=["not-a-project"]).status_code == 422
    assert (
        consent(client, rid, scope_mode="all", access="read_write").status_code == 422
    )  # it never asked to write
    assert db_rows(app, OAuthCode) == []
    assert consent(client, rid, scope_mode="all").status_code == 303


def test_someone_elses_project_cannot_be_granted(oauthed):
    client, app = oauthed
    with Session(app.state.engine) as s:
        from acm_hub.security import hash_password

        eve = User(email="eve@example.com", password_hash=hash_password(PASSWORD))
        s.add(eve)
        s.commit()
        theirs = Project(slug="eves", owner_user_id=eve.id, visibility="private")
        s.add(theirs)
        s.commit()
        pid = theirs.id
    cid = register(client).json()["client_id"]
    _, challenge = pkce()
    rid = request_id(start(client, cid, challenge).headers["location"])
    assert consent(client, rid, scope_mode="some", projects=[pid]).status_code == 422


def test_redirect_comes_from_the_stored_request_not_the_form(oauthed):
    client, app = oauthed
    cid = register(client).json()["client_id"]
    _, challenge = pkce()
    rid = request_id(start(client, cid, challenge).headers["location"])
    r = consent(
        client, rid, scope_mode="all", redirect_uri="https://evil.example/steal", next="https://evil.example"
    )
    assert r.headers["location"].startswith(LOOPBACK + "?code=") or r.headers["location"].startswith(LOOPBACK)


def test_consent_page_shows_where_it_goes_and_cannot_be_scripted(oauthed):
    client, app = oauthed
    cid = register(client, name="<script>alert(1)</script>").json()["client_id"]
    _, challenge = pkce()
    rid = request_id(start(client, cid, challenge).headers["location"])
    page = client.get(f"/oauth/consent?request={rid}")
    assert (
        "<script>alert(1)</script>" not in page.text and "&lt;script&gt;" in page.text
    )  # attacker-controlled name is inert
    assert (
        "127.0.0.1:53682" in page.text and "this computer" in page.text
    )  # loopback warning + visible destination
    csp = page.headers["content-security-policy"]
    assert "form-action 'self' http://127.0.0.1:53682" in csp and "evil" not in csp
    assert "script-src 'self'" in csp and "'unsafe-inline'" not in csp


def test_expired_requests_are_gone(oauthed):
    client, app = oauthed
    cid = register(client).json()["client_id"]
    _, challenge = pkce()
    rid = request_id(start(client, cid, challenge).headers["location"])
    with Session(app.state.engine) as s:
        r = s.get(OAuthRequest, rid)
        r.expires_at = utcnow() - timedelta(seconds=1)
        s.add(r)
        s.commit()
    assert client.get(f"/oauth/consent?request={rid}").status_code == 404
    assert client.get("/oauth/consent?request=nope").status_code == 404


# --- refresh ------------------------------------------------------------------------------------------------------------


def test_refresh_rotates_both_tokens(oauthed):
    client, app = oauthed
    cid, tok = connect(client, scope_mode="all")
    r = refresh(client, cid, tok["refresh_token"])
    assert r.status_code == 200
    new = r.json()
    assert new["refresh_token"] != tok["refresh_token"] and new["access_token"] != tok["access_token"]
    assert mcp(client, new["access_token"]).status_code == 200
    assert mcp(client, tok["access_token"]).status_code == 401  # the superseded access token is dead at once


def test_replaying_a_rotated_refresh_token_ends_the_grant(oauthed):
    client, app = oauthed
    cid, tok = connect(client, scope_mode="all")
    new = refresh(client, cid, tok["refresh_token"]).json()
    stolen = refresh(client, cid, tok["refresh_token"])  # the old one again: client or thief, we can't tell
    assert stolen.status_code == 400 and stolen.json()["error"] == "invalid_grant"
    assert mcp(client, new["access_token"]).status_code == 401  # so the legitimate chain dies too
    assert refresh(client, cid, new["refresh_token"]).status_code == 400
    assert all(g.revoked_at for g in db_rows(app, OAuthGrant))


def test_refresh_cannot_widen_scope_or_cross_clients(oauthed):
    client, app = oauthed
    cid, tok = connect(client, scope="memory:read", scope_mode="all")
    assert refresh(client, cid, tok["refresh_token"], scope="memory:read memory:write").status_code == 400
    other = register(client).json()["client_id"]
    assert refresh(client, other, tok["refresh_token"]).status_code == 400


def test_refresh_chain_has_an_absolute_lifetime(oauthed):
    client, app = oauthed
    cid, tok = connect(client, scope_mode="all")
    with Session(app.state.engine) as s:
        g = s.exec(select(OAuthGrant)).one()
        g.expires_at = utcnow() - timedelta(seconds=1)
        s.add(g)
        s.commit()
    assert refresh(client, cid, tok["refresh_token"]).status_code == 400


def test_access_tokens_expire_quickly(oauthed):
    client, app = oauthed
    cid, tok = connect(client, scope_mode="all")
    with Session(app.state.engine) as s:
        t = s.exec(select(ApiToken)).one()
        t.expires_at = utcnow() - timedelta(seconds=1)
        s.add(t)
        s.commit()
    assert mcp(client, tok["access_token"]).status_code == 401
    assert (
        refresh(client, cid, tok["refresh_token"]).status_code == 200
    )  # renewing is what the refresh token is for


# --- ending access -------------------------------------------------------------------------------------------------------


def test_disconnecting_an_app_ends_access_immediately(oauthed):
    client, app = oauthed
    cid, tok = connect(client, scope_mode="all")
    page = client.get("/settings")
    assert "Connected apps" in page.text and "Test app" in page.text
    gid = db_rows(app, OAuthGrant)[0].id
    r = client.post(
        f"/settings/apps/{gid}/disconnect", data={"csrf_token": csrf_of(page.text)}, follow_redirects=False
    )
    assert r.status_code == 303
    assert mcp(client, tok["access_token"]).status_code == 401
    assert refresh(client, cid, tok["refresh_token"]).status_code == 400
    assert (
        "Test app" not in client.get("/settings").text.split("API tokens")[0].split("Connected apps")[-1]
        or "Nothing connected" in client.get("/settings").text
    )


def test_revoking_the_access_token_row_cannot_leave_the_refresh_token_alive(oauthed):
    client, app = oauthed
    cid, tok = connect(client, scope_mode="all")
    tid = db_rows(app, ApiToken)[0].id
    page = client.get("/settings")
    client.post(f"/settings/tokens/{tid}/revoke", data={"csrf_token": csrf_of(page.text)})
    assert refresh(client, cid, tok["refresh_token"]).status_code == 400


def test_oauth_tokens_are_listed_as_apps_not_as_hand_made_tokens(oauthed):
    client, app = oauthed
    connect(client, scope_mode="all")
    page = client.get("/settings").text
    tokens_section = page.split("<h2>API tokens</h2>")[1]
    assert "OAuth ·" not in tokens_section


def test_only_the_owner_can_disconnect_a_grant(oauthed):
    client, app = oauthed
    connect(client, scope_mode="all")
    gid = db_rows(app, OAuthGrant)[0].id
    with Session(app.state.engine) as s:
        from acm_hub.security import hash_password

        s.add(User(email="eve@example.com", password_hash=hash_password(PASSWORD)))
        s.commit()
    eve = TestClient(app)
    eve.post("/login", data={"email": "eve@example.com", "password": PASSWORD})
    csrf = csrf_of(eve.get("/memory").text)
    assert eve.post(f"/settings/apps/{gid}/disconnect", data={"csrf_token": csrf}).status_code == 403
    assert db_rows(app, OAuthGrant)[0].revoked_at is None


def test_deactivating_a_user_kills_their_oauth_access(oauthed):
    client, app = oauthed
    cid, tok = connect(client, scope_mode="all")
    with Session(app.state.engine) as s:
        u = s.exec(select(User)).one()
        u.is_active = False
        s.add(u)
        s.commit()
    assert mcp(client, tok["access_token"]).status_code == 401
    assert refresh(client, cid, tok["refresh_token"]).status_code == 400  # and they can't mint a new one


def test_revocation_endpoint_ends_the_grant_from_either_token(oauthed):
    client, app = oauthed
    cid, tok = connect(client, scope_mode="all")
    r = client.post("/revoke", data={"client_id": cid, "token": tok["refresh_token"]})
    assert r.status_code == 200
    assert mcp(client, tok["access_token"]).status_code == 401  # revoking one half ends both
    cid2, tok2 = connect(client, scope_mode="all")
    client.post("/revoke", data={"client_id": cid2, "token": tok2["access_token"]})
    assert refresh(client, cid2, tok2["refresh_token"]).status_code == 400
    other = register(client).json()["client_id"]
    cid3, tok3 = connect(client, scope_mode="all")
    client.post(
        "/revoke", data={"client_id": other, "token": tok3["access_token"]}
    )  # someone else's client id
    assert mcp(client, tok3["access_token"]).status_code == 200


def test_unused_registrations_are_swept(oauthed):
    client, app = oauthed
    register(client)
    with Session(app.state.engine) as s:
        c = s.exec(select(OAuthClient)).one()
        c.created_at = utcnow() - timedelta(days=2)
        s.add(c)
        s.commit()
    register(client)  # sweeping happens on registration
    assert len(db_rows(app, OAuthClient)) == 1


# --- cascades and the offline CLI --------------------------------------------------------------------------------------


def test_deactivation_marks_grants_revoked_not_just_blocked(oauthed):
    client, app = oauthed
    with Session(app.state.engine) as s:
        from acm_hub.access import principal_for_user
        from acm_hub.orgs import create_user, set_user_active

        admin = s.exec(select(User)).one()
        other = create_user(s, principal_for_user(s, admin), "dana@example.com", password=PASSWORD)
        s.commit()
        grant = OAuthGrant(
            user_id=other.id, client_id="c", expires_at=utcnow() + timedelta(days=1), refresh_hash="h"
        )
        s.add(grant)
        s.commit()
        set_user_active(s, principal_for_user(s, admin), other, False)
        s.commit()
        assert s.get(OAuthGrant, grant.id).revoked_at is not None


def test_cli_token_revoke_also_ends_an_oauth_grant(settings, oauthed):
    client, app = oauthed
    cid, tok = connect(client, scope_mode="all")
    from acm_hub.cli import main

    tid = db_rows(app, ApiToken)[0].id
    assert main(["token", "revoke", tid]) == 0  # the CLI opens the same database file the app is using
    assert mcp(client, tok["access_token"]).status_code == 401
    assert refresh(client, cid, tok["refresh_token"]).status_code == 400  # not just the access token


def test_acm_oauth_lists_and_ends_grants_offline(settings, monkeypatch, capsys, oauthed):
    client, app = oauthed
    cid, tok = connect(client, scope_mode="all")
    from acm_hub.cli import main

    assert main(["oauth", "list"]) == 0
    out = capsys.readouterr().out
    assert "Test app" in out and "read_only" in out and "active" in out
    assert main(["oauth", "revoke"]) == 2  # refuses to guess: name a grant or pass --all
    assert (
        "Test app" in out and mcp(client, tok["access_token"]).status_code == 200
    )  # nothing was ended by that
    gid = out.split("\t")[0]
    assert main(["oauth", "revoke", gid]) == 0
    assert mcp(client, tok["access_token"]).status_code == 401
    assert refresh(client, cid, tok["refresh_token"]).status_code == 400
    assert main(["oauth", "list"]) == 0 and "revoked" in capsys.readouterr().out


def test_acm_oauth_all_users_is_admin_only(settings, monkeypatch, capsys, oauthed):
    client, app = oauthed
    connect(client, scope_mode="all")
    with Session(app.state.engine) as s:
        from acm_hub.security import hash_password

        s.add(User(email="eve@example.com", password_hash=hash_password(PASSWORD)))
        s.commit()
    from acm_hub.cli import main

    assert main(["oauth", "list", "--all-users", "--as", "eve@example.com"]) == 1  # not an admin
    capsys.readouterr()
    assert (
        main(["oauth", "list", "--as", "eve@example.com"]) == 0 and "Test app" not in capsys.readouterr().out
    )
    gid = db_rows(app, OAuthGrant)[0].id
    assert main(["oauth", "revoke", gid, "--as", "eve@example.com"]) == 2  # can't touch someone else's grant
    assert db_rows(app, OAuthGrant)[0].revoked_at is None


def test_two_exchanges_racing_on_one_code_yield_exactly_one_grant(oauthed):
    """Both requests load the (still unused) code, then both try to exchange it. The claim must be atomic: the
    sequential replay check can't help here, because neither has marked it used yet when they load it."""
    import anyio
    from mcp.server.auth.provider import TokenError

    client, app = oauthed
    cid, verifier, code, _ = make_code(client)
    provider = oauth.HubOAuthProvider(app.state.engine, app.state.settings)

    async def race():
        info = await provider.get_client(cid)
        a = await provider.load_authorization_code(info, code)
        b = await provider.load_authorization_code(info, code)
        assert a is not None and b is not None  # the window the atomic UPDATE closes
        results = []
        for loaded in (a, b):
            try:
                results.append(await provider.exchange_authorization_code(info, loaded))
            except TokenError as e:
                results.append(e)
        return results

    first, second = anyio.run(race)
    assert (
        not isinstance(first, Exception) and isinstance(second, Exception) and second.error == "invalid_grant"
    )
    assert (
        len(db_rows(app, OAuthGrant)) == 1 and len(db_rows(app, ApiToken)) == 1
    )  # one code, one grant, one token
