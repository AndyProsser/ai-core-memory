"""Per-user connections: sealed tokens, the SSRF guard, strict ownership, and the three notes connectors."""

import json
import socket

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, select

from acm_hub import connection_secrets, connections, orgs
from acm_hub import dispatcher as dp
from acm_hub import events as ev_mod
from acm_hub.access import principal_for_user
from acm_hub.models import InboxItem, InstanceSettings, PluginInstance, User
from acm_hub.plugins import egress

from .conftest import PASSWORD, csrf_of

TOKEN = "tok-very-secret-12345"
HOSTS = {
    "memos.example.com": ["93.184.216.34"],
    "joplin.example.com": ["93.184.216.35"],
    "obsidian.example.com": ["93.184.216.36"],
    "hook.example.com": ["93.184.216.37"],
    "notes.lan": ["192.168.1.20"],
    "evil-loopback.example": ["127.0.0.1"],
    "meta.example": ["169.254.169.254"],
    "mixed.example": ["93.184.216.34", "10.0.0.5"],
    "v6-private.example": ["fd00::5"],
    "cgnat.example": ["100.64.1.2"],
}
calls_to_dns: list[str] = []


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    calls_to_dns.clear()

    def fake(host, port, type=0, **kw):  # noqa: A002
        calls_to_dns.append(host)
        if host not in HOSTS:
            raise socket.gaierror("no such host")
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0))
            for a in HOSTS[host]
        ]

    monkeypatch.setattr(egress, "_getaddrinfo", fake)
    monkeypatch.delenv("MEMORY_HUB_CONNECTION_PRIVATE_HOSTS", raising=False)
    yield
    egress.set_transport(None)


def make_conn(db, user, key="memos", config=None, secrets=None, **kw):
    i = PluginInstance(
        plugin_key=key,
        name=kw.pop("name", key),
        owner_user_id=user.id,
        personal=True,
        enabled=kw.pop("enabled", True),
        scopes=kw.pop("scopes", []),
        events=kw.pop("events", []),
        config=config or {},
        **kw,
    )
    i.sealed_secrets = {
        n: connection_secrets.seal(i.id, n, v)
        for n, v in (secrets if secrets is not None else {"token": TOKEN}).items()
    }
    db.add(i)
    db.commit()
    return i


def inbox(db):
    db.expire_all()
    return {i.external_ref: i for i in db.exec(select(InboxItem)).all()}


# --- sealed secrets -------------------------------------------------------------------------------------------


def test_secrets_round_trip_and_are_bound_to_their_row_and_name(settings):
    s = connection_secrets.seal("inst-1", "token", TOKEN)
    assert TOKEN not in s and connection_secrets.open_("inst-1", "token", s) == TOKEN
    assert connection_secrets.open_("inst-2", "token", s) is None  # moved to another connection
    assert connection_secrets.open_("inst-1", "url", s) is None  # moved to another field
    assert connection_secrets.open_("inst-1", "token", s[:-2] + "AA") is None  # tampered


def test_changing_the_hub_key_makes_sealed_secrets_unreadable_not_a_crash(db, user, monkeypatch):
    i = make_conn(db, user)
    monkeypatch.setenv("MEMORY_HUB_SECRET_KEY", "a-different-hub-key")
    assert connection_secrets.open_(i.id, "token", i.sealed_secrets["token"]) is None
    plugin = dp.registry.get("memos")
    assert (
        dp.resolve_secrets(plugin, i) == {}
    )  # "not set", and the connection reports a clear error rather than crashing
    states = connections.secret_states(plugin, i)
    assert states[0]["unreadable"] and not states[0]["is_set"]


def test_a_connection_never_reads_the_environment(db, user, monkeypatch):
    """secret_refs belong to system plugins. If a connection could name an env var, a person could make the hub send
    MEMORY_HUB_SECRET_KEY to a server they control."""
    monkeypatch.setenv("LEAK_ME", "environment-value-1234")
    i = make_conn(db, user, secrets={}, secret_refs={"token": "LEAK_ME"})
    assert dp.resolve_secrets(dp.registry.get("memos"), i) == {}
    sys_i = PluginInstance(
        plugin_key="memos", name="s", owner_user_id=user.id, secret_refs={"token": "LEAK_ME"}
    )
    assert dp.resolve_secrets(dp.registry.get("memos"), sys_i) == {
        "token": "environment-value-1234"
    }  # unchanged for system


# --- the SSRF guard -------------------------------------------------------------------------------------------


def seen_requests():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"memos": []})

    egress.set_transport(httpx.MockTransport(handler))
    return seen


@pytest.mark.parametrize(
    "host",
    [
        "evil-loopback.example",
        "meta.example",
        "mixed.example",
        "v6-private.example",
        "cgnat.example",
        "notes.lan",
    ],
)
def test_connections_cannot_reach_private_loopback_or_metadata_addresses(db, user, host):
    seen = seen_requests()
    i = make_conn(db, user, config={"base_url": f"https://{host}"})
    res = dp.run_pull(db.get_bind(), i.id)
    assert res.error and seen == []  # refused before any request left the hub


@pytest.mark.parametrize(
    "literal", ["127.0.0.1", "169.254.169.254", "10.1.2.3", "[::1]", "0.0.0.0", "[::ffff:10.0.0.1]"]
)
def test_ip_literals_are_refused_too(db, user, literal):
    seen = seen_requests()
    i = make_conn(db, user, config={"base_url": f"http://{literal}:8080"})
    assert dp.run_pull(db.get_bind(), i.id).error and seen == []


def test_the_operator_can_allow_a_private_host_but_never_metadata(db, user, monkeypatch):
    monkeypatch.setenv("MEMORY_HUB_CONNECTION_PRIVATE_HOSTS", "notes.lan, 10.0.0.0/8, meta.example")
    seen = seen_requests()
    ok = make_conn(db, user, config={"base_url": "http://notes.lan:5230"}, name="lan")
    assert dp.run_pull(db.get_bind(), ok.id).error is None and seen  # allowed private, plain http
    assert seen[0].url.host == "192.168.1.20" and seen[0].headers["host"] == "notes.lan:5230"
    seen.clear()
    for host in (
        "meta.example",
        "mixed.example",
    ):  # link-local is never allowed; mixed has a 10.x answer → allowed only via CIDR
        i = make_conn(db, user, config={"base_url": f"https://{host}"}, name=host)
        res = dp.run_pull(db.get_bind(), i.id)
        if host == "meta.example":
            assert res.error and "never reach" in res.error


def test_the_request_goes_to_the_address_that_was_checked(db, user):
    """Resolve once, connect to that exact address, keep the name for Host and TLS: DNS can't change in between."""
    seen = seen_requests()
    i = make_conn(db, user, config={"base_url": "https://memos.example.com"})
    assert dp.run_pull(db.get_bind(), i.id).error is None
    req = seen[0]
    assert req.url.host == "93.184.216.34" and req.headers["host"] == "memos.example.com"
    assert req.extensions["sni_hostname"] == "memos.example.com"
    assert calls_to_dns.count("memos.example.com") == 1  # one lookup, no second one to race


def test_plain_http_to_a_public_address_and_credentials_in_urls_are_refused(db, user):
    seen = seen_requests()
    for url in ("http://memos.example.com", "https://user:pw@memos.example.com"):
        i = make_conn(db, user, config={"base_url": url}, name=url)
        assert dp.run_pull(db.get_bind(), i.id).error
    assert seen == []


def test_redirects_are_not_followed(db, user):
    hits = []

    def handler(req):
        hits.append(str(req.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})

    egress.set_transport(httpx.MockTransport(handler))
    i = make_conn(db, user, config={"base_url": "https://memos.example.com"})
    res = dp.run_pull(db.get_bind(), i.id)
    assert "HTTP 302" in res.error and len(hits) == 1


def test_system_plugins_are_not_subject_to_the_connection_guard(db, user, monkeypatch):
    """The admin's own plugins keep their documented behaviour (a LAN target is fine for them)."""
    seen = seen_requests()
    monkeypatch.setenv("T", "system-token-123456")
    i = PluginInstance(
        plugin_key="memos", name="sys", owner_user_id=user.id, enabled=True,
        config={"base_url": "http://127.0.0.1:5230"}, secret_refs={"token": "T"},
    )  # fmt: skip
    db.add(i)
    db.commit()
    assert dp.run_pull(db.get_bind(), i.id).error is None and seen


# --- the connectors -------------------------------------------------------------------------------------------


def memos_handler(req):
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    return httpx.Response(
        200,
        json={
            "memos": [
                {"name": "memos/7", "content": "Remember the retry decision #memory", "tags": ["memory"]},
                {"name": "memos/8", "content": "lunch", "tags": []},
            ]
        },
    )


def test_memos_connection_pulls_with_the_sealed_token(db, user):
    egress.set_transport(httpx.MockTransport(memos_handler))
    i = make_conn(db, user, config={"base_url": "https://memos.example.com", "tag": "memory"})
    res = dp.run_pull(db.get_bind(), i.id)
    got = inbox(db)
    assert res.error is None and res.captured == 1
    item = got["https://memos.example.com/m/7"]
    assert item.owner_user_id == user.id and item.source == "plugin:memos" and item.scope == "user"


def joplin_handler(req):
    p = req.url.path
    if p == "/ping":
        return httpx.Response(200, text="JoplinClipperServer")
    if req.url.params.get("token") != TOKEN:
        return httpx.Response(403, json={"error": "Invalid token"})
    if p == "/search":
        assert req.url.params["query"] == 'tag:"memory"'
        page = int(req.url.params["page"])
        items = {
            1: [{"id": "n1", "title": "Retry policy", "body": "Use backoff"}],
            2: [{"id": "n2", "title": "Idempotency", "body": "keys"}],
        }[page]
        return httpx.Response(200, json={"items": items, "has_more": page == 1})
    return httpx.Response(200, json={"items": [], "has_more": False})


def test_joplin_pulls_tagged_notes_across_pages_and_links_back(db, user):
    egress.set_transport(httpx.MockTransport(joplin_handler))
    i = make_conn(db, user, key="joplin", config={"base_url": "https://joplin.example.com", "tag": "memory"})
    res = dp.run_pull(db.get_bind(), i.id)
    got = inbox(db)
    assert res.error is None and res.captured == 2
    assert set(got) == {"joplin://x-callback-url/openNote?id=n1", "joplin://x-callback-url/openNote?id=n2"}
    assert dp.run_pull(db.get_bind(), i.id).captured == 0  # idempotent


def test_joplin_errors_never_contain_the_token_even_though_it_travels_in_the_url(db, user):
    egress.set_transport(httpx.MockTransport(lambda r: httpx.Response(500, text=f"boom {r.url}")))
    i = make_conn(db, user, key="joplin", config={"base_url": "https://joplin.example.com"})
    res = dp.run_pull(db.get_bind(), i.id)
    assert res.error and TOKEN not in res.error
    db.refresh(i)
    assert TOKEN not in (i.last_error or "")

    def boom(req):
        raise httpx.ConnectError(f"cannot connect to {req.url}")  # httpx errors can echo the URL

    egress.set_transport(httpx.MockTransport(boom))
    res = dp.run_pull(db.get_bind(), i.id)
    assert res.error and TOKEN not in res.error


OBS = {
    "/vault/Inbox/": {"files": ["a.md", "sub/", ".hidden.md", "pic.png"]},
    "/vault/Inbox/sub/": {"files": ["b.md"]},
    "/vault/Inbox/a.md": {"content": "# A\nfor later #memory", "tags": [], "frontmatter": {}},
    "/vault/Inbox/sub/b.md": {"content": "plain note", "tags": [], "frontmatter": {"tags": ["memory"]}},
}


def obsidian_handler(req):
    if req.headers.get("authorization") != f"Bearer {TOKEN}":
        return httpx.Response(401, json={"errorCode": 40101})
    if req.url.path == "/":
        return httpx.Response(200, json={"status": "OK", "authenticated": True})
    if req.url.path in OBS:
        if req.url.path.endswith(".md"):
            assert req.headers["accept"] == "application/vnd.olrapi.note+json"
        return httpx.Response(200, json=OBS[req.url.path])
    return httpx.Response(404, json={})


def test_obsidian_rest_walks_the_folder_and_filters_by_tag(db, user):
    egress.set_transport(httpx.MockTransport(obsidian_handler))
    i = make_conn(
        db, user, key="obsidian-rest", secrets={"api_key": TOKEN},
        config={"base_url": "https://obsidian.example.com", "folder": "Inbox", "tag": "memory", "vault_name": "My Vault"},
    )  # fmt: skip
    res = dp.run_pull(db.get_bind(), i.id)
    got = inbox(db)
    assert (
        res.error is None and res.captured == 2
    )  # inline #tag in a.md, frontmatter tag in sub/b.md; hidden + png skipped
    assert set(got) == {
        "obsidian://open?vault=My%20Vault&file=Inbox%2Fa.md",
        "obsidian://open?vault=My%20Vault&file=Inbox%2Fsub%2Fb.md",
    }


def test_obsidian_rest_reports_a_missing_folder_and_a_bad_key_without_the_key(db, user):
    egress.set_transport(httpx.MockTransport(obsidian_handler))
    i = make_conn(
        db,
        user,
        key="obsidian-rest",
        secrets={"api_key": TOKEN},
        config={"base_url": "https://obsidian.example.com", "folder": "Nope"},
    )
    assert "no folder 'Nope'" in dp.run_pull(db.get_bind(), i.id).error
    bad = make_conn(
        db,
        user,
        key="obsidian-rest",
        secrets={"api_key": "wrong-key-value-1"},
        config={"base_url": "https://obsidian.example.com"},
        name="bad",
    )
    err = dp.run_pull(db.get_bind(), bad.id).error
    assert "rejected the API key" in err and "wrong-key-value-1" not in err


@pytest.mark.parametrize(
    "key,config,secrets,handler,fragment",
    [
        ("memos", {"base_url": "https://memos.example.com"}, {"token": TOKEN}, memos_handler, "Connected to Memos"),
        ("joplin", {"base_url": "https://joplin.example.com"}, {"token": TOKEN}, joplin_handler, "Connected to Joplin"),
        ("obsidian-rest", {"base_url": "https://obsidian.example.com"}, {"api_key": TOKEN}, obsidian_handler, "Connected to Obsidian"),
    ],
)  # fmt: skip
def test_test_connection_succeeds_and_fails_with_a_clear_message(
    db, user, key, config, secrets, handler, fragment
):
    egress.set_transport(httpx.MockTransport(handler))
    good = make_conn(db, user, key=key, config=config, secrets=secrets, enabled=False)
    ok, msg = dp.check_instance(db.get_bind(), good.id)
    assert ok and fragment in msg  # works while the connection is still off
    bad = make_conn(
        db,
        user,
        key=key,
        config=config,
        secrets={n: "nope-nope-nope-1" for n in secrets},
        enabled=False,
        name="bad",
    )
    ok, msg = dp.check_instance(db.get_bind(), bad.id)
    assert not ok and "nope-nope-nope-1" not in msg and msg


# --- who a connection captures for --------------------------------------------------------------------------


def test_captures_land_only_in_the_owners_inbox(db, user):
    other = orgs.create_user(db, principal_for_user(db, user), "other@example.com", password=PASSWORD)
    db.commit()
    egress.set_transport(httpx.MockTransport(memos_handler))
    mine = make_conn(db, user, config={"base_url": "https://memos.example.com"}, name="mine")
    dp.run_pull(db.get_bind(), mine.id)
    db.expire_all()
    rows = db.exec(select(InboxItem)).all()
    assert (
        rows
        and all(r.owner_user_id == user.id for r in rows)
        and not [r for r in rows if r.owner_user_id == other.id]
    )


def test_a_project_inbox_needs_write_access_now_not_just_when_it_was_saved(db, user):
    from acm_hub.orgs import create_project

    admin_p = principal_for_user(db, user)
    create_project(db, admin_p, slug="alpha", visibility="private")
    db.commit()
    member = orgs.create_user(db, admin_p, "member@example.com", password=PASSWORD)
    db.commit()  # exists, but was never given access to "alpha"
    egress.set_transport(httpx.MockTransport(memos_handler))
    i = make_conn(
        db,
        member,
        config={"base_url": "https://memos.example.com", "inbox_scope": "project", "inbox_project": "alpha"},
    )
    res = dp.run_pull(db.get_bind(), i.id)
    assert res.error and "write access" in res.error and not inbox(db)


# --- ownership, through the web ------------------------------------------------------------------------------


def signed_in(app, email):
    c = TestClient(app)
    assert (
        c.post("/login", data={"email": email, "password": PASSWORD}, follow_redirects=False).status_code
        == 303
    )
    return c


def post(client, path, **data):
    token = csrf_of(client.get("/settings/connections").text)
    return client.post(path, data={"csrf_token": token, **data}, follow_redirects=False)


def two_users(authed):
    client, app, _ = authed
    with Session(app.state.engine) as s:
        admin = s.exec(select(User).where(User.email == "admin@example.com")).one()
        orgs.create_user(s, principal_for_user(s, admin), "bea@example.com", password=PASSWORD)
        s.commit()
    return client, signed_in(app, "bea@example.com"), app


def create(client, plugin="memos", **kw):
    data = {"plugin": plugin, "name": "My Memos", "cfg_base_url": "https://memos.example.com", "cfg_tag": "memory",
            "cfg_inbox_scope": "user", "secret_token": TOKEN, "pull_interval_minutes": "60"} | kw  # fmt: skip
    return post(client, "/settings/connections", **data)


def raw(app, sql, **kw):
    with Session(app.state.engine) as s:
        return s.execute(text(sql), kw).fetchall()


def test_creating_a_connection_seals_the_token_and_never_shows_it_again(authed):
    client, app, _ = authed
    r = create(client)
    assert r.status_code == 303
    iid = r.headers["location"].split("/settings/connections/")[1].split("?")[0]
    dump = " ".join(str(x) for x in raw(app, "select * from plugin_instances"))
    assert TOKEN not in dump and "v1." in dump  # at rest: ciphertext only
    pages = [client.get(p).text for p in ("/settings/connections", f"/settings/connections/{iid}")]
    assert all(TOKEN not in p for p in pages) and "saved" in pages[1]
    # a failed edit re-renders the form: still no token, even the one just typed
    bad = post(
        client,
        f"/settings/connections/{iid}",
        name="x",
        cfg_base_url="https://memos.example.com",
        secret_token="typed-secret-9999",
        pull_interval_minutes="1",
    )
    assert bad.status_code == 422 and "typed-secret-9999" not in bad.text
    # blank keeps the saved token; typing replaces it
    keep = post(
        client,
        f"/settings/connections/{iid}",
        name="Renamed",
        cfg_base_url="https://memos.example.com",
        secret_token="",
        pull_interval_minutes="60",
    )
    assert keep.status_code == 303
    with Session(app.state.engine) as s:
        i = s.get(PluginInstance, iid)
        assert (
            i.name == "Renamed"
            and connection_secrets.open_(i.id, "token", i.sealed_secrets["token"]) == TOKEN
        )
        assert i.secret_refs == {} and i.personal and not i.enabled
    post(
        client,
        f"/settings/connections/{iid}",
        name="Renamed",
        cfg_base_url="https://memos.example.com",
        secret_token="replacement-777777",
        pull_interval_minutes="60",
    )
    with Session(app.state.engine) as s:
        i = s.get(PluginInstance, iid)
        assert connection_secrets.open_(i.id, "token", i.sealed_secrets["token"]) == "replacement-777777"


def test_the_form_refuses_unreachable_destinations_and_saves_nothing(authed):
    client, app, _ = authed
    for url in (
        "https://evil-loopback.example",
        "https://meta.example",
        "http://memos.example.com",
        "https://notes.lan",
        "https://nonexistent.invalid",
    ):
        r = create(client, cfg_base_url=url)
        assert r.status_code == 422, url
    assert raw(app, "select count(*) from plugin_instances")[0][0] == 0


def test_nobody_else_can_see_open_change_test_pull_or_delete_a_connection(authed):
    admin_client, bea, app = two_users(authed)
    iid = create(admin_client).headers["location"].split("/settings/connections/")[1].split("?")[0]
    assert bea.get("/settings/connections/" + iid).status_code == 404
    assert "My Memos" not in bea.get("/settings/connections").text
    for action in ("", "/toggle", "/test", "/pull", "/delete"):
        r = post(bea, f"/settings/connections/{iid}{action}", name="hijack")
        assert r.status_code == 404, action
    assert raw(app, "select count(*) from plugin_instances")[0][0] == 1


def test_an_admin_cannot_open_edit_or_redirect_someone_elses_connection(authed):
    admin_client, bea, app = two_users(authed)
    iid = create(bea).headers["location"].split("/settings/connections/")[1].split("?")[0]
    assert admin_client.get(f"/settings/connections/{iid}").status_code == 404
    assert admin_client.get(f"/plugins/{iid}").status_code == 404  # the system screens refuse it too
    token = csrf_of(admin_client.get("/plugins").text)
    r = admin_client.post(
        f"/plugins/{iid}", data={"csrf_token": token, "name": "x", "cfg_base_url": "https://attacker.example"}
    )
    assert r.status_code == 404  # otherwise an admin could aim a person's token at their own server
    page = admin_client.get("/plugins").text
    assert "My Memos" not in page and "1 connection" in page
    with Session(app.state.engine) as s:
        assert s.get(PluginInstance, iid).config["base_url"] == "https://memos.example.com"


def test_only_connector_types_that_opted_in_can_be_used_by_a_person(authed):
    client, app, _ = authed
    for key in ("apprise", "obsidian", "nonexistent"):
        assert client.get(f"/settings/connections/new?plugin={key}").status_code == 404
        assert create(client, plugin=key).status_code == 404
    assert [p.info.key for p in connections.available_plugins()] == [
        "joplin",
        "memos",
        "obsidian-rest",
        "webhook",
    ]
    assert raw(app, "select count(*) from plugin_instances")[0][0] == 0


def test_team_scope_inbox_is_refused_and_the_limits_hold(authed):
    client, app, _ = authed
    assert create(client, cfg_inbox_scope="team").status_code == 422
    assert create(client, pull_interval_minutes="5").status_code == 422  # not faster than 15 minutes
    for n in range(connections.MAX_PER_USER):
        assert create(client, name=f"c{n}").status_code == 303
    assert create(client, name="one too many").status_code == 403
    assert raw(app, "select count(*) from plugin_instances")[0][0] == connections.MAX_PER_USER


# --- the admin switch and deactivation -------------------------------------------------------------------------


def test_switching_connections_off_stops_them_everywhere(authed):
    client, app, _ = authed
    iid = create(client).headers["location"].split("/settings/connections/")[1].split("?")[0]
    with Session(app.state.engine) as s:
        i = s.get(PluginInstance, iid)
        i.enabled = True
        s.add(i)
        st = s.get(InstanceSettings, 1)
        st.connections_enabled = False
        s.add(st)
        s.commit()
    egress.set_transport(httpx.MockTransport(memos_handler))
    assert "switched off" in dp.run_pull(app.state.engine, iid).error
    assert dp.pulls_due(app.state.engine) == []
    assert create(client, name="new").status_code == 403
    assert post(client, f"/settings/connections/{iid}/test").status_code == 403
    assert not inbox(Session(app.state.engine))
    assert "switched off" in client.get("/settings/connections").text
    assert (
        post(client, f"/settings/connections/{iid}/delete").status_code == 303
    )  # removing is always allowed


def test_deactivating_a_person_stops_their_connections_and_destroys_their_tokens(authed):
    admin_client, bea, app = two_users(authed)
    iid = create(bea).headers["location"].split("/settings/connections/")[1].split("?")[0]
    with Session(app.state.engine) as s:
        admin = s.exec(select(User).where(User.email == "admin@example.com")).one()
        beau = s.exec(select(User).where(User.email == "bea@example.com")).one()
        i = s.get(PluginInstance, iid)
        i.enabled = True
        s.add(i)
        s.commit()
        orgs.set_user_active(s, principal_for_user(s, admin), beau, False)
        s.commit()
        i = s.get(PluginInstance, iid)
        assert not i.enabled and i.sealed_secrets == {}


# --- webhooks: both levels ----------------------------------------------------------------------------------------


def test_a_personal_webhook_gets_only_events_about_its_owner(db, user):
    other = orgs.create_user(db, principal_for_user(db, user), "o@example.com", password=PASSWORD)
    db.commit()
    mine = make_conn(
        db,
        other,
        key="webhook",
        secrets={"url": "https://hook.example.com/x"},
        scopes=["project"],
        events=["plugin.failed"],
        name="w",
    )
    system = PluginInstance(
        plugin_key="webhook",
        name="sys",
        owner_user_id=user.id,
        scopes=["project"],
        events=["plugin.failed"],
        enabled=True,
    )
    db.add(system)
    db.commit()
    vis = ev_mod.Visibility(db)
    # a scope-less system event (some system plugin failing, owner unknown): the system webhook may get it, a person's may not
    assert ev_mod.instance_allows(
        system, vis, scope=None, project_slug=None, team_slug=None, owner_user_id=None
    )
    assert not ev_mod.instance_allows(
        mine, vis, scope=None, project_slug=None, team_slug=None, owner_user_id=None
    )
    assert not ev_mod.instance_allows(
        mine, vis, scope=None, project_slug=None, team_slug=None, owner_user_id=user.id
    )
    assert ev_mod.instance_allows(
        mine, vis, scope=None, project_slug=None, team_slug=None, owner_user_id=other.id
    )
    # project events: only for projects its owner can read (here, none)
    assert not ev_mod.instance_allows(
        mine, vis, scope="project", project_slug="alpha", team_slug=None, owner_user_id=None
    )
    # someone else's personal memory never
    assert not ev_mod.instance_allows(
        mine, vis, scope="user", project_slug=None, team_slug=None, owner_user_id=user.id
    )


def test_a_personal_webhook_signs_with_its_own_sealed_key_and_target(db, user):
    seen = []
    egress.set_transport(httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(204)))
    i = make_conn(
        db,
        user,
        key="webhook",
        secrets={"url": "https://hook.example.com/in/abc", "signing_key": "sign-key-0123456789"},
        events=["plugin.failed"],
    )
    res = dp.deliver_test_event(db.get_bind(), i.id)
    assert res.ok and seen[0].url.path == "/in/abc" and seen[0].url.host == "93.184.216.37"
    from acm_hub.plugins.builtin.webhook import sign

    assert seen[0].headers["x-acm-signature"] == sign("sign-key-0123456789", seen[0].content)


def test_notifications_stay_system_level(authed):
    client, app, _ = authed
    r = create(client, plugin="apprise")
    assert r.status_code == 404 and "apprise" not in " ".join(
        str(x) for x in raw(app, "select plugin_key from plugin_instances")
    )
    assert json.dumps(sorted(p.info.key for p in connections.available_plugins())).count("apprise") == 0
