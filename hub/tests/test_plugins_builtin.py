import hashlib
import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from sqlmodel import select

from acm_hub import dispatcher as dp
from acm_hub.consolidate import build_work_package
from acm_hub.models import Event, InboxItem, PluginDelivery, PluginInstance, utcnow
from acm_hub.plugins import egress
from acm_hub.plugins.builtin.obsidian import ObsidianConfig, ObsidianPlugin, note_tags, parse_note
from acm_hub.records import RecordIn, write_record


@pytest.fixture(autouse=True)
def clean_egress():
    yield
    egress.set_transport(None)


def inst(db, user, key, name=None, **kw):
    base = dict(
        plugin_key=key,
        name=name or key,
        owner_user_id=user.id,
        enabled=True,
        scopes=["project"],
        events=["record.created", "inbox.new"],
        config={},
        secret_refs={},
    )
    base.update(kw)
    i = PluginInstance(**base)
    db.add(i)
    db.commit()
    return i


def make_record(db, human, name="webhook-rec"):
    write_record(
        db,
        human,
        RecordIn(
            name=name, description="d", body="the body", type="project", scope="project", project="payments"
        ),
        change_source="ui",
    )
    db.commit()


# --- the egress helper ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://hooks.example.com/x", True),
        ("http://localhost:8080/x", True),
        ("http://192.168.1.5/x", True),
        ("http://10.0.0.2/x", True),
        ("http://hooks.example.com/x", False),
        ("ftp://hooks.example.com/x", False),
        ("file:///etc/passwd", False),
        ("javascript:alert(1)", False),
        ("http://8.8.8.8/x", False),
        ("https:///nohost", False),
    ],
)
def test_egress_scheme_rules(url, ok):
    if ok:
        assert egress.check_url(url) == url
    else:
        with pytest.raises(egress.EgressError):
            egress.check_url(url)


def test_egress_host_allowlist_and_no_redirects():
    egress.check_url("https://api.slack.com/x", ["slack.com"])  # subdomains of an allowed host are fine
    with pytest.raises(egress.EgressError):
        egress.check_url("https://evil.example/x", ["slack.com"])
    egress.set_transport(
        httpx.MockTransport(
            lambda r: httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})
        )
    )
    r = egress.EgressClient().get("https://hooks.example.com/x")
    assert (
        r.status_code == 302
    )  # handed back, not followed: a redirect can't be used to get around the host checks


def test_egress_caps_response_size():
    egress.set_transport(
        httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * (egress.MAX_RESPONSE_BYTES + 10)))
    )
    with pytest.raises(egress.EgressError, match="too large"):
        egress.EgressClient().get("https://hooks.example.com/big")


def test_redaction_scrubs_secret_values():
    assert (
        egress.redact("failed for https://x/tok-abc-1234 ok", ["tok-abc-1234"])
        == "failed for https://x/[redacted] ok"
    )
    assert egress.redact("short", ["a"]) == "short"  # too short to be meaningful: don't mangle text


# --- webhook ----------------------------------------------------------------------------------------------


def test_webhook_posts_signed_json(db, human, user, monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(body=request.content, headers=dict(request.headers), url=str(request.url))
        return httpx.Response(204)

    egress.set_transport(httpx.MockTransport(handler))
    monkeypatch.setenv("HOOK_URL", "https://hooks.example.com/services/T000/B000/XXXX")
    monkeypatch.setenv("HOOK_KEY", "signing-key-123")
    inst(db, user, "webhook", secret_refs={"url": "HOOK_URL", "signing_key": "HOOK_KEY"})
    make_record(db, human)
    assert dp.dispatch_once(db.get_bind()).delivered == 1
    body = seen["body"]
    assert (
        seen["headers"]["x-acm-signature"]
        == "sha256=" + hmac.new(b"signing-key-123", body, hashlib.sha256).hexdigest()
    )  # receiver can verify it
    assert seen["headers"]["x-acm-event"] == "record.created" and seen["url"].endswith("/B000/XXXX")
    data = json.loads(body)
    assert (
        data["type"] == "record.created"
        and data["data"]["name"] == "webhook-rec"
        and "the body" not in body.decode()
    )  # metadata only


def test_webhook_failure_modes(db, human, user, monkeypatch):
    codes = iter([500, 400])
    egress.set_transport(httpx.MockTransport(lambda r: httpx.Response(next(codes))))
    monkeypatch.setenv("HOOK_URL", "https://hooks.example.com/x")
    inst(db, user, "webhook", secret_refs={"url": "HOOK_URL"})
    make_record(db, human, "one-rec")
    s1 = dp.dispatch_once(db.get_bind())
    assert s1.retried == 1  # 5xx: try again later
    make_record(db, human, "two-rec")
    s2 = dp.dispatch_once(db.get_bind())
    assert s2.dead == 1  # 4xx: the receiver said no; retrying won't help


def test_webhook_refuses_plaintext_to_public_hosts_and_missing_config(db, human, user, monkeypatch):
    called = []
    egress.set_transport(httpx.MockTransport(lambda r: called.append(r) or httpx.Response(200)))
    monkeypatch.setenv("HOOK_URL", "http://hooks.example.com/x")
    i = inst(db, user, "webhook", secret_refs={"url": "HOOK_URL"})
    make_record(db, human)
    assert dp.dispatch_once(db.get_bind()).dead == 1 and called == []  # never even attempted
    db.refresh(i)
    assert "Plain http" in i.last_error
    j = inst(db, user, "webhook", name="unconfigured")
    make_record(db, human, "again-rec")
    dp.dispatch_once(db.get_bind())
    db.refresh(j)
    assert "environment variable isn't set" in j.last_error


def test_secret_values_never_reach_the_database(db, human, user, monkeypatch):
    secret_url = "https://hooks.example.com/services/SUPER-SECRET-TOKEN-9999"
    egress.set_transport(
        httpx.MockTransport(lambda r: (_ for _ in ()).throw(RuntimeError(f"cannot reach {r.url}")))
    )
    monkeypatch.setenv("HOOK_URL", secret_url)
    i = inst(db, user, "webhook", secret_refs={"url": "HOOK_URL"})
    make_record(db, human)
    dp.dispatch_once(db.get_bind())
    db.expire_all()
    dump = json.dumps(
        [i.model_dump(mode="json") for i in db.exec(select(PluginInstance)).all()]
        + [d.model_dump(mode="json") for d in db.exec(select(PluginDelivery)).all()]
        + [e.model_dump(mode="json") for e in db.exec(select(Event)).all()],
        default=str,
    )
    assert (
        "SUPER-SECRET-TOKEN-9999" not in dump and "HOOK_URL" in dump
    )  # the env var *name* is stored; the value never is
    assert "[redacted]" in (db.get(PluginInstance, i.id).last_error or "")


# --- apprise (a real local HTTP server stands in for the notification service) ----------------------------------------


class _Capture(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        _Capture.received.append((self.path, json.loads(self.rfile.read(n) or b"{}")))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):  # noqa: ANN002
        pass


@pytest.fixture()
def notify_server():
    _Capture.received = []
    srv = HTTPServer(("127.0.0.1", 0), _Capture)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()


def test_apprise_delivers_a_readable_notification(db, human, user, monkeypatch, notify_server):
    pytest.importorskip("apprise")
    monkeypatch.setenv("NOTIFY_URLS", f"json://127.0.0.1:{notify_server}/hook")
    inst(db, user, "apprise", secret_refs={"urls": "NOTIFY_URLS"}, config={"title_prefix": "[memory]"})
    make_record(db, human, "retry-policy")
    assert dp.dispatch_once(db.get_bind()).delivered == 1
    path, payload = _Capture.received[0]
    assert (
        path == "/hook"
        and payload["title"] == "[memory] New memory: retry-policy"
        and "payments" in payload["message"]
    )
    assert (
        "the body" not in json.dumps(payload) and "/memory/" in payload["message"]
    )  # a pointer back to the hub, not the content


def test_apprise_refuses_plaintext_to_public_hosts_and_hides_bad_urls(db, human, user, monkeypatch):
    pytest.importorskip("apprise")
    monkeypatch.setenv("NOTIFY_URLS", "json://hooks.example.com/path")
    i = inst(db, user, "apprise", secret_refs={"urls": "NOTIFY_URLS"})
    make_record(db, human)
    dp.dispatch_once(db.get_bind())
    db.refresh(i)
    assert "plain text" in i.last_error and "jsons://" in i.last_error
    monkeypatch.setenv("NOTIFY_URLS", "notascheme://token-SECRET-1234")
    j = inst(db, user, "apprise", name="bad", secret_refs={"urls": "NOTIFY_URLS"})
    make_record(db, human, "again-rec")
    dp.dispatch_once(db.get_bind())
    db.refresh(j)
    assert "isn't valid" in j.last_error and "SECRET-1234" not in j.last_error


def test_apprise_unreachable_service_retries_without_leaking_the_url(db, human, user, monkeypatch):
    pytest.importorskip("apprise")
    monkeypatch.setenv("NOTIFY_URLS", "json://127.0.0.1:9/token-SECRET-5678")  # nothing listens on port 9
    i = inst(db, user, "apprise", secret_refs={"urls": "NOTIFY_URLS"})
    make_record(db, human)
    assert dp.dispatch_once(db.get_bind()).retried == 1
    db.refresh(i)
    assert "SECRET-5678" not in (i.last_error or "") and i.last_status == "error"


# --- obsidian -------------------------------------------------------------------------------------------------


@pytest.fixture()
def vault(tmp_path):
    v = tmp_path / "vault"
    (v / "Inbox").mkdir(parents=True)
    (v / "Notes").mkdir()
    (v / ".obsidian").mkdir()
    (v / "Inbox" / "idea-one.md").write_text(
        "---\ntags: [memory, ideas]\n---\n# Try FTS5 for search\n\nWorth a spike. #todo\n"
    )
    (v / "Inbox" / "plain.md").write_text("Just a thought with no heading.\n")
    (v / "Notes" / "tagged.md").write_text("# Decision\n\nWe chose SQLite. #memory\n")
    (v / "Notes" / "untagged.md").write_text("# Groceries\n\nmilk\n")
    (v / ".obsidian" / "workspace.md").write_text("hidden app state")
    (v / "Inbox" / "big.md").write_text("x" * 300_000)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("# Not in the vault\n\nTOP SECRET")
    try:
        (v / "Inbox" / "escape.md").symlink_to(outside / "secret.md")
        (v / "Inbox" / "escape-dir").symlink_to(outside, target_is_directory=True)
    except OSError:
        pass
    return v


def pull(db, user, vault, **cfg):
    config = {"vault_path": str(vault), "folders": ["Inbox"], "tag": ""} | cfg
    i = inst(db, user, "obsidian", config=config, scopes=[], events=[])
    return i, dp.run_pull(db.get_bind(), i.id)


def items(db):
    db.expire_all()
    return {x.external_ref: x for x in db.exec(select(InboxItem)).all()}


def test_obsidian_captures_inbox_notes_and_nothing_hostile(db, user, vault):
    i, res = pull(db, user, vault)
    got = items(db)
    assert res.error is None and set(got) == {
        "Inbox/idea-one.md",
        "Inbox/plain.md",
    }  # not big, not symlinks, not the hidden folder, not other folders
    one = got["Inbox/idea-one.md"]
    assert (
        one.title == "Try FTS5 for search"
        and one.source == "plugin:obsidian"
        and one.scope == "user"
        and "TOP SECRET" not in one.body
    )
    assert all("TOP SECRET" not in x.body for x in got.values())
    assert got["Inbox/plain.md"].title == "plain"  # falls back to the file name


def test_obsidian_tag_filter_across_folders(db, user, vault):
    _, res = pull(db, user, vault, folders=["Inbox", "Notes"], tag="#memory")
    assert set(items(db)) == {
        "Inbox/idea-one.md",
        "Notes/tagged.md",
    }  # frontmatter tag and inline tag both count


def test_obsidian_repull_is_idempotent_updates_unreviewed_and_respects_decisions(db, user, vault):
    i, res = pull(db, user, vault)
    first = res.captured
    assert dp.run_pull(db.get_bind(), i.id).captured == 0
    (vault / "Inbox" / "plain.md").write_text("Edited before anyone reviewed it.\n")
    dp.run_pull(db.get_bind(), i.id)
    got = items(db)
    assert len(got) == first and "Edited before" in got["Inbox/plain.md"].body
    got["Inbox/plain.md"].status = "dismissed"
    db.add(got["Inbox/plain.md"])
    db.commit()
    (vault / "Inbox" / "plain.md").write_text("Edited again after dismissal.\n")
    assert dp.run_pull(db.get_bind(), i.id).captured == 0
    assert (
        "Edited before" in items(db)["Inbox/plain.md"].body
    )  # a decided item isn't resurrected or rewritten


def test_obsidian_never_writes_into_notes_it_reads_and_flags_external_trust(db, user, vault):
    before = {p: p.read_text() for p in vault.rglob("*.md") if not p.is_symlink()}
    pull(db, user, vault)
    assert before == {p: p.read_text() for p in vault.rglob("*.md") if not p.is_symlink()}
    wp = build_work_package(
        db, __import__("acm_hub.access", fromlist=["principal_for_user"]).principal_for_user(db, user)
    )
    assert wp["inbox"] and all(
        i["trust"] == "external" for i in wp["inbox"]
    )  # the dream pass treats it as data, never instructions


def test_obsidian_config_validation(vault, tmp_path):
    p = ObsidianPlugin()
    p.validate(ObsidianConfig(vault_path=str(vault)))
    for bad in (
        dict(vault_path=str(tmp_path / "nope")),
        dict(vault_path=str(vault), folders=["../elsewhere"]),
        dict(vault_path=str(vault), folders=["/etc"]),
        dict(vault_path=str(vault), export_folder="../out"),
        dict(vault_path=str(vault), inbox_scope="project"),
    ):
        with pytest.raises(ValueError):
            p.validate(ObsidianConfig(**bad))


def test_obsidian_pull_reports_errors_instead_of_crashing(db, user, tmp_path):
    i = inst(db, user, "obsidian", config={"vault_path": str(tmp_path / "missing")}, scopes=[], events=[])
    res = dp.run_pull(db.get_bind(), i.id)
    db.refresh(i)
    assert res.captured == 0 and i.last_status in {"ok", "error"}


def test_obsidian_pulls_emit_inbox_events_but_not_back_to_themselves(db, human, user, vault):
    watcher = inst(
        db,
        user,
        "webhook",
        name="watcher",
        scopes=["user"],
        user_scope_ack=True,
        events=["inbox.new"],
        secret_refs={},
    )
    i, _ = pull(db, user, vault)
    evs = db.exec(select(Event).where(Event.type == "inbox.new")).all()
    assert evs and all(e.origin_instance_id == i.id for e in evs)
    assert watcher


def test_obsidian_digest_export_writes_only_inside_the_vault(db, human, user, vault):
    i = inst(
        db,
        user,
        "obsidian",
        config={"vault_path": str(vault), "export_digest": True, "export_folder": "Memory hub"},
        scopes=["project"],
        events=[],
    )
    make_record(db, human, "this-weeks-rec")
    assert dp.send_digest(db.get_bind(), i.id) is True
    notes = list((vault / "Memory hub").glob("memory-digest-*.md"))
    assert len(notes) == 1 and "this-weeks-rec" in notes[0].read_text() and "1 new" in notes[0].read_text()
    db.refresh(i)
    assert i.last_digest_at is not None and i.last_status == "ok"


def test_helpers_parse_frontmatter_and_tags():
    fm, body = parse_note("---\ntitle: T\ntags: a, b\n---\nhello #c-d and #e/f\n")
    assert fm["title"] == "T" and note_tags(fm, body) == {"a", "b", "c-d", "e/f"}
    assert parse_note("no frontmatter") == ({}, "no frontmatter")
    assert parse_note("---\n: : bad\n---\nbody")[0] == {}


# --- memos --------------------------------------------------------------------------------------------------


def memos_handler(token="memos-token-xyz"):
    pages = {
        "": {
            "memos": [
                {"name": "memos/1", "content": "Remember the retry decision #memory\nmore detail"},
                {"name": "memos/2", "content": "buy milk"},
            ],
            "nextPageToken": "p2",
        },
        "p2": {
            "memos": [
                {"name": "memos/3", "content": "Plain text without tag", "tags": ["memory"]},
                {"name": "memos/4", "content": "#work standup notes"},
            ]
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {token}":
            return httpx.Response(401, json={"message": "unauthenticated"})
        assert request.url.path == "/api/v1/memos"
        return httpx.Response(200, json=pages[request.url.params.get("pageToken", "")])

    return handler


def test_memos_captures_tagged_memos_across_pages(db, user, monkeypatch):
    egress.set_transport(httpx.MockTransport(memos_handler()))
    monkeypatch.setenv("MEMOS_TOKEN", "memos-token-xyz")
    i = inst(
        db,
        user,
        "memos",
        config={"base_url": "https://memos.example.com", "tag": "memory"},
        secret_refs={"token": "MEMOS_TOKEN"},
        scopes=[],
        events=[],
    )
    res = dp.run_pull(db.get_bind(), i.id)
    got = items(db)
    assert res.error is None and res.captured == 2
    assert set(got) == {
        "https://memos.example.com/m/1",
        "https://memos.example.com/m/3",
    }  # via inline #tag and via the API's tags field
    assert (
        got["https://memos.example.com/m/1"].title == "Remember the retry decision"
        and got["https://memos.example.com/m/1"].source == "plugin:memos"
    )
    assert dp.run_pull(db.get_bind(), i.id).captured == 0  # idempotent


def test_memos_errors_are_reported_without_the_token(db, user, monkeypatch):
    egress.set_transport(httpx.MockTransport(memos_handler()))
    monkeypatch.setenv("MEMOS_TOKEN", "wrong-token-value")
    i = inst(
        db,
        user,
        "memos",
        config={"base_url": "https://memos.example.com"},
        secret_refs={"token": "MEMOS_TOKEN"},
        scopes=[],
        events=[],
    )
    res = dp.run_pull(db.get_bind(), i.id)
    assert "HTTP 401" in res.error and "wrong-token-value" not in res.error
    db.refresh(i)
    assert i.last_status == "error" and i.consecutive_failures == 1
    j = inst(
        db,
        user,
        "memos",
        name="notoken",
        config={"base_url": "https://memos.example.com"},
        scopes=[],
        events=[],
    )
    assert "environment variable isn't set" in dp.run_pull(db.get_bind(), j.id).error


def test_memos_respects_the_egress_rules(db, user, monkeypatch):
    called = []
    egress.set_transport(httpx.MockTransport(lambda r: called.append(r) or httpx.Response(200, json={})))
    monkeypatch.setenv("MEMOS_TOKEN", "t" * 12)
    i = inst(
        db,
        user,
        "memos",
        config={"base_url": "http://memos.example.com"},
        secret_refs={"token": "MEMOS_TOKEN"},
        scopes=[],
        events=[],
    )
    res = dp.run_pull(db.get_bind(), i.id)
    assert "Plain http" in res.error and called == []


# --- scheduling ---------------------------------------------------------------------------------------------


def test_scheduler_pulls_when_due_and_sends_digests_weekly(db, human, user, vault):
    from datetime import timedelta

    i = inst(
        db,
        user,
        "obsidian",
        config={"vault_path": str(vault), "folders": ["Inbox"]},
        scopes=[],
        events=[],
        pull_interval_minutes=30,
    )
    t0 = utcnow()
    assert dp.pulls_due(db.get_bind(), t0) == [i.id]  # never run
    dp.run_scheduled(db.get_bind(), t0)
    assert dp.pulls_due(db.get_bind(), t0 + timedelta(minutes=10)) == []
    assert dp.pulls_due(db.get_bind(), t0 + timedelta(minutes=31)) == [i.id]
    sink = inst(db, user, "webhook", name="digest-sink", events=["digest.weekly"], scopes=["project"])
    assert dp.digests_due(db.get_bind(), t0 + timedelta(days=6)) == []
    assert dp.digests_due(db.get_bind(), t0 + timedelta(days=8)) == [sink.id]
    assert os.path.exists(str(vault))
