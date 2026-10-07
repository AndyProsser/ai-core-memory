"""Runtime configuration, read from MEMORY_HUB_* environment variables."""

from __future__ import annotations

import contextlib
import os
import secrets
from dataclasses import dataclass, field
from ipaddress import IPv4Network, IPv6Network, ip_network
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


DEFAULT_TRUSTED_PROXIES = "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"


@dataclass
class Settings:
    db_path: Path = field(
        default_factory=lambda: Path(os.environ.get("MEMORY_HUB_DB_PATH", "./data/hub.sqlite3"))
    )
    secret_key: str = field(
        default_factory=lambda: os.environ.get("MEMORY_HUB_SECRET_KEY") or secrets.token_urlsafe(32)
    )
    secret_key_was_generated: bool = field(
        default_factory=lambda: not os.environ.get("MEMORY_HUB_SECRET_KEY")
    )
    public_url: str = field(
        default_factory=lambda: os.environ.get("MEMORY_HUB_PUBLIC_URL", "http://localhost:8000").rstrip("/")
    )
    admin_email: str = field(default_factory=lambda: os.environ.get("MEMORY_HUB_ADMIN_EMAIL", ""))
    # Behind a reverse proxy, honour X-Forwarded-Proto/-For. Off by default: they're spoofable.
    trust_proxy: bool = field(default_factory=lambda: _bool("MEMORY_HUB_TRUST_PROXY", False))
    # Which peers may set X-Forwarded-For/-Proto (comma-separated IPs/CIDRs; "*" = anyone, which is unsafe). Only used when
    # trust_proxy is on. Empty = loopback + private ranges. Set it to just your proxy/ingress (docs/SECURITY.md § Network edge).
    trusted_proxies: str = field(
        default_factory=lambda: os.environ.get("MEMORY_HUB_TRUSTED_PROXIES", "").strip()
    )
    # A header carrying the real client address when a CDN sits in front of the proxy (e.g. CF-Connecting-IP), because
    # the proxy's own X-Forwarded-For can hold only the CDN-facing hop. Honoured only when the peer is a trusted proxy.
    client_ip_header: str = field(
        default_factory=lambda: os.environ.get("MEMORY_HUB_CLIENT_IP_HEADER", "").strip().lower()
    )
    # Extra Host header values to accept (comma-separated). Setting this turns Host checking on: the public URL's host,
    # loopback and /healthz are always allowed. Empty = no Host enforcement.
    allowed_hosts: str = field(default_factory=lambda: os.environ.get("MEMORY_HUB_ALLOWED_HOSTS", "").strip())
    # Strict-Transport-Security max-age in seconds, sent only when the public URL is https. 0 = don't send it.
    hsts_max_age: int = field(default_factory=lambda: _int("MEMORY_HUB_HSTS_MAX_AGE", 31536000))
    # None = auto (Secure unless the request came over plain HTTP from localhost/RFC1918). An https public URL always wins.
    cookie_secure: bool | None = field(
        default_factory=lambda: (
            _bool("MEMORY_HUB_COOKIE_SECURE", True) if os.environ.get("MEMORY_HUB_COOKIE_SECURE") else None
        )
    )
    session_idle_hours: int = field(default_factory=lambda: _int("MEMORY_HUB_SESSION_IDLE_HOURS", 12))
    session_absolute_days: int = field(default_factory=lambda: _int("MEMORY_HUB_SESSION_ABSOLUTE_DAYS", 14))
    token_rate_limit_per_min: int = field(
        default_factory=lambda: _int("MEMORY_HUB_TOKEN_RATE_LIMIT_PER_MIN", 120)
    )
    # How often the mechanical consolidation pass runs (decay, duplicates, core budget). 0 disables the scheduler;
    # `acm consolidate` and the Review screen's "Run now" still work.
    consolidate_interval_hours: int = field(
        default_factory=lambda: _int("MEMORY_HUB_CONSOLIDATE_INTERVAL_HOURS", 24)
    )
    # Plugins (sinks/sources) run in this process. Set MEMORY_HUB_PLUGINS=false to switch every plugin off (an emergency off-switch).
    plugins_enabled: bool = field(default_factory=lambda: _bool("MEMORY_HUB_PLUGINS", True))
    # Plugin egress may use plain http to a *hostname* (not just a literal private IP) when every address it resolves to
    # is loopback/private — e.g. a Kubernetes Service name or a LAN hostname. Off by default (docs/SECURITY.md).
    egress_resolve_private: bool = field(
        default_factory=lambda: _bool("MEMORY_HUB_EGRESS_RESOLVE_PRIVATE", False)
    )
    # Per-user connections (docs/SECURITY.md § Per-user connections) may only reach PUBLIC addresses. Private targets the
    # operator deliberately allows (a LAN Memos, a cluster Service, a tailnet host): comma separated names, IPs or CIDRs.
    connection_private_hosts: str = field(
        default_factory=lambda: os.environ.get("MEMORY_HUB_CONNECTION_PRIVATE_HOSTS", "")
    )
    # OIDC (optional). All three of issuer/client id/secret enable it.
    oidc_issuer: str = field(default_factory=lambda: os.environ.get("MEMORY_HUB_OIDC_ISSUER", "").rstrip("/"))
    oidc_client_id: str = field(default_factory=lambda: os.environ.get("MEMORY_HUB_OIDC_CLIENT_ID", ""))
    oidc_client_secret: str = field(
        default_factory=lambda: os.environ.get("MEMORY_HUB_OIDC_CLIENT_SECRET", "")
    )
    oidc_scopes: str = field(
        default_factory=lambda: os.environ.get("MEMORY_HUB_OIDC_SCOPES", "openid email profile")
    )

    # MCP OAuth (docs/SECURITY.md § MCP OAuth): lets connectors such as Claude.ai sign in instead of pasting a token.
    # Off by default; a hub that never needs it exposes none of its endpoints.
    oauth_enabled: bool = field(default_factory=lambda: _bool("MEMORY_HUB_OAUTH_ENABLED", False))
    # Extra redirect URIs (exact match, comma separated) a registering client may use, on top of the built-in
    # allowlist (Claude's hosted callback and loopback for local tools). Deny by default.
    oauth_extra_redirect_uris: str = field(
        default_factory=lambda: os.environ.get("MEMORY_HUB_OAUTH_EXTRA_REDIRECT_URIS", "")
    )
    oauth_access_token_minutes: int = field(
        default_factory=lambda: _int("MEMORY_HUB_OAUTH_ACCESS_TOKEN_MINUTES", 60)
    )
    oauth_refresh_days: int = field(default_factory=lambda: _int("MEMORY_HUB_OAUTH_REFRESH_DAYS", 60))

    # Remote (out-of-process) plugins, registered by the operator only (docs/PLUGINS.md § Remote plugins).
    remote_plugins_inline: str = field(
        default_factory=lambda: os.environ.get("MEMORY_HUB_REMOTE_PLUGINS", "")
    )
    remote_plugins_file: str = field(
        default_factory=lambda: os.environ.get("MEMORY_HUB_REMOTE_PLUGINS_FILE", "")
    )

    @property
    def public_https(self) -> bool:
        return self.public_url.lower().startswith("https://")

    @property
    def session_cookie_name(self) -> str:
        """`__Host-` (Secure, Path=/, no Domain: subdomains and plain-http origins can't plant it) whenever the cookie is always Secure."""
        return (
            "__Host-acm_session" if self.public_https and self.cookie_secure is not False else "acm_session"
        )

    @property
    def forwarded_allow_ips(self) -> str | None:
        """What uvicorn is told to trust for X-Forwarded-*. With a list, uvicorn takes the right-most *untrusted* hop,
        which a client can't forge; with "*" it takes the left-most, which it can."""
        if not self.trust_proxy:
            return None
        return self.trusted_proxies or DEFAULT_TRUSTED_PROXIES

    @property
    def trusted_proxy_networks(self) -> tuple[IPv4Network | IPv6Network, ...]:
        """`forwarded_allow_ips` as networks; entries that don't parse (and "*") are skipped, so they trust nothing here."""
        nets = []
        for part in (self.forwarded_allow_ips or "").split(","):
            with contextlib.suppress(ValueError):
                nets.append(ip_network(part.strip(), strict=False))
        return tuple(nets)

    @property
    def extra_allowed_hosts(self) -> frozenset[str]:
        return frozenset(h.strip().lower() for h in self.allowed_hosts.split(",") if h.strip())

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id and self.oidc_client_secret)

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"


def get_settings() -> Settings:
    return Settings()
