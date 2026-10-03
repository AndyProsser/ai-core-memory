"""Users, teams, projects, and the admin/owner/member role matrix (docs/SECURITY.md § Roles)."""

import pytest
from sqlalchemy import text
from sqlmodel import select

from acm_hub import orgs
from acm_hub.access import AccessError, NotFound, principal_for_user
from acm_hub.models import (
    ApiToken,
    InstanceSettings,
    MemoryRecord,
    MemoryRevision,
    PluginInstance,
    Project,
    Team,
    TeamMember,
    User,
    WebSession,
    utcnow,
)
from acm_hub.records import RecordIn, ValidationFailed, list_records, write_record

from .conftest import make_user, token_principal


def mode(db, m):
    db.get(InstanceSettings, 1).deployment_mode = m
    db.commit()


def P(db, user):
    db.refresh(user)
    return principal_for_user(db, user)


@pytest.fixture()
def world(db):
    """admin (not on any team), alice (owner of A), bob (member of A), carol (owner of B), dave (nobody)."""
    mode(db, "multi_team")
    admin = make_user(db, "admin@example.com", admin=True)
    alice, bob, carol, dave = (
        make_user(db, f"{n}@example.com", admin=False) for n in ("alice", "bob", "carol", "dave")
    )
    a = orgs.create_team(db, P(db, admin), "Team A", "team-a", alice)
    b = orgs.create_team(db, P(db, admin), "Team B", "team-b", carol)
    orgs.add_member(db, P(db, alice), a, bob, "member")
    db.commit()
    return dict(admin=admin, alice=alice, bob=bob, carol=carol, dave=dave, a=a, b=b)


# --- who can do what ----------------------------------------------------------------------------------------------


def test_admin_creates_teams_but_is_not_an_owner_of_them(db, world):
    w = world
    admin = P(db, w["admin"])
    assert orgs.team_role(db, w["admin"].id, w["a"].id) is None
    with pytest.raises(AccessError, match="owner of this team"):
        orgs.add_member(db, admin, w["a"], w["dave"], "member")
    with pytest.raises(AccessError):
        orgs.set_member_role(db, admin, w["a"], w["bob"], "owner")
    with pytest.raises(AccessError):
        orgs.remove_member(db, admin, w["a"], w["bob"])
    with pytest.raises(AccessError, match="Only an admin"):
        orgs.create_team(db, P(db, w["alice"]), "Rogue", "rogue", w["alice"])


def test_admin_sees_no_team_memory_and_no_private_projects_it_isnt_part_of(db, world):
    w = world
    alice = P(db, w["alice"])
    proj = orgs.create_project(db, alice, "alpha", visibility="team", team=w["a"])
    write_record(
        db,
        alice,
        RecordIn(
            name="team-rule",
            description="d",
            type="rule",
            scope="team",
            team="team-a",
            confidence="confirmed",
        ),
        change_source="ui",
    )
    write_record(
        db,
        alice,
        RecordIn(name="alpha-fact", description="d", type="project", scope="project", project="alpha"),
        change_source="ui",
    )
    db.commit()
    admin = P(db, w["admin"])
    assert list_records(db, admin) == []  # admin is a platform role, not a memory-access override
    assert proj.slug not in [p.slug for p in orgs.visible_projects(db, admin)]
    assert {t.slug for t in orgs.visible_teams(db, admin)} == {
        "team-a",
        "team-b",
    }  # may *list* teams to administer them


def test_owner_manages_own_team_only(db, world):
    w = world
    alice = P(db, w["alice"])
    orgs.add_member(db, alice, w["a"], w["dave"], "member")
    orgs.set_member_role(db, alice, w["a"], w["dave"], "owner")
    orgs.set_member_role(db, alice, w["a"], w["dave"], "member")
    orgs.remove_member(db, alice, w["a"], w["dave"])
    with pytest.raises(AccessError):  # alice owns A, not B
        orgs.add_member(db, alice, w["b"], w["dave"], "member")
    with pytest.raises(AccessError):
        orgs.remove_member(db, alice, w["b"], w["carol"])
    with pytest.raises(AccessError, match="owner of this team"):  # members can't manage membership
        orgs.add_member(db, P(db, w["bob"]), w["a"], w["dave"], "member")
    with pytest.raises(AccessError):
        orgs.set_member_role(db, P(db, w["bob"]), w["a"], w["bob"], "owner")  # no self-promotion


def test_a_team_always_keeps_an_owner(db, world):
    w = world
    alice = P(db, w["alice"])
    with pytest.raises(ValidationFailed, match="at least one owner"):
        orgs.set_member_role(db, alice, w["a"], w["alice"], "member")
    with pytest.raises(ValidationFailed, match="at least one owner"):
        orgs.remove_member(db, alice, w["a"], w["alice"])
    orgs.set_member_role(db, alice, w["a"], w["bob"], "owner")  # promote a second owner...
    orgs.set_member_role(db, alice, w["a"], w["alice"], "member")  # ...and now the first can step down


def test_tokens_can_never_manage_orgs(db, world):
    w = world
    tok = token_principal(w["alice"], token_include_user_scope=True)
    tok.is_admin = True
    for call in (
        lambda: orgs.create_team(db, tok, "X", "x", w["alice"]),
        lambda: orgs.add_member(db, tok, w["a"], w["dave"]),
        lambda: orgs.create_project(db, tok, "tokproj"),
        lambda: orgs.create_user(db, tok, "n@example.com", password="correct horse battery"),
    ):
        with pytest.raises(AccessError, match="Only a person|Only an admin"):
            call()


# --- membership drives access ---------------------------------------------------------------------------------------


def test_membership_changes_take_effect_immediately(db, world):
    w = world
    alice = P(db, w["alice"])
    orgs.create_project(db, alice, "alpha", visibility="team", team=w["a"])
    write_record(
        db,
        alice,
        RecordIn(name="alpha-fact", description="d", type="project", scope="project", project="alpha"),
        change_source="ui",
    )
    write_record(
        db,
        alice,
        RecordIn(
            name="team-rule",
            description="d",
            type="rule",
            scope="team",
            team="team-a",
            confidence="confirmed",
        ),
        change_source="ui",
    )
    db.commit()
    assert {r.name for r in list_records(db, P(db, w["bob"]))} == {
        "alpha-fact",
        "team-rule",
    }  # member: reads and
    write_record(
        db,
        P(db, w["bob"]),
        RecordIn(
            name="alpha-fact",
            description="d",
            body="bob was here",
            type="project",
            scope="project",
            project="alpha",
        ),
        change_source="ui",
    )  # writes
    assert list_records(db, P(db, w["dave"])) == []  # dave: not on the team
    orgs.add_member(db, alice, w["a"], w["dave"], "member")
    assert len(list_records(db, P(db, w["dave"]))) == 2
    orgs.remove_member(db, alice, w["a"], w["dave"])
    assert list_records(db, P(db, w["dave"])) == []  # removal revokes access at once
    with pytest.raises(NotFound):
        write_record(
            db,
            P(db, w["dave"]),
            RecordIn(
                name="alpha-fact", description="d", body="x", type="project", scope="project", project="alpha"
            ),
            change_source="ui",
        )


def test_team_a_cannot_see_team_b(db, world):
    w = world
    orgs.create_project(db, P(db, w["carol"]), "bravo", visibility="team", team=w["b"])
    write_record(
        db,
        P(db, w["carol"]),
        RecordIn(name="b-fact", description="d", type="project", scope="project", project="bravo"),
        change_source="ui",
    )
    db.commit()
    assert list_records(db, P(db, w["alice"])) == [] and list_records(db, P(db, w["bob"])) == []


def test_public_projects_are_readable_by_everyone_but_writable_by_no_one_else(db, world):
    w = world
    orgs.create_project(db, P(db, w["alice"]), "open", visibility="public", team=w["a"])
    write_record(
        db,
        P(db, w["alice"]),
        RecordIn(name="open-fact", description="d", type="project", scope="project", project="open"),
        change_source="ui",
    )
    db.commit()
    assert [r.name for r in list_records(db, P(db, w["carol"]))] == ["open-fact"]
    with pytest.raises(AccessError):
        write_record(
            db,
            P(db, w["carol"]),
            RecordIn(
                name="open-fact",
                description="d",
                body="vandal",
                type="project",
                scope="project",
                project="open",
            ),
            change_source="ui",
        )


# --- projects, visibility, deployment mode ------------------------------------------------------------------------------


def test_visibility_follows_the_deployment_mode(db, world):
    w = world
    alice = P(db, w["alice"])
    mode(db, "solo")
    assert orgs.allowed_visibilities(db) == ("private", "public") and not orgs.teams_enabled(db)
    with pytest.raises(ValidationFailed, match="isn't available in solo"):
        orgs.create_project(db, alice, "s1", visibility="team", team=w["a"])
    orgs.create_project(db, alice, "s-public", visibility="public")
    with pytest.raises(ValidationFailed, match="Teams are off"):
        orgs.create_team(db, P(db, w["admin"]), "T", "t", w["alice"])
    mode(db, "team")
    with pytest.raises(ValidationFailed, match="isn't available in team"):
        orgs.create_project(db, alice, "t1", visibility="public")
    orgs.create_project(db, alice, "t-team", visibility="team", team=w["a"])
    mode(db, "multi_team")
    orgs.create_project(db, alice, "m-public", visibility="public")


def test_switching_modes_never_breaks_existing_data(db, world):
    w = world
    orgs.create_project(db, P(db, w["alice"]), "alpha", visibility="team", team=w["a"])
    write_record(
        db,
        P(db, w["alice"]),
        RecordIn(name="alpha-fact", description="d", type="project", scope="project", project="alpha"),
        change_source="ui",
    )
    db.commit()
    mode(db, "solo")  # a one-way reveal, not a migration: existing team data stays intact and readable
    assert [r.name for r in list_records(db, P(db, w["bob"]))] == ["alpha-fact"]
    proj = db.exec(select(Project).where(Project.slug == "alpha")).one()
    orgs.update_project(
        db, P(db, w["alice"]), proj, visibility="team", team=w["a"]
    )  # unchanged value is still accepted


def test_project_rules(db, world):
    w = world
    alice, bob = P(db, w["alice"]), P(db, w["bob"])
    with pytest.raises(ValidationFailed, match="needs a team"):
        orgs.create_project(db, alice, "x1", visibility="team")
    with pytest.raises(AccessError, match="owner of this team"):  # members can't create their team's projects
        orgs.create_project(db, bob, "x2", visibility="team", team=w["a"])
    with pytest.raises(AccessError):  # alice isn't an owner of B
        orgs.create_project(db, alice, "x3", visibility="team", team=w["b"])
    priv = orgs.create_project(db, alice, "mine", visibility="private", team=w["a"])
    assert priv.team_id is None  # private means just you: the team is dropped
    with pytest.raises(ValidationFailed, match="already exists"):
        orgs.create_project(db, alice, "mine")
    with pytest.raises(ValidationFailed):
        orgs.create_project(db, alice, "Bad Slug!")
    shared = orgs.create_project(db, alice, "shared", visibility="team", team=w["a"])
    with pytest.raises(AccessError, match="owner"):  # bob (member) can't change visibility
        orgs.update_project(db, bob, shared, visibility="private", team=None)
    with pytest.raises(AccessError):  # carol owns B only: can't move alice's project into B or touch it
        orgs.update_project(db, P(db, w["carol"]), shared, visibility="team", team=w["b"])
    with pytest.raises(AccessError):  # alice can't move her project into a team she doesn't own
        orgs.update_project(db, alice, shared, visibility="team", team=w["b"])
    orgs.update_project(db, alice, shared, visibility="private", team=None)
    assert shared.team_id is None and shared.visibility == "private"
    with pytest.raises(AccessError):  # admin gets nothing extra
        orgs.update_project(db, P(db, w["admin"]), shared, visibility="public", team=None)


def test_deleting_never_silently_destroys_memory(db, world):
    w = world
    alice = P(db, w["alice"])
    proj = orgs.create_project(db, alice, "alpha", visibility="team", team=w["a"])
    keep = write_record(
        db,
        alice,
        RecordIn(name="alpha-fact", description="d", type="project", scope="project", project="alpha"),
        change_source="ui",
    ).record
    team_rec = write_record(
        db,
        alice,
        RecordIn(
            name="team-rule",
            description="d",
            type="rule",
            scope="team",
            team="team-a",
            confidence="confirmed",
        ),
        change_source="ui",
    ).record
    db.commit()
    with pytest.raises(ValidationFailed, match="aren't archived"):  # live memory blocks deletion, no override
        orgs.delete_project(db, alice, proj, purge_archived=True)
    with pytest.raises(ValidationFailed, match="still owns projects"):
        orgs.delete_team(db, P(db, w["admin"]), w["a"])
    write_record(db, alice, RecordIn(id=keep.id, status="archived"), change_source="ui")
    with pytest.raises(
        ValidationFailed, match="confirm the purge"
    ):  # archived-only still needs an explicit confirmation
        orgs.delete_project(db, alice, proj)
    with pytest.raises(AccessError):  # members can't delete their team's project
        orgs.delete_project(db, P(db, w["bob"]), proj, purge_archived=True)
    assert orgs.delete_project(db, alice, proj, purge_archived=True) == 1
    db.commit()
    assert (
        db.get(MemoryRecord, keep.id) is None
        and db.exec(select(MemoryRevision).where(MemoryRevision.memory_record_id == keep.id)).all() == []
    )
    assert (
        db.execute(text("SELECT count(*) FROM memory_fts WHERE record_id = :i"), {"i": keep.id}).scalar() == 0
    )  # no searchable ghost
    with pytest.raises(ValidationFailed, match="aren't archived"):
        orgs.delete_team(db, P(db, w["admin"]), w["a"], purge_archived=True)
    write_record(db, alice, RecordIn(id=team_rec.id, status="archived"), change_source="ui")
    assert orgs.delete_team(db, P(db, w["admin"]), w["a"], purge_archived=True) == 1
    assert db.exec(select(TeamMember).where(TeamMember.team_id == w["a"].id)).all() == []
    assert db.exec(select(Team).where(Team.slug == "team-a")).first() is None
    assert db.get(User, w["bob"].id) is not None  # people outlive their teams


def test_purging_a_project_takes_its_proposals_and_keeps_inbox_items(db, world):
    from acm_hub import proposals as pr
    from acm_hub.models import InboxItem

    w = world
    alice = P(db, w["alice"])
    proj = orgs.create_project(db, alice, "alpha", visibility="team", team=w["a"])
    r1 = write_record(
        db,
        alice,
        RecordIn(name="one-rec", description="d", type="project", scope="project", project="alpha"),
        change_source="ui",
    ).record
    prop = pr.create_proposal(db, alice, "mark_stale", {"record": r1.id}, rationale="old")
    db.add(
        InboxItem(
            owner_user_id=w["alice"].id, source="ui", title="captured", scope="project", project_id=proj.id
        )
    )
    db.commit()
    write_record(db, alice, RecordIn(id=r1.id, status="archived"), change_source="ui")
    orgs.delete_project(db, alice, proj, purge_archived=True)
    db.commit()
    assert db.get(pr.Proposal, prop.id) is None  # a proposal about deleted records can't dangle
    item = db.exec(select(InboxItem)).one()
    assert item.title == "captured" and item.project_id is None


# --- accounts --------------------------------------------------------------------------------------------------------


def test_user_creation_and_validation(db, world):
    admin = P(db, world["admin"])
    u = orgs.create_user(db, admin, "New@Example.com", password="correct horse battery staple")
    assert u.email == "new@example.com" and u.password_hash.startswith("$argon2id$")
    sso = orgs.create_user(db, admin, "sso@example.com", sso_invite=True)
    assert sso.auth_provider == "oidc" and sso.password_hash is None and sso.external_id is None
    for kw in (
        dict(email="bad", password="correct horse battery"),
        dict(email="x@example.com", password="short"),
        dict(email="x@example.com"),
        dict(email="new@example.com", password="correct horse battery staple"),
    ):
        with pytest.raises(ValidationFailed):
            orgs.create_user(db, admin, **kw)
    with pytest.raises(AccessError):
        orgs.create_user(db, P(db, world["alice"]), "y@example.com", password="correct horse battery staple")


def test_deactivation_cuts_every_way_in(db, world):
    from acm_hub.auth import mint_token

    w = world
    admin = P(db, w["admin"])
    mint_token(
        db,
        w["bob"],
        label="ci",
        project_ids=[],
        access_level="read_write",
        expires_days=30,
        include_user_scope=False,
    )
    db.add(WebSession(id="s1", user_id=w["bob"].id, csrf_token="c", expires_at=utcnow().replace(year=2099)))
    db.add(PluginInstance(plugin_key="webhook", name="bobs", owner_user_id=w["bob"].id, enabled=True))
    db.commit()
    orgs.set_user_active(db, admin, w["bob"], False)
    db.commit()
    assert db.exec(select(WebSession).where(WebSession.user_id == w["bob"].id)).all() == []
    assert all(
        t.revoked_at is not None
        for t in db.exec(select(ApiToken).where(ApiToken.user_id == w["bob"].id)).all()
    )
    assert (
        db.exec(select(PluginInstance).where(PluginInstance.owner_user_id == w["bob"].id)).one().enabled
        is False
    )
    orgs.set_user_active(db, admin, w["bob"], True)  # reactivating restores sign-in, not the revoked tokens
    assert all(
        t.revoked_at is not None
        for t in db.exec(select(ApiToken).where(ApiToken.user_id == w["bob"].id)).all()
    )


def test_you_cant_lock_the_instance_out_of_admin(db, world):
    w = world
    admin = P(db, w["admin"])
    with pytest.raises(ValidationFailed, match="your own account"):
        orgs.set_user_active(db, admin, w["admin"], False)
    with pytest.raises(ValidationFailed, match="last active admin"):
        orgs.set_user_admin(db, admin, w["admin"], False)
    orgs.set_user_admin(db, admin, w["alice"], True)
    orgs.set_user_admin(db, admin, w["admin"], False)  # fine now: alice is also an admin
    with pytest.raises(ValidationFailed, match="last active admin"):
        orgs.set_user_admin(db, P(db, w["alice"]), w["alice"], False)  # alice is now the only admin left


def test_reset_password_signs_the_user_out_and_is_local_only(db, world):
    from acm_hub.security import verify_password

    w = world
    local = orgs.create_user(
        db, P(db, w["admin"]), "local@example.com", password="correct horse battery staple"
    )
    db.add(WebSession(id="s2", user_id=local.id, csrf_token="c", expires_at=utcnow().replace(year=2099)))
    db.commit()
    pw = orgs.reset_password(db, P(db, w["admin"]), local)
    assert verify_password(local.password_hash, pw) and not verify_password(
        local.password_hash, "correct horse battery staple"
    )
    assert db.exec(select(WebSession).where(WebSession.user_id == local.id)).all() == []
    sso = orgs.create_user(db, P(db, w["admin"]), "sso@example.com", sso_invite=True)
    with pytest.raises(ValidationFailed, match="single sign-on"):
        orgs.reset_password(db, P(db, w["admin"]), sso)
    with pytest.raises(AccessError):
        orgs.reset_password(db, P(db, w["alice"]), local)
