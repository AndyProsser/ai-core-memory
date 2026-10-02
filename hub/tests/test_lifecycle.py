"""Reinforcement, supersession (docs/ARCHITECTURE.md § How memory changes over time)."""

import pytest
from sqlmodel import select

from acm_hub.access import AccessError, NotFound, principal_for_user
from acm_hub.models import MemoryLink, MemoryRevision, Proposal, Reinforcement
from acm_hub.records import (
    Conflict,
    RecordIn,
    ValidationFailed,
    list_records,
    reinforce,
    supersede,
    supersession_chain,
    write_record,
)

from .conftest import make_user, token_principal


def mk(db, p, name="prefers-tabs", **kw):
    base = dict(name=name, description=f"{name} description", body="body text", type="feedback", scope="user")
    base.update(kw)
    return write_record(db, p, RecordIn(**base), change_source="ui").record


# --- reinforcement -------------------------------------------------------------------------------------


def test_two_independent_sources_promote_observed_to_confirmed(db, human):
    r = mk(db, human)
    a = reinforce(db, human, r, "session-A", change_source="mcp-write")
    assert a.counted and not a.promoted and r.confidence == "observed" and r.last_reinforced is not None
    b = reinforce(db, human, r, "session-B", change_source="mcp-write")
    assert b.counted and b.promoted and r.confidence == "confirmed" and r.reinforcement_count == 2
    assert any(
        "promoted observed -> confirmed" in (v.change_note or "")
        for v in db.exec(select(MemoryRevision)).all()
    )


def test_one_source_cannot_reinforce_itself(db, human):
    r = mk(db, human)
    for _ in range(5):
        res = reinforce(db, human, r, "chatty-session", change_source="mcp-write")
    assert r.reinforcement_count == 1 and not res.counted and r.confidence == "observed"
    assert len(db.exec(select(Reinforcement)).all()) == 1


def test_the_creating_session_does_not_count_as_a_second_source(db, human):
    r = write_record(
        db,
        human,
        RecordIn(name="x-y", description="d", type="feedback", scope="user", source_ref="s1"),
        change_source="mcp-write",
    ).record
    res = reinforce(db, human, r, "s1", change_source="mcp-write")  # same session that wrote it
    assert not res.counted and r.reinforcement_count == 0
    assert reinforce(db, human, r, "s2", change_source="mcp-write").counted


def test_rewriting_identical_content_with_a_source_ref_is_reinforcement(db, human):
    kw = dict(name="idem", description="d", body="b", type="feedback", scope="user")
    write_record(db, human, RecordIn(**kw, source_ref="s1"), change_source="mcp-write")
    r2 = write_record(db, human, RecordIn(**kw, source_ref="s2"), change_source="mcp-write")
    assert r2.action == "reinforced" and r2.record.reinforcement_count == 1
    r3 = write_record(db, human, RecordIn(**kw, source_ref="s3"), change_source="mcp-write")
    assert r3.record.confidence == "confirmed" and any("confirmed" in n for n in r3.notices)
    assert (
        write_record(db, human, RecordIn(**kw), change_source="mcp-write").action == "unchanged"
    )  # no ref: still a no-op


def test_editing_content_registers_the_source_without_counting(db, human):
    r = mk(db, human)
    write_record(db, human, RecordIn(id=r.id, body="edited", source_ref="s1"), change_source="mcp-write")
    assert not reinforce(db, human, r, "s1", change_source="mcp-write").counted


def test_reinforcing_never_alters_content_and_works_on_established(db, human, user):
    r = mk(db, human, confidence="established")
    res = reinforce(
        db, token_principal(user, token_include_user_scope=True), r, "s1", change_source="mcp-write"
    )
    assert res.counted and r.confidence == "established" and r.body == "body text"


def test_reinforcement_revives_a_stale_record_but_not_a_superseded_one(db, human):
    r = mk(db, human)
    write_record(db, human, RecordIn(id=r.id, status="stale"), change_source="ui")
    reinforce(db, human, r, "s1", change_source="mcp-write")
    assert r.status == "active" and r.valid_to is None
    write_record(db, human, RecordIn(id=r.id, status="archived"), change_source="ui")
    with pytest.raises(ValidationFailed):
        reinforce(db, human, r, "s2", change_source="mcp-write")


def test_reinforce_requires_write_access_and_a_source_ref(db, human, user):
    r = mk(db, human)
    with pytest.raises(AccessError):
        reinforce(
            db,
            token_principal(user, read_only=True, token_include_user_scope=True),
            r,
            "s1",
            change_source="mcp-write",
        )
    with pytest.raises(ValidationFailed):
        reinforce(db, human, r, "  ", change_source="mcp-write")
    stranger = principal_for_user(db, make_user(db, "x@example.com", admin=False))
    with pytest.raises(NotFound):  # can't even see it
        reinforce(db, stranger, r, "s1", change_source="ui")


# --- supersession --------------------------------------------------------------------------------------


def test_supersession_keeps_history_and_drops_the_old_record_from_view(db, human):
    old = mk(db, human, "likes-tabs", body="uses tabs")
    new = write_record(
        db,
        human,
        RecordIn(
            name="likes-spaces",
            description="d",
            body="uses spaces",
            type="feedback",
            scope="user",
            supersedes=[old.id],
        ),
        change_source="ui",
    ).record
    db.refresh(old)
    assert old.status == "superseded" and old.valid_to is not None
    assert [r.name for r in list_records(db, human)] == ["likes-spaces"]  # default view hides it
    assert [r.name for r in list_records(db, human, status="superseded")] == ["likes-tabs"]
    assert db.get(MemoryLink, (new.id, old.id, "supersedes")) is not None
    assert [r.name for r in supersession_chain(db, human, new)] == ["likes-tabs", "likes-spaces"]
    assert [r.name for r in supersession_chain(db, human, old)] == ["likes-tabs", "likes-spaces"]
    # and the old record's content is still there, with its history
    assert old.body == "uses tabs"


def test_supersession_chain_follows_multiple_hops_in_order(db, human):
    a = mk(db, human, "v-one")
    b = write_record(
        db,
        human,
        RecordIn(name="v-two", description="d", type="feedback", scope="user", supersedes=[a.id]),
        change_source="ui",
    ).record
    c = write_record(
        db,
        human,
        RecordIn(name="v-three", description="d", type="feedback", scope="user", supersedes=[b.id]),
        change_source="ui",
    ).record
    assert [r.name for r in supersession_chain(db, human, b)] == ["v-one", "v-two", "v-three"]
    assert c.status == "active"


def test_superseding_an_established_record_follows_the_confidence_rules(db, human, user):
    old = mk(db, human, "hold-the-line", confidence="established")
    tok = token_principal(user, token_include_user_scope=True)
    with pytest.raises(Conflict):  # an AI client can't retire it
        write_record(
            db,
            tok,
            RecordIn(name="replacement", description="d", type="feedback", scope="user", supersedes=[old.id]),
            change_source="mcp-write",
        )
    with pytest.raises(Conflict) as e:  # a person needs to confirm
        write_record(
            db,
            human,
            RecordIn(name="replacement", description="d", type="feedback", scope="user", supersedes=[old.id]),
            change_source="ui",
        )
    assert e.value.needs_confirmation
    write_record(
        db,
        human,
        RecordIn(name="replacement", description="d", type="feedback", scope="user", supersedes=[old.id]),
        change_source="ui",
        confirm_established=True,
    )
    db.refresh(old)
    assert old.status == "superseded"


def test_supersession_is_scoped_and_not_circular(db, human):
    a = mk(db, human, "in-user")
    b = mk(db, human, "in-project", scope="project", project="payments", type="project")
    with pytest.raises(ValidationFailed, match="same scope"):
        supersede(db, human, a, b, change_source="ui")
    with pytest.raises(ValidationFailed):
        supersede(db, human, a, a, change_source="ui")
    c = mk(db, human, "another")
    supersede(db, human, a, c, change_source="ui")
    with pytest.raises(ValidationFailed, match="already superseded"):
        supersede(db, human, a, c, change_source="ui")


def test_ai_superseding_a_core_record_asks_a_person_to_promote_the_replacement(db, human, user):
    old = mk(db, human, "standing-rule", tier="core", type="rule", confidence="confirmed")
    tok = token_principal(user, token_include_user_scope=True)
    new = write_record(
        db,
        tok,
        RecordIn(
            name="new-standing-rule",
            description="d",
            type="rule",
            scope="user",
            confidence="confirmed",
            supersedes=[old.id],
        ),
        change_source="mcp-write",
    ).record
    assert new.tier == "associated"  # tokens can't set core
    prop = db.exec(select(Proposal)).one()
    assert prop.kind == "promote_core" and prop.payload["record"] == new.id and prop.status == "pending"
