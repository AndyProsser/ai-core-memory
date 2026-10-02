import json

import pytest
from sqlmodel import select

from acm_hub.access import AccessError, principal_for_user
from acm_hub.exportimport import export_files, import_files, read_zip, to_zip
from acm_hub.models import MemoryRecord, MemoryRevision
from acm_hub.records import RecordIn, list_records, write_record

from .conftest import make_user, token_principal


def seed(db, human):
    a = write_record(
        db,
        human,
        RecordIn(
            name="idempotent-retries",
            description="Retries must be idempotent",
            body="Use keys.",
            type="project",
            scope="project",
            project="payments",
            topics=["payments"],
            tier="core",
            confidence="established",
        ),
        change_source="ui",
    ).record
    write_record(
        db,
        human,
        RecordIn(
            name="incident-1042",
            description='Double charge: it\'s "bad": really',
            body="Replay.\n\n- bullet",
            type="project",
            scope="project",
            project="payments",
            links=[a.id],
        ),
        change_source="ui",
    )
    write_record(
        db,
        human,
        RecordIn(name="terse", description="likes terse replies", type="user", scope="user"),
        change_source="ui",
    )


def test_round_trip_into_fresh_instance_preserves_ids_links_and_is_idempotent(
    db, human, tmp_path, monkeypatch
):
    seed(db, human)
    files = export_files(db, human, with_history=True)
    manifest = json.loads(files["manifest.json"])
    assert manifest["counts"]["records"] == 3 and "project/payments/MEMORY.md" in files
    assert (
        "★ [idempotent-retries]" in files["project/payments/MEMORY.md"].decode()
    )  # core marked in the index
    assert any(k.startswith("_history/") for k in files)

    # Fresh instance
    from sqlmodel import Session

    from acm_hub.config import Settings
    from acm_hub.db import make_engine, migrate
    from acm_hub.models import InstanceSettings

    monkeypatch.setenv("MEMORY_HUB_DB_PATH", str(tmp_path / "fresh" / "hub.sqlite3"))
    eng = make_engine(Settings())
    migrate(eng)
    with Session(eng) as s2:
        s2.add(InstanceSettings(id=1))
        u2 = make_user(s2, "me@new.example")
        p2 = principal_for_user(s2, u2)
        report = import_files(s2, p2, read_zip(to_zip(files)), apply=True)
        s2.commit()
        assert report.summary["create"] == 3 and report.summary["error"] == 0, report.items
        names = {r.name for r in list_records(s2, p2)}
        assert names == {"idempotent-retries", "incident-1042", "terse"}
        orig = {r.name: r.id for r in db.exec(select(MemoryRecord)).all()}
        assert {r.name: r.id for r in s2.exec(select(MemoryRecord)).all()} == orig  # ids survive
        inc = s2.exec(select(MemoryRecord).where(MemoryRecord.name == "incident-1042")).one()
        from acm_hub.records import linked_records

        assert [r.name for r in linked_records(s2, p2, inc)] == ["idempotent-retries"]
        again = import_files(s2, p2, read_zip(to_zip(files)), apply=True)
        assert again.summary["unchanged"] == 3 and again.summary["create"] == 0  # idempotent


def test_dry_run_changes_nothing(db, human):
    seed(db, human)
    files = export_files(db, human)
    # edit the exported file to differ, then dry-run it
    files["project/payments/incident-1042.md"] = files["project/payments/incident-1042.md"].replace(
        b"Replay.", b"Replay, twice."
    )
    before = db.exec(select(MemoryRevision)).all()
    report = import_files(db, human, files, apply=False)
    assert report.summary["update"] == 1
    db.expire_all()
    assert (
        db.exec(select(MemoryRecord).where(MemoryRecord.name == "incident-1042"))
        .one()
        .body.startswith("Replay.\n")
    )
    assert len(db.exec(select(MemoryRevision)).all()) == len(before)


def test_established_conflict_is_parked_not_applied(db, human):
    seed(db, human)
    files = export_files(db, human)
    files["project/payments/idempotent-retries.md"] = files["project/payments/idempotent-retries.md"].replace(
        b"Use keys.", b"Do something else."
    )
    rep = import_files(db, human, files, apply=True)
    assert rep.summary["conflict"] == 1
    rec = db.exec(select(MemoryRecord).where(MemoryRecord.name == "idempotent-retries")).one()
    assert rec.body == "Use keys."
    pending = db.exec(select(MemoryRevision).where(MemoryRevision.applied == False)).all()  # noqa: E712
    assert len(pending) == 1 and pending[0].body == "Do something else." and pending[0].flagged


def test_restoring_an_older_backup_does_not_clobber_newer_edits(db, human):
    seed(db, human)
    old = export_files(db, human)
    import re

    old = {
        k: (re.sub(rb"updated: '?\d{4}", b"updated: '2019", v) if k.endswith(".md") else v)
        for k, v in old.items()
    }  # predates the edit
    write_record(
        db,
        human,
        RecordIn(
            name="terse", description="likes terse replies", body="newer edit", type="user", scope="user"
        ),
        change_source="ui",
    )
    rep = import_files(db, human, old, apply=True)
    terse = [i for i in rep.items if i.name == "terse"][0]
    assert terse.action == "conflict"
    assert db.exec(select(MemoryRecord).where(MemoryRecord.name == "terse")).one().body == "newer edit"


def test_import_rejects_tokens_bad_zips_and_garbage(db, human, user):
    with pytest.raises(AccessError):
        import_files(db, token_principal(user), {}, apply=False)
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.md", "x")
    from acm_hub.records import ValidationFailed

    with pytest.raises(ValidationFailed):
        read_zip(buf.getvalue())
    rep = import_files(
        db,
        human,
        {"project/x/broken.md": b"no frontmatter", "project/x/b2.md": b"---\n: : :\n---\n"},
        apply=True,
    )
    assert rep.summary["error"] == 2


def test_import_cannot_write_into_someone_elses_project(db, human):
    seed(db, human)
    files = export_files(db, human)
    intruder = principal_for_user(db, make_user(db, "x@example.com", admin=False))
    rep = import_files(db, intruder, files, apply=True)
    assert rep.summary["create"] == 1  # their own user-scope copy only ('terse' is user-scoped to *them* now)
    assert rep.summary["error"] == 2  # the payments records are not theirs
