"""Regression: a browser submits a form's own hidden `csrf_token` field, not the page's meta tag.

The `csrf()` macro was once imported without template context and rendered an empty token, so every plain form POST
returned 403 in a real browser while the tests (which read the token from the meta tag) all passed.
"""

import re

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from acm_hub.models import InstanceSettings, Project, User

from .conftest import csrf_of

HIDDEN = re.compile(r'<input type="hidden" name="csrf_token" value="([^"]*)"')
FORM = re.compile(r"<form\b[^>]*method=\"post\"[^>]*>", re.I)


def pages_with_forms(client, app):
    client.post(
        "/api/v1/records",
        json={"name": "a-fact", "description": "d", "type": "feedback", "scope": "user"},
        headers={"X-CSRF-Token": client.get("/api/v1/me").json()["csrf_token"]},
    )
    with Session(app.state.engine) as s:
        owner = s.exec(select(User)).one()
        s.get(InstanceSettings, 1).deployment_mode = "multi_team"
        s.add(Project(slug="alpha", owner_user_id=owner.id, visibility="private"))
        s.commit()
    rid = client.get("/api/v1/records").json()["records"][0]["id"]
    return [
        "/settings",
        "/settings/projects",
        "/settings/users",
        "/settings/teams",
        "/review",
        "/data",
        "/focus",
        "/memory",
        "/memory/new",
        f"/memory/{rid}",
        f"/memory/{rid}?edit=1",
        "/plugins",
        "/plugins/new?plugin=webhook",
    ]


def test_every_form_carries_a_real_csrf_token(authed):
    client, app, meta_token = authed
    checked = 0
    for path in pages_with_forms(client, app):
        r = client.get(path)
        assert r.status_code == 200, path
        meta = csrf_of(r.text)
        forms = FORM.findall(r.text)
        tokens = HIDDEN.findall(r.text)
        for t in tokens:
            assert t and t == meta, f"{path}: a form's hidden csrf_token is empty or wrong ({t!r})"
        # every post form on the page (other than ones that use htmx headers or sign-in) has such a field
        assert len(tokens) >= len([f for f in forms if "hx-post" not in f]) - r.text.count(
            'action="/login"'
        ), path
        checked += len(tokens)
    assert checked > 10  # we really did look at a meaningful number of forms


def test_a_form_works_using_only_its_own_hidden_field(authed):
    """Submit the new-project form exactly as a browser would: fields and hidden token taken from the HTML."""
    client, app, _ = authed
    page = client.get("/settings/projects").text
    token = HIDDEN.search(page).group(1)
    assert token
    r = client.post(
        "/settings/projects",
        data={"csrf_token": token, "slug": "from-browser", "visibility": "private"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with Session(app.state.engine) as s:
        assert s.exec(select(Project).where(Project.slug == "from-browser")).first() is not None
    # and without the field it is refused, as a forged cross-site post would be
    fresh = client.post("/settings/projects", data={"slug": "forged", "visibility": "private"})
    assert fresh.status_code == 403


def test_a_real_looking_browser_post_with_origin_passes_csrf(authed):
    client, app, _ = authed
    token = HIDDEN.search(client.get("/settings/projects").text).group(1)
    r = client.post(
        "/settings/projects",
        data={"csrf_token": token, "slug": "with-origin", "visibility": "private"},
        headers={"Origin": "http://testserver", "Host": "testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 303  # browsers send Origin; a matching one must not be treated as cross-site
    stranger = TestClient(app)  # same token, but no session cookie: nothing may be created
    stranger.post(
        "/settings/projects",
        data={"csrf_token": token, "slug": "no-cookie", "visibility": "private"},
        follow_redirects=False,
    )
    with Session(app.state.engine) as s:
        assert s.exec(select(Project).where(Project.slug == "no-cookie")).first() is None
