import re

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from acm_hub.auth import mint_token
from acm_hub.models import ApiToken, MemoryRecord, Project, Team, TeamMember, User
from acm_hub.security import hash_password

from .conftest import PASSWORD, csrf_of


def as_user(app, email):
    c = TestClient(app)
    assert (
        c.post("/login", data={"email": email, "password": PASSWORD}, follow_redirects=False).status_code
        == 303
    )
    return c, csrf_of(c.get("/memory").text)


def make(app, email, admin=False):
    with Session(app.state.engine) as s:
        s.add(User(email=email, password_hash=hash_password(PASSWORD), is_admin=admin))
        s.commit()


@pytest.fixture()
def org(authed):
    """Admin signed in; mode multi_team; alice owns team-a, bob is a member, carol owns team-b, dave is nobody."""
    client, app, token = authed
    client.post(
        "/settings/instance",
        data={
            "csrf_token": token,
            "deployment_mode": "multi_team",
            "core_token_budget": "2000",
            "default_token_expiry_days": "90",
            "max_token_expiry_days": "365",
            "stale_after_days_observed": "90",
            "stale_after_days_confirmed": "365",
            "review_established_days": "365",
        },
    )
    for n in ("alice", "bob", "carol", "dave"):
        make(app, f"{n}@example.com")
    client.post(
        "/settings/teams",
        data={"csrf_token": token, "name": "Team A", "slug": "team-a", "owner": "alice@example.com"},
    )
    client.post(
        "/settings/teams",
        data={"csrf_token": token, "name": "Team B", "slug": "team-b", "owner": "carol@example.com"},
    )
    alice, ta = as_user(app, "alice@example.com")
    alice.post(
        "/settings/teams/team-a/members",
        data={"csrf_token": ta, "email": "bob@example.com", "role": "member"},
    )
    return client, app, token, {"alice": (alice, ta)}


def test_solo_mode_hides_teams_and_offers_only_private_and_public(authed):
    client, app, token = authed
    page = client.get("/settings/projects").text
    assert 'href="/settings/teams"' not in page and 'value="team"' not in page and 'value="public"' in page
    assert client.get("/settings/teams").status_code == 404
    r = client.post(
        "/settings/projects",
        data={"csrf_token": token, "slug": "side-project", "visibility": "team"},
        follow_redirects=False,
    )
    assert "solo" in r.headers["location"].lower() or "available" in r.headers["location"].lower()
    with Session(app.state.engine) as s:
        assert s.exec(select(Project)).all() == []  # the refused visibility created nothing
    client.post(
        "/settings/projects", data={"csrf_token": token, "slug": "side-project", "visibility": "public"}
    )
    assert "side-project" in client.get("/settings/projects").text


def test_settings_subnav_reflects_role_and_mode(org):
    client, app, token, _ = org
    admin_nav = client.get("/settings").text
    assert all(
        h in admin_nav
        for h in (
            'href="/settings/projects"',
            'href="/settings/teams"',
            'href="/settings/users"',
            'href="/plugins"',
        )
    )
    alice, _ = as_user(app, "alice@example.com")
    member_nav = alice.get("/settings").text
    assert (
        'href="/settings/teams"' in member_nav
        and 'href="/settings/users"' not in member_nav
        and 'href="/plugins"' not in member_nav
    )
    assert alice.get("/settings/users").status_code == 403 and alice.get("/plugins").status_code == 403


def test_admin_creates_team_and_cannot_manage_or_see_its_members(org):
    client, app, token, _ = org
    page = client.get("/settings/teams").text
    assert "Team A" in page and "not a member" in page
    detail = client.get("/settings/teams/team-a").text
    assert "can&#39;t see who is on it" in detail or "can't see who is on it" in detail
    assert (
        "alice@example.com" not in detail and "bob@example.com" not in detail
    )  # membership stays with the members
    r = client.post(
        "/settings/teams/team-a/members",
        data={"csrf_token": token, "email": "dave@example.com"},
        follow_redirects=False,
    )
    assert "owner%20of%20this%20team" in r.headers["location"]
    with Session(app.state.engine) as s:
        assert len(s.exec(select(TeamMember)).all()) == 3  # alice, bob, carol — unchanged


def test_owner_runs_membership_and_members_cannot(org):
    client, app, token, ctx = org
    alice, ta = ctx["alice"]
    detail = alice.get("/settings/teams/team-a").text
    assert "bob@example.com" in detail and "Add someone" in detail
    r = alice.post(
        "/settings/teams/team-a/members",
        data={"csrf_token": ta, "email": "dave@example.com", "role": "member"},
        follow_redirects=False,
    )
    assert "Added" in r.headers["location"]
    bob, tb = as_user(app, "bob@example.com")
    bob_page = bob.get("/settings/teams/team-a").text
    assert (
        "Add someone" not in bob_page and "dave@example.com" in bob_page
    )  # a member can see colleagues but not manage them
    r = bob.post(
        "/settings/teams/team-a/members",
        data={"csrf_token": tb, "email": "carol@example.com"},
        follow_redirects=False,
    )
    assert "owner%20of%20this%20team" in r.headers["location"]
    carol, tc = as_user(app, "carol@example.com")
    assert (
        carol.get("/settings/teams/team-a").status_code == 404
    )  # another team's page doesn't even exist for her
    r = carol.post(
        "/settings/teams/team-a/members",
        data={"csrf_token": tc, "email": "carol@example.com"},
        follow_redirects=False,
    )
    assert r.status_code == 404


def test_last_owner_is_protected_through_the_ui(org):
    client, app, token, ctx = org
    alice, ta = ctx["alice"]
    with Session(app.state.engine) as s:
        aid = s.exec(select(User).where(User.email == "alice@example.com")).one().id
    r = alice.post(
        f"/settings/teams/team-a/members/{aid}/role",
        data={"csrf_token": ta, "role": "member"},
        follow_redirects=False,
    )
    assert "at%20least%20one%20owner" in r.headers["location"]
    r = alice.post(
        f"/settings/teams/team-a/members/{aid}/remove", data={"csrf_token": ta}, follow_redirects=False
    )
    assert "at%20least%20one%20owner" in r.headers["location"]


def test_removing_a_member_ends_their_access_immediately(org):
    client, app, token, ctx = org
    alice, ta = ctx["alice"]
    alice.post(
        "/settings/projects", data={"csrf_token": ta, "slug": "alpha", "visibility": "team", "team": "team-a"}
    )
    alice.post(
        "/memory/new",
        data={
            "csrf_token": ta,
            "name": "alpha-fact",
            "description": "d",
            "type": "project",
            "scope": "project",
            "project": "alpha",
        },
    )
    bob, tb = as_user(app, "bob@example.com")
    assert "alpha-fact" in bob.get("/memory").text
    with Session(app.state.engine) as s:
        bid = s.exec(select(User).where(User.email == "bob@example.com")).one().id
    alice.post(f"/settings/teams/team-a/members/{bid}/remove", data={"csrf_token": ta})
    assert "alpha-fact" not in bob.get("/memory").text  # same session, next request: gone
    carol, _ = as_user(app, "carol@example.com")
    assert "alpha-fact" not in carol.get("/memory").text


def test_projects_page_enforces_owner_rights_and_delete_confirmation(org):
    client, app, token, ctx = org
    alice, ta = ctx["alice"]
    alice.post(
        "/settings/projects", data={"csrf_token": ta, "slug": "alpha", "visibility": "team", "team": "team-a"}
    )
    bob, tb = as_user(app, "bob@example.com")
    assert (
        "alpha" in bob.get("/settings/projects").text
        and "/settings/projects/alpha/delete" not in bob.get("/settings/projects").text
    )  # no controls for a member
    r = bob.post(
        "/settings/projects/alpha", data={"csrf_token": tb, "visibility": "private"}, follow_redirects=False
    )
    assert "owner" in r.headers["location"]
    r = bob.post(
        "/settings/projects",
        data={"csrf_token": tb, "slug": "bobs", "visibility": "team", "team": "team-a"},
        follow_redirects=False,
    )
    assert "owner%20of%20this%20team" in r.headers["location"]
    assert "/settings/projects/alpha/delete" in alice.get("/settings/projects").text
    r = alice.post(
        "/settings/projects/alpha/delete", data={"csrf_token": ta, "confirm": "wrong"}, follow_redirects=False
    )
    assert "type%20its%20name" in r.headers["location"]
    alice.post("/settings/projects/alpha/delete", data={"csrf_token": ta, "confirm": "alpha"})
    with Session(app.state.engine) as s:
        assert s.exec(select(Project).where(Project.slug == "alpha")).first() is None
    dave, _ = as_user(app, "dave@example.com")
    assert dave.get("/settings/projects").status_code == 200


def test_team_members_can_write_team_scope_memory_from_the_form(org):
    client, app, token, ctx = org
    bob, tb = as_user(app, "bob@example.com")
    assert 'value="team"' in bob.get("/memory/new").text
    dave, td = as_user(app, "dave@example.com")
    assert 'value="team"' not in dave.get("/memory/new").text  # no team, no team scope option
    r = bob.post(
        "/memory/new",
        data={
            "csrf_token": tb,
            "name": "team-convention",
            "description": "d",
            "type": "rule",
            "scope": "team",
            "team": "team-a",
            "confidence": "confirmed",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "team-convention" in bob.get("/memory").text and "team-convention" not in dave.get("/memory").text


def test_users_page_create_deactivate_reactivate(org):
    client, app, token, _ = org
    r = client.post(
        "/settings/users",
        data={"csrf_token": token, "email": "erin@example.com", "password": PASSWORD},
        follow_redirects=False,
    )
    assert "Created" in r.headers["location"]
    erin, te = as_user(app, "erin@example.com")
    with Session(app.state.engine) as s:
        eid = s.exec(select(User).where(User.email == "erin@example.com")).one().id
        mint_token(
            s,
            s.get(User, eid),
            label="ci",
            project_ids=[],
            access_level="read_write",
            expires_days=7,
            include_user_scope=True,
        )
        s.commit()
    r = client.post(
        f"/settings/users/{eid}/active", data={"csrf_token": token, "active": "0"}, follow_redirects=False
    )
    assert "deactivated" in r.headers["location"]
    assert erin.get("/memory", follow_redirects=False).status_code == 303  # her open session died
    fresh = TestClient(app)
    assert (
        fresh.post("/login", data={"email": "erin@example.com", "password": PASSWORD}).status_code == 401
    )  # and she can't sign in
    with Session(app.state.engine) as s:
        assert all(t.revoked_at for t in s.exec(select(ApiToken).where(ApiToken.user_id == eid)).all())
    client.post(f"/settings/users/{eid}/active", data={"csrf_token": token, "active": "1"})
    assert (
        fresh.post(
            "/login", data={"email": "erin@example.com", "password": PASSWORD}, follow_redirects=False
        ).status_code
        == 303
    )


def test_deactivated_users_tokens_are_refused_by_mcp(org):
    client, app, token, _ = org
    with Session(app.state.engine) as s:
        u = s.exec(select(User).where(User.email == "dave@example.com")).one()
        raw, tok = mint_token(
            s,
            u,
            label="x",
            project_ids=[],
            access_level="read_only",
            expires_days=7,
            include_user_scope=False,
        )
        tok.revoked_at = None
        s.commit()
        did = u.id
    h = {"Authorization": f"Bearer {raw}", "Accept": "application/json, text/event-stream"}

    def call():
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        return client.post("/mcp", headers=h, json=body).status_code

    assert call() == 200
    with Session(
        app.state.engine
    ) as s:  # deactivate directly, leaving the token un-revoked: the check must hold on its own
        u = s.get(User, did)
        u.is_active = False
        s.add(u)
        s.commit()
    assert call() == 401


def test_reset_password_shows_it_once_and_never_in_a_url(org):
    client, app, token, _ = org
    with Session(app.state.engine) as s:
        did = s.exec(select(User).where(User.email == "dave@example.com")).one().id
    dave, _ = as_user(app, "dave@example.com")
    r = client.post(f"/settings/users/{did}/reset-password", data={"csrf_token": token})
    assert r.status_code == 200 and "Temporary password for dave@example.com" in r.text
    pw = re.search(r'class="tokenbox mono spaced" id="tp">([^<]+)<', r.text).group(1)
    assert pw not in client.get("/settings/users").text  # shown exactly once
    assert dave.get("/memory", follow_redirects=False).status_code == 303  # signed out everywhere
    assert (
        TestClient(app)
        .post("/login", data={"email": "dave@example.com", "password": pw}, follow_redirects=False)
        .status_code
        == 303
    )


def test_org_actions_need_csrf_and_the_right_role(org):
    client, app, token, ctx = org
    alice, ta = ctx["alice"]
    for path, data in (
        ("/settings/projects", {"slug": "x"}),
        ("/settings/teams", {"name": "x", "slug": "x", "owner": "a@b.co"}),
        ("/settings/users", {"email": "z@example.com"}),
        ("/settings/teams/team-a/members", {"email": "dave@example.com"}),
    ):
        assert client.post(path, data=data).status_code == 403  # no CSRF token
    assert (
        alice.post(
            "/settings/users", data={"csrf_token": ta, "email": "z@example.com", "password": PASSWORD}
        ).status_code
        == 403
    )
    assert (
        alice.post(
            "/settings/teams",
            data={"csrf_token": ta, "name": "Rogue", "slug": "rogue", "owner": "alice@example.com"},
            follow_redirects=False,
        )
        .headers["location"]
        .count("Only%20an%20admin")
        == 1
    )
    with Session(app.state.engine) as s:
        assert s.exec(select(Team).where(Team.slug == "rogue")).first() is None
        assert s.exec(select(MemoryRecord)).all() == []
