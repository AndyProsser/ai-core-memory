"""Creating and validating plugin instances from the Plugins screen. Admin-only (enforced by the routes).
The form is generated from each plugin's pydantic config schema, so a new plugin needs no UI code."""

from __future__ import annotations

import re
import types
import typing
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlmodel import Session, select

from .models import PluginInstance, Project, User
from .plugins.base import EVENT_TYPES, BasePlugin

SCOPES = ("project", "team", "user")
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
NAME_RE = re.compile(r"^.{1,80}$")


@dataclass
class FieldSpec:
    name: str
    label: str
    kind: str  # text | number | bool | select | list
    help: str = ""
    options: list[str] = field(default_factory=list)
    value: Any = ""
    required: bool = False


def _unwrap(tp: Any) -> Any:
    if typing.get_origin(tp) in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        return args[0] if len(args) == 1 else tp
    return tp


def field_specs(schema: type[BaseModel], values: dict | None = None) -> list[FieldSpec]:
    out = []
    for name, f in schema.model_fields.items():
        tp = _unwrap(f.annotation)
        origin = typing.get_origin(tp)
        val = (values or {}).get(
            name, f.get_default(call_default_factory=True) if not f.is_required() else ""
        )
        spec = FieldSpec(
            name,
            name.replace("_", " ").capitalize(),
            "text",
            f.description or "",
            value=val,
            required=f.is_required(),
        )
        if origin is typing.Literal:
            spec.kind, spec.options = "select", [str(a) for a in typing.get_args(tp)]
        elif origin is list:
            spec.kind, spec.value = "list", ", ".join(val or [])
        elif tp is bool:
            spec.kind = "bool"
        elif tp is int:
            spec.kind = "number"
        out.append(spec)
    return out


def parse_config(schema: type[BaseModel], form: Any) -> tuple[dict, list[str]]:
    raw: dict[str, Any] = {}
    for name, f in schema.model_fields.items():
        tp = _unwrap(f.annotation)
        origin = typing.get_origin(tp)
        v = form.get(f"cfg_{name}")
        if tp is bool:
            raw[name] = v in ("on", "1", "true", True)
        elif origin is list:
            raw[name] = [x.strip() for x in str(v or "").split(",") if x.strip()]
        elif v is None or v == "":
            if f.is_required():
                raw[name] = ""
        elif tp is int:
            try:
                raw[name] = int(v)
            except ValueError:
                return raw, [f"{name.replace('_', ' ').capitalize()} must be a number."]
        else:
            raw[name] = str(v).strip()
    try:
        cfg = schema(**raw)
    except ValidationError as e:
        return raw, [
            f"{str(err['loc'][-1]).replace('_', ' ').capitalize()}: {err['msg']}" for err in e.errors()
        ]
    return cfg.model_dump(), []


@dataclass
class InstanceForm:
    name: str
    enabled: bool
    config: dict
    secret_refs: dict
    scopes: list[str]
    projects: list[str]
    events: list[str]
    egress: str
    user_scope_ack: bool
    pull_interval_minutes: int
    errors: list[str] = field(default_factory=list)


def parse_instance_form(session: Session, plugin: BasePlugin, form: Any) -> InstanceForm:
    errors: list[str] = []
    name = str(form.get("name") or "").strip()
    if not NAME_RE.match(name):
        errors.append("Give the instance a name (up to 80 characters).")
    config, cfg_errors = parse_config(plugin.info.config_schema, form)
    errors += cfg_errors
    if not cfg_errors:
        try:
            plugin.validate(plugin.info.config_schema(**config))
        except ValueError as e:
            errors.append(str(e))
    refs = {}
    for s in plugin.info.secret_names:
        env = str(form.get(f"secret_{s}") or "").strip()
        if env and not ENV_NAME_RE.match(env):
            errors.append(
                f"“{env[:30]}” isn't an environment variable name (like SLACK_WEBHOOK_URL). Enter the variable's name, never the secret itself."
            )
        elif env:
            refs[s] = env
    scopes = [s for s in form.getlist("scopes") if s in SCOPES] if hasattr(form, "getlist") else []
    projects = [p.strip() for p in str(form.get("projects") or "").split(",") if p.strip()]
    known = {p.slug for p in session.exec(select(Project)).all()}
    errors += [f"No project named “{p}”." for p in projects if p not in known]
    is_sink = plugin.info.kind in {"sink", "both"}
    events = (
        [e for e in form.getlist("events") if e in EVENT_TYPES]
        if hasattr(form, "getlist") and is_sink
        else []
    )
    egress = str(form.get("egress") or "metadata")
    if egress not in {"metadata", "full"}:
        errors.append("Unknown egress level.")
    ack = form.get("user_scope_ack") in ("on", "1", True)
    if "user" in scopes and not ack:
        errors.append("Including personal (user-scope) memory needs the acknowledgement box ticked.")
    try:
        interval = int(form.get("pull_interval_minutes") or 60)
    except ValueError:
        interval = 60
        errors.append("Pull interval must be a number of minutes.")
    if not 5 <= interval <= 10080:
        errors.append("Pull interval must be between 5 minutes and 7 days.")
    return InstanceForm(
        name,
        form.get("enabled") in ("on", "1", True),
        config,
        refs,
        scopes,
        projects,
        events,
        egress,
        bool(ack and "user" in scopes),
        interval,
        errors,
    )


def apply_form(inst: PluginInstance, f: InstanceForm) -> None:
    inst.name, inst.enabled, inst.config, inst.secret_refs = f.name, f.enabled, f.config, f.secret_refs
    inst.scopes, inst.projects, inst.events, inst.egress = f.scopes, f.projects, f.events, f.egress
    inst.user_scope_ack, inst.pull_interval_minutes = f.user_scope_ack, f.pull_interval_minutes
    inst.last_status = inst.last_status if inst.last_status else None


def default_instance(user: User, plugin: BasePlugin) -> PluginInstance:
    return PluginInstance(
        plugin_key=plugin.info.key,
        name=plugin.info.name,
        owner_user_id=user.id,
        enabled=False,
        events=list(plugin.info.default_events),
        scopes=[],
        config={},
        secret_refs={},
        pull_interval_minutes=15 if plugin.info.kind == "search" else 60,
    )
