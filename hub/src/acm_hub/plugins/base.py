"""What a plugin author sees: metadata, a config schema, and a narrow context. No DB handle, no API token."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from .egress import EgressClient

EVENT_TYPES = (
    "record.created",
    "record.updated",
    "record.superseded",
    "proposal.pending",
    "conflict.flagged",
    "core.budget_exceeded",
    "inbox.new",
    "digest.weekly",
    "plugin.failed",
)


class PluginInfo(BaseModel):
    key: str
    name: str
    description: str = ""
    kind: Literal["source", "sink", "both", "search"]
    config_schema: type[BaseModel]  # non-secret settings, rendered as a form in the Plugins screen
    secret_names: list[
        str
    ] = []  # secrets the plugin needs; the operator maps each to an ENVIRONMENT VARIABLE NAME
    secret_help: dict[str, str] = {}
    default_events: list[str] = []  # events a new sink instance subscribes to (the operator can narrow)
    # May a non-admin set this plugin up for themselves (Settings → Connections)? Only plugins whose own secrets and
    # network use are safe under the connection rules (sealed secrets, public-only egress) say yes. Default: no.
    personal_ok: bool = False
    optional_secrets: list[str] = []  # secrets a connection may leave empty (everything else is required)
    personal_help: dict[
        str, str
    ] = {}  # what to paste into each secret box when it is a connection (a value, not an env var name)


@dataclass(frozen=True)
class Event:
    """What a sink sees: already filtered to the instance's scope allowlist and egress level."""

    id: str
    type: str
    created_at: datetime
    payload: dict[str, Any]
    link: str | None  # absolute URL back to the relevant screen in the hub


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    retry: bool = False
    message: str = ""

    @classmethod
    def success(cls, message: str = "") -> DeliveryResult:
        return cls(True, False, message)

    @classmethod
    def retry_later(cls, message: str) -> DeliveryResult:
        return cls(False, True, message)

    @classmethod
    def failed(cls, message: str) -> DeliveryResult:
        return cls(False, False, message)


class InboxWriter:
    """Source plugins' only way to write: add to the inbox. It can't create records or approve anything."""

    def __init__(self, add):  # noqa: ANN001
        self._add = add

    def add(self, title: str, body: str = "", *, external_ref: str | None = None) -> bool:
        """Returns True if a new item was captured (False: already known / unchanged)."""
        return self._add(title, body, external_ref)


@dataclass
class PluginContext:
    instance_id: str
    instance_name: str
    config: BaseModel
    secrets: Mapping[str, str]  # resolved from the environment for this call only; never persisted
    scopes: frozenset[str]
    egress: str  # "metadata" | "full"
    log: logging.LoggerAdapter
    http: EgressClient
    public_url: str
    inbox: InboxWriter | None = None  # sources only
    extras: dict[str, Any] = field(default_factory=dict)


class BasePlugin:
    """Subclass, set `info`, implement what applies. Everything is called off the request path, in a worker thread,
    under a timeout; an exception is a failed attempt, never a crash."""

    info: PluginInfo
    remote: bool = False  # True for a plugin that runs as a separate service (plugins/remote.py)

    def validate(self, config: BaseModel) -> None:
        """Extra checks beyond the schema (e.g. a path must exist). Raise ValueError with a human message."""

    def check(self, ctx: PluginContext) -> str:
        """Connections: one small authenticated read proving the URL and token work. Return a short human message;
        raise on failure (the message is shown to the person, with secrets scrubbed)."""
        raise NotImplementedError

    def deliver(self, ctx: PluginContext, event: Event) -> DeliveryResult:  # sinks
        raise NotImplementedError

    def pull(self, ctx: PluginContext) -> None:  # sources: call ctx.inbox.add(...) for each new thing
        raise NotImplementedError

    def export_digest(self, ctx: PluginContext, digest: dict[str, Any]) -> None:  # sources, optional
        raise NotImplementedError

    # search plugins (docs/PLUGINS.md § Search plugins): the hub keeps the index in step and asks for candidate ids
    def index(self, ctx: PluginContext, records: list[dict[str, Any]]) -> None:
        raise NotImplementedError

    def remove(self, ctx: PluginContext, ids: list[str]) -> None:
        raise NotImplementedError

    def reset(self, ctx: PluginContext) -> None:
        raise NotImplementedError

    def search(self, ctx: PluginContext, query: str, limit: int, timeout: float) -> list[tuple[str, float]]:
        raise NotImplementedError
