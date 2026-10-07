"""The one place plugin code reaches the network, and the one place secrets are redacted.

Rules (docs/SECURITY.md § Plugins and egress): HTTPS only — except to loopback / a private LAN, which a
self-hosted hub legitimately talks to — an optional operator host allowlist, no redirects, bounded time and size.

"A private LAN" normally means a literal private IP. With MEMORY_HUB_EGRESS_RESOLVE_PRIVATE it also covers a hostname
whose every DNS answer is private (a Kubernetes Service, a LAN name); the egress client then connects to the address it
checked, so a DNS answer that changes between the check and the connection can't redirect plaintext traffic.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit

import httpx

from ..config import get_settings
from ..security import is_link_local, is_local_or_private

TIMEOUT = httpx.Timeout(10.0, connect=5.0)
MAX_RESPONSE_BYTES = 5 * 1024 * 1024

# Tests (and only tests) inject an httpx transport here; production leaves it None.
_transport: httpx.BaseTransport | None = None


class EgressError(Exception):
    """A plugin tried to reach somewhere it isn't allowed to."""


def set_transport(transport: httpx.BaseTransport | None) -> None:
    global _transport
    _transport = transport


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def resolve_private(host: str) -> list[str] | None:
    """Every address `host` resolves to, if there is at least one and all are loopback/private; otherwise None.

    All, not any: a name that answers with one private and one public address could be steered to the public one."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return None
    addrs = sorted({str(info[4][0]) for info in infos})
    if addrs and all(is_local_or_private(a) and not is_link_local(a) for a in addrs):
        return addrs
    return None


def plaintext_allowed(host: str | None) -> bool:
    """May plaintext (http, json://, …) go to this host? Loopback/private IPs always; private-resolving names on opt-in."""
    if not host:
        return False
    if is_link_local(host):  # 169.254.x / fe80:: — cloud metadata lives here; a plugin has no business there
        return False
    if is_local_or_private(host):
        return True
    if _is_ip_literal(host) or not get_settings().egress_resolve_private:
        return False
    return resolve_private(host) is not None


def _pin_plaintext(url: str, kw: dict) -> tuple[str, dict]:
    """For plain http to a hostname, connect to the private address we just verified, keeping the original Host."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "http" or host == "localhost" or _is_ip_literal(host):
        return url, kw
    addrs = resolve_private(host)
    if addrs is None:  # re-checked here, not trusted from check_url: this lookup is the one we connect with
        raise EgressError("Plain http is only allowed to localhost or a private network; use https.")
    ip = addrs[0]
    netloc = (f"[{ip}]" if ":" in ip else ip) + (f":{parts.port}" if parts.port else "")
    headers = httpx.Headers(kw.pop("headers", None))
    if "host" not in headers:
        headers["Host"] = parts.netloc.rpartition("@")[2]
    return parts._replace(netloc=netloc).geturl(), {**kw, "headers": headers}


# --- per-user connections: public targets only (docs/SECURITY.md § Per-user connections) ---------------------------

_getaddrinfo = socket.getaddrinfo  # tests substitute a resolver; production never changes it


def _private_allowlist() -> tuple[set[str], list[ipaddress.IPv4Network | ipaddress.IPv6Network]]:
    """The operator's MEMORY_HUB_CONNECTION_PRIVATE_HOSTS: names (exact) and IPs/CIDRs."""
    names: set[str] = set()
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in (get_settings().connection_private_hosts or "").split(","):
        item = raw.strip().lower()
        if not item:
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            names.add(item)
    return names, nets


def _unmap(ip: ipaddress.IPv4Address | ipaddress.IPv6Address):  # noqa: ANN202
    return ip.ipv4_mapped if ip.version == 6 and ip.ipv4_mapped else ip


def resolve_for_connection(host: str) -> tuple[list[str], bool]:
    """Every address `host` resolves to, each checked; and whether the target is a (deliberately allowed) private one.

    Public (globally routable) addresses pass. Anything else — loopback, RFC 1918, ULA, CGNAT, link-local, multicast,
    unspecified — is refused unless the operator listed the host (by name, IP or CIDR); link-local (cloud metadata),
    multicast and unspecified are refused regardless. One non-public answer refuses the name: all or nothing."""
    names, nets = _private_allowlist()
    if _is_ip_literal(host):
        addrs = [host]
    else:
        try:
            infos = _getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError) as e:
            raise EgressError(f"Couldn't resolve {host}.") from e
        addrs = sorted({str(i[4][0]) for i in infos})
    if not addrs:
        raise EgressError(f"Couldn't resolve {host}.")
    private = False
    for a in addrs:
        ip = _unmap(ipaddress.ip_address(a.split("%")[0]))
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise EgressError(f"{host} resolves to an address connections may never reach.")
        if ip.is_global:
            continue
        if host in names or any(ip in n for n in nets if n.version == ip.version):
            private = True
            continue
        raise EgressError(
            f"{host} is on a private network. Ask the operator to allow it in MEMORY_HUB_CONNECTION_PRIVATE_HOSTS."
        )
    return addrs, private


def guard_connection_url(url: str, allowed_hosts: Iterable[str] = ()) -> tuple[str, dict, str]:
    """Check a person-supplied URL and return (url pinned to the address we checked, extra request kwargs, host).

    The name is resolved once and the connection goes to that exact address, with the original Host header and TLS
    server name, so DNS can't change between the check and the request."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in {"http", "https"} or not host:
        raise EgressError("Only http(s) URLs are allowed.")
    if parts.username or parts.password:
        raise EgressError("Put credentials in the token box, not in the URL.")
    allow = [h.strip().lower() for h in allowed_hosts if h and h.strip()]
    if allow and host not in allow and not any(host.endswith("." + h) for h in allow):
        raise EgressError(f"{host} isn't in this instance's allowed hosts.")
    addrs, private = resolve_for_connection(host)
    if parts.scheme == "http" and not private:
        raise EgressError(
            "Use https for a public address; plain http is only for hosts the operator allowed."
        )
    ip = addrs[0]
    netloc = (f"[{ip}]" if ":" in ip else ip) + (f":{parts.port}" if parts.port else "")
    kw: dict = {"host_header": parts.netloc.rpartition("@")[2]}
    if parts.scheme == "https":
        kw["sni"] = host
    return parts._replace(netloc=netloc).geturl(), kw, host


def check_url(url: str, allowed_hosts: Iterable[str] = ()) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in {"http", "https"} or not host:
        raise EgressError("Only http(s) URLs are allowed.")
    if parts.scheme == "http" and not plaintext_allowed(host):
        raise EgressError("Plain http is only allowed to localhost or a private network; use https.")
    allow = [h.strip().lower() for h in allowed_hosts if h and h.strip()]
    if allow and host not in allow and not any(host.endswith("." + h) for h in allow):
        raise EgressError(f"{host} isn't in this instance's allowed hosts.")
    return url


class EgressClient:
    """A tiny, guarded HTTP client handed to plugins. No redirects (a redirect is a way around the host check)."""

    def __init__(self, allowed_hosts: Iterable[str] = (), *, connection: bool = False):
        self.allowed_hosts = list(allowed_hosts)
        self.connection = (
            connection  # a person's own URL: public addresses only, pinned (guard_connection_url)
        )

    def request(self, method: str, url: str, **kw) -> httpx.Response:  # noqa: ANN003
        if self.connection:
            url, extra, _host = guard_connection_url(url, self.allowed_hosts)
            headers = httpx.Headers(kw.pop("headers", None))
            if "host" not in headers:
                headers["Host"] = extra["host_header"]
            kw["headers"] = headers
            if "sni" in extra:
                kw["extensions"] = {"sni_hostname": extra["sni"]}
        else:
            check_url(url, self.allowed_hosts)
            url, kw = _pin_plaintext(url, kw)
        t = kw.pop(
            "timeout", None
        )  # a caller with a tight budget (a search query) can ask for less, never more
        timeout = httpx.Timeout(min(float(t), 10.0), connect=min(float(t), 5.0)) if t else TIMEOUT
        with httpx.Client(timeout=timeout, follow_redirects=False, transport=_transport) as c:
            with c.stream(method, url, **kw) as r:
                body = b""
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise EgressError("Response too large.")
                r._content = body  # noqa: SLF001 — hand back a fully-read response
                return r

    def get(self, url: str, **kw) -> httpx.Response:  # noqa: ANN003
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> httpx.Response:  # noqa: ANN003
        return self.request("POST", url, **kw)


def redact(text: str, secrets: Iterable[str]) -> str:
    """Remove secret values (and the token-looking parts of URLs) from anything that might be logged or stored."""
    out = text or ""
    for s in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        out = out.replace(s, "[redacted]")
    return out


def redacting_logger(name: str, secrets: Mapping[str, str]) -> logging.LoggerAdapter:
    values = list(secrets.values())

    class _Adapter(logging.LoggerAdapter):
        def process(self, msg, kwargs):  # noqa: ANN001, ANN202
            return redact(str(msg), values), kwargs

    return _Adapter(logging.getLogger(name), {})
