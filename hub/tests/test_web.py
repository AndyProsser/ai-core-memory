import io
import re
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from acm_hub.app import create_app
from acm_hub.auth import issue_setup_code
from acm_hub.models import ApiToken, MemoryRecord, User

PASSWORD = "correct horse battery staple"


@pytest.fixture()
def hub(settings):
    root = create_app(settings)
    with TestClient(root) as client:
        yield client, root.fastapi


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf" content="([^"]+)"', html)
    assert m, "no csrf meta on page"
    return m.group(1)


def setup_admin(client, app, email="admin@example.com"):
    with Session(app.state.engine) as s:
        code = issue_setup_code(s)
    r = client.post(
        "/setup",
        data={
            "setup_code": code,
            "email": email,
            "password": PASSWORD,
            "confirm": PASSWORD,
            "deployment_mode": "solo",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return r


@pytest.fixture()
def authed(hub):
    client, app = hub
    setup_admin(client, app)
    page = client.get("/memory")
    assert page.status_code == 200
    return client, app, csrf_of(page.text)


def test_first_run_redirects_to_setup_and_setup_needs_the_code(hub):
    client, app = hub
    assert client.get("/memory", follow_redirects=False).headers["location"] == "/setup"
    bad = client.post(
        "/setup", data={"setup_code": "nope", "email": "a@b.co", "password": PASSWORD, "confirm": PASSWORD}
    )
    assert bad.status_code == 400 and "setup code" in bad.text
    assert client.get("/healthz").json()["status"] == "ok"
    setup_admin(client, app)
    assert (
        client.get("/setup", follow_redirects=False).headers["location"] == "/login"
    )  # one admin, then closed
    with Session(app.state.engine) as s:
        u = s.exec(select(User)).one()
        assert u.is_admin and u.password_hash.startswith("$argon2id$")


def test_setup_code_is_single_use(hub):
    client, app = hub
    with Session(app.state.engine) as s:
        code = issue_setup_code(s)
    client.post(
        "/setup", data={"setup_code": code, "email": "a@b.co", "password": PASSWORD, "confirm": PASSWORD}
    )
    c2 = TestClient(app)  # a second visitor
    r = c2.post(
        "/setup",
        data={"setup_code": code, "email": "evil@b.co", "password": PASSWORD, "confirm": PASSWORD},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/login"
    with Session(app.state.engine) as s:
        assert len(s.exec(select(User)).all()) == 1


def test_login_logout_and_throttle(hub):
    client, app = hub
    setup_admin(client, app)
    client.post("/logout", headers={"X-CSRF-Token": csrf_of(client.get("/memory").text)})
    assert client.get("/memory", follow_redirects=False).status_code == 303
    assert (
        client.post("/login", data={"email": "admin@example.com", "password": "wrong-password-1"}).status_code
        == 401
    )
    ok = client.post(
        "/login", data={"email": "admin@example.com", "password": PASSWORD}, follow_redirects=False
    )
    assert ok.status_code == 303 and "acm_session" in ok.headers["set-cookie"]
    assert "HttpOnly" in ok.headers["set-cookie"] and "samesite=lax" in ok.headers["set-cookie"].lower()
    for _ in range(8):
        client.post("/login", data={"email": "ghost@example.com", "password": "x" * 12})
    assert client.post("/login", data={"email": "ghost@example.com", "password": "x" * 12}).status_code == 429


def test_open_redirect_is_refused(hub):
    client, app = hub
    setup_admin(client, app)
    c2 = TestClient(app)
    r = c2.post(
        "/login",
        data={"email": "admin@example.com", "password": PASSWORD, "next": "//evil.example/x"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/memory"


def test_csrf_is_required_on_state_changes(authed):
    client, app, token = authed
    form = {"name": "a-rule", "description": "d", "type": "feedback", "scope": "user"}
    r = client.post("/memory/new", data=form)
    assert r.status_code == 403
    r = client.post("/memory/new", data=form | {"csrf_token": "forged"})
    assert r.status_code == 403
    r = client.post("/memory/new", data=form | {"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    cross = client.post(
        "/memory/new",
        data=form | {"csrf_token": token, "name": "other"},
        headers={"Origin": "https://evil.example"},
    )
    assert cross.status_code == 403


def test_security_headers_and_no_external_resources(authed):
    client, *_ = authed
    r = client.get("/memory")
    assert (
        "script-src 'self'" in r.headers["content-security-policy"]
        and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    )
    assert r.headers["cache-control"] == "no-store"
    assert (
        "http://" not in re.sub(r"http://testserver", "", r.text) and "https://" not in r.text
    )  # nothing third-party


def test_record_lifecycle_in_ui_and_markdown_is_sanitized(authed):
    client, app, token = authed
    body = "Hello <script>alert(1)</script>\n\n[x](javascript:alert(1)) ![i](http://evil/x.png)"
    r = client.post(
        "/memory/new",
        data={
            "csrf_token": token,
            "name": "xss-check",
            "description": "d",
            "body": body,
            "type": "feedback",
            "scope": "user",
            "topics": "Security, ui",
        },
        follow_redirects=False,
    )
    rid = r.headers["location"].split("/memory/")[1].split("?")[0]
    page = client.get(f"/memory/{rid}").text
    assert (
        "<script>alert" not in page and 'href="javascript:' not in page and "<img" not in page
    )  # no script, no js: links, no remote images
    assert "&lt;script&gt;" in page
    assert client.get("/memory?q=hello").text.count("xss-check") >= 1
    assert "#security" in client.get("/memory").text
    # edit, history with diff
    client.post(
        f"/memory/{rid}/edit",
        data={
            "csrf_token": token,
            "name": "xss-check",
            "description": "d2",
            "body": "second",
            "type": "feedback",
            "confidence": "observed",
            "tier": "associated",
            "status": "active",
            "topics": "ui",
            "links": "",
        },
    )
    hist = client.get(f"/memory/{rid}?tab=history").text
    assert "second" in hist and "ui" in hist
    # promote to core, appears in focus preview, then archive
    client.post(f"/memory/{rid}/quick", data={"csrf_token": token, "tier": "core"})
    assert "xss-check" in client.get("/focus?task=anything+at+all").text
    client.post(f"/memory/{rid}/quick", data={"csrf_token": token, "status": "archived"})
    assert "xss-check" not in client.get("/memory").text
    assert "xss-check" in client.get("/memory?status=archived").text


def test_established_needs_confirmation_in_ui(authed):
    client, app, token = authed
    r = client.post(
        "/memory/new",
        data={
            "csrf_token": token,
            "name": "hold-the-line",
            "description": "d",
            "body": "v1",
            "type": "rule",
            "scope": "user",
            "confidence": "established",
        },
        follow_redirects=False,
    )
    rid = r.headers["location"].split("/memory/")[1].split("?")[0]
    edit = {
        "csrf_token": token,
        "name": "hold-the-line",
        "description": "d",
        "body": "v2",
        "type": "rule",
        "confidence": "established",
        "tier": "associated",
        "status": "active",
        "topics": "",
        "links": "",
    }
    blocked = client.post(f"/memory/{rid}/edit", data=edit)
    assert blocked.status_code == 409 and "established" in blocked.text
    ok = client.post(f"/memory/{rid}/edit", data=edit | {"confirm_established": "1"}, follow_redirects=False)
    assert ok.status_code == 303
    with Session(app.state.engine) as s:
        assert s.get(MemoryRecord, rid).body == "v2"


def test_export_import_via_ui(authed):
    client, app, token = authed
    client.post(
        "/memory/new",
        data={
            "csrf_token": token,
            "name": "keep-me",
            "description": "d",
            "body": "b",
            "type": "feedback",
            "scope": "user",
        },
    )
    z = client.get("/data/export?history=1")
    assert z.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert "manifest.json" in names and "user/keep-me.md" in names
    prev = client.post(
        "/data/import", data={"csrf_token": token}, files={"file": ("x.zip", z.content, "application/zip")}
    )
    assert prev.status_code == 200 and "Dry run" in prev.text and "unchanged" in prev.text
    stash = re.search(r'name="stash" value="([^"]+)"', prev.text).group(1)
    done = client.post(
        "/data/import/apply", data={"csrf_token": token, "stash": stash}, follow_redirects=False
    )
    assert done.status_code == 303
    again = client.post(
        "/data/import/apply", data={"csrf_token": token, "stash": stash}, follow_redirects=False
    )  # stash is single-use
    assert "expired" in again.headers["location"]


def test_tokens_ui_hashing_scoping_and_revocation(authed):
    client, app, token = authed
    bad = client.post(
        "/settings/tokens",
        data={"csrf_token": token, "label": "ci", "access_level": "read_only", "password": "nope-nope-nope"},
    )
    assert bad.status_code == 403
    r = client.post(
        "/settings/tokens",
        data={
            "csrf_token": token,
            "label": "laptop",
            "access_level": "read_write",
            "expires_days": "30",
            "password": PASSWORD,
        },
    )
    raw = re.search(r"(acm_live_[A-Za-z0-9_\-]{43})", r.text).group(1)
    assert "claude mcp add" in r.text
    with Session(app.state.engine) as s:
        t = s.exec(select(ApiToken)).one()
        assert t.token_hash != raw and raw not in str(t.model_dump()) and len(t.token_hash) == 64
        assert t.expires_at and t.include_user_scope is False
    assert raw not in client.get("/settings").text  # shown exactly once
    too_long = client.post(
        "/settings/tokens",
        data={"csrf_token": token, "label": "x", "expires_days": "9999", "password": PASSWORD},
    )
    assert too_long.status_code == 422
    rev = client.post(f"/settings/tokens/{t.id}/revoke", data={"csrf_token": token}, follow_redirects=False)
    assert rev.status_code == 303


def test_theme_preference_is_persisted_and_rendered(authed):
    client, app, token = authed
    client.post("/settings/theme", data={"csrf_token": token, "theme": "dark"})
    assert 'data-pref="dark"' in client.get("/memory").text
    assert "data-theme-set" in client.get("/memory").text  # the toggle is on every screen
    assert client.post("/settings/theme", data={"csrf_token": token, "theme": "neon"}).status_code == 422


def test_only_admin_changes_instance_settings(authed):
    client, app, token = authed
    ok = client.post(
        "/settings/instance",
        data={
            "csrf_token": token,
            "deployment_mode": "team",
            "core_token_budget": "3000",
            "default_token_expiry_days": "60",
            "max_token_expiry_days": "200",
        },
        follow_redirects=False,
    )
    assert ok.status_code == 303
    bad = client.post(
        "/settings/instance",
        data={
            "csrf_token": token,
            "deployment_mode": "solo",
            "core_token_budget": "5",
            "default_token_expiry_days": "60",
            "max_token_expiry_days": "200",
        },
    )
    assert bad.status_code == 422


def test_inbox_capture_and_conversion(authed):
    client, app, token = authed
    client.post(
        "/inbox",
        data={"csrf_token": token, "title": "Idea: try SQLite FTS", "body": "worth a spike", "scope": "user"},
    )
    rv = client.get("/review").text
    assert "Idea: try SQLite FTS" in rv
    iid = re.search(r"inbox_id=([0-9A-Z]{26})", rv).group(1)
    form = client.get(f"/memory/new?inbox_id={iid}").text
    assert "idea-try-sqlite-fts" in form
    client.post(
        "/memory/new",
        data={
            "csrf_token": token,
            "name": "idea-try-sqlite-fts",
            "description": "d",
            "type": "intent",
            "scope": "user",
            "confidence": "confirmed",
            "inbox_id": iid,
        },
    )
    assert "Idea: try SQLite FTS" not in client.get("/review").text  # harvested
