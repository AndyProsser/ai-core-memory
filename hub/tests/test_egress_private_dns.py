"""MEMORY_HUB_EGRESS_RESOLVE_PRIVATE: plain http to a hostname that resolves only to private addresses.

Each test here guards one part of the rule — off by default, *every* answer must be private, and the connection goes to
the address that was checked (so DNS can't swap in a public host between the check and the request)."""

import json
import socket

import httpx
import pytest

from acm_hub.plugins import egress, remote
from acm_hub.plugins.builtin.apprise_sink import _check_targets

SVC = "memos.memos.svc.cluster.local"


@pytest.fixture(autouse=True)
def clean_egress():
    yield
    egress.set_transport(None)


def fake_dns(monkeypatch, answers):
    """answers: host -> list of IPs, or a callable returning one (to change answers between lookups)."""

    def getaddrinfo(host, port, *a, **kw):
        got = answers.get(host)
        ips = got() if callable(got) else got
        if not ips:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [
            (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))
            for ip in ips
        ]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture
def resolve_on(monkeypatch):
    monkeypatch.setenv("MEMORY_HUB_EGRESS_RESOLVE_PRIVATE", "true")


def test_off_by_default_a_private_resolving_hostname_is_still_refused(monkeypatch):
    fake_dns(monkeypatch, {SVC: ["10.43.0.5"]})
    with pytest.raises(egress.EgressError, match="Plain http"):
        egress.check_url(f"http://{SVC}:5230/api/v1/memos")


def test_on_a_hostname_whose_every_answer_is_private_is_allowed(monkeypatch, resolve_on):
    fake_dns(monkeypatch, {SVC: ["10.43.0.5"], "nas.lan": ["192.168.1.5", "fd00::5"]})
    egress.check_url(f"http://{SVC}:5230/x")
    egress.check_url("http://nas.lan/x")


def test_on_one_public_answer_is_enough_to_refuse(monkeypatch, resolve_on):
    fake_dns(monkeypatch, {"mixed.example": ["10.0.0.9", "93.184.216.34"]})
    with pytest.raises(egress.EgressError):
        egress.check_url("http://mixed.example/x")


def test_on_public_and_unresolvable_names_are_refused(monkeypatch, resolve_on):
    fake_dns(monkeypatch, {"hooks.example.com": ["93.184.216.34"]})
    for url in ("http://hooks.example.com/x", "http://does-not-resolve.invalid/x", "http://8.8.8.8/x"):
        with pytest.raises(egress.EgressError):
            egress.check_url(url)


def test_https_never_depends_on_resolution(monkeypatch, resolve_on):
    fake_dns(monkeypatch, {})  # nothing resolves; https needs no private check
    assert egress.check_url("https://hooks.example.com/x") == "https://hooks.example.com/x"


def test_client_connects_to_the_checked_address_and_keeps_the_host_header(monkeypatch, resolve_on):
    fake_dns(monkeypatch, {SVC: ["10.43.0.5"]})
    seen = {}

    def handler(r: httpx.Request) -> httpx.Response:
        seen["url"], seen["host"], seen["auth"] = (
            str(r.url),
            r.headers["host"],
            r.headers.get("authorization"),
        )
        return httpx.Response(200, json={"ok": True})

    egress.set_transport(httpx.MockTransport(handler))
    r = egress.EgressClient().get(
        f"http://{SVC}:5230/api/v1/memos?x=1", headers={"Authorization": "Bearer t"}
    )
    assert r.status_code == 200
    assert seen == {
        "url": "http://10.43.0.5:5230/api/v1/memos?x=1",
        "host": f"{SVC}:5230",
        "auth": "Bearer t",
    }


def test_a_dns_answer_that_turns_public_after_the_check_is_never_connected_to(monkeypatch, resolve_on):
    answers = iter(
        [["10.43.0.5"], ["93.184.216.34"]]
    )  # rebinding: private for the check, public for the connect
    fake_dns(monkeypatch, {SVC: lambda: next(answers)})
    egress.set_transport(httpx.MockTransport(lambda r: pytest.fail(f"connected to {r.url}")))
    with pytest.raises(egress.EgressError):
        egress.EgressClient().get(f"http://{SVC}/x")


def test_ip_literals_and_https_are_passed_through_unpinned(monkeypatch, resolve_on):
    fake_dns(monkeypatch, {})
    urls = []
    egress.set_transport(httpx.MockTransport(lambda r: urls.append(str(r.url)) or httpx.Response(204)))
    egress.EgressClient().get("http://192.168.1.9:9000/a")
    egress.EgressClient().get("https://hooks.example.com/b")
    assert urls == ["http://192.168.1.9:9000/a", "https://hooks.example.com/b"]


def test_apprise_plaintext_targets_follow_the_same_rule(monkeypatch):
    fake_dns(monkeypatch, {"ntfy.ntfy.svc.cluster.local": ["10.43.1.2"]})
    url = "json://ntfy.ntfy.svc.cluster.local/topic"
    assert "plain text" in (_check_targets([url], []) or "")
    monkeypatch.setenv("MEMORY_HUB_EGRESS_RESOLVE_PRIVATE", "true")
    assert _check_targets([url], []) is None


def test_remote_plugin_registration_accepts_a_service_name_only_when_enabled(monkeypatch):
    fake_dns(monkeypatch, {"feed-plugin": ["10.43.2.3"]})
    spec = json.dumps([{"key": "feed", "url": "http://feed-plugin:9000", "secret_env": "FEED_PLUGIN_SECRET"}])
    specs, errors = remote.load_specs(spec)
    assert not specs and errors
    monkeypatch.setenv("MEMORY_HUB_EGRESS_RESOLVE_PRIVATE", "true")
    specs, errors = remote.load_specs(spec)
    assert [s.key for s in specs] == ["feed"] and not errors
