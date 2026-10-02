from datetime import timedelta

import pytest
from sqlmodel import select

from acm_hub import proposals as pr
from acm_hub.access import AccessError, NotFound, principal_for_user, system_principal
from acm_hub.models import InstanceSettings, MemoryRecord, MemoryRevision, Proposal, Team, TeamMember, utcnow
from acm_hub.records import Conflict, RecordIn, ValidationFailed, write_record

from .conftest import make_user, token_principal


def mk(db, p, name, **kw):
    base = dict(
        name=name, description=f"{name} description", body=f"body of {name}", type="feedback", scope="user"
    )
    base.update(kw)
    return write_record(db, p, RecordIn(**base), change_source="ui").record


def ai(user):
    return token_principal(user, token_include_user_scope=True)


# --- who may do what -----------------------------------------------------------------------------------


def test_ai_can_propose_but_never_approve(db, human, user):
    a, b = mk(db, human, "dup-one"), mk(db, human, "dup-two")
    tok = ai(user)
    prop = pr.create_proposal(db, tok, "merge", {"keep": a.id, "retire": b.id}, rationale="same fact twice")
    assert (
        prop.status == "pending" and prop.generated_by == "dream-skill" and prop.generated_by_token_id is None
    )  # (test token has no id)
    for forbidden in (tok, system_principal()):
        with pytest.raises(AccessError):
            pr.decide(db, forbidden, prop.id, approve=True)
        with pytest.raises(AccessError):
            pr.decide(db, forbidden, prop.id, approve=False)
    db.refresh(b)
    assert b.status == "active"


def test_ai_cannot_raise_hub_only_kinds_or_when_read_only(db, human, user):
    a = mk(db, human, "some-record", confidence="established")
    for kind, payload in (
        ("review_established", {"record": a.id}),
        ("conflict", {"record": a.id, "revision": "x"}),
    ):
        with pytest.raises(AccessError):
            pr.create_proposal(db, ai(user), kind, payload, rationale="r")
    with pytest.raises(AccessError):
        pr.create_proposal(
            db,
            token_principal(user, read_only=True, token_include_user_scope=True),
            "archive",
            {"record": a.id},
            rationale="r",
        )


def test_ai_cannot_claim_to_be_mechanical(db, human, user):
    a = mk(db, human, "some-record")
    prop = pr.create_proposal(
        db, ai(user), "archive", {"record": a.id}, rationale="r", generated_by="mechanical"
    )
    assert prop.generated_by == "dream-skill"  # so it can never be auto-applied


def test_proposals_are_private_to_people_who_can_read_the_records(db, human):
    a, b = mk(db, human, "mine-one"), mk(db, human, "mine-two")
    prop = pr.create_proposal(db, human, "merge", {"keep": a.id, "retire": b.id}, rationale="r")
    other = principal_for_user(db, make_user(db, "other@example.com", admin=True))
    assert pr.list_proposals(db, other) == []
    with pytest.raises(NotFound):
        pr.get_proposal(db, other, prop.id)
    with pytest.raises(NotFound):
        pr.decide(db, other, prop.id, approve=True)
    with pytest.raises(NotFound):  # can't propose about records you can't see either
        pr.create_proposal(db, other, "archive", {"record": a.id}, rationale="r")


def test_validation_rejects_bad_payloads(db, human):
    a = mk(db, human, "rec-a")
    for kind, payload in (
        ("merge", {"keep": a.id}),
        ("nonsense", {}),
        ("archive", {"record": "NOPE"}),
        ("promote_scope", {"source": a.id, "to_scope": "session"}),
        ("merge", {"keep": a.id, "retire": a.id}),
        ("promote_core", {"record": a.id, "demote": "x"}),
    ):
        with pytest.raises((ValidationFailed, NotFound)):
            pr.create_proposal(db, human, kind, payload, rationale="r")
    with pytest.raises(ValidationFailed):
        pr.create_proposal(db, human, "archive", {"record": a.id}, rationale="   ")


# --- dedupe and the rejection cooldown -------------------------------------------------------------------


def test_identical_open_proposals_are_reused_and_rejections_cool_down(db, human):
    a = mk(db, human, "rec-a")
    p1 = pr.create_proposal(db, human, "mark_stale", {"record": a.id}, rationale="r")
    assert pr.create_proposal(db, human, "mark_stale", {"record": a.id}, rationale="again") is p1
    pr.decide(db, human, p1.id, approve=False, note="no, still used")
    assert (
        pr.create_proposal(db, human, "mark_stale", {"record": a.id}, rationale="r") is None
    )  # cooling down
    p1.decided_at = utcnow() - timedelta(days=31)
    db.add(p1)
    p2 = pr.create_proposal(db, human, "mark_stale", {"record": a.id}, rationale="r")
    assert p2 is not None and p2.id != p1.id


# --- applying each kind ----------------------------------------------------------------------------------


def test_merge_with_ai_written_text_retires_the_duplicate(db, human, user):
    keep, retire = mk(db, human, "keep-me", body="short"), mk(db, human, "retire-me", body="extra detail")
    prop = pr.create_proposal(
        db,
        ai(user),
        "merge",
        {"keep": keep.id, "retire": retire.id, "merged": {"body": "short plus extra detail"}},
        rationale="dupes",
    )
    pr.decide(db, human, prop.id, approve=True)
    db.refresh(keep)
    db.refresh(retire)
    assert keep.body == "short plus extra detail" and retire.status == "superseded"
    assert prop.status == "applied" and prop.decided_by_user_id == human.user_id
    assert any(
        v.change_source == "ui" and "proposal" in (v.change_note or "")
        for v in db.exec(select(MemoryRevision)).all()
    )


def test_supersede_proposal(db, human):
    old, new = mk(db, human, "old-way"), mk(db, human, "new-way")
    pr.decide(
        db,
        human,
        pr.create_proposal(db, human, "supersede", {"old": old.id, "new": new.id}, rationale="r").id,
        approve=True,
    )
    db.refresh(old)
    assert old.status == "superseded"


def test_promote_scope_project_to_user_creates_the_user_record_and_retires_the_source(db, human):
    src = mk(
        db,
        human,
        "likes-short-prs",
        scope="project",
        project="payments",
        type="feedback",
        confidence="confirmed",
    )
    prop = pr.create_proposal(
        db,
        human,
        "promote_scope",
        {"source": src.id, "to_scope": "user"},
        rationale="about the person, not the project",
    )
    pr.decide(db, human, prop.id, approve=True)
    db.refresh(src)
    new = db.exec(
        select(MemoryRecord).where(MemoryRecord.scope == "user", MemoryRecord.name == "likes-short-prs")
    ).one()
    assert new.user_id == human.user_id and new.confidence == "confirmed" and new.tier == "associated"
    assert src.status == "superseded"


def test_promote_scope_to_team_needs_membership_and_only_goes_broader(db, human):
    src = mk(db, human, "team-convention", scope="project", project="payments", type="project")
    team = Team(name="Core", slug="core")
    db.add(team)
    db.commit()
    prop = pr.create_proposal(
        db,
        human,
        "promote_scope",
        {"source": src.id, "to_scope": "team", "team": "core"},
        rationale="everyone should know",
    )
    with pytest.raises(AccessError):  # approver isn't in the team
        pr.decide(db, human, prop.id, approve=True)
    db.refresh(prop)
    assert prop.status == "pending"
    db.add(TeamMember(team_id=team.id, user_id=human.user_id, role="owner"))
    db.commit()
    human.team_ids.add(team.id)
    pr.decide(db, human, prop.id, approve=True)
    assert db.exec(select(MemoryRecord).where(MemoryRecord.scope == "team")).one().team_id == team.id
    user_rec = mk(db, human, "personal-pref")
    with pytest.raises(ValidationFailed, match="broader"):
        pr.create_proposal(
            db, human, "promote_scope", {"source": user_rec.id, "to_scope": "user"}, rationale="r"
        )


def test_promote_core_makes_room_atomically_and_respects_the_budget(db, human):
    db.get(InstanceSettings, 1).core_token_budget = 60
    db.commit()
    big = mk(db, human, "big-core", body="x" * 150, tier="core")
    want = mk(db, human, "wants-core", body="y" * 150)
    prop = pr.create_proposal(db, human, "promote_core", {"record": want.id}, rationale="r")
    with pytest.raises(ValidationFailed, match="Core is limited"):  # no room, no demotion offered
        pr.decide(db, human, prop.id, approve=True)
    db.refresh(want)
    assert want.tier == "associated" and prop.status == "pending"
    p2 = pr.create_proposal(
        db, human, "promote_core", {"record": want.id, "demote": [big.id]}, rationale="swap them"
    )
    pr.decide(db, human, p2.id, approve=True)
    db.refresh(want)
    db.refresh(big)
    assert (want.tier, big.tier) == ("core", "associated")


def test_a_failed_apply_changes_nothing(db, human):
    """promote_core with a demotion, but the new record is too big even alone: the demotion must roll back too."""
    db.get(InstanceSettings, 1).core_token_budget = 50
    db.commit()
    keep_core = mk(db, human, "small-core", body="z" * 20, tier="core")
    huge = mk(db, human, "huge", body="h" * 400)
    prop = pr.create_proposal(
        db, human, "promote_core", {"record": huge.id, "demote": [keep_core.id]}, rationale="r"
    )
    with pytest.raises(ValidationFailed):
        pr.decide(db, human, prop.id, approve=True)
    db.expire_all()
    assert (
        db.get(MemoryRecord, keep_core.id).tier == "core"
    )  # demotion was rolled back with the failed promotion
    assert db.get(Proposal, prop.id).status == "pending"


def test_demote_stale_archive(db, human):
    c, s, a = (
        mk(db, human, "was-core", tier="core"),
        mk(db, human, "going-stale"),
        mk(db, human, "to-archive"),
    )
    for kind, rec in (("demote_core", c), ("mark_stale", s), ("archive", a)):
        pr.decide(
            db, human, pr.create_proposal(db, human, kind, {"record": rec.id}, rationale="r").id, approve=True
        )
    db.expire_all()
    assert db.get(MemoryRecord, c.id).tier == "associated"
    assert db.get(MemoryRecord, s.id).status == "stale"
    assert db.get(MemoryRecord, a.id).status == "archived"


def test_review_established_renews_without_changing_anything(db, human):
    r = mk(db, human, "bedrock", confidence="established", type="rule")
    before = (r.body, r.confidence, r.description)
    prop = pr.create_proposal(
        db, system_principal(), "review_established", {"record": r.id}, rationale="not reviewed in a year"
    )
    assert prop.generated_by == "mechanical"
    pr.decide(
        db, human, prop.id, approve=True
    )  # approving an established-review needs no extra confirmation: it changes nothing
    db.refresh(r)
    assert (r.body, r.confidence, r.description) == before and r.last_reinforced is not None


# --- established records stay protected -----------------------------------------------------------------


def test_approving_something_that_touches_an_established_record_needs_explicit_confirmation(db, human):
    keep, retire = mk(db, human, "kept"), mk(db, human, "bedrock-dup", confidence="established", type="rule")
    prop = pr.create_proposal(db, human, "merge", {"keep": keep.id, "retire": retire.id}, rationale="r")
    assert pr.view(db, prop).needs_confirm
    with pytest.raises(Conflict) as e:
        pr.decide(db, human, prop.id, approve=True)
    assert e.value.needs_confirmation
    db.refresh(retire)
    assert retire.status == "active" and prop.status == "pending"
    pr.decide(db, human, prop.id, approve=True, confirm_established=True)
    db.refresh(retire)
    assert retire.status == "superseded"
    assert any(
        v.flagged
        for v in db.exec(select(MemoryRevision).where(MemoryRevision.memory_record_id == retire.id)).all()
    )


def test_the_mechanical_job_can_never_change_an_established_record(db, human):
    r = mk(db, human, "bedrock", confidence="established", type="rule")
    with pytest.raises(Conflict):
        write_record(db, system_principal(), RecordIn(id=r.id, status="stale"), change_source="mechanical")
    with pytest.raises(AccessError):
        write_record(
            db,
            system_principal(),
            RecordIn(id=mk(db, human, "plain").id, tier="core"),
            change_source="mechanical",
        )


# --- conflicts ------------------------------------------------------------------------------------------


def test_conflict_proposals_apply_or_dismiss_the_parked_version(db, human, user):
    r = mk(db, human, "bedrock", confidence="established", type="rule", body="original")
    incoming = RecordIn(id=r.id, body="an AI's different idea")
    prop = pr.park_conflict(
        db, ai(user), r, incoming, source="mcp-write", note="AI tried to change an established record"
    )
    assert prop.kind == "conflict" and prop.status == "pending"
    assert (
        pr.park_conflict(db, ai(user), r, incoming, source="mcp-write", note="again") is prop
    )  # a retry doesn't pile up
    view = pr.view(db, prop)
    assert view.needs_confirm and view.incoming.body == "an AI's different idea"
    with pytest.raises(Conflict):
        pr.decide(db, human, prop.id, approve=True)  # established: explicit confirmation required
    db.refresh(r)
    assert r.body == "original"
    pr.decide(db, human, prop.id, approve=False)
    db.refresh(r)
    assert (
        r.body == "original"
        and not db.exec(select(MemoryRevision).where(MemoryRevision.applied.is_(False))).all()
    )  # type: ignore[attr-defined]
    prop2 = pr.park_conflict(
        db, ai(user), r, RecordIn(id=r.id, body="second try"), source="mcp-write", note="again"
    )
    pr.decide(db, human, prop2.id, approve=True, confirm_established=True)
    db.refresh(r)
    assert r.body == "second try"


def test_resolving_a_conflict_from_the_record_page_closes_its_proposal(db, human, user):
    from acm_hub.records import resolve_conflict

    r = mk(db, human, "bedrock", confidence="established", type="rule", body="original")
    prop = pr.park_conflict(db, ai(user), r, RecordIn(id=r.id, body="other"), source="mcp-write", note="n")
    resolve_conflict(db, human, prop.payload["revision"], apply=False)
    db.refresh(prop)
    assert prop.status == "rejected"


# --- staleness of proposals ----------------------------------------------------------------------------


def test_a_proposal_whose_records_moved_on_is_closed_not_applied(db, human):
    a = mk(db, human, "going-away")
    prop = pr.create_proposal(db, human, "mark_stale", {"record": a.id}, rationale="r")
    write_record(db, human, RecordIn(id=a.id, status="archived"), change_source="ui")
    assert pr.view(db, prop).stale_reason
    with pytest.raises(ValidationFailed, match="out of date"):
        pr.decide(db, human, prop.id, approve=True)
    db.refresh(prop)
    assert prop.status == "expired"
    p2 = pr.create_proposal(db, human, "archive", {"record": mk(db, human, "other-one").id}, rationale="r")
    db.get(MemoryRecord, p2.target_ids[0]).status = "archived"
    assert pr.expire_outdated(db) == 1
