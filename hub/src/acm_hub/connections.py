"""Per-user connections (docs/PLUGINS.md § Connections, docs/SECURITY.md § Per-user connections).

The same plugin machinery as an admin-configured instance, set up by the person it serves: their own token (sealed in
the database), their own inbox, only what they may read. Everything here is the checking and bookkeeping around that;
running a connection is the ordinary dispatcher."""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, col, select

from . import connection_secrets
from . import plugin_admin as pa
from .access import NotFound, principal_for_user, writable_project_ids
from .models import InstanceSettings, PluginDelivery, PluginInstance, Project, User
from .plugins import registry
from .plugins.base import EVENT_TYPES, BasePlugin
from .plugins.egress import EgressError, resolve_for_connection

MAX_PER_USER = 10
MIN_INTERVAL_MINUTES = 15
MAX_SECRET_LENGTH = 2000
SCOPES = pa.SCOPES


def enabled(session: Session) -> bool:
    return (session.get(InstanceSettings, 1) or InstanceSettings()).connections_enabled


def available_plugins() -> list[BasePlugin]:
    return [p for p in registry.all_plugins() if p.info.personal_ok and not p.remote]


def available(key: str) -> BasePlugin | None:
    p = registry.get(key)
    return p if p is not None and p.info.personal_ok and not p.remote else None


def list_for(session: Session, user: User) -> list[PluginInstance]:
    return list(
        session.exec(
            select(PluginInstance)
            .where(PluginInstance.personal == True, PluginInstance.owner_user_id == user.id)  # noqa: E712
            .order_by(col(PluginInstance.created_at))
        ).all()
    )


def get_owned(session: Session, user: User, iid: str) -> PluginInstance:
    """Someone else's connection (an admin's included) and a system plugin are both simply "not found"."""
    inst = session.get(PluginInstance, iid)
    if inst is None or not inst.personal or inst.owner_user_id != user.id:
        raise NotFound("No such connection.")
    return inst


def count_all(session: Session) -> int:
    return len(session.exec(select(PluginInstance.id).where(PluginInstance.personal == True)).all())  # noqa: E712


def secret_states(plugin: BasePlugin, inst: PluginInstance) -> list[dict]:
    """Which secrets are saved. Never the values, and not even a hint of them."""
    out = []
    for name in plugin.info.secret_names:
        sealed = (inst.sealed_secrets or {}).get(name)
        ok = bool(sealed and connection_secrets.open_(inst.id, name, sealed))
        out.append(
            {
                "name": name,
                "label": name.replace("_", " ").capitalize().replace("Url", "URL").replace("Api", "API"),
                "is_set": ok,
                "unreadable": bool(sealed) and not ok,  # saved, but the hub key changed: needs re-entering
                "optional": name in plugin.info.optional_secrets,
                "help": plugin.info.personal_help.get(name, ""),
            }
        )
    return out


@dataclass
class ConnForm:
    name: str
    enabled: bool
    config: dict
    new_secrets: dict[str, str]  # only those typed in this submission
    scopes: list[str]
    events: list[str]
    egress: str
    user_scope_ack: bool
    interval: int
    errors: list[str] = field(default_factory=list)


def _check_destination(config: dict, secrets: dict[str, str], plugin: BasePlugin) -> list[str]:
    """Early feedback on a URL the person typed: it must resolve to somewhere a connection may go (SSRF guard).
    The same guard runs again at every request; this just spares them a confusing failure later."""
    errs = []
    targets = []
    if config.get("base_url"):
        targets.append(("The address", str(config["base_url"])))
    if "url" in plugin.info.secret_names and secrets.get("url"):
        targets.append(("The webhook URL", secrets["url"]))
    for label, raw in targets:
        parts = urllib.parse.urlsplit(raw)
        host = (parts.hostname or "").lower()
        if parts.scheme not in {"http", "https"} or not host:
            errs.append(f"{label} must be an http(s) address.")
            continue
        try:
            _addrs, private = resolve_for_connection(host)
        except EgressError as e:
            errs.append(f"{label}: {e}")
            continue
        if parts.scheme == "http" and not private:
            errs.append(f"{label}: use https for a public address.")
    return errs


def parse_form(
    session: Session, user: User, plugin: BasePlugin, form: Any, existing: PluginInstance | None
) -> ConnForm:
    errors: list[str] = []
    info = plugin.info
    name = str(form.get("name") or "").strip()
    if not pa.NAME_RE.match(name):
        errors.append("Give the connection a name (up to 80 characters).")
    config, cfg_errors = pa.parse_config(info.config_schema, form)
    errors += cfg_errors
    if not cfg_errors:
        try:
            plugin.validate(info.config_schema(**config))
        except ValueError as e:
            errors.append(str(e))
        scope = config.get("inbox_scope")
        if scope is not None:
            if scope == "team":
                errors.append("A connection captures into your own inbox or a project, never team memory.")
            elif scope == "project":
                proj = session.exec(
                    select(Project).where(Project.slug == config.get("inbox_project"))
                ).first()
                p = principal_for_user(session, user)
                if proj is None or proj.id not in writable_project_ids(session, p):
                    errors.append("You can only capture into a project you can write to.")
    new_secrets: dict[str, str] = {}
    for s in info.secret_names:
        v = str(form.get(f"secret_{s}") or "").strip()
        if len(v) > MAX_SECRET_LENGTH:
            errors.append(f"{s.replace('_', ' ').capitalize()} is too long.")
        elif v:
            new_secrets[s] = v
        elif not (existing and (existing.sealed_secrets or {}).get(s)) and s not in info.optional_secrets:
            errors.append(f"{s.replace('_', ' ').capitalize().replace('Url', 'URL')} is required.")
    if not errors:
        errors += _check_destination(config, new_secrets, plugin)
    is_sink = info.kind in {"sink", "both"}
    getlist = form.getlist if hasattr(form, "getlist") else (lambda _k: [])
    scopes = [s for s in getlist("scopes") if s in SCOPES] if is_sink else []
    events = [e for e in getlist("events") if e in EVENT_TYPES] if is_sink else []
    egress = str(form.get("egress") or "metadata") if is_sink else "metadata"
    if egress not in {"metadata", "full"}:
        errors.append("Unknown detail level.")
    ack = form.get("user_scope_ack") in ("on", "1", True)
    if "user" in scopes and not ack:
        errors.append("Including your personal memory needs the acknowledgement box ticked.")
    if config.get("export_digest"):  # a digest of the owner's own memory, posted to the owner's own app
        scopes, ack = list(SCOPES), True
    try:
        interval = int(form.get("pull_interval_minutes") or 60)
    except ValueError:
        interval = 60
        errors.append("Check interval must be a number of minutes.")
    if not MIN_INTERVAL_MINUTES <= interval <= 10080:
        errors.append(f"Check interval must be between {MIN_INTERVAL_MINUTES} minutes and 7 days.")
    return ConnForm(
        name,
        form.get("enabled") in ("on", "1", True),
        config,
        new_secrets,
        scopes,
        events,
        egress,
        bool(ack and "user" in scopes),
        interval,
        errors,
    )


def new_instance(user: User, plugin: BasePlugin) -> PluginInstance:
    return PluginInstance(
        plugin_key=plugin.info.key,
        name=plugin.info.name,
        owner_user_id=user.id,
        personal=True,
        enabled=False,
        events=list(plugin.info.default_events),
        scopes=[],
        config={},
        secret_refs={},
        sealed_secrets={},
        pull_interval_minutes=60,
    )


def can_create(session: Session, user: User) -> str | None:
    if not enabled(session):
        return "An admin has turned connections off."
    if len(list_for(session, user)) >= MAX_PER_USER:
        return f"You can have at most {MAX_PER_USER} connections."
    return None


def apply(session: Session, inst: PluginInstance, f: ConnForm) -> None:
    inst.name, inst.enabled, inst.config = f.name, f.enabled, f.config
    inst.scopes, inst.events, inst.egress = f.scopes, f.events, f.egress
    inst.user_scope_ack, inst.pull_interval_minutes = f.user_scope_ack, f.interval
    inst.secret_refs = {}  # a connection never names environment variables
    sealed = dict(inst.sealed_secrets or {})
    for name, value in f.new_secrets.items():
        sealed[name] = connection_secrets.seal(inst.id, name, value)
    inst.sealed_secrets = sealed
    session.add(inst)


def apply_preview(inst: PluginInstance, f: ConnForm) -> None:
    """Show what was submitted when the form is re-rendered with errors: in memory only, nothing is saved or sealed."""
    inst.name, inst.enabled, inst.scopes, inst.events = f.name, f.enabled, f.scopes, f.events
    inst.egress, inst.user_scope_ack, inst.pull_interval_minutes = f.egress, f.user_scope_ack, f.interval


def wipe_secrets(inst: PluginInstance) -> None:
    inst.sealed_secrets = {}


def delete(session: Session, inst: PluginInstance) -> None:
    for d in session.exec(select(PluginDelivery).where(PluginDelivery.instance_id == inst.id)).all():
        session.delete(d)
    session.delete(inst)


def disable_all_for(session: Session, user_id: str) -> None:
    """When an account is deactivated: its connections stop and their secrets are destroyed."""
    for inst in session.exec(
        select(PluginInstance).where(PluginInstance.personal == True, PluginInstance.owner_user_id == user_id)  # noqa: E712
    ).all():
        inst.enabled = False
        wipe_secrets(inst)
        session.add(inst)
