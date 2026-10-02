"""Notifications to Slack, Microsoft Teams, ntfy, Discord, Telegram, email, ... through one dependency (Apprise).
Service URLs carry tokens, so they live in an environment variable the operator names — never in the database."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from ...security import is_local_or_private
from ..base import BasePlugin, DeliveryResult, Event, PluginContext, PluginInfo
from ..egress import check_url
from ..text import render

PLAINTEXT_SCHEMES = {
    "json",
    "form",
    "xml",
    "http",
}  # Apprise's non-TLS HTTP targets; the TLS variants add an "s"
HTTPISH = {"json", "jsons", "form", "forms", "xml", "xmls", "http", "https"}


class AppriseConfig(BaseModel):
    allowed_hosts: list[str] = Field(
        default_factory=list, description="Optional: for generic http(s)/json targets, only these hosts."
    )
    title_prefix: str = Field(
        default="", description="Prepended to every notification title, e.g. “[memory]”."
    )


def _check_targets(urls: list[str], allowed_hosts: list[str]) -> str | None:
    for u in urls:
        parts = urlsplit(u)
        scheme = parts.scheme.lower()
        if scheme in PLAINTEXT_SCHEMES and not is_local_or_private(parts.hostname):
            return f"{scheme}:// sends in plain text; use {scheme}s:// (plain http is only allowed to localhost or a private network)."
        if scheme in HTTPISH:  # same host rules as every other plugin egress
            try:
                check_url(
                    ("https" if scheme.endswith("s") or scheme == "https" else "http")
                    + "://"
                    + (parts.netloc or ""),
                    allowed_hosts,
                )
            except Exception as e:  # noqa: BLE001
                return str(e)
    return None


class ApprisePlugin(BasePlugin):
    info = PluginInfo(
        key="apprise",
        name="Notifications (Slack, Teams, ntfy, email, …)",
        kind="sink",
        description="Posts events to chat and notification services via Apprise. One instance per destination; the destination URL(s) come from an environment variable you name.",
        config_schema=AppriseConfig,
        secret_names=["urls"],
        secret_help={
            "urls": "Environment variable holding one or more Apprise URLs, one per line or space-separated (e.g. slack://…, msteams://…, ntfys://…)."
        },
        default_events=["proposal.pending", "conflict.flagged", "digest.weekly", "plugin.failed"],
    )

    def deliver(self, ctx: PluginContext, event: Event) -> DeliveryResult:
        raw = ctx.secrets.get("urls")
        if not raw:
            return DeliveryResult.failed("The Apprise URL environment variable isn't set.")
        urls = [u for u in re.split(r"\s+", raw.strip()) if u]
        if problem := _check_targets(urls, list(ctx.config.allowed_hosts)):  # type: ignore[attr-defined]
            return DeliveryResult.failed(problem)
        try:
            import apprise
        except ImportError:
            return DeliveryResult.failed(
                "The 'apprise' package isn't installed (pip install 'ai-core-memory-hub[notify]')."
            )
        ap = apprise.Apprise()
        for u in urls:
            if not ap.add(u):
                return DeliveryResult.failed(
                    "One of the Apprise URLs isn't valid (details withheld: it may contain a token)."
                )
        title, body = render(event)
        prefix = (ctx.config.title_prefix or "").strip()  # type: ignore[attr-defined]
        ok = ap.notify(title=f"{prefix} {title}".strip(), body=body, body_format=apprise.NotifyFormat.TEXT)
        return (
            DeliveryResult.success()
            if ok
            else DeliveryResult.retry_later(
                "The notification service refused the message or was unreachable."
            )
        )
