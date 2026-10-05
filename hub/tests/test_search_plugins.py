import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from acm_hub import dispatcher, search_sync
from acm_hub.access import principal_for_user
from acm_hub.auth import mint_token
from acm_hub.focus import build_focus
from acm_hub.models import PluginInstance, Project, SearchIndexEntry, User, utcnow
from acm_hub.plugins import egress, registry, remote
from acm_hub.plugins import remote_sdk as sdk
from acm_hub.records import RecordIn, write_record
from acm_hub.security import hash_password

from .conftest import PASSWORD, csrf_of
from .remote_support import (  # noqa: F401
    SECRET,
    URL,
    Down,
    add_instance,
    admin_id,
    asgi_transport,
    raw_service,
)

SEARCH_MANIFEST = {
    "key": "feed",
    "name": "Embeddings",
    "description": "semantic",
    "kind": "search",
    "config": [],
}


class Svc:
    """A fake search service: stores what it is sent, answers `search` with whatever the test says."""

    def __init__(self):
        self.docs: dict[str, dict[str, dict]] = {}
        self.calls: list[tuple[str, int]] = []
        self.canned: list[dict] | None = None
        self.queries: list[str] = []

    def app(self):
        def index(instance, records):
            self.calls.append(("index", len(records)))
            self.docs.setdefault(instance["id"], {}).update({r["id"]: r for r in records})
            return {"ok": True}

        def remove(instance, ids):
            self.calls.append(("remove", len(ids)))
            for i in ids:
                self.docs.get(instance["id"], {}).pop(i, None)
            return {"ok": True}

        def reset(instance):
            self.calls.append(("reset", 0))
            self.docs[instance["id"]] = {}
            return {"ok": True}

        def search(instance, query, limit):
            self.queries.append(query)
            if self.canned is not None:
                return {"results": self.canned}
            words = set(query.lower().split())
            hits = [
                {
                    "id": rid,
                    "score": float(len(words & set((d["name"] + " " + d["description"]).lower().split()))),
                }
                for rid, d in self.docs.get(instance["id"], {}).items()
            ]
            return {"results": sorted((h for h in hits if h["score"] > 0), key=lambda h: -h["score"])[:limit]}

        return sdk.create_app(
            secret=SECRET, manifest=SEARCH_MANIFEST, index=index, remove=remove, reset=reset, search=search
        )


def make_owner_records(app, names_by_project, *, scope="project"):
    """Write one record per (project, name). Returns {name: record id}."""
    ids = {}
    with Session(app.state.engine) as s:
        owner = admin_user(s)
        for slug in names_by_project:
            if not s.exec(select(Project).where(Project.slug == slug)).first():
                s.add(Project(slug=slug, owner_user_id=owner.id, visibility="private"))
        s.commit()
        p = principal_for_user(s, owner)
        for slug, names in names_by_project.items():
            for name in names:
                r = write_record(
                    s,
                    p,
                    RecordIn(
                        name=name, description=f"about {name}", body=f"BODY-{name}", type="project",
                        scope="project", project=slug, topics=["t1"],
                    ),
                    change_source="ui",
                ).record  # fmt: skip
                ids[name] = r.id
        s.commit()
    return ids


def admin_user(s):  # noqa: ANN001, ANN201
    return s.exec(select(User).where(User.email == "admin@example.com")).one()


def search_inst(app, **kw):
    base = dict(scopes=["project"], egress="metadata", config={}, events=[], pull_interval_minutes=15)
    return add_instance(app, **(base | kw))


def sync(app, iid, **kw):
    return search_sync.run_sync(app.state.engine, iid, **kw)


# --- the plugin kind -------------------------------------------------------------------------------------------------------------


def test_a_search_service_registers_and_gets_a_form_without_events(start):
    svc = Svc()
    client, app = start(svc.app())
    assert registry.get("feed").info.kind == "search"
    page = client.get("/plugins/new?plugin=feed").text
    assert (
        "What it may see" in page and "Sync every" in page and 'name="events"' not in page
    )  # nothing to subscribe to
    assert "adds semantic search" in client.get("/plugins").text


def test_search_instances_never_receive_events(start):
    client, app = start(Svc().app())
    iid = search_inst(app, events=["record.created", "proposal.pending"])
    from acm_hub.models import Event

    with Session(app.state.engine) as s:
        s.add(Event(type="proposal.pending", payload={"link": "/review"}, owner_user_id=admin_id(app)))
        s.commit()
        assert dispatcher.fan_out(s) == 0  # a search plugin is not a sink
    assert iid


# --- what gets sent ----------------------------------------------------------------------------------------------------------------


def test_only_allowed_records_are_sent_and_never_bodies_at_metadata_egress(start):
    svc = Svc()
    client, app = start(svc.app())
    make_owner_records(app, {"alpha": ["a-one", "a-two"], "beta": ["b-one"]})
    iid = search_inst(app, projects=["alpha"])
    res = sync(app, iid)
    assert res.error is None and res.indexed == 2
    docs = svc.docs[iid]
    assert {d["name"] for d in docs.values()} == {"a-one", "a-two"}  # beta isn't on the allowlist: never sent
    assert all("body" not in d for d in docs.values()) and "BODY-" not in str(
        docs
    )  # names, descriptions, topics only
    assert all(d["topics"] == ["t1"] for d in docs.values())


def test_full_egress_sends_bodies_and_is_an_explicit_choice(start):
    svc = Svc()
    client, app = start(svc.app())
    make_owner_records(app, {"alpha": ["a-one"]})
    iid = search_inst(app, egress="full")
    sync(app, iid)
    assert [d["body"] for d in svc.docs[iid].values()] == ["BODY-a-one"]


def test_personal_memory_needs_the_acknowledgement_and_is_only_the_owners(start):
    svc = Svc()
    client, app = start(svc.app())
    with Session(app.state.engine) as s:
        admin = admin_user(s)
        eve = User(email="eve@example.com", password_hash=hash_password(PASSWORD))
        s.add(eve)
        s.commit()
        for u, name in ((admin, "mine-private"), (eve, "eves-private")):
            write_record(
                s,
                principal_for_user(s, u),
                RecordIn(name=name, description="d", body="x", type="user", scope="user"),
                change_source="ui",
            )
        s.commit()
    iid = search_inst(app, scopes=["user"], user_scope_ack=False)
    assert sync(app, iid).indexed == 0 and not svc.docs.get(iid)  # no acknowledgement, nothing leaves
    with Session(app.state.engine) as s:
        i = s.get(PluginInstance, iid)
        i.user_scope_ack = True
        s.add(i)
        s.commit()
    sync(app, iid)
    assert {d["name"] for d in svc.docs[iid].values()} == {
        "mine-private"
    }  # and never someone else's personal memory


def test_syncs_are_incremental_and_removals_are_exact(start):
    svc = Svc()
    client, app = start(svc.app())
    ids = make_owner_records(app, {"alpha": ["a-one", "a-two", "a-three"]})
    iid = search_inst(app)
    assert sync(app, iid).indexed == 3
    svc.calls.clear()
    assert sync(app, iid).indexed == 0 and svc.calls == []  # nothing changed: nothing sent
    with Session(app.state.engine) as s:
        p = principal_for_user(s, admin_user(s))
        write_record(s, p, RecordIn(id=ids["a-one"], description="rewritten"), change_source="ui")  # edited
        write_record(s, p, RecordIn(id=ids["a-two"], status="archived"), change_source="ui")  # archived
        s.commit()
    res = sync(app, iid)
    assert (res.indexed, res.removed) == (1, 1)
    assert (
        set(svc.docs[iid]) == {ids["a-one"], ids["a-three"]}
        and svc.docs[iid][ids["a-one"]]["description"] == "rewritten"
    )
    with Session(app.state.engine) as s:  # narrowing the allowlist removes what is no longer allowed
        i = s.get(PluginInstance, iid)
        i.scopes = []
        s.add(i)
        s.commit()
    assert sync(app, iid).removed == 2 and svc.docs[iid] == {}


def test_rebuild_resets_the_service_and_resends_everything(start):
    svc = Svc()
    client, app = start(svc.app())
    make_owner_records(app, {"alpha": ["a-one", "a-two"]})
    iid = search_inst(app)
    sync(app, iid)
    svc.calls.clear()
    res = sync(app, iid, rebuild=True)
    assert res.indexed == 2 and svc.calls[0] == ("reset", 0)
    with Session(app.state.engine) as s:
        assert search_sync.indexed_count(s, iid) == 2


def test_a_long_first_index_continues_across_runs(start):
    svc = Svc()
    client, app = start(svc.app())
    make_owner_records(app, {"alpha": [f"rec-{i}" for i in range(7)]})
    iid = search_inst(app)
    first = sync(app, iid, budget_seconds=-1.0)  # out of time before the first batch
    assert first.indexed == 0 and first.remaining == 7 and iid in search_sync.MORE_TO_DO
    assert iid in search_sync.sync_due(app.state.engine, utcnow())  # due again at the very next tick
    assert sync(app, iid).indexed == 7 and iid not in search_sync.MORE_TO_DO


def test_a_deactivated_owner_is_not_indexed(start):
    client, app = start(Svc().app())
    iid = search_inst(app)
    with Session(app.state.engine) as s:
        u = admin_user(s)
        u.is_active = False
        s.add(u)
        s.commit()
    assert "no longer active" in sync(app, iid).error


# --- query time: fusion and, above all, access ------------------------------------------------------------------------------


def focus(app, task, *, project=None, user_email="admin@example.com", **kw):
    with Session(app.state.engine) as s:
        u = s.exec(select(User).where(User.email == user_email)).one()
        return build_focus(s, principal_for_user(s, u), task, project=project, use_semantic=True, **kw)


def test_a_semantic_only_hit_joins_the_pack_with_its_reason(start):
    svc = Svc()
    client, app = start(svc.app())
    ids = make_owner_records(app, {"alpha": ["billing-retries", "unrelated"]})
    iid = search_inst(app)
    sync(app, iid)
    svc.canned = [{"id": ids["billing-retries"], "score": 0.9}]  # no word overlap with the task at all
    pack = focus(app, "what happens when a customer card keeps failing", project="alpha")
    hit = {i.record.name: i for i in pack.associated}
    assert "billing-retries" in hit and any(
        "semantic match (my feed)" in w for w in hit["billing-retries"].why
    )
    assert "unrelated" not in hit


@pytest.mark.parametrize(
    "hostile", ["other-users-private", "archived-one", "core-one", "other-project", "ghost"]
)
def test_a_service_cannot_surface_what_the_caller_may_not_see(start, hostile):
    svc = Svc()
    client, app = start(svc.app())
    ids = make_owner_records(app, {"alpha": ["visible", "archived-one"], "beta": ["other-project"]})
    with Session(app.state.engine) as s:
        admin = admin_user(s)
        eve = User(email="eve@example.com", password_hash=hash_password(PASSWORD))
        s.add(eve)
        s.commit()
        theirs = write_record(
            s,
            principal_for_user(s, eve),
            RecordIn(name="other-users-private", description="d", body="x", type="user", scope="user"),
            change_source="ui",
        ).record
        core = write_record(
            s,
            principal_for_user(s, admin),
            RecordIn(
                name="core-one",
                description="d",
                body="x",
                type="rule",
                scope="user",
                tier="core",
                confidence="confirmed",
            ),
            change_source="ui",
        ).record
        write_record(
            s,
            principal_for_user(s, admin),
            RecordIn(id=ids["archived-one"], status="archived"),
            change_source="ui",
        )
        s.commit()
        ids |= {"other-users-private": theirs.id, "core-one": core.id, "ghost": "01ARZ3NDEKTSV4RRFFQ69G5FAV"}
    iid = search_inst(app)
    svc.canned = [{"id": ids[hostile], "score": 99.0}]
    pack = focus(app, "zzz nothing in common", project="alpha")
    names = {i.record.name for i in pack.associated}
    assert hostile not in names  # the id was offered; it still isn't in the pack
    assert not (
        hostile == "core-one" and any(i.record.name == "core-one" and i.why != ["core"] for i in pack.core)
    )
    assert iid


def test_an_instance_only_ever_answers_its_owner(start):
    svc = Svc()
    client, app = start(svc.app())
    ids = make_owner_records(app, {"alpha": ["shared-project-fact"]})
    with Session(app.state.engine) as s:
        eve = User(email="eve@example.com", password_hash=hash_password(PASSWORD))
        s.add(eve)
        s.commit()
        proj = s.exec(select(Project).where(Project.slug == "alpha")).one()
        proj.visibility = "public"  # eve can read this project...
        s.add(proj)
        s.commit()
    search_inst(app)  # ...but the index instance belongs to the admin
    svc.canned = [{"id": ids["shared-project-fact"], "score": 5}]
    svc.queries.clear()
    pack = focus(app, "anything", user_email="eve@example.com")
    assert svc.queries == []  # eve's focus never touched the admin's index
    assert not any("semantic" in w for i in pack.associated for w in i.why)
    focus(app, "anything", user_email="admin@example.com")
    assert svc.queries == ["anything"]  # while the owner's does


def test_semantic_search_respects_the_projects_partition(start):
    svc = Svc()
    client, app = start(svc.app())
    ids = make_owner_records(app, {"alpha": ["a-fact"], "beta": ["b-fact"]})
    search_inst(app)
    svc.canned = [{"id": ids["b-fact"], "score": 5}]
    assert "b-fact" not in {
        i.record.name for i in focus(app, "zzz", project="alpha").associated
    }  # cross-talk rule holds
    assert "b-fact" in {
        i.record.name for i in focus(app, "zzz", project="alpha", include_other_projects=True).associated
    }


# --- it must never be able to hurt retrieval ----------------------------------------------------------------------------------


def fts_baseline(app):
    return {i.record.name for i in focus(app, "retries", project="alpha").associated}


@pytest.mark.parametrize(
    "transport",
    [
        Down(),
        raw_service(signed=False),
        raw_service(body=b'{"results":[{"id":"x","score":-3}]}'),  # negative score: invalid
        raw_service(body=b'{"results":"nope"}'),
        raw_service(status=500),
    ],
    ids=["down", "unsigned", "bad-score", "bad-shape", "server-error"],
)
def test_a_failing_service_costs_a_note_and_nothing_else(start, transport):
    svc = Svc()
    client, app = start(svc.app())
    make_owner_records(app, {"alpha": ["retries-policy"]})
    search_inst(app)
    before = fts_baseline(app)
    egress.set_transport(
        transport if isinstance(transport, httpx.BaseTransport) else asgi_transport(transport)
    )
    pack = focus(app, "retries", project="alpha")
    assert (
        {i.record.name for i in pack.associated} == before == {"retries-policy"}
    )  # plain FTS retrieval unchanged
    assert any("Semantic search (my feed)" in n for n in pack.notes)


def test_results_are_capped_at_fifty(start):
    svc = Svc()
    client, app = start(svc.app())
    make_owner_records(app, {"alpha": ["r"]})
    search_inst(app)
    svc.canned = [{"id": f"id{i}", "score": 1.0} for i in range(120)]
    pack = focus(app, "zzz", project="alpha")
    assert any(
        "Semantic search" in n for n in pack.notes
    )  # an oversized answer is rejected whole, not truncated


def test_a_service_that_is_down_is_noted_once_known_to_be_a_search_service(start):
    svc = Svc()
    client, app = start(svc.app())
    search_inst(app)
    egress.set_transport(Down())
    remote.STATE["feed"].plugin = None
    remote.STATE["feed"].error = "couldn't reach the service"
    registry.unregister("feed")
    pack = focus(app, "anything")
    assert any("is unavailable" in n for n in pack.notes)


# --- over MCP, as an assistant would use it -----------------------------------------------------------------------------------------


def test_memory_focus_over_mcp_uses_the_callers_own_index(start):
    svc = Svc()
    client, app = start(svc.app())
    ids = make_owner_records(app, {"alpha": ["billing-retries"]})
    search_inst(app)
    svc.canned = [{"id": ids["billing-retries"], "score": 3}]
    with Session(app.state.engine) as s:
        raw, _ = mint_token(
            s,
            admin_user(s),
            label="t",
            project_ids=[],
            access_level="read_only",
            expires_days=1,
            include_user_scope=False,
        )
        s.commit()
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {raw}", "Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "memory_focus",
                "arguments": {"task": "card keeps failing", "project": "alpha"},
            },
        },
    )
    assert r.status_code == 200 and "semantic match" in r.text and "billing-retries" in r.text


# --- admin surface ---------------------------------------------------------------------------------------------------------------------


def test_rebuild_and_delete_from_the_ui(start):
    svc = Svc()
    client, app = start(svc.app())
    make_owner_records(app, {"alpha": ["a-one", "a-two"]})
    page = client.get("/plugins/new?plugin=feed")
    data = {
        "csrf_token": csrf_of(page.text),
        "plugin": "feed",
        "name": "vectors",
        "enabled": "on",
        "scopes": "project",
        "egress": "metadata",
        "pull_interval_minutes": "15",
    }
    r = client.post("/plugins", data=data, follow_redirects=False)
    assert r.status_code == 303
    with Session(app.state.engine) as s:
        inst = s.exec(select(PluginInstance).where(PluginInstance.name == "vectors")).one()
        assert inst.events == [] and inst.pull_interval_minutes == 15
        iid = inst.id
    detail = client.get(f"/plugins/{iid}")
    assert "Rebuild index" in detail.text and "0 record(s) indexed" in detail.text
    token = csrf_of(detail.text)
    assert client.post(f"/plugins/{iid}/rebuild").status_code == 403  # CSRF
    ok = client.post(f"/plugins/{iid}/rebuild", data={"csrf_token": token}, follow_redirects=False)
    assert ok.status_code == 303 and len(svc.docs[iid]) == 2
    assert "2 record(s) indexed" in client.get(f"/plugins/{iid}").text
    client.post(f"/plugins/{iid}/delete", data={"csrf_token": token})
    assert svc.docs[iid] == {}  # the service was asked to forget it
    with Session(app.state.engine) as s:
        assert s.exec(select(SearchIndexEntry)).all() == []  # and so did the hub's bookkeeping


def test_only_an_admin_can_rebuild(start):
    svc = Svc()
    client, app = start(svc.app())
    iid = search_inst(app)
    with Session(app.state.engine) as s:
        s.add(User(email="eve@example.com", password_hash=hash_password(PASSWORD)))
        s.commit()
    eve = TestClient(app)
    eve.post("/login", data={"email": "eve@example.com", "password": PASSWORD})
    assert (
        eve.post(f"/plugins/{iid}/rebuild", data={"csrf_token": csrf_of(eve.get("/memory").text)}).status_code
        == 403
    )
    assert svc.calls == []


def test_the_scheduler_runs_syncs(start):
    svc = Svc()
    client, app = start(svc.app())
    make_owner_records(app, {"alpha": ["a-one"]})
    iid = search_inst(app)
    out = dispatcher.run_scheduled(app.state.engine)
    assert len(out["syncs"]) == 1 and out["syncs"][0].indexed == 1
    assert (
        dispatcher.run_scheduled(app.state.engine)["syncs"] == []
    )  # not due again until its interval passes
    assert svc.docs[iid]
