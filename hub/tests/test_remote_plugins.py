import json
import threading
import time
from datetime import timedelta

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from acm_hub import dispatcher
from acm_hub.access import principal_for_user
from acm_hub.app import create_app
from acm_hub.config import Settings
from acm_hub.models import Event, PluginDelivery, PluginInstance, Project, User, utcnow
from acm_hub.plugins import egress, registry, remote
from acm_hub.plugins import remote_sdk as sdk
from acm_hub.plugins.base import DeliveryResult
from acm_hub.records import RecordIn, write_record

from .conftest import csrf_of, setup_admin
from .remote_support import (
    SECRET,
    SINK,
    SOURCE,
    URL,
    Down,
    add_instance,
    admin_id,
    asgi_transport,
    inbox,
    raw_service,
)

# --- registration policy -----------------------------------------------------------------------------------------------


def test_registration_is_validated_and_bad_entries_are_skipped_not_fatal():
    specs, errors = remote.load_specs(
        json.dumps(
            [
                {"key": "good", "url": "https://svc.example", "secret_env": "GOOD_SECRET"},
                {"key": "Bad Key", "url": "https://svc.example", "secret_env": "X_SECRET"},
                {
                    "key": "plain",
                    "url": "http://public.example.com",
                    "secret_env": "X_SECRET",
                },  # plain http off-LAN
                {"key": "lan", "url": "http://192.168.1.9:9000", "secret_env": "LAN_SECRET"},
                {
                    "key": "raw",
                    "url": "https://svc.example",
                    "secret_env": "this-is-the-secret-value",
                },  # a value, not a NAME
                {"key": "good", "url": "https://other.example", "secret_env": "GOOD_SECRET"},  # duplicate
                {"key": "extra", "url": "https://svc.example", "secret_env": "E_SECRET", "token": "oops"},
                "not-an-object",
            ]
        )
    )
    assert [s.key for s in specs] == ["good", "lan"]
    assert len(errors) == 6
    assert any("never the secret" in e for e in errors)
    assert remote.load_specs("{not json")[1] and remote.load_specs('{"a": 1}')[1]
    assert remote.load_specs("", "/definitely/not/here.json")[1]  # unreadable file is reported, not raised


def test_registration_can_come_from_a_file(tmp_path):
    f = tmp_path / "remote.json"
    f.write_text(json.dumps([{"key": "filed", "url": "https://svc.example", "secret_env": "FILED_SECRET"}]))
    specs, errors = remote.load_specs("", str(f))
    assert [s.key for s in specs] == ["filed"] and not errors


# --- the wire format ------------------------------------------------------------------------------------------------------


def test_signatures_cover_timestamp_and_exact_body():
    body = b'{"a":1}'
    sig = sdk.sign(SECRET, 1000, body)
    assert sdk.verify(SECRET, "1000", sig, body, now=1000)
    assert not sdk.verify(SECRET, "1000", sig, body + b" ", now=1000)  # any change to the body
    assert not sdk.verify(SECRET, "1001", sig, body, now=1001)  # signature is bound to its timestamp
    assert not sdk.verify("x" * 40, "1000", sig, body, now=1000)  # wrong key
    assert not sdk.verify(SECRET, "1000", sig, body, now=1000 + sdk.MAX_SKEW_SECONDS + 1)  # replay window
    assert not sdk.verify(SECRET, "garbage", sig, body) and not sdk.verify(SECRET, None, None, body)


def test_a_service_refuses_unsigned_and_stale_requests():
    app = sdk.create_app(secret=SECRET, manifest=SOURCE)
    t = asgi_transport(app)
    plain = httpx.Request("GET", URL + "/acm/v1/manifest")
    assert t.handle_request(plain).status_code == 401  # no signature at all
    ts = str(int(time.time()) - 3600)
    old = httpx.Request(
        "GET",
        URL + "/acm/v1/manifest",
        headers={"X-ACM-Timestamp": ts, "X-ACM-Signature": sdk.sign(SECRET, ts, b"")},
    )
    assert t.handle_request(old).status_code == 401  # validly signed but an hour old
    with pytest.raises(ValueError, match="at least"):
        sdk.create_app(secret="short", manifest=SOURCE)


# --- becoming a plugin -------------------------------------------------------------------------------------------------------


def test_manifest_becomes_a_plugin_with_a_generated_form_and_no_secret_fields(start):
    client, app = start(sdk.create_app(secret=SECRET, manifest=SOURCE))
    p = registry.get("feed")
    assert p is not None and p.remote and p.info.kind == "source" and p.info.secret_names == []
    from acm_hub.plugin_admin import field_specs

    specs = {f.name: f for f in field_specs(p.info.config_schema)}
    assert (
        specs["feed_url"].required
        and specs["max_items"].kind == "number"
        and specs["inbox_scope"].options == ["user", "project", "team"]
    )
    page = client.get("/plugins/new?plugin=feed")
    assert page.status_code == 200 and "cfg_feed_url" in page.text and "secret_" not in page.text
    listing = client.get("/plugins").text
    assert (
        "Remote plugin services" in listing and "reachable" in listing and "runs outside the hub" in listing
    )


def test_the_form_asks_the_service_to_validate_settings(start):
    def validate(instance, config):
        return {"ok": config.get("feed_url", "").startswith("https://"), "error": "Feed URL must be https."}

    client, app = start(sdk.create_app(secret=SECRET, manifest=SOURCE, validate=validate))
    page = client.get("/plugins/new?plugin=feed")
    base = {
        "csrf_token": csrf_of(page.text),
        "plugin": "feed",
        "name": "f",
        "cfg_feed_url": "http://plain.example/x",
    }
    r = client.post("/plugins", data=base, follow_redirects=False)
    assert (
        r.status_code in (200, 422) and "must be https" in r.text
    )  # the service's own message reaches the form
    assert client.get("/plugins").text.count("my feed") == 0
    ok = client.post("/plugins", data=base | {"cfg_feed_url": "https://ok.example/x"}, follow_redirects=False)
    assert ok.status_code == 303


@pytest.mark.parametrize(
    "manifest, why",
    [
        ({**SOURCE, "key": "someone-else"}, "not 'feed'"),
        ({**SOURCE, "kind": "root"}, "invalid"),
        ({**SOURCE, "config": [{"name": "Bad Name!", "type": "string"}]}, "invalid"),
        ({**SOURCE, "config": [{"name": "a", "type": "string"}, {"name": "a", "type": "string"}]}, "invalid"),
        ({**SOURCE, "config": [{"name": "s", "type": "select"}]}, "invalid"),
        ({**SOURCE, "config": [{"name": f"f{i}", "type": "string"} for i in range(31)]}, "invalid"),
    ],
)
def test_a_bad_manifest_never_becomes_a_plugin(start, manifest, why):
    start(sdk.create_app(secret=SECRET, manifest=manifest))
    assert registry.get("feed") is None
    assert why in (remote.unavailable_reason("feed") or "")


def test_a_key_that_collides_with_a_builtin_is_refused(start):
    start(
        sdk.create_app(secret=SECRET, manifest={**SOURCE, "key": "webhook"}),
        spec={"key": "webhook", "url": URL, "secret_env": "FEED_SECRET"},
    )
    assert (
        registry.get("webhook").info.name == "Webhook" and not registry.get("webhook").remote
    )  # the built-in wins
    assert "collides" in remote.unavailable_reason("webhook")


def test_a_missing_or_short_signing_key_means_never_contacted(start):
    calls = []

    class Spy(httpx.BaseTransport):
        def handle_request(self, request):
            calls.append(request)
            raise AssertionError("must not be contacted")

    start(Spy(), secret="too-short")
    assert not calls and "signing key" in remote.unavailable_reason("feed")


# --- sources ---------------------------------------------------------------------------------------------------------------------


def items(n, **extra):
    return [
        {"title": f"Entry {i}", "body": f"body {i}", "external_ref": f"urn:e{i}"} | extra for i in range(n)
    ]


def test_pull_files_items_in_the_inbox_as_external_and_dedupes(start):
    seen = []

    def pull(instance, config, since, limit):
        seen.append((instance["name"], config["feed_url"], since, limit))
        return {"items": items(3)}

    client, app = start(sdk.create_app(secret=SECRET, manifest=SOURCE, pull=pull))
    iid = add_instance(app)
    first = dispatcher.run_pull(app.state.engine, iid)
    assert first.error is None and first.captured == 3
    rows = inbox(app)
    assert {r.source for r in rows} == {"plugin:feed"} and {r.status for r in rows} == {
        "new"
    }  # external, undecided
    assert dispatcher.run_pull(app.state.engine, iid).captured == 0  # same external_refs: nothing new
    assert seen[0][2] is None and seen[0][3] == 200  # first run starts from scratch
    assert seen[1][2] is not None and seen[1][2].endswith("Z")  # after a clean run it can resume


def test_after_a_failure_the_service_starts_over_instead_of_skipping(start):
    state = {"fail": False, "since": []}

    def pull(instance, config, since, limit):
        state["since"].append(since)
        if state["fail"]:
            raise RuntimeError("upstream down")
        return {"items": items(1)}

    client, app = start(sdk.create_app(secret=SECRET, manifest=SOURCE, pull=pull))
    iid = add_instance(app)
    dispatcher.run_pull(app.state.engine, iid)
    state["fail"] = True
    assert dispatcher.run_pull(
        app.state.engine, iid
    ).error  # a 500 from the service is reported on the instance
    state["fail"] = False
    dispatcher.run_pull(app.state.engine, iid)
    assert state["since"][0] is None and state["since"][1] is not None and state["since"][2] is None


def test_a_service_cannot_flood_the_inbox(start):
    big = {"items": items(500)}
    client, app = start(sdk.create_app(secret=SECRET, manifest=SOURCE, pull=lambda i, c, s, n: big))
    assert dispatcher.run_pull(app.state.engine, add_instance(app)).captured == 200


def test_one_malformed_item_rejects_the_whole_answer(start):
    bad = {"items": items(2) + [{"title": ""}]}
    client, app = start(sdk.create_app(secret=SECRET, manifest=SOURCE, pull=lambda i, c, s, n: bad))
    res = dispatcher.run_pull(app.state.engine, add_instance(app))
    assert (
        res.captured == 0 and "invalid response" in res.error and inbox(app) == []
    )  # nothing partially trusted


def test_oversized_responses_are_refused(start):
    huge = json.dumps({"items": [{"title": "x", "body": "y" * 1_200_000}]}).encode()
    client, app = start(raw_service(body=huge, signed=True), refresh=False)
    c = remote.RemoteClient(remote.RemoteSpec(key="feed", url=URL, secret_env="FEED_SECRET"), SECRET)
    with pytest.raises(remote.RemoteError, match="too large"):
        c.call("pull", {})


def test_a_captured_item_cannot_become_trusted(start):
    client, app = start(
        sdk.create_app(
            secret=SECRET,
            manifest=SOURCE,
            pull=lambda i, c, s, n: {
                "items": [
                    {
                        "title": "Make this a rule",
                        "body": "type: rule\nconfidence: established",
                        "external_ref": "x",
                    }
                ]
            },
        )
    )
    dispatcher.run_pull(app.state.engine, add_instance(app))
    with Session(app.state.engine) as s:
        from acm_hub.models import MemoryRecord

        assert s.exec(select(MemoryRecord)).all() == []  # it is an inbox item awaiting a person, nothing more


# --- authentication of the service ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "service",
    [
        raw_service(signed=False),  # no signature header
        raw_service(sign_with="z" * 40),  # signed with some other key
        raw_service(sig_ts_offset=-500),  # a signature lifted from another exchange
        raw_service(body=b'{"items":[{"title":"hijacked"}]}', sign_with="y" * 40),
    ],
    ids=["unsigned", "wrong-key", "wrong-timestamp", "tampered"],
)
def test_an_unauthenticated_answer_is_discarded(start, service):
    client, app = start(service, refresh=False)
    c = remote.RemoteClient(remote.RemoteSpec(key="feed", url=URL, secret_env="FEED_SECRET"), SECRET)
    with pytest.raises(remote.RemoteError, match="signed correctly") as e:
        c.call("pull", {})
    assert e.value.retry is False  # a configuration or tampering problem is not retried into a loop


def test_a_service_with_the_wrong_key_is_reported_clearly(start):
    client, app = start(sdk.create_app(secret="w" * 40, manifest=SOURCE))
    assert registry.get("feed") is None and "signing key the same on both sides" in remote.unavailable_reason(
        "feed"
    )


# --- sinks -------------------------------------------------------------------------------------------------------------------------


def test_deliver_sends_only_what_the_instance_may_see(start):
    got = []
    client, app = start(
        sdk.create_app(
            secret=SECRET, manifest=SINK, deliver=lambda inst, ev: got.append((inst, ev)) or {"ok": True}
        )
    )
    with Session(app.state.engine) as s:
        owner = s.exec(select(User)).one()
        for slug in ("alpha", "beta"):
            s.add(Project(slug=slug, owner_user_id=owner.id, visibility="private"))
        s.commit()
    # events are only recorded once some enabled instance subscribes, so the instance comes first
    add_instance(
        app, events=["record.created"], scopes=["project"], projects=["alpha"], egress="metadata", config={}
    )
    with Session(app.state.engine) as s:
        p = principal_for_user(s, s.exec(select(User)).one())
        for slug in ("alpha", "beta"):
            write_record(
                s,
                p,
                RecordIn(
                    name=f"{slug}-fact",
                    description="d",
                    body=f"SECRET-{slug}",
                    type="project",
                    scope="project",
                    project=slug,
                ),
                change_source="ui",
            )
        s.commit()
    dispatcher.dispatch_once(app.state.engine)
    assert [e["payload"]["name"] for _, e in got] == [
        "alpha-fact"
    ]  # beta isn't on the allowlist: never serialised
    assert "body" not in got[0][1]["payload"] and "SECRET" not in json.dumps(
        got
    )  # metadata egress carries no text
    assert set(got[0][0]) == {"id", "name"}  # the service learns the instance's id and name, nothing else
    # full egress is an explicit opt-in per instance, and still respects the allowlist
    got.clear()
    with Session(app.state.engine) as s:
        for inst in s.exec(select(PluginInstance)).all():
            inst.egress = "full"
            s.add(inst)
        for e in s.exec(select(Event)).all():
            e.dispatched_at = None
            s.add(e)
        for d in s.exec(select(PluginDelivery)).all():
            s.delete(d)
        s.commit()
    dispatcher.dispatch_once(app.state.engine)
    assert [e["payload"]["body"] for _, e in got] == ["SECRET-alpha"]


def test_a_down_service_means_retry_not_lost_and_not_dead(start):
    client, app = start(sdk.create_app(secret=SECRET, manifest=SINK, deliver=lambda i, e: {"ok": True}))
    iid = add_instance(app, events=["plugin.failed", "inbox.new"], config={})
    egress.set_transport(Down())  # the service falls over after having been registered
    remote.STATE["feed"].plugin = None  # (as after a restart of the hub while it was down)
    remote.STATE["feed"].error = "couldn't reach the service"
    registry.unregister("feed")
    res = dispatcher.deliver_test_event(app.state.engine, iid)
    assert not res.ok and res.retry and "unavailable" in res.message  # retry, never "not installed"
    # events that arrive while it is down still get a delivery row, to be retried
    with Session(app.state.engine) as s:
        s.add(Event(type="inbox.new", payload={"title": "t", "link": "/review"}, owner_user_id=admin_id(app)))
        s.commit()
        assert dispatcher.fan_out(s) >= 1
        s.commit()
    egress.set_transport(
        asgi_transport(sdk.create_app(secret=SECRET, manifest=SINK, deliver=lambda i, e: {"ok": True}))
    )
    remote.refresh_due(force=True)
    later = utcnow() + timedelta(hours=1)
    stats = dispatcher.deliver_due(app.state.engine, now=later)
    assert stats.delivered >= 1 and stats.dead == 0  # and once it's back, the queued event is delivered


@pytest.mark.parametrize(
    "answer, retry",
    [
        ({"ok": False, "retry": True, "message": "busy"}, True),
        ({"ok": False, "message": "no"}, False),
        ({"nonsense": 1}, False),
    ],
)
def test_service_verdicts_map_to_retry_or_fail(start, answer, retry):
    client, app = start(sdk.create_app(secret=SECRET, manifest=SINK, deliver=lambda i, e: answer))
    res = dispatcher.deliver_test_event(
        app.state.engine, add_instance(app, events=["plugin.failed"], config={})
    )
    assert not res.ok and res.retry is retry


def test_a_service_error_is_a_retryable_failure(start):
    def boom(instance, event):
        raise RuntimeError("kaput")

    client, app = start(sdk.create_app(secret=SECRET, manifest=SINK, deliver=boom))
    res = dispatcher.deliver_test_event(
        app.state.engine, add_instance(app, events=["plugin.failed"], config={})
    )
    assert not res.ok and res.retry and isinstance(res, DeliveryResult)


# --- admin surface ----------------------------------------------------------------------------------------------------------------------


def test_only_an_admin_with_csrf_can_make_the_hub_contact_services(start):
    client, app = start(sdk.create_app(secret=SECRET, manifest=SOURCE))
    token = csrf_of(client.get("/plugins").text)
    assert client.post("/plugins/remote/refresh").status_code == 403  # no CSRF token
    assert (
        client.post("/plugins/remote/refresh", data={"csrf_token": token}, follow_redirects=False).status_code
        == 303
    )
    with Session(app.state.engine) as s:
        from acm_hub.security import hash_password

        s.add(User(email="eve@example.com", password_hash=hash_password("correct horse battery staple")))
        s.commit()
    eve = TestClient(app)
    eve.post("/login", data={"email": "eve@example.com", "password": "correct horse battery staple"})
    assert (
        eve.post("/plugins/remote/refresh", data={"csrf_token": csrf_of(eve.get("/memory").text)}).status_code
        == 403
    )


def test_registration_errors_are_visible_to_the_operator(settings, monkeypatch):
    monkeypatch.setenv(
        "MEMORY_HUB_REMOTE_PLUGINS",
        json.dumps([{"key": "BAD", "url": "https://x.example", "secret_env": "X_SECRET"}]),
    )
    root = create_app(Settings())
    with TestClient(root) as client:
        setup_admin(client, root.fastapi)
        assert "key must be lower-case" in client.get("/plugins").text


def test_the_offline_cli_never_contacts_a_service(settings, monkeypatch, capsys):
    import socket

    monkeypatch.setenv(
        "MEMORY_HUB_REMOTE_PLUGINS", json.dumps([{"key": "feed", "url": URL, "secret_env": "FEED_SECRET"}])
    )

    def deny(*a, **k):
        raise AssertionError("the offline CLI touched the network")

    monkeypatch.setattr(socket.socket, "connect", deny)
    from acm_hub.cli import main

    assert main(["plugins"]) == 0


# --- over a real socket -----------------------------------------------------------------------------------------------------------


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_example_feed_service_end_to_end_over_real_http(settings, monkeypatch, tmp_path):
    """Hub -> real HTTP -> the shipped example service -> real HTTP -> a feed, then into the inbox."""
    import importlib.util
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from pathlib import Path

    atom = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
      <entry><title>First &amp; best</title><id>urn:1</id><link href="https://blog.example/1"/><summary>&lt;p&gt;Hello &lt;b&gt;there&lt;/b&gt;&lt;/p&gt;</summary></entry>
      <entry><title>Second</title><id>urn:2</id><link href="https://blog.example/2"/><summary>more</summary></entry></feed>"""

    class Feed(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(atom)

        def log_message(self, *a):
            pass

    feed_port, svc_port = free_port(), free_port()
    feed_srv = HTTPServer(("127.0.0.1", feed_port), Feed)
    threading.Thread(target=feed_srv.serve_forever, daemon=True).start()

    monkeypatch.setenv("FEED_PLUGIN_SECRET", SECRET)
    monkeypatch.setenv("FEED_ALLOW_HTTP", "1")
    spec = importlib.util.spec_from_file_location(
        "remote_feed_example", Path(__file__).parents[1] / "examples" / "remote-feed" / "app.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    server = uvicorn.Server(uvicorn.Config(mod.app, host="127.0.0.1", port=svc_port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        monkeypatch.setenv(
            "MEMORY_HUB_REMOTE_PLUGINS",
            json.dumps(
                [{"key": "feed", "url": f"http://127.0.0.1:{svc_port}", "secret_env": "FEED_PLUGIN_SECRET"}]
            ),
        )
        root = create_app(Settings())
        with TestClient(root) as client:
            setup_admin(client, root.fastapi)
            remote.refresh_due(force=True)
            assert registry.get("feed") is not None, remote.unavailable_reason("feed")
            iid = add_instance(
                root.fastapi,
                config={
                    "feed_url": f"http://127.0.0.1:{feed_port}/atom.xml",
                    "max_items": 10,
                    "inbox_scope": "user",
                    "inbox_project": "",
                },
            )
            res = dispatcher.run_pull(root.fastapi.state.engine, iid)
            assert res.error is None and res.captured == 2, res
            rows = {r.title: r for r in inbox(root.fastapi)}
            assert (
                set(rows) == {"First & best", "Second"} and "Hello there" in rows["First & best"].body
            )  # tags stripped
            assert (
                rows["First & best"].source == "plugin:feed" and rows["First & best"].external_ref == "urn:1"
            )
    finally:
        server.should_exit = True
        t.join(5)
        feed_srv.shutdown()


def test_the_example_service_refuses_hostile_xml():
    import importlib.util
    import os
    from pathlib import Path

    os.environ["FEED_PLUGIN_SECRET"] = SECRET
    spec = importlib.util.spec_from_file_location(
        "remote_feed_example2", Path(__file__).parents[1] / "examples" / "remote-feed" / "app.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    bomb = b'<?xml version="1.0"?><!DOCTYPE lol [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><rss><channel><item><title>&b;</title></item></channel></rss>'
    with pytest.raises(ValueError, match="DOCTYPE"):
        mod.parse_feed(bomb, 10)
    with pytest.raises(ValueError, match="https"):
        mod._fetch(
            "http://169.254.169.254/latest/meta-data"
        )  # not fetched unless the operator opts in to http
    with pytest.raises(ValueError, match="https"):
        mod._fetch("file:///etc/passwd")
