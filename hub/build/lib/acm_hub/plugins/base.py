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
    kind: Literal["source", "sink", "both"]
    config_schema: type[BaseModel]  # non-secret settings, rendered as a form in the Plugins screen
    secret_names: list[
        str
    ] = []  # secrets the plugin needs; the operator maps each to an ENVIRONMENT VARIABLE NAME
    secret_help: dict[str, str] = {}
    default_events: list[str] = []  # events a new sink instance subscribes to (the operator can narrow)


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

    def validate(self, config: BaseModel) -> None:
        """Extra checks beyond the schema (e.g. a path must exist). Raise ValueError with a human message."""

    def deliver(self, ctx: PluginContext, event: Event) -> DeliveryResult:  # sinks
        raise NotImplementedError

    def pull(self, ctx: PluginContext) -> None:  # sources: call ctx.inbox.add(...) for each new thing
        raise NotImplementedError

    def export_digest(self, ctx: PluginContext, digest: dict[str, Any]) -> None:  # sources, optional
        raise NotImplementedError
