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


def test_templates_have_no_inline_styles_or_scripts():
    """The CSP (style-src 'self', script-src 'self') silently drops inline styles/scripts, so they must never appear."""
    from pathlib import Path

    import acm_hub.web as web

    for f in (Path(web.__file__).parent / "templates").glob("*.html"):
        text = f.read_text()
        assert " style=" not in text, f"{f.name} uses an inline style attribute (blocked by CSP)"
        assert not re.search(r"<script(?![^>]*\bsrc=)", text), f"{f.name} has an inline <script>"
        assert not re.search(r"\bon[a-z]+=", text), f"{f.name} has an inline event handler"


def test_missing_and_foreign_records_are_404_not_500(authed):
    client, app, token = authed
    ghost = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert client.get(f"/memory/{ghost}").status_code == 404
    assert (
        client.post(
            f"/memory/{ghost}/edit", data={"csrf_token": token, "name": "x", "description": "d"}
        ).status_code
        == 404
    )
    assert (
        client.post(f"/memory/{ghost}/quick", data={"csrf_token": token, "tier": "core"}).status_code == 404
    )
    assert (
        client.post(
            f"/memory/{ghost}/conflicts/{ghost}", data={"csrf_token": token, "action": "apply"}
        ).status_code
        == 404
    )


def test_password_change_ends_other_sessions(authed):
    client, app, token = authed
    other = TestClient(app)  # a second browser, signed in as the same person
    other.post("/login", data={"email": "admin@example.com", "password": PASSWORD})
    assert other.get("/memory", follow_redirects=False).status_code == 200
    new = "an entirely different passphrase"
    r = client.post(
        "/settings/password",
        data={"csrf_token": token, "current": PASSWORD, "new": new, "confirm": new},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "changed" in r.headers["location"]
    assert client.get("/memory", follow_redirects=False).status_code == 200  # this session stays
    assert other.get("/memory", follow_redirects=False).status_code == 303  # the other one is gone
    assert (
        TestClient(app).post("/login", data={"email": "admin@example.com", "password": PASSWORD}).status_code
        == 401
    )


# --- Phase 2 UI: Review (proposals), lifecycle on the record page ---------------------------------------------

DUP = "Always send an idempotency key on webhook retries so a replay can never double charge a customer."


def new_record(client, token, name, body=DUP, **kw):
    data = {
        "csrf_token": token,
        "name": name,
        "description": f"{name} about webhook retries",
        "body": body,
        "type": "feedback",
        "scope": "user",
    } | kw
    r = client.post("/memory/new", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r.headers["location"].split("/memory/")[1].split("?")[0]


def run_now(client, token):
    return client.post("/review/consolidate", data={"csrf_token": token}, follow_redirects=False)


def test_review_shows_proposals_with_a_badge_and_approving_applies_them(authed):
    client, app, token = authed
    a, b = new_record(client, token, "keys-one"), new_record(client, token, "keys-two")
    r = run_now(client, token)
    assert "1%20new%20proposal" in r.headers["location"]
    page = client.get("/review").text
    assert "Merge keys-two into keys-one" in page or "Merge keys-one into keys-two" in page
    assert "from the hub" in page and "Text overlap" in page
    assert re.search(r'class="count"[^>]*>1<', client.get("/memory").text)  # the nav badge counts it
    pid = re.search(r'action="/review/proposals/([0-9A-Z]{26})"', page).group(1)
    r = client.post(
        f"/review/proposals/{pid}", data={"csrf_token": token, "action": "approve"}, follow_redirects=False
    )
    assert r.status_code == 303 and "Applied" in r.headers["location"]
    active_ids = set(re.findall(r'href="/memory/([0-9A-Z]{26})"', client.get("/memory").text))
    retired_ids = set(
        re.findall(r'href="/memory/([0-9A-Z]{26})"', client.get("/memory?status=superseded").text)
    )
    assert (
        len(active_ids & {a, b}) == 1 and len(retired_ids & {a, b}) == 1
    )  # one kept, one retired into history
    assert "Nothing to review" in client.get("/review").text
    assert "applied" in client.get("/review?show=all").text
    again = client.post(
        f"/review/proposals/{pid}", data={"csrf_token": token, "action": "approve"}, follow_redirects=False
    )
    assert "already" in again.headers["location"]


def test_reject_and_csrf_on_review_actions(authed):
    client, app, token = authed
    new_record(client, token, "keys-one"), new_record(client, token, "keys-two")
    run_now(client, token)
    pid = re.search(r'action="/review/proposals/([0-9A-Z]{26})"', client.get("/review").text).group(1)
    assert (
        client.post(f"/review/proposals/{pid}", data={"action": "approve"}).status_code == 403
    )  # no CSRF token
    assert client.post("/review/consolidate", data={}).status_code == 403
    assert client.post("/review/approve-low-risk", data={}).status_code == 403
    client.post(
        f"/review/proposals/{pid}", data={"csrf_token": token, "action": "reject", "note": "different"}
    )
    assert "Nothing to review" in client.get("/review").text
    run_now(client, token)
    assert "Nothing to review" in client.get("/review").text  # rejected: not re-raised


def test_batch_approve_only_takes_low_risk_proposals(authed):
    client, app, token = authed
    t2 = "Run the linter locally before every commit so the build never fails on style alone, ever."
    for n in ("keys-one", "keys-two"):
        new_record(client, token, n)
    for n in ("lint-one", "lint-two"):
        new_record(client, token, n, body=t2, confidence="confirmed")  # confirmed: not low risk
    run_now(client, token)
    page = client.get("/review").text
    assert page.count('class="card proposal"') == 2 and "1 low-risk" not in page
    # one low-risk proposal doesn't get a batch button (the button appears from 2 up)
    assert "/review/approve-low-risk" not in page
    new_record(
        client,
        token,
        "other-a",
        body="Prefer small focused pull requests with descriptive titles and linked issues always.",
    )
    new_record(
        client,
        token,
        "other-b",
        body="Prefer small focused pull requests with descriptive titles and linked issues always.",
    )
    run_now(client, token)
    page = client.get("/review").text
    assert "2 low-risk proposals" in page
    r = client.post("/review/approve-low-risk", data={"csrf_token": token}, follow_redirects=False)
    assert "Approved%202" in r.headers["location"]
    after = client.get("/review").text
    assert (
        after.count('class="card proposal"') == 1 and "lint-" in after
    )  # only the confirmed pair is left for a person


def test_established_proposal_requires_the_confirmation_box(authed):
    client, app, token = authed
    from acm_hub import proposals as pr
    from acm_hub.access import principal_for_user
    from acm_hub.models import MemoryRecord, User

    old = new_record(client, token, "bedrock-rule", confidence="established", type="rule")
    new = new_record(client, token, "newer-rule", body="a replacement")
    with Session(app.state.engine) as s:
        human = principal_for_user(s, s.exec(select(User)).one())
        pr.create_proposal(s, human, "supersede", {"old": old, "new": new}, rationale="the rule changed")
        s.commit()
    page = client.get("/review").text
    assert "established" in page and 'name="confirm_established"' in page
    pid = re.search(r'action="/review/proposals/([0-9A-Z]{26})"', page).group(1)
    r = client.post(
        f"/review/proposals/{pid}", data={"csrf_token": token, "action": "approve"}, follow_redirects=False
    )
    assert "confirmation" in r.headers["location"]
    with Session(app.state.engine) as s:
        assert s.get(MemoryRecord, old).status == "active"
    r = client.post(
        f"/review/proposals/{pid}",
        data={"csrf_token": token, "action": "approve", "confirm_established": "1"},
        follow_redirects=False,
    )
    assert "Applied" in r.headers["location"]
    with Session(app.state.engine) as s:
        assert s.get(MemoryRecord, old).status == "superseded"


def test_supersede_from_the_record_page_keeps_history_and_shows_the_timeline(authed):
    client, app, token = authed
    old = new_record(client, token, "prefers-tabs", body="Use tabs.")
    form = client.get(f"/memory/new?supersedes={old}").text
    assert (
        "will <strong>replace</strong>" in form and 'name="supersedes"' in form and "prefers-tabs-v2" in form
    )
    r = client.post(
        "/memory/new",
        data={
            "csrf_token": token,
            "name": "prefers-spaces",
            "description": "now spaces",
            "body": "Use spaces.",
            "type": "feedback",
            "scope": "user",
            "supersedes": old,
        },
        follow_redirects=False,
    )
    new = r.headers["location"].split("/memory/")[1].split("?")[0]
    page = client.get(f"/memory/{new}").text
    assert "How this changed" in page and "prefers-tabs" in page
    oldpage = client.get(f"/memory/{old}").text
    assert (
        "has been replaced" in oldpage and "prefers-spaces" in oldpage and "Use tabs." in oldpage
    )  # history is intact
    assert (
        "prefers-tabs" not in client.get("/memory").text
        and "prefers-tabs" in client.get("/memory?status=superseded").text
    )
    assert "Replace with" not in oldpage  # can't re-supersede a superseded record


def test_still_true_restarts_the_clock_and_revives_stale_records(authed):
    client, app, token = authed
    from acm_hub.models import MemoryRecord

    rid = new_record(client, token, "maybe-old")
    client.post(f"/memory/{rid}/quick", data={"csrf_token": token, "status": "stale"})
    assert (
        "stale" in client.get(f"/memory/{rid}").text and "isn't served" in client.get(f"/memory/{rid}").text
    )
    client.post(f"/memory/{rid}/still-true", data={"csrf_token": token})
    with Session(app.state.engine) as s:
        rec = s.get(MemoryRecord, rid)
        assert rec.status == "active" and rec.last_reinforced is not None and rec.body == DUP
    assert client.post(f"/memory/{rid}/still-true", data={}).status_code == 403


def test_lifecycle_settings_are_validated_and_saved(authed):
    client, app, token = authed
    base = {
        "csrf_token": token,
        "deployment_mode": "solo",
        "core_token_budget": "2000",
        "default_token_expiry_days": "90",
        "max_token_expiry_days": "365",
        "stale_after_days_observed": "30",
        "stale_after_days_confirmed": "200",
        "review_established_days": "180",
        "auto_apply_proposals": "1",
    }
    assert client.post("/settings/instance", data=base, follow_redirects=False).status_code == 303
    from acm_hub.models import InstanceSettings

    with Session(app.state.engine) as s:
        i = s.get(InstanceSettings, 1)
        assert (
            i.stale_after_days_observed,
            i.stale_after_days_confirmed,
            i.review_established_days,
            i.auto_apply_proposals,
        ) == (30, 200, 180, True)
    assert (
        client.post("/settings/instance", data=base | {"stale_after_days_observed": "1"}).status_code == 422
    )
    assert (
        'name="auto_apply_proposals"' in client.get("/settings").text
        and "checked" in client.get("/settings").text
    )


def test_proposals_are_private_to_their_owner_in_the_ui(authed):
    client, app, token = authed
    new_record(client, token, "keys-one"), new_record(client, token, "keys-two")
    run_now(client, token)
    pid = re.search(r'action="/review/proposals/([0-9A-Z]{26})"', client.get("/review").text).group(1)
    from acm_hub.models import User
    from acm_hub.security import hash_password

    with Session(app.state.engine) as s:
        s.add(User(email="other@example.com", password_hash=hash_password(PASSWORD)))
        s.commit()
    other = TestClient(app)
    other.post("/login", data={"email": "other@example.com", "password": PASSWORD})
    t2 = csrf_of(other.get("/memory").text)
    assert "Nothing to review" in other.get("/review").text
    assert (
        other.post(f"/review/proposals/{pid}", data={"csrf_token": t2, "action": "approve"}).status_code
        == 404
    )
