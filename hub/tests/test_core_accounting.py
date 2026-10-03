"""The core budget bounds what ONE session loads: user + team core plus the core of the project in play."""

import pytest
from sqlmodel import select

from acm_hub.consolidate import run_consolidation
from acm_hub.models import InstanceSettings, Proposal
from acm_hub.records import RecordIn, ValidationFailed, core_load, write_record

BUDGET = 100


def core(db, p, name, scope="project", project=None, size=200):
    return write_record(
        db,
        p,
        RecordIn(
            name=name,
            description=f"{name} d",
            body="x" * size,
            type="rule" if scope != "user" else "user",
            scope=scope,
            project=project,
            tier="core",
            confidence="confirmed",
        ),
        change_source="ui",
    ).record


@pytest.fixture()
def budgeted(db, human):
    db.get(InstanceSettings, 1).core_token_budget = BUDGET
    db.commit()
    return db, human


def test_another_projects_core_does_not_block_this_one(budgeted):
    db, p = budgeted
    core(db, p, "alpha-core", project="alpha")  # ~50 tokens in alpha's sessions
    core(db, p, "beta-core", project="beta")  # a different project: never loaded alongside alpha's
    load = core_load(db, p)
    assert len(load.per_project) == 2
    assert load.worst <= BUDGET and load.worst < sum(load.per_project.values())


def test_a_project_is_still_bounded_by_its_own_sessions(budgeted):
    db, p = budgeted
    core(db, p, "alpha-1", project="alpha")
    with pytest.raises(ValidationFailed, match="per session.*this project"):
        core(db, p, "alpha-2", project="alpha")


def test_user_core_rides_in_every_session_so_the_heaviest_project_bounds_it(budgeted):
    db, p = budgeted
    core(db, p, "alpha-1", project="alpha")  # leaves alpha's sessions nearly full
    with pytest.raises(ValidationFailed, match="heaviest project"):
        core(db, p, "personal", scope="user")
    core(db, p, "tiny-personal", scope="user", size=4)  # something small still fits


def test_user_core_counts_against_every_project(budgeted):
    db, p = budgeted
    core(db, p, "personal", scope="user", size=200)  # fits alone, but sits in every session
    with pytest.raises(ValidationFailed, match="Core is limited"):
        core(db, p, "alpha-big", project="alpha")  # fits alone, not next to the user core
    core(db, p, "alpha-small", project="alpha", size=8)


def test_consolidation_relieves_each_overloaded_session_and_only_those(budgeted):
    db, p = budgeted
    a = core(db, p, "alpha-core", project="alpha", size=250)
    b = core(db, p, "beta-core", project="beta", size=250)
    ok = core(db, p, "gamma-core", project="gamma", size=4)  # small: its sessions are fine
    db.get(
        InstanceSettings, 1
    ).core_token_budget = 50  # lowered after the fact: alpha and beta alone are now over
    db.commit()
    run_consolidation(db)
    targets = {
        t
        for pr in db.exec(select(Proposal).where(Proposal.kind == "demote_core")).all()
        for t in pr.target_ids
    }
    assert targets == {a.id, b.id}  # both overloaded sessions get relief; the healthy one is left alone
    assert ok.id not in targets


def test_a_demotion_of_shared_core_relieves_every_session_at_once(budgeted):
    db, p = budgeted
    personal = core(db, p, "personal", scope="user", size=200)
    core(db, p, "alpha-core", project="alpha", size=4)
    core(db, p, "beta-core", project="beta", size=4)
    db.get(
        InstanceSettings, 1
    ).core_token_budget = 60  # personal alone nearly fills it; each project tips it over
    db.commit()
    run_consolidation(db)
    targets = [
        t
        for pr in db.exec(select(Proposal).where(Proposal.kind == "demote_core")).all()
        for t in pr.target_ids
    ]
    assert targets == [personal.id]  # one proposal fixes both projects' sessions; no pointless second one
