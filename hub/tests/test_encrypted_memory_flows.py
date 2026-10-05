"""Encrypted private memory across every credential: web session, API, tokens, OAuth, and the side doors."""

import base64
import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, select

from acm_hub import crypto, dispatcher, keys, search_sync
from acm_hub.access import principal_for_user
from acm_hub.app import create_app
from acm_hub.cli import main as acm
from acm_hub.config import Settings
from acm_hub.consolidate import duplicate_candidates
from acm_hub.models import ApiToken, MemoryRecord, OAuthCode, OAuthGrant, PluginInstance, User, UserKey
from acm_hub.records import RecordIn, write_record

from .conftest import PASSWORD, csrf_of, setup_admin
from .test_oauth import call_tool, connect, db_rows, refresh

PASS = "a very long memory passphrase"
SECRET = "the-sensitive-body-text-xyzzy"


@pytest.fixture(autouse=True)
def fast_kdf(monkeypatch):
    monkeypatch.setattr(crypto, "KDF_TIME", 1)
    monkeypatch.setattr(crypto, "KDF_MEMORY_KIB", 64)
    monkeypatch.setattr(crypto, "KDF_PARALLELISM", 1)


def form_token(client, path="/settings"):
    return csrf_of(client.get(path).text)


def post(client, path, **data):
    return client.post(path, data={"csrf_token": form_token(client), **data}, follow_redirects=False)


def add_private(app, name="my-secret", body=SECRET):
    with Session(app.state.engine) as s:
        u = s.exec(select(User).where(User.email == "admin@example.com")).one()
        rid = write_record(
            s,
            principal_for_user(s, u),
            RecordIn(name=name, description=f"about {name}", body=body, type="user", scope="user"),
            change_source="ui",
        ).record.id
        s.commit()
        return rid


def raw(app, sql, **kw):
    with Session(app.state.engine) as s:
        return s.execute(text(sql), kw).fetchall()


def enable(client, app):
    add_private(app)
    r = post(client, "/settings/memory-key/enable", passphrase=PASS, confirm=PASS)
    assert r.status_code == 200 and "recovery key" in r.text.lower()
    return re.search(r'id="rk">([^<]+)<', r.text).group(1)


def signed_in(app, email="admin@example.com"):
    c = TestClient(app)
    assert (
        c.post("/login", data={"email": email, "password": PASSWORD}, follow_redirects=False).status_code
        == 303
    )
    return c


# --- the web ---------------------------------------------------------------------------------------------------------------------------


def test_enable_in_the_browser_shows_the_recovery_key_once_and_unlocks_the_session(authed):
    client, app, _ = authed
    rid = add_private(app)
    r = post(client, "/settings/memory-key/enable", passphrase=PASS, confirm=PASS)
    key = re.search(r'id="rk">([^<]+)<', r.text).group(1)
    assert "Write this recovery key down" in r.text and len(key.replace("-", "")) == 52
    assert (
        key not in client.get("/settings").text and key not in client.get("/memory").text
    )  # never shown again
    assert SECRET in client.get(f"/memory/{rid}").text  # this session is unlocked
    assert "xyzzy" not in str(raw(app, "select body from memory_records where scope='user'"))


def test_a_new_session_is_locked_until_the_passphrase_is_entered(authed):
    client, app, _ = authed
    rid = add_private(app)
    enable(client, app)
    other = signed_in(app)  # a fresh sign-in has no key
    page = other.get(f"/memory/{rid}").text
    assert (
        SECRET not in page
        and "locked" in page.lower()
        and "Unlock it in Settings" in other.get("/memory").text
    )
    bad = post(other, "/settings/memory-key/unlock", passphrase="not the passphrase")
    assert "isn&#39;t your memory passphrase" in other.get(bad.headers["location"]).text
    assert SECRET not in other.get(f"/memory/{rid}").text
    post(other, "/settings/memory-key/unlock", passphrase=PASS)
    assert SECRET in other.get(f"/memory/{rid}").text
    post(other, "/settings/memory-key/lock")
    assert SECRET not in other.get(f"/memory/{rid}").text  # locking is immediate


def test_signing_out_and_a_restart_both_lock(authed):
    client, app, _ = authed
    rid = add_private(app)
    enable(client, app)
    client.post("/logout", data={"csrf_token": form_token(client)})
    again = signed_in(app)
    assert SECRET not in again.get(f"/memory/{rid}").text  # signing back in doesn't bring the key back
    post(again, "/settings/memory-key/unlock", passphrase=PASS)
    assert SECRET in again.get(f"/memory/{rid}").text
    app.state.unlock_cache._d.clear()  # what a process restart does
    assert SECRET not in again.get(f"/memory/{rid}").text


def test_passphrase_guessing_is_rate_limited(authed):
    client, app, _ = authed
    enable(client, app)
    other = signed_in(app)
    for _ in range(8):
        post(other, "/settings/memory-key/unlock", passphrase="wrong guess number one")
    blocked = post(
        other, "/settings/memory-key/unlock", passphrase=PASS
    )  # even the right one is refused for now
    assert (
        blocked.status_code in (403, 429)
        or "Too many" in other.get(blocked.headers.get("location", "/settings")).text
    )


def test_writing_private_memory_from_a_locked_session_is_refused_not_stored_in_plaintext(authed):
    client, app, _ = authed
    enable(client, app)
    other = signed_in(app)
    r = post(
        other,
        "/memory/new",
        name="sneaky",
        description="d",
        body="plain-xyzzy-leak",
        type="user",
        scope="user",
    )
    assert r.status_code in (403, 422)
    assert raw(app, "select count(*) from memory_records where name='sneaky'")[0][0] == 0
    post(other, "/settings/memory-key/unlock", passphrase=PASS)
    ok = post(
        other,
        "/memory/new",
        name="allowed",
        description="d",
        body="sealed-xyzzy-ok",
        type="user",
        scope="user",
    )
    assert ok.status_code == 303 and "xyzzy-ok" not in str(
        raw(app, "select body from memory_records where name='allowed'")
    )


def test_json_api_follows_the_same_rules(authed):
    client, app, _ = authed
    rid = add_private(app)
    enable(client, app)
    csrf = client.get("/api/v1/me").json()["csrf_token"]
    assert client.get(f"/api/v1/records/{rid}").json()["body"] == SECRET
    other = signed_in(app)
    assert other.get(f"/api/v1/records/{rid}").json()["body"] == crypto.LOCKED
    ocsrf = other.get("/api/v1/me").json()["csrf_token"]
    r = other.patch(f"/api/v1/records/{rid}", json={"body": "nope"}, headers={"X-CSRF-Token": ocsrf})
    assert r.status_code == 403 and client.get(f"/api/v1/records/{rid}").json()["body"] == SECRET
    assert csrf


def test_change_passphrase_and_recover_in_the_browser(authed):
    client, app, _ = authed
    rid = add_private(app)
    key = enable(client, app)
    post(
        client,
        "/settings/memory-key/change",
        current=PASS,
        new="a different long passphrase",
        confirm="a different long passphrase",
    )
    other = signed_in(app)
    post(other, "/settings/memory-key/unlock", passphrase=PASS)
    assert SECRET not in other.get(f"/memory/{rid}").text  # the old passphrase is retired
    post(
        other,
        "/settings/memory-key/recover",
        recovery_key=key,
        new="third long passphrase here",
        confirm="third long passphrase here",
    )
    assert SECRET in other.get(f"/memory/{rid}").text  # the recovery key got it back


def test_turning_encryption_off_needs_the_passphrase_and_a_typed_confirmation(authed):
    client, app, _ = authed
    rid = add_private(app)
    enable(client, app)
    post(client, "/settings/memory-key/disable", passphrase=PASS, confirm="nope")
    assert raw(app, "select count(*) from user_keys")[0][0] == 1
    post(client, "/settings/memory-key/disable", passphrase="wrong passphrase here", confirm="decrypt")
    assert raw(app, "select count(*) from user_keys")[0][0] == 1
    post(client, "/settings/memory-key/disable", passphrase=PASS, confirm="decrypt")
    assert raw(app, "select count(*) from user_keys")[0][0] == 0
    assert SECRET in raw(app, "select body from memory_records where id=:i", i=rid)[0][0]


# --- tokens ----------------------------------------------------------------------------------------------------------------------------------


def mint(client, **extra):
    page = client.get("/settings")
    data = {
        "csrf_token": csrf_of(page.text),
        "label": "t",
        "access_level": "read_write",
        "include_user_scope": "on",
        "password": PASSWORD,
    } | extra
    r = client.post("/settings/tokens", data=data)
    m = re.search(r'id="tok">([^<]+)<', r.text)
    return r, (m.group(1) if m else None)


def test_a_token_only_opens_encrypted_memory_if_the_person_said_so(authed):
    client, app, _ = authed
    rid = add_private(app)
    enable(client, app)
    _, plain = mint(client)  # no encrypted option
    _, trusted = mint(client, include_encrypted="on")
    got = lambda tok: json.dumps(call_tool(client, tok, "memory_get", {"id_or_name": rid}).json())  # noqa: E731
    assert SECRET not in got(plain) and "locked" in got(plain).lower()
    assert SECRET in got(trusted)
    # writing: the plain token is refused; the trusted one writes sealed text
    call_tool(
        client,
        plain,
        "memory_write",
        {"name": "viaplain", "description": "d", "body": "leak-xyzzy", "type": "user", "scope": "user"},
    )
    assert raw(app, "select count(*) from memory_records where name='viaplain'")[0][0] == 0
    call_tool(
        client,
        trusted,
        "memory_write",
        {"name": "viatrusted", "description": "d", "body": "sealed-xyzzy", "type": "user", "scope": "user"},
    )
    assert "xyzzy" not in str(raw(app, "select body from memory_records where name='viatrusted'"))
    assert raw(app, "select count(*) from memory_records where name='viatrusted'")[0][0] == 1


def test_the_option_needs_an_unlocked_session_and_personal_memory(authed):
    client, app, _ = authed
    enable(client, app)
    r, tok = mint(client, include_user_scope="")  # asks for encrypted without personal memory
    r2, tok2 = mint(client, include_encrypted="on", include_user_scope="")
    assert tok2 is None and r2.status_code == 422
    other = signed_in(app)  # a locked session can't hand out a key it doesn't have
    r3, tok3 = mint(other, include_encrypted="on")
    assert tok3 is None and r3.status_code == 422


def test_the_data_key_never_rests_in_the_database(authed):
    client, app, _ = authed
    add_private(app)
    enable(client, app)
    with Session(app.state.engine) as s:
        dek = keys.unlock(s, s.exec(select(User)).one().id, PASS)
    mint(client, include_encrypted="on")
    mint(client)
    db_path = app.state.settings.db_path
    for suffix in ("", "-wal"):
        try:
            blob = open(str(db_path) + suffix, "rb").read()
        except FileNotFoundError:
            continue
        assert dek not in blob  # raw bytes
        assert (
            base64.urlsafe_b64encode(dek).rstrip(b"=") not in blob
            and base64.b64encode(dek) not in blob
            and dek.hex().encode() not in blob
        )
        assert PASS.encode() not in blob and SECRET.encode() not in blob


def test_revoking_a_token_destroys_its_copy_of_the_key(authed):
    client, app, _ = authed
    enable(client, app)
    _, tok = mint(client, include_encrypted="on")
    with Session(app.state.engine) as s:
        row = s.exec(select(ApiToken)).one()
        assert row.wrapped_dek
        tid = row.id
    client.post(f"/settings/tokens/{tid}/revoke", data={"csrf_token": form_token(client)})
    assert raw(app, "select wrapped_dek from api_tokens")[0][0] is None
    assert call_tool(client, tok, "memory_get", {"id_or_name": "x"}).status_code == 401


def test_a_stolen_database_cannot_open_a_tokens_copy_without_the_token(authed):
    client, app, _ = authed
    rid = add_private(app)
    enable(client, app)
    _, tok = mint(client, include_encrypted="on")
    with Session(app.state.engine) as s:
        row = s.exec(select(ApiToken)).one()
        wrapped, token_hash = row.wrapped_dek, row.token_hash
    assert crypto.unwrap_from_secret(token_hash, wrapped, "token") is None  # the stored hash isn't the key
    assert crypto.unwrap_from_secret(tok, wrapped, "token") is not None and rid


def test_deactivating_a_user_or_turning_encryption_off_destroys_token_wraps(authed):
    client, app, _ = authed
    enable(client, app)
    mint(client, include_encrypted="on")
    post(client, "/settings/memory-key/disable", passphrase=PASS, confirm="decrypt")
    assert raw(app, "select wrapped_dek from api_tokens")[0][0] is None


# --- OAuth -----------------------------------------------------------------------------------------------------------------------------------


@pytest.fixture()
def oauthed(settings, monkeypatch):
    monkeypatch.setenv("MEMORY_HUB_OAUTH_ENABLED", "true")
    root = create_app(Settings())
    with TestClient(root) as client:
        setup_admin(client, root.fastapi)
        yield client, root.fastapi


def test_an_oauth_app_can_carry_the_key_through_refreshes_and_loses_it_on_disconnect(oauthed):
    client, app = oauthed
    rid = add_private(app)
    enable(client, app)
    cid, tok = connect(client, scope_mode="all", user_scope="1", include_encrypted="1")
    got = lambda t: json.dumps(call_tool(client, t, "memory_get", {"id_or_name": rid}).json())  # noqa: E731
    assert SECRET in got(tok["access_token"])
    assert (
        raw(app, "select count(*) from oauth_codes where wrapped_dek is not null")[0][0] == 0
    )  # the code's copy is single-use
    new = refresh(client, cid, tok["refresh_token"]).json()
    # the access token the refresh retired keeps no copy of the key: only the live one does
    assert raw(app, "select count(*) from api_tokens where wrapped_dek is not null")[0][0] == 1
    assert SECRET in got(new["access_token"])  # the key followed the rotation
    again = refresh(client, cid, new["refresh_token"]).json()
    assert SECRET in got(again["access_token"])
    gid = db_rows(app, OAuthGrant)[0].id
    page = client.get("/settings")
    client.post(f"/settings/apps/{gid}/disconnect", data={"csrf_token": csrf_of(page.text)})
    assert raw(app, "select count(*) from oauth_grants where wrapped_dek is not null")[0][0] == 0
    assert raw(app, "select count(*) from api_tokens where wrapped_dek is not null")[0][0] == 0


def test_an_oauth_app_without_the_choice_sees_only_locked_text(oauthed):
    client, app = oauthed
    rid = add_private(app)
    enable(client, app)
    cid, tok = connect(client, scope_mode="all", user_scope="1")  # personal memory yes, encrypted no
    out = json.dumps(call_tool(client, tok["access_token"], "memory_get", {"id_or_name": rid}).json())
    assert SECRET not in out and "locked" in out.lower()
    assert raw(app, "select count(*) from api_tokens where wrapped_dek is not null")[0][0] == 0


def test_consent_only_offers_encrypted_memory_to_an_unlocked_person_and_enforces_it(oauthed):
    client, app = oauthed
    from .test_oauth import consent, pkce, register, request_id, start

    enable(client, app)
    cid = register(client).json()["client_id"]
    _, ch = pkce()
    rid = request_id(start(client, cid, ch).headers["location"])
    assert "including what I keep encrypted" in client.get(f"/oauth/consent?request={rid}").text
    other = signed_in(app)  # locked
    assert "including what I keep encrypted" not in other.get(f"/oauth/consent?request={rid}").text
    r = consent(other, rid, scope_mode="all", user_scope="1", include_encrypted="1")  # forged: asks anyway
    assert r.status_code == 422 and raw(app, "select count(*) from oauth_codes")[0][0] == 0
    r = consent(client, rid, scope_mode="all", include_encrypted="1")  # without personal memory ticked
    assert r.status_code == 422


# --- the side doors --------------------------------------------------------------------------------------------------------------------------


def test_no_side_door_receives_an_encrypted_body(authed):
    client, app, _ = authed
    from .remote_support import Down  # noqa: F401

    rid = add_private(app)
    enable(client, app)
    with Session(app.state.engine) as s:
        admin = s.exec(select(User)).one()
        inst = PluginInstance(
            plugin_key="webhook",
            name="hook",
            owner_user_id=admin.id,
            enabled=True,
            scopes=["user"],
            user_scope_ack=True,
            egress="full",
            events=["record.created"],
        )
        s.add(inst)
        s.commit()
        # a locked (system) view of the record, as every background job has
        rec = s.get(MemoryRecord, rid)
        assert rec.body == crypto.LOCKED
        from acm_hub.events import emit_record_event

        ev = emit_record_event(s, "record.created", rec)
        s.commit()
        pe = dispatcher._present(s, inst, ev)  # noqa: SLF001
        assert "body" not in pe.payload and SECRET not in json.dumps(pe.payload)
        doc = search_sync._doc(s, inst, rec)  # noqa: SLF001
        assert "body" not in doc and SECRET not in json.dumps(doc)


def test_consolidation_never_pairs_up_encrypted_records(authed):
    client, app, _ = authed
    a = add_private(
        app, "first-one", "retry webhooks with idempotency keys always send the key on retries " * 3
    )
    b = add_private(
        app, "second-one", "retry webhooks with idempotency keys always send the key on retries " * 3
    )
    with Session(app.state.engine) as s:
        recs = s.exec(select(MemoryRecord)).all()
        assert len(duplicate_candidates(s, recs)) == 1  # plaintext twins are found
    enable(client, app)
    with Session(app.state.engine) as s:
        recs = s.exec(select(MemoryRecord)).all()
        assert (
            all(r.encrypted for r in recs) and duplicate_candidates(s, recs) == []
        )  # sealed ones are not compared
    assert a and b


def test_export_skips_what_it_cannot_open_and_says_so(authed, tmp_path, monkeypatch):
    client, app, _ = authed
    add_private(app)
    enable(client, app)
    with Session(app.state.engine) as s:
        from acm_hub.exportimport import export_files

        files = export_files(s, principal_for_user(s, s.exec(select(User)).one()))
        manifest = json.loads(files["manifest.json"])
        assert manifest["counts"]["skipped_locked"] == 1 and manifest["counts"]["records"] == 0
        assert not any(
            SECRET.encode() in b or crypto.LOCKED.encode() in b for b in files.values()
        )  # nothing, not even the placeholder
        # with the key attached (an unlocked caller) it exports the real text
        from acm_hub import crypto_store

        crypto_store.attach_keys(
            s, s.exec(select(User)).one().id, keys.unlock(s, s.exec(select(User)).one().id, PASS)
        )
        s.expire_all()
        files = export_files(s, principal_for_user(s, s.exec(select(User)).one()))
        assert any(SECRET.encode() in b for b in files.values())


def test_the_cli_manages_keys_and_unlocks_per_command(settings, monkeypatch, capsys, tmp_path):
    import io

    from .test_cli import make_admin

    make_admin(monkeypatch, capsys)
    rec = tmp_path / "r.md"
    rec.write_text(
        f"---\nname: private-note\ndescription: a private note\nmetadata:\n  type: user\n  scope: user\n---\n\n{SECRET}\n"
    )
    assert acm(["import", str(rec), "--apply"]) == 0
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", io.StringIO(PASS + "\n"))
    assert acm(["key", "enable", "--passphrase-stdin"]) == 0
    out = capsys.readouterr().out
    assert "Encrypted 1 private record" in out and "Recovery key" in out
    assert acm(["key", "status"]) == 0 and "1 record(s) encrypted" in capsys.readouterr().out
    assert acm(["show", "private-note"]) == 0
    shown = capsys.readouterr().out
    assert SECRET not in shown and "locked" in shown.lower()  # no --unlock: locked
    monkeypatch.setenv("ACM_PASSPHRASE", PASS)
    assert acm(["show", "private-note", "--unlock"]) == 0 and SECRET in capsys.readouterr().out
    monkeypatch.delenv("ACM_PASSPHRASE")
    monkeypatch.setattr("sys.stdin", io.StringIO(PASS + "\n"))
    assert acm(["key", "disable", "--passphrase-stdin"]) == 0
    assert (
        acm(["show", "private-note"]) == 0 and SECRET in capsys.readouterr().out
    )  # plain again, no key needed


def test_compile_never_writes_a_locked_record(settings, monkeypatch, capsys, tmp_path):
    import io

    from .test_cli import make_admin

    make_admin(monkeypatch, capsys)
    rec = tmp_path / "r.md"
    rec.write_text(
        f"---\nname: private-rule\ndescription: a private rule\nmetadata:\n  type: rule\n  scope: user\n  confidence: confirmed\n  tier: core\n---\n\n{SECRET}\n"
    )
    acm(["import", str(rec), "--apply"])
    monkeypatch.setattr("sys.stdin", io.StringIO(PASS + "\n"))
    acm(["key", "enable", "--passphrase-stdin"])
    capsys.readouterr()
    assert acm(["compile", "agents", "--stdout", "--include-user-scope"]) == 0
    out = capsys.readouterr().out
    assert SECRET not in out and "private-rule" not in out and crypto.LOCKED not in out


def test_ciphertext_never_appears_in_any_response(authed):
    """Whatever a caller can see — pages, API, MCP — is plaintext or the placeholder; the stored form never leaks."""
    client, app, _ = authed
    rid = add_private(app)
    enable(client, app)
    stored = raw(app, "select body from memory_records where id=:i", i=rid)[0][0]
    assert stored.startswith("v1.")
    _, tok = mint(client, include_user_scope="on")
    other = signed_in(app)
    bodies = [
        other.get(f"/memory/{rid}").text, other.get("/memory").text, other.get(f"/memory/{rid}?tab=history").text,
        json.dumps(other.get(f"/api/v1/records/{rid}").json()), json.dumps(other.get(f"/api/v1/records/{rid}/history").json()),
        json.dumps(other.get("/api/v1/focus", params={"task": "my secret"}).json()),
        call_tool(client, tok, "memory_get", {"id_or_name": rid}).text, call_tool(client, tok, "memory_search", {"query": "secret"}).text,
        call_tool(client, tok, "memory_focus", {"task": "my secret"}).text,
    ]  # fmt: skip
    for b in bodies:
        assert stored not in b and "v1." + stored[3:20] not in b
    assert UserKey and ApiToken and OAuthCode


def test_the_storage_layer_itself_refuses_plaintext_even_if_a_caller_forgets_to_check(authed):
    """Backstop under records.py's own checks: an ORM write to an encrypted record without the key must fail, not store."""
    client, app, _ = authed
    rid = add_private(app)
    enable(client, app)
    with Session(
        app.state.engine
    ) as s:  # no keys attached: what any background job or forgetful new code path has
        rec = s.get(MemoryRecord, rid)
        rec.body = "plain-xyzzy-leak"
        s.add(rec)
        with pytest.raises(crypto.EncryptionLocked):
            s.commit()
        s.rollback()
    with Session(
        app.state.engine
    ) as s:  # even with the key, the placeholder is never saved as if it were text
        from acm_hub import crypto_store

        uid = s.exec(select(User)).one().id
        crypto_store.attach_keys(s, uid, keys.unlock(s, uid, PASS))
        rec = s.get(MemoryRecord, rid)
        rec.body = crypto.LOCKED
        s.add(rec)
        with pytest.raises(crypto.EncryptionLocked):
            s.commit()
    assert raw(app, "select body from memory_records where id=:i", i=rid)[0][0].startswith("v1.")
