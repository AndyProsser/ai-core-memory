import json
import logging
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from acm_hub.access import principal_for_user
from acm_hub.app import create_app
from acm_hub.auth import mint_token
from acm_hub.models import ApiToken, MemoryRecord, Project, User, utcnow
from acm_hub.records import RecordIn, write_record


@pytest.fixture()
def env(settings):
    root = create_app(settings)
    with TestClient(root) as client:
        app = root.fastapi
        with Session(app.state.engine) as s:
            u = User(email="andy@example.com", is_admin=True)
            s.add(u)
            s.commit()
            s.refresh(u)
        yield client, app, u.id


def token(app, user_id, **kw):
    kw = {
        "label": "t",
        "project_ids": [],
        "access_level": "read_write",
        "expires_days": 30,
        "include_user_scope": False,
    } | kw
    with Session(app.state.engine) as s:
        u = s.get(User, user_id)
        raw, tok = mint_token(s, u, **kw)
        s.commit()
        return raw, tok.id


def rpc(client, raw, method, params=None, **kw):
    headers = {"Authorization": f"Bearer {raw}", "Accept": "application/json, text/event-stream"}
    return client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        **kw,
    )


def call(client, raw, tool, **args):
    r = rpc(client, raw, "tools/call", {"name": tool, "arguments": args})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    text = res["content"][0]["text"] if res.get("content") else ""
    try:
        return res["isError"], json.loads(text)
    except ValueError:
        return res["isError"], text


def test_requires_a_valid_token(env):
    client, app, uid = env
    assert client.post("/mcp", json={}).status_code == 401
    assert rpc(client, "acm_live_" + "x" * 43, "tools/list").status_code == 401
    assert rpc(client, "not-a-token", "tools/list").status_code == 401
    raw, _ = token(app, uid)
    r = rpc(client, raw, "tools/list")
    assert r.status_code == 200
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert names == {
        "memory_focus",
        "memory_search",
        "memory_get",
        "memory_write",
        "memory_sync",
        "inbox_add",
        "inbox_resolve",
        "memory_reinforce",
        "memory_propose",
        "memory_consolidate",
    }
    # a web-session cookie is not an API credential
    assert (
        client.post("/mcp", headers={"Accept": "application/json, text/event-stream"}, json={}).status_code
        == 401
    )


def test_revoked_and_expired_tokens_stop_working_immediately(env):
    client, app, uid = env
    raw, tid = token(app, uid)
    assert rpc(client, raw, "tools/list").status_code == 200
    with Session(app.state.engine) as s:
        t = s.get(ApiToken, tid)
        t.revoked_at = utcnow()
        s.add(t)
        s.commit()
    assert rpc(client, raw, "tools/list").status_code == 401  # no cache window
    raw2, tid2 = token(app, uid)
    with Session(app.state.engine) as s:
        t = s.get(ApiToken, tid2)
        t.expires_at = utcnow() - timedelta(seconds=1)
        s.add(t)
        s.commit()
    assert rpc(client, raw2, "tools/list").status_code == 401


def test_plaintext_http_from_a_public_address_is_refused(env):
    client, app, uid = env
    raw, _ = token(app, uid)
    public = TestClient(client.app, client=("8.8.8.8", 4444))  # same app, public peer, plain http
    r = rpc(public, raw, "tools/list")
    assert r.status_code == 400 and "HTTPS" in r.text
    lan = TestClient(client.app, client=("192.168.1.20", 4444))
    assert rpc(lan, raw, "tools/list").status_code == 200  # RFC1918 is fine on a self-hosted LAN


def test_per_token_rate_limit(env, settings):
    client, app, uid = env
    app.state.token_limiter.limit = 3
    raw, _ = token(app, uid)
    codes = [rpc(client, raw, "tools/list").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3] == 429
    other, _ = token(app, uid)  # a different token has its own budget
    assert rpc(client, other, "tools/list").status_code == 200


def test_tokens_are_never_logged(env, caplog):
    client, app, uid = env
    raw, _ = token(app, uid)
    with caplog.at_level(logging.DEBUG):
        rpc(client, raw, "tools/list")
        rpc(client, raw + "x", "tools/list")
    assert raw not in caplog.text


def test_write_get_search_focus_roundtrip(env):
    client, app, uid = env
    raw, _ = token(app, uid)
    err, out = call(
        client,
        raw,
        "memory_write",
        name="idempotent-retries",
        description="Retries must be idempotent",
        type="project",
        scope="project",
        project="payments",
        body="Use idempotency keys.",
        topics=["payments"],
    )
    assert not err and out["action"] == "created" and out["confidence"] == "observed"
    err, rec = call(client, raw, "memory_get", id_or_name="idempotent-retries")
    assert rec["body"] == "Use idempotency keys." and rec["project"] == "payments"
    err, hits = call(client, raw, "memory_search", query="idempotency keys")
    assert [h["name"] for h in hits["results"]] == ["idempotent-retries"]
    err, pack = call(
        client, raw, "memory_focus", task="retry logic for the payments webhook", project="payments"
    )
    assert [a["name"] for a in pack["associated"]] == ["idempotent-retries"] and pack["associated"][0]["why"]
    with Session(app.state.engine) as s:
        assert s.exec(select(MemoryRecord)).one().last_retrieved is not None  # served -> recorded...
        assert (
            s.exec(select(MemoryRecord)).one().reinforcement_count == 0
        )  # ...but retrieval never reinforces


def test_ai_clients_cannot_promote_or_touch_established(env):
    client, app, uid = env
    raw, _ = token(app, uid, include_user_scope=True)
    with Session(app.state.engine) as s:
        write_record(
            s,
            principal_for_user(s, s.get(User, uid)),
            RecordIn(
                name="never-merge-red",
                description="d",
                body="v1",
                type="rule",
                scope="project",
                project="payments",
                confidence="established",
                tier="core",
            ),
            change_source="ui",
        )
        s.commit()
    err, out = call(
        client,
        raw,
        "memory_write",
        name="never-merge-red",
        description="d",
        body="v2",
        type="rule",
        scope="project",
        project="payments",
    )
    assert err and "CONFLICT" in out and "established" in out
    err, out = call(
        client,
        raw,
        "memory_write",
        name="n2",
        description="d",
        type="project",
        scope="project",
        project="payments",
        confidence="established",
    )
    assert err and "person" in out.lower()
    err, out = call(
        client, raw, "memory_write", name="n3", description="d", type="project", scope="team", team="x"
    )
    assert err and "team" in out.lower()
    with Session(app.state.engine) as s:
        assert s.exec(select(MemoryRecord).where(MemoryRecord.name == "never-merge-red")).one().body == "v1"


def test_read_only_and_project_scoped_tokens(env):
    client, app, uid = env
    rw, _ = token(app, uid)
    call(
        client,
        rw,
        "memory_write",
        name="a-one",
        description="d",
        type="project",
        scope="project",
        project="alpha",
        body="alpha thing",
    )
    call(
        client,
        rw,
        "memory_write",
        name="b-one",
        description="d",
        type="project",
        scope="project",
        project="beta",
        body="beta thing",
    )
    with Session(app.state.engine) as s:
        alpha = s.exec(select(Project).where(Project.slug == "alpha")).one().id
    ro, _ = token(app, uid, access_level="read_only")
    err, out = call(
        client,
        ro,
        "memory_write",
        name="c-one",
        description="d",
        type="project",
        scope="project",
        project="alpha",
    )
    assert err and "read-only" in out
    err, _ = call(client, ro, "inbox_add", title="x")
    assert err
    limited, _ = token(app, uid, project_ids=[alpha])
    err, hits = call(client, limited, "memory_search", query="thing")
    assert [h["name"] for h in hits["results"]] == ["a-one"]  # the leaked CI token can't see beta
    err, out = call(client, limited, "memory_get", id_or_name="b-one")
    assert err and "No such record" in out


def test_user_scope_needs_an_explicit_grant(env):
    client, app, uid = env
    plain, _ = token(app, uid)
    err, out = call(client, plain, "memory_write", name="terse", description="d", type="user", scope="user")
    assert err and "user-scope" in out
    granted, _ = token(app, uid, include_user_scope=True)
    err, out = call(
        client,
        granted,
        "memory_write",
        name="terse",
        description="likes terse replies",
        type="user",
        scope="user",
    )
    assert not err
    err, hits = call(client, plain, "memory_search", query="terse")
    assert hits["results"] == []  # and a token without the grant can't read it back


def test_sync_upserts_and_parks_conflicts(env):
    client, app, uid = env
    raw, _ = token(app, uid)
    doc = lambda body, conf="observed": (  # noqa: E731
        f"---\nname: retry-policy\ndescription: Retry policy\nmetadata:\n  type: project\n  scope: project\n  confidence: {conf}\n---\n\n{body}\n"
    )
    err, out = call(client, raw, "memory_sync", project="payments", records=[doc("v1")])
    assert not err and out["summary"]["create"] == 1
    err, out = call(client, raw, "memory_sync", project="payments", records=[doc("v1")])
    assert out["summary"]["unchanged"] == 1
    err, out = call(client, raw, "memory_sync", project="payments", records=[doc("v2")])
    assert out["summary"]["update"] == 1
    with Session(app.state.engine) as s:
        rec = s.exec(select(MemoryRecord)).one()
        rec.confidence = "established"
        s.add(rec)
        s.commit()
    err, out = call(client, raw, "memory_sync", project="payments", records=[doc("v3")])
    assert out["summary"]["conflict"] == 1  # parked for a human, not applied
    with Session(app.state.engine) as s:
        assert s.exec(select(MemoryRecord)).one().body == "v2"


def test_inbox_add(env):
    client, app, uid = env
    raw, _ = token(app, uid)
    err, out = call(client, raw, "inbox_add", title="Try the thing", body="notes")
    assert not err and out["status"] == "new"


# --- Phase 2: learning tools -----------------------------------------------------------------------------


def test_reinforce_via_mcp_confirms_after_two_independent_sessions(env):
    client, app, uid = env
    raw, _ = token(app, uid)
    call(
        client,
        raw,
        "memory_write",
        name="use-uv",
        description="Use uv for Python envs",
        type="feedback",
        scope="project",
        project="tools",
        body="Create environments with uv.",
        source_ref="session-0",
    )
    err, out = call(client, raw, "memory_reinforce", id_or_name="use-uv", source_ref="session-0")
    assert not err and out["counted"] is False  # the session that wrote it can't vouch for it
    err, out = call(client, raw, "memory_reinforce", id_or_name="use-uv", source_ref="session-1")
    assert out["counted"] and out["confidence"] == "observed"
    err, out = call(client, raw, "memory_reinforce", id_or_name="use-uv", source_ref="session-2")
    assert out["promoted_to_confirmed"] and out["confidence"] == "confirmed"
    err, again = call(
        client,
        raw,
        "memory_write",
        name="use-uv",
        description="Use uv for Python envs",
        type="feedback",
        scope="project",
        project="tools",
        body="Create environments with uv.",
        source_ref="session-3",
    )
    assert again["action"] == "reinforced" and again["reinforcement_count"] == 3


def test_supersede_via_mcp_keeps_history(env):
    client, app, uid = env
    raw, _ = token(app, uid)
    _, old = call(
        client,
        raw,
        "memory_write",
        name="use-pip",
        description="Use pip",
        type="feedback",
        scope="project",
        project="tools",
        body="pip install",
    )
    err, new = call(
        client,
        raw,
        "memory_write",
        name="use-uv",
        description="Use uv",
        type="feedback",
        scope="project",
        project="tools",
        body="uv sync",
        supersedes=[old["id"]],
    )
    assert not err
    err, got = call(client, raw, "memory_get", id_or_name=old["id"])
    assert got["status"] == "superseded" and [h["name"] for h in got["history"]] == ["use-uv"]
    err, hits = call(client, raw, "memory_search", query="pip", project="tools")
    assert hits["results"] == []  # default search is active-only
    err, hits = call(client, raw, "memory_search", query="pip", project="tools", status="superseded")
    assert [h["name"] for h in hits["results"]] == ["use-pip"]


def test_established_conflict_is_filed_for_the_user_not_lost(env):
    client, app, uid = env
    raw, _ = token(app, uid)
    with Session(app.state.engine) as s:
        write_record(
            s,
            principal_for_user(s, s.get(User, uid)),
            RecordIn(
                name="never-merge-red",
                description="d",
                body="v1",
                type="rule",
                scope="project",
                project="payments",
                confidence="established",
            ),
            change_source="ui",
        )
        s.commit()
    err, out = call(
        client,
        raw,
        "memory_write",
        name="never-merge-red",
        description="d",
        body="v2",
        type="rule",
        scope="project",
        project="payments",
    )
    assert err and "filed for the user" in out and "CONFLICT" in out
    err, out2 = call(
        client,
        raw,
        "memory_write",
        name="never-merge-red",
        description="d",
        body="v2",
        type="rule",
        scope="project",
        project="payments",
    )
    assert err
    from acm_hub.models import Proposal

    with Session(app.state.engine) as s:
        props = s.exec(select(Proposal)).all()
        assert (
            len(props) == 1 and props[0].kind == "conflict" and props[0].status == "pending"
        )  # the retry reused it
        assert s.exec(select(MemoryRecord)).one().body == "v1"


def test_propose_and_consolidate_via_mcp(env):
    client, app, uid = env
    raw, _ = token(app, uid)
    body = "Always send an idempotency key on webhook retries to avoid double charges."
    _, a = call(
        client,
        raw,
        "memory_write",
        name="idem-one",
        description="Webhook retries need idempotency keys",
        type="project",
        scope="project",
        project="payments",
        body=body,
    )
    _, b = call(
        client,
        raw,
        "memory_write",
        name="idem-two",
        description="Webhook retries need idempotency keys",
        type="project",
        scope="project",
        project="payments",
        body=body,
    )
    call(client, raw, "inbox_add", title="Idea: add a retry dashboard", body="grafana")
    err, wp = call(client, raw, "memory_consolidate", project="payments")
    assert (
        not err
        and len(wp["duplicate_candidates"]) == 1
        and wp["inbox"][0]["title"] == "Idea: add a retry dashboard"
    )
    assert "inbox_resolve" in wp["instructions"]
    err, prop = call(
        client,
        raw,
        "memory_propose",
        kind="merge",
        payload={"keep": a["id"], "retire": b["id"], "merged": {"body": body + " Check the dedupe table."}},
        rationale="Same fact written twice.",
    )
    assert not err and prop["status"] == "pending" and "idem-two into idem-one" in prop["summary"]
    err, same = call(
        client,
        raw,
        "memory_propose",
        kind="merge",
        payload={"keep": a["id"], "retire": b["id"]},
        rationale="again",
    )
    assert same["proposal_id"] == prop["proposal_id"]  # not duplicated
    err, wp2 = call(client, raw, "memory_consolidate")
    assert [x["id"] for x in wp2["pending_proposals"]] == [prop["proposal_id"]]
    # the AI cannot apply it: nothing changed, and there's no tool to approve
    err, got = call(client, raw, "memory_get", id_or_name="idem-two", project="payments")
    assert got["status"] == "active"
    err, out = call(
        client, raw, "memory_propose", kind="review_established", payload={"record": a["id"]}, rationale="x"
    )
    assert err
    err, out = call(
        client,
        raw,
        "memory_propose",
        kind="promote_core",
        payload={"record": a["id"]},
        rationale="make it core",
    )
    assert not err and out["status"] == "pending"  # may be *proposed*; only a person can do it


def test_inbox_resolve_only_touches_your_own_items(env):
    client, app, uid = env
    raw, _ = token(app, uid)
    _, item = call(client, raw, "inbox_add", title="mine")
    err, out = call(client, raw, "inbox_resolve", item_id=item["id"], action="harvested")
    assert not err and out["status"] == "harvested"
    err, _ = call(client, raw, "inbox_resolve", item_id=item["id"], action="deleted")
    assert err
    with Session(app.state.engine) as s:
        other = User(email="other@example.com")
        s.add(other)
        s.commit()
        from acm_hub.models import InboxItem

        theirs = InboxItem(owner_user_id=other.id, source="ui", title="theirs")
        s.add(theirs)
        s.commit()
        tid = theirs.id
    err, out = call(client, raw, "inbox_resolve", item_id=tid, action="dismissed")
    assert err and "No such inbox item" in out
