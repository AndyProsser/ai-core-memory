from sqlmodel import select
import pytest

from acm_hub.access import AccessError, NotFound
from acm_hub.focus import build_focus
from acm_hub.models import MemoryRevision
from acm_hub.records import Conflict, RecordIn, ValidationFailed, get_record, list_records, write_record

from .conftest import make_user, token_principal


def w(db, p, **kw):
    kw.setdefault("source", "test")
    return write_record(db, p, RecordIn(**kw), change_source=kw.pop("cs", "ui") if False else "ui")


def rec(**kw):
    base = dict(
        name="idempotent-retries",
        description="Retries must be idempotent.",
        body="Use idempotency keys.",
        type="project",
        scope="project",
        project="payments",
    )
    base.update(kw)
    return RecordIn(**base)


def test_create_update_revision_and_fts(db, human):
    r = write_record(db, human, rec(), change_source="ui")
    assert r.action == "created" and r.record.confidence == "observed"
    r2 = write_record(db, human, rec(body="Use idempotency keys and a dedupe table."), change_source="ui")
    assert r2.action == "updated"
    assert (
        write_record(
            db, human, rec(body="Use idempotency keys and a dedupe table."), change_source="ui"
        ).action
        == "unchanged"
    )
    assert len(db.exec(select(MemoryRevision)).all()) == 2
    assert [x.name for x in list_records(db, human, q="dedupe")] == ["idempotent-retries"]


def test_rule_cannot_be_observed(db, human):
    with pytest.raises(ValidationFailed):
        write_record(
            db, human, rec(name="no-red-merges", type="rule", confidence="observed"), change_source="ui"
        )


def test_established_blocks_tokens_and_needs_human_confirmation(db, human, user):
    r = write_record(db, human, rec(confidence="established"), change_source="ui").record
    tok = token_principal(user)
    with pytest.raises(Conflict) as e:
        write_record(db, tok, rec(body="changed"), change_source="mcp-write")
    assert not e.value.needs_confirmation
    with pytest.raises(Conflict) as e:
        write_record(db, human, rec(body="changed"), change_source="ui")
    assert e.value.needs_confirmation
    out = write_record(db, human, rec(body="changed"), change_source="ui", confirm_established=True)
    assert out.record.body == "changed"
    assert db.get(MemoryRevision, out.revision_id).flagged


def test_confirmed_update_is_flagged(db, human, user):
    write_record(db, human, rec(confidence="confirmed"), change_source="ui")
    out = write_record(db, token_principal(user), rec(body="new body"), change_source="mcp-write")
    assert out.notices and db.get(MemoryRevision, out.revision_id).flagged


def test_token_cannot_promote(db, human, user):
    tok = token_principal(user)
    with pytest.raises(AccessError):
        write_record(db, tok, rec(tier="core"), change_source="mcp-write")
    with pytest.raises(AccessError):
        write_record(db, tok, rec(confidence="established"), change_source="mcp-write")
    with pytest.raises(AccessError):
        write_record(db, tok, rec(scope="team", team="x"), change_source="mcp-write")
    with pytest.raises(AccessError):  # user scope needs an explicit grant on the token
        write_record(
            db,
            tok,
            RecordIn(name="terse", description="d", type="user", scope="user"),
            change_source="mcp-write",
        )
    ok = token_principal(user, token_include_user_scope=True)
    write_record(
        db, ok, RecordIn(name="terse", description="d", type="user", scope="user"), change_source="mcp-write"
    )


def test_read_only_token_cannot_write(db, user):
    with pytest.raises(AccessError):
        write_record(db, token_principal(user, read_only=True), rec(), change_source="mcp-write")


def test_user_scope_is_private_even_from_admin(db, human):
    write_record(
        db,
        human,
        RecordIn(name="terse", description="likes terse", type="user", scope="user"),
        change_source="ui",
    )
    other = make_user(db, "boss@example.com", admin=True)
    from acm_hub.access import principal_for_user

    op = principal_for_user(db, other)
    assert list_records(db, op) == []
    with pytest.raises(NotFound):
        get_record(db, op, "terse")


def test_project_visibility(db, human):
    from acm_hub.access import principal_for_user
    from acm_hub.models import Project
    from sqlmodel import select

    write_record(db, human, rec(), change_source="ui")
    other = principal_for_user(db, make_user(db, "b@example.com", admin=False))
    assert list_records(db, other) == []  # private by default
    proj = db.exec(select(Project)).one()
    proj.visibility = "public"
    db.add(proj)
    db.commit()
    assert len(list_records(db, other)) == 1  # public: readable by any authenticated user...
    with pytest.raises(AccessError):  # ...but never writable
        write_record(db, other, rec(body="vandalism"), change_source="ui")


def test_project_limited_token(db, human, user):
    write_record(db, human, rec(), change_source="ui")
    write_record(db, human, rec(name="other", project="blog"), change_source="ui")
    from acm_hub.models import Project
    from sqlmodel import select

    pid = db.exec(select(Project).where(Project.slug == "payments")).one().id
    tok = token_principal(user, token_project_ids=[pid])
    assert [r.name for r in list_records(db, tok)] == ["idempotent-retries"]
    with pytest.raises(AccessError):  # can't create/reach other projects
        write_record(db, tok, rec(name="x", project="brand-new"), change_source="mcp-write")


def test_core_budget_enforced(db, human):
    from acm_hub.models import InstanceSettings

    db.get(InstanceSettings, 1).core_token_budget = 50
    db.commit()
    write_record(db, human, rec(name="a", body="x" * 100, tier="core"), change_source="ui")
    with pytest.raises(ValidationFailed, match="Core is limited"):
        write_record(db, human, rec(name="b", body="y" * 100, tier="core"), change_source="ui")


def test_focus_core_topics_links_and_scope_leak(db, human):
    write_record(
        db,
        human,
        RecordIn(name="terse", description="prefers terse replies", type="user", scope="user", tier="core"),
        change_source="ui",
    )
    a = write_record(db, human, rec(topics=["payments", "webhooks"]), change_source="ui").record
    b = write_record(
        db,
        human,
        rec(
            name="incident-1042", description="Double charge incident", body="Replay caused it", links=[a.id]
        ),
        change_source="ui",
    ).record
    write_record(
        db,
        human,
        rec(name="blog-theme", project="blog", description="Blog theme is dark", body="webhook"),
        change_source="ui",
    )
    pack = build_focus(db, human, "fix the retry logic on the payments webhook", project="payments")
    names = [i.record.name for i in pack.associated]
    assert [i.record.name for i in pack.core] == ["terse"]
    assert "idempotent-retries" in names and "incident-1042" in names
    assert "blog-theme" not in names  # other project's memory doesn't leak into this task
    why = {i.record.name: i.why for i in pack.associated}
    assert any("topic" in x for x in why["idempotent-retries"]) and any(
        "linked" in x for x in why["incident-1042"]
    )
    assert b.id  # link expansion reached the un-matched-by-text record
    wide = build_focus(
        db,
        human,
        "fix the retry logic on the payments webhook",
        project="payments",
        include_other_projects=True,
    )
    assert "blog-theme" in [i.record.name for i in wide.associated]  # explicit opt-in only


def test_fts_input_is_not_injectable(db, human):
    write_record(db, human, rec(), change_source="ui")
    assert list_records(db, human, q='idempotent" OR name:* NOT "x') is not None
    build_focus(db, human, '") OR 1=1 --')
