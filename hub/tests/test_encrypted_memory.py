"""Encrypted private memory: what is on disk, who can read it, and what refuses to write it."""

import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from acm_hub import crypto, crypto_store, keys
from acm_hub.access import AccessError, Principal, principal_for_user, system_principal
from acm_hub.models import MemoryRecord, MemoryRevision, UserKey
from acm_hub.records import RecordIn, ValidationFailed, get_record, list_records, write_record

from .conftest import make_user

PASSPHRASE = "a very long memory passphrase"
SECRET = "the-sensitive-body-text-xyzzy"


@pytest.fixture(autouse=True)
def fast_kdf(monkeypatch):
    monkeypatch.setattr(crypto, "KDF_TIME", 1)
    monkeypatch.setattr(crypto, "KDF_MEMORY_KIB", 64)
    monkeypatch.setattr(crypto, "KDF_PARALLELISM", 1)


def private(db, p, name="my-secret", body=SECRET, **kw):
    return write_record(
        db,
        p,
        RecordIn(name=name, description=f"about {name}", body=body, type="user", scope="user", **kw),
        change_source="ui",
    ).record


def raw(db, sql, **params):
    return db.execute(text(sql), params).fetchall()


def fresh(engine, user_id=None, dek=None):
    """A new session as another request would have it: keyed only if the caller is given the key."""
    s = Session(engine)
    crypto_store.attach_keys(s, user_id, dek)
    return s


@pytest.fixture()
def enabled(db, user, human):
    """A user with one pre-existing private record, then encryption switched on. -> (dek, record id)."""
    rec = private(db, human)
    db.commit()
    res = keys.enable(db, user, PASSPHRASE)
    db.commit()
    return res, rec.id


# --- enabling ------------------------------------------------------------------------------------------------------------------


def test_enabling_seals_existing_private_memory_everywhere_it_rests(db, user, human):
    private(db, human, "one")
    p2 = private(db, human, "two", body="second " + SECRET)
    write_record(
        db, human, RecordIn(id=p2.id, body="second revised " + SECRET), change_source="ui"
    )  # a revision too
    shared = write_record(
        db,
        human,
        RecordIn(
            name="proj", description="d", body="PROJECT-BODY", type="project", scope="project", project="p"
        ),
        change_source="ui",
    ).record
    db.commit()
    assert SECRET in str(raw(db, "select body from memory_records")) and "xyzzy" in str(
        raw(db, "select body from memory_fts")
    )
    res = keys.enable(db, user, PASSPHRASE)
    db.commit()
    assert res.records == 2 and len(res.recovery_key.replace("-", "")) == 52
    assert "xyzzy" not in str(raw(db, "select body from memory_records where scope='user'"))  # the records
    assert "xyzzy" not in str(raw(db, "select body from memory_revisions"))  # their history
    assert "xyzzy" not in str(raw(db, "select * from memory_fts"))  # the search index
    assert (
        raw(db, "select body from memory_records where id=:i", i=shared.id)[0][0] == "PROJECT-BODY"
    )  # other scopes untouched
    assert all(r[0] for r in raw(db, "select encrypted from memory_records where scope='user'"))
    assert {
        r[0] for r in raw(db, "select encrypted from memory_revisions where memory_record_id=:i", i=p2.id)
    } == {1}


def test_with_the_key_it_reads_normally_and_without_it_it_is_locked(db, user, enabled, engine):
    res, rid = enabled
    with fresh(engine, user.id, res.dek) as s:
        assert s.get(MemoryRecord, rid).body == SECRET
    with fresh(engine) as s:
        rec = s.get(MemoryRecord, rid)
        assert rec.body == crypto.LOCKED and rec.encrypted and crypto_store.is_locked(rec)
    with fresh(
        engine, user.id, crypto.new_key()
    ) as s:  # the wrong key is just "locked", never an error or garbage
        assert s.get(MemoryRecord, rid).body == crypto.LOCKED


def test_ciphertext_is_bound_to_its_row(db, user, human, engine):
    a, b = private(db, human, "a", body="AAA-body"), private(db, human, "b", body="BBB-body")
    db.commit()
    res = keys.enable(db, user, PASSPHRASE)
    db.commit()
    ca = raw(db, "select body from memory_records where id=:i", i=a.id)[0][0]
    db.execute(
        text("update memory_records set body=:c where id=:i"), {"c": ca, "i": b.id}
    )  # move a's ciphertext onto b
    db.commit()
    with fresh(engine, user.id, res.dek) as s:
        assert s.get(MemoryRecord, b.id).body == crypto.LOCKED  # authenticated data: a copy can't pass as b
        assert s.get(MemoryRecord, a.id).body == "AAA-body"


def test_tampered_ciphertext_is_locked_not_a_crash(db, user, enabled, engine):
    res, rid = enabled
    c = raw(db, "select body from memory_records where id=:i", i=rid)[0][0]
    db.execute(text("update memory_records set body=:c where id=:i"), {"c": c[:-4] + "AAAA", "i": rid})
    db.commit()
    with fresh(engine, user.id, res.dek) as s:
        assert s.get(MemoryRecord, rid).body == crypto.LOCKED


def test_enabling_twice_or_with_a_weak_passphrase_is_refused(db, user):
    with pytest.raises(ValidationFailed, match="at least"):
        keys.enable(db, user, "short")
    keys.enable(db, user, PASSPHRASE)
    with pytest.raises(ValidationFailed, match="already on"):
        keys.enable(db, user, PASSPHRASE)


# --- writing -----------------------------------------------------------------------------------------------------------------------


def test_new_private_records_are_sealed_and_only_a_keyed_credential_may_write_them(db, user, enabled, engine):
    res, _ = enabled
    with fresh(engine, user.id, res.dek) as s:
        p = principal_for_user(s, user)
        made = private(s, p, "new-one", body="written-while-unlocked-" + SECRET)
        s.commit()
        assert made.encrypted and made.body.startswith(
            "written-while-unlocked"
        )  # the caller still sees plaintext
        assert "unlocked" not in raw(s, "select body from memory_records where id=:i", i=made.id)[0][0]
    with fresh(
        engine, user.id
    ) as s:  # a credential without the key: refused, never a silent plaintext fallback
        p = principal_for_user(s, user)
        with pytest.raises(AccessError, match="encrypted private memory"):
            private(s, p, "sneaky", body="would-be-plaintext-xyzzy")
        s.rollback()
        assert raw(s, "select count(*) from memory_records where name='sneaky'")[0][0] == 0
    with fresh(engine, user.id) as s:  # a token that never got the key is the same
        tok = Principal(user_id=user.id, kind="token", token_include_user_scope=True)
        with pytest.raises(AccessError):
            write_record(
                s,
                tok,
                RecordIn(name="viatoken", description="d", body="x", type="user", scope="user"),
                change_source="mcp-write",
            )


def test_updates_reseal_and_revisions_follow(db, user, enabled, engine):
    res, rid = enabled
    with fresh(engine, user.id, res.dek) as s:
        p = principal_for_user(s, user)
        write_record(s, p, RecordIn(id=rid, body="edited " + SECRET), change_source="ui")
        s.commit()
        assert "edited" not in str(raw(s, "select body from memory_records where id=:i", i=rid))
        assert "edited" not in str(
            raw(s, "select body from memory_revisions where memory_record_id=:i", i=rid)
        )
        revs = s.exec(select(MemoryRevision).where(MemoryRevision.memory_record_id == rid)).all()
        assert {r.body for r in revs} >= {SECRET, "edited " + SECRET}  # history is readable with the key
    with fresh(engine, user.id) as s:
        p = principal_for_user(s, user)
        with pytest.raises(AccessError):
            write_record(
                s, p, RecordIn(id=rid, body="locked-edit"), change_source="ui"
            )  # needs the key to change a body


def test_a_locked_credential_can_still_change_non_text_fields_without_damaging_the_body(
    db, user, enabled, engine
):
    """Consolidation (no key) archives/stales records; that must neither fail nor touch the encrypted body."""
    res, rid = enabled
    with fresh(engine) as s:
        write_record(
            s, system_principal(), RecordIn(id=rid, status="stale"), change_source="mechanical", note="idle"
        )
        s.commit()
        assert s.get(MemoryRecord, rid).status == "stale"
        revs = s.exec(select(MemoryRevision).where(MemoryRevision.memory_record_id == rid)).all()
        assert all(
            r.body in ("", crypto.LOCKED) for r in revs if r.status == "stale"
        )  # nothing leaked into history
    with fresh(engine, user.id, res.dek) as s:
        assert s.get(MemoryRecord, rid).body == SECRET  # and the stored ciphertext is intact


def test_the_placeholder_can_never_be_written_over_a_record(db, user, enabled, engine):
    res, rid = enabled
    with fresh(engine, user.id, res.dek) as s:
        with pytest.raises(ValidationFailed, match="placeholder"):
            write_record(
                s, principal_for_user(s, user), RecordIn(id=rid, body=crypto.LOCKED), change_source="ui"
            )
        with pytest.raises(ValidationFailed, match="placeholder"):
            private(s, principal_for_user(s, user), "copy", body=crypto.LOCKED)


# --- the search index ----------------------------------------------------------------------------------------------------------------


def test_the_index_finds_encrypted_records_by_name_and_description_but_never_by_body(
    db, user, human, enabled, engine
):
    res, rid = enabled
    with fresh(engine, user.id, res.dek) as s:
        p = principal_for_user(s, user)
        assert [r.id for r in list_records(s, p, q="secret")] == [rid]  # the name has "secret" in it
        assert (
            list_records(s, p, q="xyzzy") == []
        )  # a body-only word isn't indexed: that is the stated trade-off


# --- passphrase, recovery, disabling -------------------------------------------------------------------------------------------------------


def test_unlock_checks_the_passphrase(db, user, enabled):
    res, _ = enabled
    assert keys.unlock(db, user.id, PASSPHRASE) == res.dek
    assert keys.unlock(db, user.id, "wrong passphrase here") is None and keys.unlock(db, user.id, "") is None
    assert keys.unlock(db, "nobody", PASSPHRASE) is None


def test_changing_the_passphrase_keeps_the_data_and_retires_the_old_one(db, user, enabled, engine):
    res, rid = enabled
    with pytest.raises(AccessError):
        keys.change_passphrase(db, user, "not the old one", "another long passphrase")
    keys.change_passphrase(db, user, PASSPHRASE, "another long passphrase")
    db.commit()
    assert keys.unlock(db, user.id, PASSPHRASE) is None
    dek = keys.unlock(db, user.id, "another long passphrase")
    assert dek == res.dek  # same data key: nothing had to be re-encrypted
    with fresh(engine, user.id, dek) as s:
        assert s.get(MemoryRecord, rid).body == SECRET


def test_the_recovery_key_works_once_the_passphrase_is_forgotten(db, user, enabled, engine):
    res, rid = enabled
    with pytest.raises(AccessError):
        keys.recover(db, user, "AAAAAAAA-AAAAAAAA", "new long passphrase here")
    dek = keys.recover(db, user, res.recovery_key, "new long passphrase here")
    db.commit()
    assert dek == res.dek and keys.unlock(db, user.id, "new long passphrase here") == res.dek
    new_text = keys.regenerate_recovery_key(db, user, "new long passphrase here")
    db.commit()
    with pytest.raises(AccessError):
        keys.recover(db, user, res.recovery_key, "yet another passphrase")  # the old recovery key is retired
    assert keys.recover(db, user, new_text, "yet another passphrase") == res.dek


def test_disabling_returns_everything_to_plaintext_including_the_index(db, user, enabled, engine):
    res, rid = enabled
    with pytest.raises(AccessError):
        keys.disable(db, user, "wrong passphrase here")
    assert keys.disable(db, user, PASSPHRASE) == 1
    db.commit()
    assert db.get(UserKey, user.id) is None
    assert SECRET in raw(db, "select body from memory_records where id=:i", i=rid)[0][0]
    assert SECRET in str(raw(db, "select body from memory_revisions"))
    with fresh(engine) as s:  # no key needed any more
        assert s.get(MemoryRecord, rid).body == SECRET
        assert [r.id for r in list_records(s, principal_for_user(s, user), q="xyzzy")] == [
            rid
        ]  # searchable by body again


def test_a_second_user_is_unaffected(db, user, human, enabled, engine):
    other = make_user(db, "eve@example.com", admin=False)
    p = principal_for_user(db, other)
    rec = private(db, p, "eves", body="plain-eve-body")
    db.commit()
    assert (
        not rec.encrypted
        and raw(db, "select body from memory_records where id=:i", i=rec.id)[0][0] == "plain-eve-body"
    )
    res, rid = enabled
    with fresh(
        engine, other.id, crypto.new_key()
    ) as s:  # eve cannot read the first user's record even with a key
        assert get_record(s, principal_for_user(s, other), "eves").body == "plain-eve-body"


# --- the in-memory cache ---------------------------------------------------------------------------------------------------------------------


def test_unlock_cache_expires_is_per_owner_and_drops_on_demand():
    c = keys.UnlockCache(idle=0.2)
    c.put("sess", "u1", b"k" * 32)
    assert c.get("sess", "u1") == b"k" * 32 and c.get("sess", "someone-else") is None  # owner-checked
    c.put("sess", "u1", b"k" * 32)
    c.drop("sess")
    assert c.get("sess", "u1") is None
    c.put("a", "u1", b"k" * 32)
    c.put("b", "u1", b"k" * 32)
    c.drop_user("u1")
    assert c.get("a", "u1") is None and c.get("b", "u1") is None
    import time

    c.put("s", "u", b"k" * 32)
    time.sleep(0.3)
    assert c.get("s", "u") is None  # idle expiry
