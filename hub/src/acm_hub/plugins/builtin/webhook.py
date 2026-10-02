"""Signed JSON webhook: the escape hatch for anything without its own plugin, and a template for new sinks."""

from __future__ import annotations

import hashlib
import hmac
import json

from pydantic import BaseModel, Field

from ..base import BasePlugin, DeliveryResult, Event, PluginContext, PluginInfo
from ..egress import EgressError


class WebhookConfig(BaseModel):
    allowed_hosts: list[str] = Field(
        default_factory=list, description="Optional: only POST to these hosts (comma-separated)."
    )


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class WebhookPlugin(BasePlugin):
    info = PluginInfo(
        key="webhook",
        name="Webhook",
        kind="sink",
        description="POSTs each event as JSON to a URL, signed with HMAC-SHA256 (header X-ACM-Signature) so the receiver can verify it came from your hub.",
        config_schema=WebhookConfig,
        secret_names=["url", "signing_key"],
        secret_help={
            "url": "Environment variable holding the webhook URL (URLs often embed tokens, so it's a secret).",
            "signing_key": "Environment variable holding the HMAC signing key (optional but recommended).",
        },
        default_events=["proposal.pending", "conflict.flagged", "digest.weekly", "plugin.failed"],
    )

    def deliver(self, ctx: PluginContext, event: Event) -> DeliveryResult:
        url = ctx.secrets.get("url")
        if not url:
            return DeliveryResult.failed("The URL environment variable isn't set.")
        body = json.dumps(
            {
                "id": event.id,
                "type": event.type,
                "created_at": event.created_at.isoformat() + "Z",
                "link": event.link,
                "data": event.payload,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        headers = {
            "Content-Type": "application/json",
            "X-ACM-Event": event.type,
            "X-ACM-Delivery": event.id,
            "User-Agent": "ai-core-memory-hub",
        }
        if key := ctx.secrets.get("signing_key"):
            headers["X-ACM-Signature"] = sign(key, body)
        try:
            r = ctx.http.post(url, content=body, headers=headers)
        except EgressError as e:
            return DeliveryResult.failed(str(e))  # a policy refusal won't fix itself
        if 200 <= r.status_code < 300:
            return DeliveryResult.success()
        if r.status_code == 429 or r.status_code >= 500:
            return DeliveryResult.retry_later(f"receiver returned HTTP {r.status_code}")
        return DeliveryResult.failed(f"receiver rejected it with HTTP {r.status_code}")
