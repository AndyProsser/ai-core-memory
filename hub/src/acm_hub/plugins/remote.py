"""Remote (out-of-process) plugins: the hub as a client of an operator-registered HTTP service.

See docs/PLUGINS.md § Remote plugins for the protocol and docs/SECURITY.md for the trust model. The service shares
nothing with the hub: no process, no database handle, no environment, no token. The hub signs every request, requires
a signed response, validates everything that comes back, and decides what the service may see before serialising it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from . import registry
from .base import EVENT_TYPES, BasePlugin, DeliveryResult, Event, PluginContext, PluginInfo
from .egress import EgressClient, EgressError, check_url
from .remote_sdk import MIN_SECRET_LENGTH, PREFIX, PROTOCOL, sign, verify

log = logging.getLogger("acm_hub.plugins.remote")

KEY_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
MAX_RESPONSE_BYTES = 1_000_000
MAX_ITEMS = 200
MAX_FIELDS = 30
REFRESH_EVERY = 900.0  # seconds between manifest refreshes of a healthy service
RETRY_MIN, RETRY_MAX = 30.0, 600.0


class RemoteError(Exception):
    def __init__(self, message: str, *, retry: bool = True):
        super().__init__(message)
        self.retry = retry


# --- registration (operator-controlled) ------------------------------------------------------------------------------


class RemoteSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a stray `token`/`secret` field must be an error, not ignored

    key: str
    url: str
    secret_env: str


def load_specs(raw_inline: str = "", file_path: str = "") -> tuple[list[RemoteSpec], list[str]]:
    """Parse the operator's registration. Bad entries are reported and skipped, never fatal: one typo must not stop
    the hub (or the other plugins) from starting."""
    errors: list[str] = []
    docs: list[Any] = []
    for label, text in (("MEMORY_HUB_REMOTE_PLUGINS_FILE", None), ("MEMORY_HUB_REMOTE_PLUGINS", raw_inline)):
        if label.endswith("FILE"):
            if not file_path:
                continue
            try:
                text = open(file_path, encoding="utf-8").read()  # noqa: SIM115
            except OSError as e:
                errors.append(f"{label}: can't read {file_path}: {e.strerror}")
                continue
        elif not text:
            continue
        try:
            data = json.loads(text)
        except ValueError as e:
            errors.append(f"{label}: not valid JSON ({e})")
            continue
        if not isinstance(data, list):
            errors.append(f"{label}: expected a JSON list of {{key, url, secret_env}} objects")
            continue
        docs.extend(data)
    specs: list[RemoteSpec] = []
    seen: set[str] = set()
    for entry in docs:
        try:
            spec = RemoteSpec.model_validate(entry)
        except ValidationError:
            errors.append("a remote plugin entry needs exactly: key, url, secret_env")
            continue
        problem = None
        if not KEY_RE.match(spec.key):
            problem = "key must be lower-case letters, digits and hyphens (2-41 characters)"
        elif spec.key in seen:
            problem = "key is registered twice"
        elif not ENV_RE.match(spec.secret_env):
            problem = (
                "secret_env must be an environment variable NAME (like FEED_PLUGIN_SECRET), never the secret"
            )
        else:
            try:
                check_url(spec.url)
            except EgressError as e:
                problem = str(e)
        if problem:
            errors.append(f"remote plugin {spec.key!r}: {problem}")
            continue
        seen.add(spec.key)
        specs.append(spec)
    return specs, errors


# --- the manifest ------------------------------------------------------------------------------------------------------


class ManifestField(BaseModel):
    name: str
    type: Literal["string", "integer", "boolean", "select", "list"]
    label: str = ""
    help: str = Field(default="", max_length=300)
    required: bool = False
    default: Any = None
    options: list[str] = Field(default_factory=list, max_length=50)


class Manifest(BaseModel):
    protocol: Literal[1]
    key: str
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    kind: Literal["source", "sink", "both"]
    config: list[ManifestField] = Field(default_factory=list, max_length=MAX_FIELDS)
    default_events: list[str] = Field(default_factory=list, max_length=len(EVENT_TYPES))


def build_config_model(key: str, fields: list[ManifestField]) -> type[BaseModel]:
    """A pydantic model for the instance form, built only from validated field descriptions."""
    defs: dict[str, Any] = {}
    seen: set[str] = set()
    for f in fields:
        if not FIELD_RE.match(f.name) or f.name in seen:
            raise ValueError(f"bad or duplicate config field name {f.name!r}")
        seen.add(f.name)
        desc = f.help or f.label
        if f.type == "select":
            if not f.options:
                raise ValueError(f"select field {f.name!r} needs options")
            tp: Any = Literal[tuple(f.options)]  # type: ignore[valid-type]
            default: Any = f.default if f.default in f.options else f.options[0]
        elif f.type == "integer":
            tp, default = (
                int,
                f.default if isinstance(f.default, int) and not isinstance(f.default, bool) else 0,
            )
        elif f.type == "boolean":
            tp, default = bool, bool(f.default)
        elif f.type == "list":
            tp = list[str]
            defs[f.name] = (tp, Field(default_factory=list, description=desc))
            continue
        else:
            tp, default = str, f.default if isinstance(f.default, str) else ""
        defs[f.name] = (tp, Field(... if f.required else default, description=desc))
    return create_model(f"RemoteConfig_{key.replace('-', '_')}", **defs)


# --- talking to the service ----------------------------------------------------------------------------------------------


class RemoteClient:
    def __init__(self, spec: RemoteSpec, secret: str):
        self.spec = spec
        self.secret = secret
        self.http = EgressClient([urlsplit(spec.url).hostname or ""])

    def call(self, op: str, payload: dict | None = None, *, method: str = "POST") -> dict:
        body = (
            b""
            if method == "GET"
            else json.dumps(payload or {}, separators=(",", ":"), ensure_ascii=False).encode()
        )
        ts = str(int(time.time()))
        headers = {
            "X-ACM-Protocol": str(PROTOCOL),
            "X-ACM-Timestamp": ts,
            "X-ACM-Signature": sign(self.secret, ts, body),
            "Content-Type": "application/json",
            "User-Agent": "ai-core-memory-hub",
        }
        url = self.spec.url.rstrip("/") + PREFIX + op
        try:
            r = self.http.request(method, url, content=body, headers=headers)
        except EgressError as e:
            raise RemoteError(str(e), retry=False) from e
        except Exception as e:  # noqa: BLE001 — connection refused, timeout, TLS: the service is down, try again later
            raise RemoteError(f"couldn't reach the service ({type(e).__name__})") from e
        if len(r.content) > MAX_RESPONSE_BYTES:
            raise RemoteError("the service's response was too large", retry=False)
        if r.status_code in (401, 403):
            raise RemoteError(
                "the service refused our signature (is the signing key the same on both sides?)", retry=False
            )
        if not verify(self.secret, ts, r.headers.get("x-acm-response-signature"), r.content, max_skew=60):
            # Not "retry": a wrong or missing signature is a configuration or tampering problem, not a blip.
            raise RemoteError(
                "the service's response wasn't signed correctly, so it was discarded", retry=False
            )
        if r.status_code == 429 or r.status_code >= 500:
            raise RemoteError(f"the service returned HTTP {r.status_code}")
        if r.status_code >= 400:
            raise RemoteError(f"the service rejected the request with HTTP {r.status_code}", retry=False)
        try:
            data = json.loads(r.content or b"{}")
        except ValueError as e:
            raise RemoteError("the service returned something that isn't JSON", retry=False) from e
        if not isinstance(data, dict):
            raise RemoteError("the service returned an unexpected JSON shape", retry=False)
        return data


# --- response schemas (everything from a service is untrusted) ---------------------------------------------------------


class _Ok(BaseModel):
    ok: bool
    retry: bool = False
    message: str = Field(default="", max_length=500)


class _Validate(BaseModel):
    ok: bool
    error: str = Field(default="", max_length=300)


class _Item(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(default="", max_length=100_000)
    external_ref: str | None = Field(default=None, max_length=500)


class _Pull(BaseModel):
    items: list[_Item] = Field(default_factory=list)
    message: str = Field(default="", max_length=500)


def _parse(model: type[BaseModel], data: dict) -> Any:
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise RemoteError(
            f"the service returned an invalid response ({e.errors()[0]['loc']})", retry=False
        ) from e


# --- the plugin the rest of the hub sees ---------------------------------------------------------------------------------


class RemotePlugin(BasePlugin):
    remote = True

    def __init__(self, client: RemoteClient, manifest: Manifest):
        self.client = client
        self.info = PluginInfo(
            key=client.spec.key,
            name=manifest.name,
            description=manifest.description,
            kind=manifest.kind,
            config_schema=build_config_model(client.spec.key, manifest.config),
            secret_names=[],  # the service holds its own credentials; the hub never sees them
            default_events=[e for e in manifest.default_events if e in EVENT_TYPES],
        )

    def _instance(self, ctx: PluginContext) -> dict:
        return {"id": ctx.instance_id, "name": ctx.instance_name}

    def validate(self, config: BaseModel) -> None:
        try:
            out = _parse(
                _Validate, self.client.call("validate", {"instance": {}, "config": config.model_dump()})
            )
        except RemoteError as e:
            raise ValueError(f"couldn't check the settings with the service: {e}") from e
        if not out.ok:
            raise ValueError(out.error or "the service rejected these settings")

    def deliver(self, ctx: PluginContext, event: Event) -> DeliveryResult:
        payload = {
            "instance": self._instance(ctx),
            "event": {
                "id": event.id,
                "type": event.type,
                "created_at": event.created_at.isoformat() + "Z",
                "link": event.link,
                "payload": event.payload,
            },
        }
        try:
            out = _parse(_Ok, self.client.call("deliver", payload))
        except RemoteError as e:
            return DeliveryResult.retry_later(str(e)) if e.retry else DeliveryResult.failed(str(e))
        if out.ok:
            return DeliveryResult.success(out.message)
        return (
            DeliveryResult.retry_later(out.message or "the service asked for a retry")
            if out.retry
            else DeliveryResult.failed(out.message or "the service reported a failure")
        )

    def pull(self, ctx: PluginContext) -> None:
        payload = {
            "instance": self._instance(ctx),
            "config": ctx.config.model_dump(),
            "since": ctx.extras.get("since"),
            "limit": MAX_ITEMS,
        }
        out = _parse(
            _Pull, self.client.call("pull", payload)
        )  # RemoteError propagates: the dispatcher records it
        for item in out.items[:MAX_ITEMS]:
            ctx.inbox.add(item.title, item.body, external_ref=item.external_ref)  # type: ignore[union-attr]

    def export_digest(self, ctx: PluginContext, digest: dict[str, Any]) -> None:
        payload = {"instance": self._instance(ctx), "config": ctx.config.model_dump(), "digest": digest}
        out = _parse(_Ok, self.client.call("digest", payload))
        if not out.ok:
            raise RemoteError(out.message or "the service couldn't export the digest", retry=False)


# --- availability tracking (never on a request path) ------------------------------------------------------------------


@dataclass
class RemoteState:
    spec: RemoteSpec
    plugin: RemotePlugin | None = None
    error: str | None = "not contacted yet"
    next_try: float = 0.0
    failures: int = 0


STATE: dict[str, RemoteState] = {}
CONFIG_ERRORS: list[str] = []


def reset() -> None:
    for st in STATE.values():
        if st.plugin is not None:
            registry.unregister(st.spec.key)
    STATE.clear()
    CONFIG_ERRORS.clear()


def configure(settings: Any) -> None:
    """Read the operator's registration. Does no network I/O: contacting services happens in `refresh_due`."""
    reset()
    specs, errors = load_specs(
        getattr(settings, "remote_plugins_inline", ""), getattr(settings, "remote_plugins_file", "")
    )
    CONFIG_ERRORS.extend(errors)
    for e in errors:
        log.error("remote plugin configuration: %s", e)
    for spec in specs:
        STATE[spec.key] = RemoteState(spec)


def is_remote_key(key: str) -> bool:
    return key in STATE


def unavailable_reason(key: str) -> str | None:
    st = STATE.get(key)
    return None if st is None or st.plugin is not None else (st.error or "unavailable")


def _fail(st: RemoteState, message: str, now: float) -> None:
    st.failures += 1
    st.error = message
    st.next_try = now + min(RETRY_MAX, RETRY_MIN * 2 ** (st.failures - 1))


def refresh_one(key: str, *, now: float | None = None) -> None:
    now = time.time() if now is None else now
    st = STATE[key]
    secret = os.environ.get(st.spec.secret_env, "")
    if len(secret) < MIN_SECRET_LENGTH:
        _fail(
            st,
            f"the signing key in ${st.spec.secret_env} isn't set or is shorter than {MIN_SECRET_LENGTH} characters",
            now,
        )
        return
    existing = registry.get(key)
    if existing is not None and existing is not st.plugin:
        _fail(st, "its key collides with a built-in or installed plugin, so it was not loaded", now)
        st.next_try = now + RETRY_MAX
        return
    client = RemoteClient(st.spec, secret)
    try:
        raw = client.call("manifest", method="GET")
        manifest = Manifest.model_validate(raw)
        if manifest.key != st.spec.key:
            raise RemoteError(f"the service says it is {manifest.key!r}, not {st.spec.key!r}", retry=False)
        plugin = RemotePlugin(client, manifest)
    except RemoteError as e:
        _fail(st, str(e), now)
        return
    except (ValidationError, ValueError) as e:
        _fail(st, f"its manifest is invalid ({str(e).splitlines()[0][:120]})", now)
        return
    registry.register(plugin)
    st.plugin, st.error, st.failures = plugin, None, 0
    st.next_try = now + REFRESH_EVERY


def refresh_due(*, now: float | None = None, force: bool = False) -> int:
    """Contact services that are down (with backoff) or due for a manifest refresh. Run from the scheduler thread."""
    now = time.time() if now is None else now
    n = 0
    for key, st in list(STATE.items()):
        if force or now >= st.next_try:
            refresh_one(key, now=now)
            n += 1
    return n
