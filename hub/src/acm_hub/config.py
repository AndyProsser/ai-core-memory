"""Runtime configuration, read from MEMORY_HUB_* environment variables."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


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
    # None = auto (Secure unless the request came over plain HTTP from localhost/RFC1918).
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
    # OIDC (optional). All three of issuer/client id/secret enable it.
    oidc_issuer: str = field(default_factory=lambda: os.environ.get("MEMORY_HUB_OIDC_ISSUER", "").rstrip("/"))
    oidc_client_id: str = field(default_factory=lambda: os.environ.get("MEMORY_HUB_OIDC_CLIENT_ID", ""))
    oidc_client_secret: str = field(
        default_factory=lambda: os.environ.get("MEMORY_HUB_OIDC_CLIENT_SECRET", "")
    )
    oidc_scopes: str = field(
        default_factory=lambda: os.environ.get("MEMORY_HUB_OIDC_SCOPES", "openid email profile")
    )

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id and self.oidc_client_secret)

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"


def get_settings() -> Settings:
    return Settings()
