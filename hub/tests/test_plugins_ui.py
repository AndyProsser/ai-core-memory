from pathlib import Path

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from acm_hub import dispatcher as dp
from acm_hub.models import InboxItem, PluginDelivery, PluginInstance, User
from acm_hub.plugins import egress
from acm_hub.security import hash_password

from .conftest import PASSWORD, csrf_of


@pytest.fixture(autouse=True)
def reset_egress():
    yield
    egress.set_transport(None)


def add_webhook(client, token, events=("proposal.pending", "digest.weekly"), **over):
    data = {
        "csrf_token": token,
        "plugin": "webhook",
        "name": "Ops channel",
        "enabled": "on",
        "secret_url": "OPS_HOOK_URL",
        "secret_signing_key": "OPS_HOOK_KEY",
        "egress": "metadata",
        "scopes": ["project"],
        "events": list(events),
    } | over
    return client.post("/plugins", data=data, follow_redirects=False)


def instance_id(app):
    with Session(app.state.engine) as s:
        return s.exec(select(PluginInstance)).one().id


def test_plugins_are_admin_only_and_csrf_protected(authed):
    client, app, token = authed
    assert client.get("/plugins").status_code == 200 and "Add a plugin" in client.get("/plugins").text
    assert client.post("/plugins", data={"plugin": "webhook", "name": "x"}).status_code == 403  # no CSRF
    with Session(app.state.engine) as s:
        s.add(User(email="member@example.com", password_hash=hash_password(PASSWORD)))
        s.commit()
    member = TestClient(app)
    member.post("/login", data={"email": "member@example.com", "password": PASSWORD})
    t2 = csrf_of(member.get("/memory").text)
    for path in ("/plugins", "/plugins/new?plugin=webhook"):
        assert member.get(path).status_code == 403
    assert (
        member.post("/plugins", data={"csrf_token": t2, "plugin": "webhook", "name": "x"}).status_code == 403
    )
    assert TestClient(app).get("/plugins", follow_redirects=False).status_code == 303  # signed out


def test_secrets_are_env_var_names_never_values(authed, monkeypatch):
    client, app, token = authed
    r = add_webhook(client, token, secret_url="https://hooks.slack.com/services/T0/B0/XXXXSECRETXXXX")
    assert (
        r.status_code == 422
        and "environment variable" in r.text
        and "XXXXSECRETXXXX" not in client.get("/plugins").text
    )
    with Session(app.state.engine) as s:
        assert s.exec(select(PluginInstance)).all() == []
    monkeypatch.setenv("OPS_HOOK_URL", "https://hooks.slack.com/services/T0/B0/REALSECRETVALUE")
    assert add_webhook(client, token).status_code == 303
    iid = instance_id(app)
    page = client.get(f"/plugins/{iid}").text
    assert (
        "OPS_HOOK_URL" in page and "set in the hub's environment" in page and "REALSECRETVALUE" not in page
    )  # name + status, never the value
    assert "not set in the hub" in page  # the signing key env var isn't set
    with Session(app.state.engine) as s:
        assert "REALSECRETVALUE" not in str(s.exec(select(PluginInstance)).one().model_dump())


def test_validation_scopes_projects_and_user_ack(authed):
    client, app, token = authed
    r = client.post(
        "/plugins",
        data={
            "csrf_token": token,
            "plugin": "webhook",
            "name": "x",
            "scopes": ["user"],
            "events": ["proposal.pending"],
        },
    )
    assert r.status_code == 422 and "acknowledgement" in r.text
    r = client.post(
        "/plugins",
        data={
            "csrf_token": token,
            "plugin": "webhook",
            "name": "x",
            "scopes": ["project"],
            "projects": "no-such-project",
        },
    )
    assert r.status_code == 422 and "No project named" in r.text
    r = client.post(
        "/plugins", data={"csrf_token": token, "plugin": "webhook", "name": "", "egress": "everything"}
    )
    assert r.status_code == 422 and "name" in r.text and "egress" in r.text.lower()
    r = client.post(
        "/plugins",
        data={
            "csrf_token": token,
            "plugin": "obsidian",
            "name": "v",
            "cfg_vault_path": "/definitely/not/here",
        },
    )
    assert r.status_code == 422 and "isn&#39;t a directory" in r.text
    assert (
        client.post("/plugins", data={"csrf_token": token, "plugin": "nonexistent", "name": "x"}).status_code
        == 404
    )
    ok = client.post(
        "/plugins",
        data={
            "csrf_token": token,
            "plugin": "webhook",
            "name": "ack-ok",
            "scopes": ["user"],
            "user_scope_ack": "on",
        },
        follow_redirects=False,
    )
    assert ok.status_code == 303


def test_list_warns_about_what_an_instance_can_see(authed):
    client, app, token = authed
    client.post(
        "/plugins", data={"csrf_token": token, "plugin": "webhook", "name": "sees-nothing", "enabled": "on"}
    )
    client.post(
        "/plugins",
        data={
            "csrf_token": token,
            "plugin": "webhook",
            "name": "sees-everything",
            "enabled": "on",
            "scopes": ["project", "user"],
            "user_scope_ack": "on",
            "egress": "full",
        },
    )
    page = client.get("/plugins").text
    assert (
        "nothing — no scopes allowed" in page and "full text" in page and "includes personal memory" in page
    )


def test_test_button_reports_success_and_failure_without_leaking(authed, monkeypatch):
    client, app, token = authed
    monkeypatch.setenv("OPS_HOOK_URL", "https://hooks.example.com/services/TOPSECRETTOKEN")
    add_webhook(client, token)
    iid = instance_id(app)
    egress.set_transport(httpx.MockTransport(lambda r: httpx.Response(200)))
    r = client.post(f"/plugins/{iid}/test", data={"csrf_token": token}, follow_redirects=False)
    assert "Test%20sent" in r.headers["location"]
    egress.set_transport(httpx.MockTransport(lambda r: httpx.Response(500)))
    r = client.post(f"/plugins/{iid}/test", data={"csrf_token": token}, follow_redirects=False)
    assert "Test%20failed" in r.headers["location"] and "TOPSECRETTOKEN" not in r.headers["location"]
    page = client.get(f"/plugins/{iid}").text
    assert "failing" in page and "HTTP 500" in page and "TOPSECRETTOKEN" not in page
    client.post(f"/plugins/{iid}/toggle", data={"csrf_token": token})
    assert (
        "Turn%20the%20plugin%20on"
        in client.post(f"/plugins/{iid}/test", data={"csrf_token": token}, follow_redirects=False).headers[
            "location"
        ]
    )


def test_end_to_end_notification_when_a_proposal_needs_review(authed, monkeypatch):
    client, app, token = authed
    sent = []
    egress.set_transport(httpx.MockTransport(lambda r: sent.append(r.read()) or httpx.Response(200)))
    monkeypatch.setenv("OPS_HOOK_URL", "https://hooks.example.com/x")
    add_webhook(client, token)
    text = "Always send an idempotency key on webhook retries so a replay can never double charge a customer."
    for n in ("keys-one", "keys-two"):
        client.post(
            "/memory/new",
            data={
                "csrf_token": token,
                "name": n,
                "description": f"{n} about retries",
                "body": text,
                "type": "project",
                "scope": "project",
                "project": "payments",
            },
        )
    client.post("/review/consolidate", data={"csrf_token": token})
    stats = dp.dispatch_once(app.state.engine)
    assert stats.delivered >= 1
    bodies = b"".join(sent).decode()
    assert (
        "proposal.pending" in bodies and "Merge" in bodies and "idempotency key" not in bodies
    )  # the summary, not the memory text


def test_obsidian_pull_from_the_ui_lands_in_the_inbox_as_external(authed, tmp_path):
    client, app, token = authed
    vault = tmp_path / "vault"
    (vault / "Inbox").mkdir(parents=True)
    (vault / "Inbox" / "clip.md").write_text("# A clipped idea\n\nTry property tests.\n")
    r = client.post(
        "/plugins",
        data={
            "csrf_token": token,
            "plugin": "obsidian",
            "name": "My vault",
            "enabled": "on",
            "cfg_vault_path": str(vault),
            "cfg_folders": "Inbox",
            "cfg_tag": "",
            "cfg_inbox_scope": "user",
            "cfg_max_files": "50",
            "cfg_max_file_kb": "64",
            "cfg_export_folder": "Memory hub",
            "pull_interval_minutes": "30",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    iid = instance_id(app)
    pulled = client.post(f"/plugins/{iid}/pull", data={"csrf_token": token}, follow_redirects=False)
    assert "1%20new%20item" in pulled.headers["location"]
    review = client.get("/review").text
    assert "A clipped idea" in review and "plugin:obsidian" in review
    with Session(app.state.engine) as s:
        item = s.exec(select(InboxItem)).one()
        assert (
            item.source == "plugin:obsidian" and item.external_ref == "Inbox/clip.md" and item.status == "new"
        )
    assert Path(vault / "Inbox" / "clip.md").read_text().startswith("# A clipped idea")  # untouched


def test_removing_a_plugin_keeps_what_it_captured(authed, tmp_path):
    client, app, token = authed
    vault = tmp_path / "v"
    (vault / "Inbox").mkdir(parents=True)
    (vault / "Inbox" / "n.md").write_text("# Keep me\n")
    client.post(
        "/plugins",
        data={
            "csrf_token": token,
            "plugin": "obsidian",
            "name": "v",
            "enabled": "on",
            "cfg_vault_path": str(vault),
            "cfg_folders": "Inbox",
            "cfg_inbox_scope": "user",
            "cfg_max_files": "5",
            "cfg_max_file_kb": "64",
            "cfg_export_folder": "Memory hub",
        },
    )
    iid = instance_id(app)
    client.post(f"/plugins/{iid}/pull", data={"csrf_token": token})
    assert (
        client.post(f"/plugins/{iid}/delete", data={"csrf_token": token}, follow_redirects=False).status_code
        == 303
    )
    with Session(app.state.engine) as s:
        assert s.exec(select(PluginInstance)).all() == [] and s.exec(select(PluginDelivery)).all() == []
        assert [i.title for i in s.exec(select(InboxItem)).all()] == ["Keep me"]


def test_scheduler_loop_delivers_and_survives_a_bad_tick(authed, monkeypatch):
    from acm_hub.app import _plugin_loop

    client, app, token = authed
    egress.set_transport(httpx.MockTransport(lambda r: httpx.Response(200)))
    monkeypatch.setenv("OPS_HOOK_URL", "https://hooks.example.com/x")
    add_webhook(client, token, events=("record.created",))
    client.post(
        "/memory/new",
        data={
            "csrf_token": token,
            "name": "loop-rec",
            "description": "d",
            "type": "project",
            "scope": "project",
            "project": "payments",
        },
    )
    calls = {"n": 0}
    real = dp.run_scheduled

    def flaky(engine, now=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("one bad tick")
        return real(engine, now)

    monkeypatch.setattr(dp, "run_scheduled", flaky)

    async def go():
        with anyio.move_on_after(1.5):
            await _plugin_loop(app.state.engine, first_delay=0, every=0.1)

    anyio.run(go)
    assert calls["n"] >= 2  # the loop outlived the failing tick
    with Session(app.state.engine) as s:
        assert [d.status for d in s.exec(select(PluginDelivery)).all()] == ["delivered"]


def test_cli_plugin_switch_works_offline_and_never_loads_plugin_code(settings, monkeypatch, capsys):
    from acm_hub.cli import main
    from acm_hub.db import make_engine
    from acm_hub.models import InstanceSettings, PluginInstance
    from acm_hub.plugins import registry

    eng = make_engine(settings)
    from acm_hub.db import migrate

    migrate(eng)
    with Session(eng) as s:
        s.add(InstanceSettings(id=1))
        u = User(email="a@b.co", is_admin=True)
        s.add(u)
        s.commit()
        inst = PluginInstance(
            plugin_key="webhook",
            name="Ops",
            owner_user_id=u.id,
            enabled=True,
            scopes=["project"],
            last_status="error",
            last_error="boom",
        )
        s.add(inst)
        s.commit()
        iid = inst.id
    monkeypatch.setattr(
        registry,
        "load_all",
        lambda **k: (_ for _ in ()).throw(AssertionError("the CLI must not load plugin code")),
    )
    assert main(["plugins"]) == 0
    out = capsys.readouterr().out
    assert iid in out and "on" in out and "boom" in out
    assert main(["plugins", "disable", iid]) == 0
    with Session(eng) as s:
        assert s.get(PluginInstance, iid).enabled is False
    assert main(["plugins", "enable", iid]) == 0
    assert main(["plugins", "disable", "NOPE"]) == 2
