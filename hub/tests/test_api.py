import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from acm_hub.auth import mint_token
from acm_hub.models import MemoryRevision, User

from .conftest import PASSWORD

REC = {
    "name": "retry-policy",
    "description": "Retries are idempotent",
    "body": "Use keys.",
    "type": "feedback",
    "scope": "user",
}


@pytest.fixture()
def api(authed):
    client, app, _ = authed
    csrf = client.get("/api/v1/me").json()["csrf_token"]
    return client, app, {"X-CSRF-Token": csrf}


def test_requires_a_signed_in_session(hub):
    client, app = hub
    r = client.get("/api/v1/me")
    assert r.status_code == 401 and r.json() == {
        "error": "Not signed in."
    }  # JSON, never a redirect or HTML page
    assert client.get("/api/v1/records").status_code == 401


def test_tokens_are_refused_even_valid_ones(api):
    client, app, _ = api
    with Session(app.state.engine) as s:
        user = s.exec(select(User)).one()
        raw, _tok = mint_token(
            s,
            user,
            label="x",
            project_ids=[],
            access_level="read_write",
            expires_days=7,
            include_user_scope=True,
        )
        s.commit()
    fresh = TestClient(app)  # no cookie: only the token
    for path in ("/api/v1/me", "/api/v1/records"):
        r = fresh.get(path, headers={"Authorization": f"Bearer {raw}"})
        assert r.status_code == 401 and "not API tokens" in r.json()["error"]
    # ...and a token alongside a perfectly good session cookie is still refused, not silently ignored
    assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {raw}"}).status_code == 401
    r = fresh.post("/api/v1/records", json=REC, headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 401


def test_writes_need_the_csrf_header(api):
    client, app, csrf = api
    assert client.post("/api/v1/records", json=REC).status_code == 403  # cookie alone isn't enough
    assert client.post("/api/v1/records", json=REC, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    cross = client.post("/api/v1/records", json=REC, headers=csrf | {"Origin": "https://evil.example"})
    assert cross.status_code == 403  # right token, wrong origin
    assert client.get("/api/v1/records").json() == {"records": []}  # none of those wrote anything
    assert client.post("/api/v1/records", json=REC, headers=csrf).status_code == 200


def test_create_read_update_history_roundtrip(api):
    client, app, csrf = api
    made = client.post("/api/v1/records", json=REC, headers=csrf).json()
    rid = made["record"]["id"]
    assert made["action"] == "created" and made["record"]["confidence"] == "observed"
    assert client.get(f"/api/v1/records/{rid}").json()["body"] == "Use keys."
    assert client.get("/api/v1/records/retry-policy").json()["id"] == rid  # by name too
    assert "body" not in client.get("/api/v1/records").json()["records"][0]  # listings stay light
    upd = client.patch(f"/api/v1/records/{rid}", json={"body": "Use keys and a dedupe table."}, headers=csrf)
    assert upd.json()["action"] == "updated"
    revs = client.get(f"/api/v1/records/{rid}/history").json()["revisions"]
    assert len(revs) == 2 and {r["source"] for r in revs} == {
        "api"
    }  # attributed to the API, not passed off as the UI
    assert client.get("/api/v1/records", params={"q": "dedupe"}).json()["records"][0]["id"] == rid


def test_errors_are_json_with_the_right_status(api):
    client, app, csrf = api
    assert client.get("/api/v1/records/01ARZ3NDEKTSV4RRFFQ69G5FAV").status_code == 404
    bad = client.post("/api/v1/records", json=REC | {"type": "nonsense"}, headers=csrf)
    assert bad.status_code == 422 and "error" in bad.json()
    assert (
        client.get("/api/v1/records/01ARZ3NDEKTSV4RRFFQ69G5FAV")
        .headers["content-type"]
        .startswith("application/json")
    )


def test_established_changes_need_explicit_confirmation(api):
    client, app, csrf = api
    made = client.post(
        "/api/v1/records", json=REC | {"type": "rule", "confidence": "established"}, headers=csrf
    )
    rid = made.json()["record"]["id"]
    r = client.patch(f"/api/v1/records/{rid}", json={"body": "changed"}, headers=csrf)
    assert r.status_code == 409 and r.json()["needs_confirmation"] is True
    assert client.get(f"/api/v1/records/{rid}").json()["body"] == "Use keys."  # untouched
    ok = client.patch(
        f"/api/v1/records/{rid}?confirm_established=true", json={"body": "changed"}, headers=csrf
    )
    assert ok.status_code == 200 and ok.json()["record"]["body"] == "changed"


def test_one_users_records_are_invisible_to_another(authed):
    client, app, token = authed
    client.post(
        "/api/v1/records", json=REC, headers={"X-CSRF-Token": client.get("/api/v1/me").json()["csrf_token"]}
    )
    with Session(app.state.engine) as s:
        from acm_hub.security import hash_password

        s.add(User(email="eve@example.com", password_hash=hash_password(PASSWORD)))
        s.commit()
    eve = TestClient(app)
    eve.post("/login", data={"email": "eve@example.com", "password": PASSWORD})
    assert eve.get("/api/v1/records").json() == {"records": []}
    rid = client.get("/api/v1/records").json()["records"][0]["id"]
    assert eve.get(f"/api/v1/records/{rid}").status_code in (403, 404)
    assert eve.get(f"/api/v1/records/{rid}/history").status_code in (403, 404)
    csrf = eve.get("/api/v1/me").json()["csrf_token"]
    assert eve.patch(
        f"/api/v1/records/{rid}", json={"body": "hijack"}, headers={"X-CSRF-Token": csrf}
    ).status_code in (403, 404)
    with Session(app.state.engine) as s:
        assert len(s.exec(select(MemoryRevision)).all()) == 1


def test_focus_and_projects_and_teams(api):
    client, app, csrf = api
    client.post("/api/v1/records", json=REC | {"tier": "core", "confidence": "confirmed"}, headers=csrf)
    pack = client.get("/api/v1/focus", params={"task": "retry webhooks"}).json()
    assert [i["name"] for i in pack["core"]] == ["retry-policy"]
    assert client.get("/api/v1/projects").status_code == 200 and client.get("/api/v1/teams").json() == {
        "teams": []
    }


def test_deactivated_users_session_is_dead_on_the_api(api):
    client, app, _ = api
    with Session(app.state.engine) as s:
        u = s.exec(select(User)).one()
        u.is_active = False
        s.add(u)
        s.commit()
    assert client.get("/api/v1/me").status_code == 401
