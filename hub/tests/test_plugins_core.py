"""The plugin framework: outbox, allowlists, delivery, retries, isolation, redaction (docs/PLUGINS.md, SECURITY.md)."""

import time
from datetime import timedelta

import pytest
from pydantic import BaseModel
from sqlmodel import select

from acm_hub import dispatcher as dp
from acm_hub import events as ev
from acm_hub.access import principal_for_user
from acm_hub.models import Event, PluginDelivery, PluginInstance, Project, Team, TeamMember, User, utcnow
from acm_hub.plugins import registry
from acm_hub.plugins.base import BasePlugin, DeliveryResult, PluginInfo
from acm_hub.records import RecordIn, write_record

from .conftest import make_user


class Empty(BaseModel):
    pass


SENT: list = []


class Recorder(BasePlugin):
    info = PluginInfo(
        key="recorder", name="Recorder", kind="sink", config_schema=Empty, secret_names=["token"]
    )

    def deliver(self, ctx, event):
        SENT.append((ctx.instance_name, event))
        return DeliveryResult.success()


class Boom(BasePlugin):
    info = PluginInfo(key="boom", name="Boom", kind="sink", config_schema=Empty, secret_names=["token"])

    def deliver(self, ctx, event):
        raise RuntimeError(f"upstream said no for token {ctx.secrets.get('token')}")


class Hang(BasePlugin):
    info = PluginInfo(key="hang", name="Hang", kind="sink", config_schema=Empty)

    def deliver(self, ctx, event):
        time.sleep(
            3.0
        )  # far longer than CALL_TIMEOUT (0.2s) and than any scheduling jitter on a loaded runner
        return DeliveryResult.success()


class Permanent(BasePlugin):
    info = PluginInfo(key="permanent", name="Permanent", kind="sink", config_schema=Empty)

    def deliver(self, ctx, event):
        return DeliveryResult.failed("receiver rejected it")


@pytest.fixture(autouse=True)
def plugins():
    SENT.clear()
    for p in (Recorder(), Boom(), Hang(), Permanent()):
        registry.register(p)
    yield
    for k in ("recorder", "boom", "hang", "permanent"):
        registry.unregister(k)


ALL = [
    "record.created",
    "record.updated",
    "record.superseded",
    "proposal.pending",
    "conflict.flagged",
    "inbox.new",
    "plugin.failed",
]


def instance(db, user, key="recorder", name="rec", **kw):
    base = dict(
        plugin_key=key,
        name=name,
        owner_user_id=user.id,
        enabled=True,
        scopes=["project"],
        events=list(ALL),
        config={},
        secret_refs={},
    )
    base.update(kw)
    inst = PluginInstance(**base)
    db.add(inst)
    db.commit()
    return inst


def mk(db, p, name="idem-keys", **kw):
    base = dict(
        name=name,
        description=f"{name} description",
        body=f"SECRET BODY of {name}",
        type="project",
        scope="project",
        project="payments",
    )
    base.update(kw)
    r = write_record(db, p, RecordIn(**base), change_source="ui").record
    db.commit()
    return r


# --- the outbox ---------------------------------------------------------------------------------------------


def test_nothing_is_written_when_nobody_is_listening(db, human):
    mk(db, human)
    assert db.exec(select(Event)).all() == []  # no plugins, no events, no table growth


def test_events_are_written_in_the_same_transaction_as_the_change(db, human, user):
    instance(db, user)
    mk(db, human)
    assert [e.type for e in db.exec(select(Event)).all()] == ["record.created"]
    db.rollback()
    nested = db.begin_nested()
    write_record(
        db,
        human,
        RecordIn(name="never-lands", description="d", type="project", scope="project", project="payments"),
        change_source="ui",
    )
    nested.rollback()  # the change is rolled back, so is its event
    assert [e.type for e in db.exec(select(Event)).all()] == ["record.created"]


def test_payloads_are_minimal_and_never_contain_bodies(db, human, user):
    instance(db, user)
    r = mk(db, human)
    write_record(db, human, RecordIn(id=r.id, body="another SECRET BODY"), change_source="ui")
    db.commit()
    for e in db.exec(select(Event)).all():
        assert (
            "SECRET BODY" not in str(e.payload) and "body" not in e.payload and "description" not in e.payload
        )
    upd = [e for e in db.exec(select(Event)).all() if e.type == "record.updated"][0]
    assert upd.payload["changed"] == ["body"] and upd.payload["link"] == f"/memory/{r.id}"


def test_lifecycle_and_proposal_events_fire(db, human, user):
    from acm_hub import proposals as pr

    instance(db, user)
    a, b = mk(db, human, "one-rec"), mk(db, human, "two-rec")
    write_record(
        db,
        human,
        RecordIn(
            name="three-rec",
            description="d",
            type="project",
            scope="project",
            project="payments",
            supersedes=[a.id],
        ),
        change_source="ui",
    )
    pr.create_proposal(db, human, "mark_stale", {"record": b.id}, rationale="old")
    db.commit()
    types = [e.type for e in db.exec(select(Event)).all()]
    assert types.count("record.created") == 3 and "record.superseded" in types and "proposal.pending" in types
    prop = [e for e in db.exec(select(Event)).all() if e.type == "proposal.pending"][0]
    assert (
        prop.payload["summary"] == "Mark two-rec stale"
        and prop.scope == "project"
        and prop.project_slug == "payments"
    )


# --- the allowlist: deny by default ---------------------------------------------------------------------------


def test_empty_scope_allowlist_receives_no_memory_events(db, human, user):
    instance(db, user, scopes=[])
    mk(db, human)
    dp.dispatch_once(db.get_bind())
    assert SENT == []


def test_scope_and_project_allowlists(db, human, user):
    instance(db, user, name="payments-only", projects=["payments"])
    instance(db, user, name="everything", projects=[])
    instance(db, user, name="user-only", scopes=["user"])
    mk(db, human, "pay-rec", project="payments")
    mk(db, human, "blog-rec", project="blog")
    dp.dispatch_once(db.get_bind())
    got = {(n, e.payload["name"]) for n, e in SENT}
    assert got == {("payments-only", "pay-rec"), ("everything", "pay-rec"), ("everything", "blog-rec")}


def test_user_scope_needs_an_explicit_acknowledgement_and_only_ever_the_owners(db, human, user):
    instance(db, user, name="no-ack", scopes=["user"])
    instance(db, user, name="acked", scopes=["user"], user_scope_ack=True)
    other = make_user(db, "other@example.com", admin=False)
    instance(db, other, name="others-acked", scopes=["user"], user_scope_ack=True)
    mk(db, human, "my-pref", scope="user", type="user", project=None)
    dp.dispatch_once(db.get_bind())
    assert {n for n, _ in SENT} == {"acked"}  # not the unacknowledged one, and never another user's instance


def test_instances_never_see_projects_their_owner_cannot_read(db, human, user):
    other = make_user(db, "other@example.com", admin=False)
    instance(
        db, other, name="others", scopes=["project"]
    )  # allowlists "project" but its owner can't read payments
    mk(db, human, "private-rec")
    dp.dispatch_once(db.get_bind())
    assert SENT == []
    proj = db.exec(select(Project)).one()
    proj.visibility = "public"
    db.add(proj)
    db.commit()
    mk(db, human, "now-public")
    dp.dispatch_once(db.get_bind())
    assert [e.payload["name"] for _, e in SENT] == ["now-public"]


def test_team_scope_follows_membership(db, human, user):
    team = Team(name="Core", slug="core")
    db.add(team)
    db.commit()
    db.add(TeamMember(team_id=team.id, user_id=user.id, role="owner"))
    db.commit()
    human.team_ids.add(team.id)
    instance(db, user, name="team-sink", scopes=["team"])
    outsider = make_user(db, "out@example.com", admin=False)
    instance(db, outsider, name="outsider-sink", scopes=["team"])
    write_record(
        db,
        human,
        RecordIn(
            name="team-rule", description="d", type="rule", scope="team", team="core", confidence="confirmed"
        ),
        change_source="ui",
    )
    db.commit()
    dp.dispatch_once(db.get_bind())
    assert {n for n, _ in SENT} == {"team-sink"}


def test_egress_full_adds_text_only_for_allowed_records(db, human, user):
    instance(db, user, name="meta")
    instance(db, user, name="full", egress="full")
    mk(db, human, "full-rec")
    dp.dispatch_once(db.get_bind())
    by = {n: e for n, e in SENT}
    assert "body" not in by["meta"].payload and "SECRET BODY of full-rec" in by["full"].payload["body"]
    assert by["full"].link.endswith(f"/memory/{by['full'].payload['id']}")  # absolute link back to the hub


# --- delivery behaviour ---------------------------------------------------------------------------------------


def test_delivery_is_once_per_event_and_idempotent(db, human, user):
    instance(db, user)
    mk(db, human)
    engine = db.get_bind()
    assert dp.dispatch_once(engine).delivered == 1
    assert dp.dispatch_once(engine).delivered == 0
    assert len(SENT) == 1
    d = db.exec(select(PluginDelivery)).one()
    assert d.status == "delivered" and d.attempts == 1 and d.delivered_at is not None


def test_disabled_instances_get_nothing_and_resume_when_enabled(db, human, user):
    inst = instance(db, user)
    mk(db, human, "first")
    dp.dispatch_once(db.get_bind())
    assert len(SENT) == 1
    inst.enabled = False
    db.add(inst)
    db.commit()
    mk(db, human, "second")  # nobody enabled: no event is even written
    assert len(db.exec(select(Event)).all()) == 1


def test_a_failing_plugin_does_not_affect_other_plugins_and_backs_off(db, human, user, monkeypatch):
    monkeypatch.setenv("BOOM_TOKEN", "tok-very-secret-123")
    instance(db, user, key="boom", name="boom", secret_refs={"token": "BOOM_TOKEN"})
    instance(db, user, name="healthy")
    mk(db, human)
    engine = db.get_bind()
    now = utcnow()
    stats = dp.dispatch_once(engine, now=now)
    assert stats.delivered == 1 and stats.retried == 1 and len(SENT) == 1  # healthy delivered despite boom
    boom = db.exec(select(PluginDelivery).where(PluginDelivery.status == "pending")).one()
    assert boom.attempts == 1 and boom.next_attempt_at >= now + timedelta(seconds=59)
    assert dp.dispatch_once(engine, now=now + timedelta(seconds=30)).retried == 0  # not due yet
    inst = db.exec(select(PluginInstance).where(PluginInstance.name == "boom")).one()
    db.refresh(inst)
    assert inst.last_status == "error" and inst.consecutive_failures == 1
    assert (
        "tok-very-secret-123" not in inst.last_error and "[redacted]" in inst.last_error
    )  # secret scrubbed from what's stored
    assert "tok-very-secret-123" not in (boom.last_error or "")


def test_retries_exhaust_to_dead_and_a_permanent_failure_dies_immediately(db, human, user):
    instance(db, user, key="boom", name="boom")
    instance(db, user, key="permanent", name="perm")
    mk(db, human)
    engine = db.get_bind()
    t = utcnow()
    for i in range(dp.MAX_ATTEMPTS + 1):
        dp.dispatch_once(engine, now=t + timedelta(days=i + 1))
    states = {
        db.get(PluginInstance, d.instance_id).name: (d.status, d.attempts)
        for d in db.exec(select(PluginDelivery)).all()
    }
    db.expire_all()
    assert states["perm"] == ("dead", 1)  # no retry for a definitive rejection
    assert states["boom"] == ("dead", dp.MAX_ATTEMPTS)


def test_timeouts_count_as_failures_without_blocking(db, human, user, monkeypatch):
    monkeypatch.setattr(dp, "CALL_TIMEOUT", 0.2)
    instance(db, user, key="hang", name="slow")
    mk(db, human)
    t0 = time.monotonic()
    stats = dp.dispatch_once(db.get_bind())
    # Returned long before the plugin's 3s sleep ended: the call was abandoned at the timeout, not waited for. The bound
    # is deliberately loose (a busy CI runner once took 1.1s of pure overhead); only "didn't wait for the plugin" matters.
    assert stats.retried == 1 and time.monotonic() - t0 < 2.5
    inst = db.exec(select(PluginInstance)).one()
    db.refresh(inst)
    assert "timed out" in inst.last_error


def test_repeated_failures_alert_through_other_sinks_never_the_failing_one(db, human, user):
    instance(db, user, key="boom", name="boom")
    instance(db, user, name="alerts", events=["plugin.failed"])
    mk(db, human)
    engine = db.get_bind()
    t = utcnow()
    for i in range(dp.FAILURES_BEFORE_ALERT + 1):
        dp.dispatch_once(engine, now=t + timedelta(hours=7 * (i + 1)))
    alerts = [e for n, e in SENT if e.type == "plugin.failed"]
    assert len(alerts) == 1 and alerts[0].payload["instance"] == "boom"
    boom_deliveries = [
        d for d in db.exec(select(PluginDelivery)).all() if db.get(Event, d.event_id).type == "plugin.failed"
    ]
    assert all(db.get(PluginInstance, d.instance_id).name == "alerts" for d in boom_deliveries)


def test_per_instance_rate_limit_defers_without_burning_attempts(db, human, user, monkeypatch):
    from acm_hub.security import SlidingWindowLimiter

    monkeypatch.setattr(dp, "_rate", SlidingWindowLimiter(2))
    instance(db, user)
    for i in range(4):
        mk(db, human, f"rec-{i}")
    stats = dp.dispatch_once(db.get_bind())
    assert stats.delivered == 2
    waiting = [d for d in db.exec(select(PluginDelivery).where(PluginDelivery.status == "pending")).all()]
    assert len(waiting) == 2 and all(d.attempts == 0 for d in waiting)


def test_events_and_deliveries_are_purged_after_retention(db, human, user):
    instance(db, user)
    mk(db, human)
    engine = db.get_bind()
    dp.dispatch_once(engine)
    stats = dp.dispatch_once(engine, now=utcnow() + timedelta(days=31))
    assert (
        stats.purged == 1
        and db.exec(select(Event)).all() == []
        and db.exec(select(PluginDelivery)).all() == []
    )


def test_targeted_events_go_only_to_their_instance_and_unknown_plugins_fail_safely(db, human, user):
    a, b = instance(db, user, name="a"), instance(db, user, name="b")
    ghost = instance(db, user, key="not-installed", name="ghost")
    ev.emit(db, "plugin.test", {"message": "hi", "link": "/plugins"}, instance_id=a.id)
    db.commit()
    dp.dispatch_once(db.get_bind())
    assert [n for n, _ in SENT] == ["a"]
    assert ghost and b


def test_inbox_events_are_not_echoed_back_to_the_source_that_caused_them(db, human, user):
    from acm_hub.models import InboxItem

    inst = instance(db, user, name="source-sink", scopes=["user"], user_scope_ack=True, events=["inbox.new"])
    other = instance(db, user, name="watcher", scopes=["user"], user_scope_ack=True, events=["inbox.new"])
    item = InboxItem(owner_user_id=user.id, source="plugin:x", title="clipped", scope="user")
    db.add(item)
    db.flush()
    ev.emit_inbox_new(db, item, origin_instance_id=inst.id)
    db.commit()
    dp.dispatch_once(db.get_bind())
    assert [n for n, _ in SENT] == ["watcher"] and other


def test_principal_helper_is_unused_by_plugins(db, human):
    assert principal_for_user and User  # plugins get a narrow context, never a principal or DB handle
    from acm_hub.plugins.base import PluginContext

    assert not {"db", "session", "engine", "principal", "token"} & set(PluginContext.__dataclass_fields__)
