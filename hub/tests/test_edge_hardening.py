"""The network edge (docs/SECURITY.md § Network edge): headers, proxy trust, cookie flags, throttles, body caps.

Each test here fails if the protection it names is removed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from acm_hub.app import MAX_BODY_BYTES, create_app
from acm_hub.config import DEFAULT_TRUSTED_PROXIES, Settings
from acm_hub.plugins.egress import EgressError, check_url, plaintext_allowed
from acm_hub.security import (
    _MAX_KEYS,
    LoginThrottle,
    SlidingWindowLimiter,
    is_link_local,
    is_local_or_private,
)
from acm_hub.web.deps import STASH_PER_USER, STASH_TOTAL, stash_put

from .conftest import PASSWORD, csrf_of, setup_admin

EXPECTED_HEADERS = {
    "content-security-policy": "default-src 'none'",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "same-origin",
    "permissions-policy": "camera=()",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
}


def https_hub(settings, **changes):
    settings.public_url = "https://memory.example.com"
    for k, v in changes.items():
        setattr(settings, k, v)
    root = create_app(settings)
    return root, TestClient(root, base_url="https://memory.example.com")


# --- headers ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/setup", "/healthz", "/static/app.css", "/no-such-page", "/mcp"])
def test_every_response_carries_the_security_headers(hub, path):
    """Including /mcp and the 404 page: the headers are added by the outermost layer, not by one router."""
    client, _ = hub
    r = client.get(path)
    for name, fragment in EXPECTED_HEADERS.items():
        assert fragment in r.headers.get(name, ""), f"{name} missing on {path}"
    assert "strict-transport-security" not in r.headers  # plain-http hub: HSTS would be meaningless
    assert r.headers["cache-control"] == (
        "public, max-age=3600" if path.startswith("/static/") else "no-store"
    )


def test_hsts_is_sent_when_the_public_url_is_https(settings):
    _, client = https_hub(settings)
    with client:
        assert client.get("/healthz").headers["strict-transport-security"] == "max-age=31536000"


def test_hsts_can_be_switched_off(settings):
    _, client = https_hub(settings, hsts_max_age=0)
    with client:
        assert "strict-transport-security" not in client.get("/healthz").headers


# --- cookies and proxy trust --------------------------------------------------------------------------------------


def test_session_cookie_is_secure_and_host_prefixed_when_public_url_is_https(settings):
    """Even when the hop the hub sees is plain http from a private address (a TLS-terminating proxy)."""
    root, client = https_hub(settings)
    with client:
        r = setup_admin(client, root.fastapi)
        cookie = r.headers["set-cookie"]
        assert cookie.startswith("__Host-acm_session=")
        assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie.replace("Lax", "lax")
        assert "Domain" not in cookie and "Path=/" in cookie


def test_cookie_is_secure_behind_a_proxy_that_the_hub_sees_as_plain_http(settings):
    """The case the old rule got wrong: TLS ends at the proxy, so the hub sees http from a private address. The https
    public URL, not that last hop, decides."""
    settings.public_url = "https://memory.example.com"
    root = create_app(settings)
    with TestClient(
        root, base_url="http://memory.example.com"
    ) as client:  # scheme http, peer "testclient" (local)
        cookie = setup_admin(client, root.fastapi).headers["set-cookie"]
        assert "Secure" in cookie and cookie.startswith("__Host-acm_session=")


def test_logout_clears_a_host_prefixed_cookie_with_secure(settings):
    """A `__Host-` cookie is only cleared by a Set-Cookie that is itself Secure; a plain delete would be ignored."""
    root, client = https_hub(settings)
    with client:
        setup_admin(client, root.fastapi)
        token = csrf_of(client.get("/memory").text)
        r = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
        assert r.status_code == 303
        cleared = r.headers["set-cookie"]
        assert cleared.startswith("__Host-acm_session=") and "Secure" in cleared and "Max-Age=0" in cleared


def test_plain_http_hub_keeps_the_plain_cookie_name(hub):
    client, app = hub
    r = setup_admin(client, app)
    assert r.headers["set-cookie"].startswith("acm_session=")


def test_cookie_name_and_secure_follow_settings(settings):
    assert Settings().session_cookie_name == "acm_session"
    s = Settings()
    s.public_url = "https://x.example"
    assert s.session_cookie_name == "__Host-acm_session"
    s.cookie_secure = False  # an explicit opt-out can't also promise a Secure-only name
    assert s.session_cookie_name == "acm_session"


def test_a_direct_caller_cannot_claim_https_with_a_header(settings):
    """X-Forwarded-Proto is honoured only where uvicorn was told to trust the peer (it rewrites the ASGI scheme there).
    The app reading the header itself would let anyone who can reach it directly skip the plain-http token ban."""
    settings.trust_proxy = True
    root = create_app(settings)
    client = TestClient(root, client=("8.8.8.8", 4000))
    with client:
        r = client.post(
            "/mcp",
            headers={"Authorization": "Bearer acm_live_whatever", "X-Forwarded-Proto": "https"},
            json={},
        )
        assert r.status_code == 400 and "only accepted over HTTPS" in r.text


def test_trusted_proxies_are_what_uvicorn_is_told(monkeypatch):
    s = Settings()
    assert s.forwarded_allow_ips is None  # proxy trust off: nothing is trusted
    s.trust_proxy = True
    assert s.forwarded_allow_ips == DEFAULT_TRUSTED_PROXIES and "*" not in DEFAULT_TRUSTED_PROXIES
    s.trusted_proxies = "10.42.0.0/16"
    assert s.forwarded_allow_ips == "10.42.0.0/16"
    monkeypatch.setenv("MEMORY_HUB_TRUSTED_PROXIES", " 192.0.2.1 ")
    assert Settings().trusted_proxies == "192.0.2.1"


def test_serve_never_hands_uvicorn_a_wildcard_by_default(monkeypatch):
    """The regression this guards: `forwarded_allow_ips="*"` made uvicorn take the *left-most* X-Forwarded-For entry,
    which the client writes."""
    import uvicorn

    from acm_hub import cli

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: seen.update(kw))
    monkeypatch.setenv("MEMORY_HUB_TRUST_PROXY", "true")
    monkeypatch.delenv("MEMORY_HUB_TRUSTED_PROXIES", raising=False)
    cli.cmd_serve(SimpleNamespace(host="127.0.0.1", port=1))
    assert seen["proxy_headers"] is True
    assert seen["forwarded_allow_ips"] == DEFAULT_TRUSTED_PROXIES
    assert seen["server_header"] is False


def test_real_uvicorn_ignores_a_forged_forwarded_for_from_an_untrusted_peer():
    """Pins the behaviour the whole proxy design leans on, against the installed uvicorn."""
    from uvicorn.middleware.proxy_headers import _TrustedHosts

    hosts = _TrustedHosts("10.42.0.0/16")
    # client forged "1.2.3.4", the ingress (10.42.0.9) appended the real peer 203.0.113.7
    assert hosts.get_trusted_client_address("1.2.3.4, 203.0.113.7")[0] == "203.0.113.7"
    # the old wildcard setting believed the forgery
    assert _TrustedHosts("*").get_trusted_client_address("1.2.3.4, 203.0.113.7")[0] == "1.2.3.4"


def _scope(peer):
    return {"client": (peer, 5000)}


def _cdn_settings(**changes):
    s = Settings()
    s.trust_proxy, s.trusted_proxies, s.client_ip_header = True, "10.42.0.0/24", "cf-connecting-ip"
    for k, v in changes.items():
        setattr(s, k, v)
    return s


def test_client_ip_header_is_believed_from_a_trusted_proxy():
    from acm_hub.app import real_client_ip

    got = real_client_ip(_cdn_settings(), _scope("10.42.0.1"), {"cf-connecting-ip": " 203.0.113.7 "})
    assert got == "203.0.113.7"
    assert (
        real_client_ip(_cdn_settings(), _scope("10.42.0.1"), {"cf-connecting-ip": "2001:db8::1"})
        == "2001:db8::1"
    )


def test_client_ip_header_is_ignored_from_anyone_else():
    """A direct caller outside the trusted range writes the header itself; believing it would let it pick its bucket."""
    from acm_hub.app import real_client_ip

    forged = {"cf-connecting-ip": "203.0.113.7"}
    assert real_client_ip(_cdn_settings(), _scope("198.51.100.9"), forged) is None
    assert real_client_ip(_cdn_settings(trust_proxy=False), _scope("10.42.0.1"), forged) is None
    assert real_client_ip(_cdn_settings(client_ip_header=""), _scope("10.42.0.1"), forged) is None
    assert real_client_ip(_cdn_settings(trusted_proxies="*"), _scope("198.51.100.9"), forged) is None


@pytest.mark.parametrize("value", ["", "not-an-ip", "1.2.3.4, 5.6.7.8", "1.2.3.4:80", "999.1.1.1"])
def test_client_ip_header_must_be_a_single_ip_literal(value):
    from acm_hub.app import real_client_ip

    assert real_client_ip(_cdn_settings(), _scope("10.42.0.1"), {"cf-connecting-ip": value}) is None


def test_client_ip_header_setting_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("MEMORY_HUB_CLIENT_IP_HEADER", " CF-Connecting-IP ")
    assert Settings().client_ip_header == "cf-connecting-ip"


def test_per_ip_limits_follow_the_real_client_behind_a_shared_proxy_address(settings):
    """The deployed bug: every client arrives from the one proxy address, so the 'per-IP' limit was one shared bucket."""
    settings.trust_proxy, settings.trusted_proxies, settings.client_ip_header = (
        True,
        "10.42.0.0/24",
        "cf-connecting-ip",
    )
    root = create_app(settings)
    proxy = TestClient(root, client=("10.42.0.1", 1))
    with proxy:
        setup_admin(proxy, root.fastapi)
        bad = {"email": "admin@example.com", "password": "wrong-password-1"}
        for _ in range(8):
            assert (
                proxy.post("/login", data=bad, headers={"CF-Connecting-IP": "203.0.113.5"}).status_code == 401
            )
        blocked = proxy.post(
            "/login",
            data={**bad, "password": PASSWORD},
            headers={"CF-Connecting-IP": "203.0.113.5"},
        )
        assert blocked.status_code == 429
        ok = proxy.post(
            "/login",
            data={**bad, "password": PASSWORD},
            headers={"CF-Connecting-IP": "198.51.100.9"},
            follow_redirects=False,
        )
        assert ok.status_code == 303


def test_only_loopback_counts_as_a_local_name(monkeypatch):
    monkeypatch.setattr("acm_hub.security._LOCAL_HOSTNAMES", frozenset({"localhost"}))  # production's value
    assert is_local_or_private("localhost") and is_local_or_private("127.0.0.1")
    assert not is_local_or_private(
        "testclient"
    )  # the conftest widens this for TestClient only, via monkeypatch


# --- throttles ----------------------------------------------------------------------------------------------------


def test_checking_a_key_costs_no_memory():
    t = LoginThrottle()
    for i in range(500):
        assert not t.blocked(f"acct:{i}@example.com")
    assert t._fails == {}


def test_the_tables_stay_bounded_when_keys_are_attacker_chosen():
    t = LoginThrottle()
    for i in range(_MAX_KEYS * 2):
        t.failure(f"acct:{i}")
    assert len(t._fails) <= _MAX_KEYS + 1
    lim = SlidingWindowLimiter(5)
    for i in range(_MAX_KEYS * 2):
        lim.allow(f"ip:{i}")
    assert len(lim._hits) <= _MAX_KEYS + 1


def test_one_address_cannot_lock_someone_out_of_their_own_account():
    t = LoginThrottle()
    for _ in range(t.max_failures):
        t.attempt_failed("admin@example.com", "203.0.113.5")
    assert t.attempt_blocked("admin@example.com", "203.0.113.5")  # the guesser is stopped…
    assert not t.attempt_blocked("admin@example.com", "198.51.100.9")  # …the owner, elsewhere, is not


def test_many_addresses_guessing_one_account_still_hit_the_account_wide_budget():
    t = LoginThrottle()
    for i in range(LoginThrottle.ACCOUNT_LIMIT):
        t.attempt_failed("admin@example.com", f"198.51.100.{i}")
    assert t.attempt_blocked("admin@example.com", "192.0.2.1")


def test_a_success_elsewhere_does_not_reset_the_account_wide_budget():
    t = LoginThrottle()
    for i in range(LoginThrottle.ACCOUNT_LIMIT):
        t.attempt_failed("admin@example.com", f"198.51.100.{i}")
    t.attempt_succeeded("admin@example.com", "192.0.2.1")
    assert t.attempt_blocked("admin@example.com", "192.0.2.1")


def test_login_lockout_is_per_address_end_to_end(hub):
    client, app = hub
    setup_admin(client, app)
    guesser = TestClient(client.app, client=("203.0.113.5", 1))
    owner = TestClient(client.app, client=("198.51.100.9", 1))
    for _ in range(8):
        assert (
            guesser.post(
                "/login", data={"email": "admin@example.com", "password": "wrong-password-1"}
            ).status_code
            == 401
        )
    assert (
        guesser.post("/login", data={"email": "admin@example.com", "password": PASSWORD}).status_code == 429
    )
    ok = owner.post(
        "/login", data={"email": "admin@example.com", "password": PASSWORD}, follow_redirects=False
    )
    assert ok.status_code == 303


def test_password_checks_behind_a_live_session_are_throttled(authed):
    client, _, token = authed
    codes = [
        client.post(
            "/settings/password",
            data={"csrf_token": token, "current": "nope-nope-nope", "new": "x" * 14, "confirm": "x" * 14},
            follow_redirects=False,
        ).status_code
        for _ in range(10)
    ]
    assert codes[:8] == [303] * 8 and codes[8:] == [403, 403]


# --- cross-site requests ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_login_refuses_a_cross_site_submission_even_without_an_origin_header(hub, site):
    client, app = hub
    setup_admin(client, app)
    r = TestClient(client.app).post(
        "/login", data={"email": "admin@example.com", "password": PASSWORD}, headers={"Sec-Fetch-Site": site}
    )
    assert r.status_code == 403


def test_login_accepts_same_origin_and_address_bar_navigations(hub):
    client, app = hub
    setup_admin(client, app)
    for site in ("same-origin", "none"):
        r = TestClient(client.app).post(
            "/login",
            data={"email": "admin@example.com", "password": PASSWORD},
            headers={"Sec-Fetch-Site": site},
            follow_redirects=False,
        )
        assert r.status_code == 303


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_a_valid_csrf_token_is_not_enough_from_another_site(authed, site):
    client, _, token = authed
    r = client.post(
        "/settings/theme", data={"csrf_token": token, "theme": "dark"}, headers={"Sec-Fetch-Site": site}
    )
    assert r.status_code == 403
    ok = client.post(
        "/settings/theme",
        data={"csrf_token": token, "theme": "dark"},
        headers={"Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )
    assert ok.status_code == 303


def test_the_json_api_applies_the_same_fetch_site_rule(authed):
    client, _, token = authed
    body = {"name": "a-fact", "description": "d", "type": "feedback", "scope": "user"}
    bad = client.post(
        "/api/v1/records", json=body, headers={"X-CSRF-Token": token, "Sec-Fetch-Site": "cross-site"}
    )
    assert bad.status_code == 403
    good = client.post(
        "/api/v1/records", json=body, headers={"X-CSRF-Token": token, "Sec-Fetch-Site": "same-origin"}
    )
    assert good.status_code == 200


# --- body size ----------------------------------------------------------------------------------------------------


def test_an_oversized_declared_body_is_refused_before_any_route_runs(hub):
    client, _ = hub
    r = client.post("/login", content=b"x" * (MAX_BODY_BYTES + 1), headers={"content-type": "text/plain"})
    assert r.status_code == 413


def test_a_chunked_body_that_never_declares_its_size_is_cut_off(hub):
    client, _ = hub

    def chunks():
        for _ in range(MAX_BODY_BYTES // (1024 * 1024) + 2):
            yield b"x" * (1024 * 1024)

    r = client.post("/login", content=chunks(), headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413


def test_an_import_archive_may_exceed_the_ordinary_cap(authed):
    client, _, token = authed
    junk = b"\0" * (MAX_BODY_BYTES + 1024)
    r = client.post(
        "/data/import", data={"csrf_token": token}, files={"file": ("notes.txt", junk, "text/plain")}
    )
    assert r.status_code != 413  # reaches the importer, which says there are no markdown records


def test_import_previews_are_bounded_per_person_and_overall():
    state = SimpleNamespace(import_stash={})
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    for _ in range(5):
        stash_put(request, "u1", {"a.md": b"x"})
    assert sum(1 for v in state.import_stash.values() if v[0] == "u1") == STASH_PER_USER
    for i in range(STASH_TOTAL * 2):
        stash_put(request, f"user-{i}", {"a.md": b"x"})
    assert len(state.import_stash) <= STASH_TOTAL


# --- Host header --------------------------------------------------------------------------------------------------


def test_host_checking_is_off_unless_hosts_are_listed(settings):
    _, client = https_hub(settings)
    with client:
        assert TestClient(client.app, base_url="https://anything.test").get("/setup").status_code == 200


def test_unlisted_hosts_are_refused_once_hosts_are_listed(settings):
    _, client = https_hub(settings, allowed_hosts="hub.internal.svc")
    with client:
        for host, want in [
            ("memory.example.com", 200),
            ("hub.internal.svc", 200),
            ("HUB.internal.svc:8000", 200),
            ("localhost:8000", 200),
            ("evil.test", 400),
            ("memory.example.com.evil.test", 400),
        ]:
            r = client.get("/setup", headers={"Host": host})
            assert r.status_code == want, host
            assert "content-security-policy" in r.headers  # the refusal carries the headers too
        # kubelet probes by pod IP, so the health check is exempt
        assert client.get("/healthz", headers={"Host": "10.42.1.7:8000"}).status_code == 200


# --- plugin egress ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["http://169.254.169.254/latest/meta-data", "http://[fe80::1]/", "http://[::ffff:169.254.169.254]/"],
)
def test_plugins_cannot_reach_link_local_addresses_in_plaintext(url):
    with pytest.raises(EgressError):
        check_url(url)


def test_link_local_is_recognised_and_lan_addresses_still_work():
    assert is_link_local("169.254.169.254") and is_link_local("fe80::1") and not is_link_local("192.168.1.5")
    assert not plaintext_allowed("169.254.169.254")
    assert (
        plaintext_allowed("192.168.1.5") and plaintext_allowed("127.0.0.1") and plaintext_allowed("localhost")
    )
    check_url("http://10.0.0.7:8080/hook")
