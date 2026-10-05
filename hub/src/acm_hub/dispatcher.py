"""Runs plugins: fan events out to sink instances, deliver with retry/backoff, pull sources, send digests.

Everything here executes off the request path, in worker threads under timeouts; a plugin that raises, hangs, or
returns garbage is a failed attempt on *its* instance only — it can't block a memory write or another plugin.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import ValidationError
from sqlalchemy.engine import Engine
from sqlmodel import Session, delete, select

from . import events as ev_mod
from .access import principal_for_user
from .config import get_settings
from .models import (
    Event,
    InboxItem,
    MemoryRecord,
    PluginDelivery,
    PluginInstance,
    Project,
    Proposal,
    User,
    utcnow,
)
from .plugins import registry, remote
from .plugins.base import BasePlugin, DeliveryResult, InboxWriter, PluginContext
from .plugins.base import Event as PluginEvent
from .plugins.egress import EgressClient, redact, redacting_logger
from .records import list_records
from .security import SlidingWindowLimiter

log = logging.getLogger("acm_hub.dispatcher")
BACKOFF_SECONDS = (60, 300, 1800, 7200, 21600)  # then the delivery is dead
MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1
CALL_TIMEOUT = 30.0
PULL_TIMEOUT = 120.0
FAILURES_BEFORE_ALERT = 3
RETENTION = timedelta(days=30)
DIGEST_EVERY = timedelta(days=7)
_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="acm-plugin")
_rate = SlidingWindowLimiter(60)  # deliveries per instance per minute


# --- context ------------------------------------------------------------------------------------------------


class PluginConfigError(Exception):
    pass


def resolve_secrets(plugin: BasePlugin, inst: PluginInstance) -> dict[str, str]:
    """Values come from the environment at call time and live only in this dict; the database holds env var *names*."""
    out = {}
    for name in plugin.info.secret_names:
        env = (inst.secret_refs or {}).get(name)
        if env and os.environ.get(env):
            out[name] = os.environ[env]
    return out


def build_context(
    inst: PluginInstance, plugin: BasePlugin, *, inbox: InboxWriter | None = None
) -> PluginContext:
    try:
        config = plugin.info.config_schema(**(inst.config or {}))
    except ValidationError as e:
        raise PluginConfigError(
            f"invalid configuration: {e.errors()[0]['loc'][-1]}: {e.errors()[0]['msg']}"
        ) from e
    secrets = resolve_secrets(plugin, inst)
    allowed = getattr(config, "allowed_hosts", []) or []
    return PluginContext(
        instance_id=inst.id,
        instance_name=inst.name,
        config=config,
        secrets=secrets,
        scopes=frozenset(inst.scopes or []),
        egress=inst.egress,
        log=redacting_logger(f"acm_hub.plugin.{inst.plugin_key}", secrets),
        http=EgressClient(allowed),
        public_url=get_settings().public_url,
        inbox=inbox,
    )


def _present(session: Session, inst: PluginInstance, ev: Event) -> PluginEvent:
    """The event as this instance may see it: metadata by default; text only at egress=full and only if allowed."""
    payload = dict(ev.payload)
    link = payload.pop("link", None)
    if inst.egress == "full" and ev.type in {
        "record.created",
        "record.updated",
        "record.superseded",
        "conflict.flagged",
    }:
        rec = session.get(MemoryRecord, payload.get("id") or payload.get("record"))
        if rec is not None:
            project = session.get(Project, rec.project_id).slug if rec.project_id else None
            vis = ev_mod.Visibility(session)
            if ev_mod.instance_allows(
                inst,
                vis,
                scope=rec.scope,
                project_slug=project,
                team_slug=payload.get("team"),
                owner_user_id=rec.user_id,
            ):
                payload["description"] = rec.description
                payload["body"] = rec.body[:4000]
    return PluginEvent(
        id=ev.id,
        type=ev.type,
        created_at=ev.created_at,
        payload=payload,
        link=(get_settings().public_url + link) if link else None,
    )


def _call(fn, timeout: float):  # noqa: ANN001, ANN202
    fut = _pool.submit(fn)
    try:
        return fut.result(timeout=timeout)
    except FutureTimeout as e:
        fut.cancel()
        raise TimeoutError(f"timed out after {int(timeout)}s") from e


# --- fan-out and delivery -----------------------------------------------------------------------------------


@dataclass
class DispatchStats:
    fanned_out: int = 0
    delivered: int = 0
    retried: int = 0
    dead: int = 0
    purged: int = 0


def _sink_capable(key: str) -> bool:
    p = registry.get(key)
    if p is None:
        # A remote service that is down right now might be a sink. Queue its deliveries (they retry with backoff)
        # rather than letting the event be fanned out once and lost; a source never subscribes to events anyway.
        return remote.is_remote_key(key)
    return p.info.kind in {"sink", "both"}


def fan_out(session: Session, *, limit: int = 500, now: datetime | None = None) -> int:
    pending = session.exec(
        select(Event).where(Event.dispatched_at.is_(None)).order_by(Event.created_at).limit(limit)
    ).all()  # type: ignore[union-attr]
    if not pending:
        return 0
    instances = session.exec(select(PluginInstance).where(PluginInstance.enabled == True)).all()  # noqa: E712
    vis = ev_mod.Visibility(session)
    n = 0
    for ev in pending:
        for inst in instances:
            if (
                ev_mod.event_matches(inst, ev, vis, sink_capable=_sink_capable(inst.plugin_key))
                and session.get(PluginDelivery, (ev.id, inst.id)) is None
            ):
                session.add(
                    PluginDelivery(event_id=ev.id, instance_id=inst.id, next_attempt_at=now or utcnow())
                )
                n += 1
        ev.dispatched_at = utcnow()
        session.add(ev)
    session.flush()
    return n


def _attempt(session: Session, inst: PluginInstance, ev: Event) -> DeliveryResult:
    plugin = registry.get(inst.plugin_key)
    if plugin is None:
        if (why := remote.unavailable_reason(inst.plugin_key)) is not None:
            return DeliveryResult.retry_later(f"the plugin service is unavailable: {why}")
        return DeliveryResult.failed(f"plugin {inst.plugin_key!r} isn't installed")
    try:
        ctx = build_context(inst, plugin)
    except PluginConfigError as e:
        return DeliveryResult.failed(str(e))
    pe = _present(session, inst, ev)
    secrets = list(ctx.secrets.values())
    try:
        res = _call(lambda: plugin.deliver(ctx, pe), CALL_TIMEOUT)
        if not isinstance(res, DeliveryResult):
            return DeliveryResult.failed("plugin returned an invalid result")
        return DeliveryResult(res.ok, res.retry, redact(res.message, secrets))
    except Exception as e:  # noqa: BLE001 — isolation: any plugin failure is just a failed attempt
        return DeliveryResult.retry_later(redact(f"{type(e).__name__}: {e}", secrets))


def _record_outcome(session: Session, inst: PluginInstance, ok: bool, message: str, now: datetime) -> None:
    inst.last_run_at = now
    if ok:
        inst.last_status, inst.last_error, inst.consecutive_failures = "ok", None, 0
    else:
        inst.last_status, inst.last_error = "error", (message or "failed")[:500]
        inst.consecutive_failures += 1
        if inst.consecutive_failures == FAILURES_BEFORE_ALERT:
            ev_mod.emit(  # tell the operator through *other* sinks; never back to the failing instance
                session,
                "plugin.failed",
                {
                    "instance": inst.name,
                    "plugin": inst.plugin_key,
                    "error": inst.last_error,
                    "link": "/plugins",
                },
                owner_user_id=inst.owner_user_id,
                origin_instance_id=inst.id,
            )
    session.add(inst)


def deliver_due(engine: Engine, *, now: datetime | None = None, limit: int = 100) -> DispatchStats:
    now = now or utcnow()
    stats = DispatchStats()
    with Session(engine) as s:
        due = s.exec(
            select(PluginDelivery)
            .where(PluginDelivery.status == "pending", PluginDelivery.next_attempt_at <= now)
            .order_by(PluginDelivery.next_attempt_at)
            .limit(limit)
        ).all()
        for d in due:
            inst, ev = s.get(PluginInstance, d.instance_id), s.get(Event, d.event_id)
            if inst is None or ev is None:
                s.delete(d)
                continue
            if not inst.enabled:
                continue  # parked until re-enabled
            if not _rate.allow(inst.id):
                d.next_attempt_at = now + timedelta(
                    seconds=60
                )  # per-instance rate limit: wait, don't burn an attempt
                s.add(d)
                continue
            res = _attempt(s, inst, ev)
            d.attempts += 1
            if res.ok:
                d.status, d.delivered_at, d.last_error = "delivered", now, None
                stats.delivered += 1
            elif not res.retry or d.attempts >= MAX_ATTEMPTS:
                d.status, d.last_error = "dead", res.message
                stats.dead += 1
            else:
                d.last_error = res.message
                d.next_attempt_at = now + timedelta(
                    seconds=BACKOFF_SECONDS[min(d.attempts - 1, len(BACKOFF_SECONDS) - 1)]
                )
                stats.retried += 1
            _record_outcome(s, inst, res.ok, res.message, now)
            s.add(d)
            s.commit()  # per delivery: one plugin's slowness never holds a transaction for the others
    return stats


def purge_old(session: Session, now: datetime | None = None) -> int:
    cutoff = (now or utcnow()) - RETENTION
    old = [
        e.id
        for e in session.exec(
            select(Event).where(Event.created_at < cutoff, Event.dispatched_at.is_not(None))
        ).all()
    ]  # type: ignore[union-attr]
    if not old:
        return 0
    open_ids = set(
        session.exec(
            select(PluginDelivery.event_id).where(
                PluginDelivery.status == "pending", PluginDelivery.event_id.in_(old)
            )
        ).all()
    )  # type: ignore[attr-defined]
    gone = [i for i in old if i not in open_ids]
    session.exec(delete(PluginDelivery).where(PluginDelivery.event_id.in_(gone)))  # type: ignore[call-overload,attr-defined]
    session.exec(delete(Event).where(Event.id.in_(gone)))  # type: ignore[call-overload,attr-defined]
    return len(gone)


def dispatch_once(engine: Engine, *, now: datetime | None = None) -> DispatchStats:
    with Session(engine) as s:
        fanned = fan_out(s, now=now)
        s.commit()
    # Evaluate "now" *after* fan-out: deliveries created a moment ago must count as due, not wait for the next tick.
    now = now or utcnow()
    stats = deliver_due(engine, now=now)
    stats.fanned_out = fanned
    with Session(engine) as s:
        stats.purged = purge_old(s, now)
        s.commit()
    return stats


def deliver_test_event(engine: Engine, instance_id: str) -> DeliveryResult:
    """Send a test event to one instance right now (the Plugins screen's button) and report exactly what happened."""
    with Session(engine) as s:
        inst = s.get(PluginInstance, instance_id)
        if inst is None:
            return DeliveryResult.failed("No such instance.")
        if not _sink_capable(inst.plugin_key):
            return DeliveryResult.failed("This plugin doesn't deliver notifications.")
        ev = Event(
            type="plugin.test",
            payload={
                "message": "This is a test from your memory hub.",
                "instance": inst.name,
                "link": "/plugins",
            },
            instance_id=inst.id,
        )
        res = _attempt(s, inst, ev)
        _record_outcome(s, inst, res.ok, res.message, utcnow())
        s.commit()
        return res


# --- sources ------------------------------------------------------------------------------------------------


@dataclass
class PullResult:
    captured: int = 0
    error: str | None = None


def _inbox_adder(engine: Engine, inst: PluginInstance, counter: list[int]):  # noqa: ANN202
    owner, key, cfg = inst.owner_user_id, inst.plugin_key, dict(inst.config or {})
    inst_id = inst.id

    def add(title: str, body: str, external_ref: str | None) -> bool:
        title = (title or "").strip()[:200]
        if not title:
            return False
        body = (body or "")[:20000]
        with Session(engine) as s:
            scope = cfg.get("inbox_scope") or "user"
            proj = (
                s.exec(select(Project).where(Project.slug == cfg["inbox_project"])).first()
                if cfg.get("inbox_project")
                else None
            )
            if scope == "project" and proj is None:
                raise ValueError("inbox_scope is 'project' but inbox_project doesn't exist")
            if external_ref:
                prior = s.exec(
                    select(InboxItem).where(
                        InboxItem.owner_user_id == owner,
                        InboxItem.source == f"plugin:{key}",
                        InboxItem.external_ref == external_ref,
                    )
                ).first()
                if prior is not None:
                    if prior.status == "new" and (prior.title, prior.body) != (title, body):
                        prior.title, prior.body = title, body  # the note changed before anyone reviewed it
                        s.add(prior)
                        s.commit()
                    return False  # already known (harvested/dismissed items stay decided)
            item = InboxItem(
                owner_user_id=owner,
                source=f"plugin:{key}",
                scope=scope,
                project_id=proj.id if proj else None,
                title=title,
                body=body,
                external_ref=external_ref,
            )
            s.add(item)
            s.flush()
            ev_mod.emit_inbox_new(s, item, origin_instance_id=inst_id)
            s.commit()
            counter[0] += 1
            return True

    return add


def run_pull(engine: Engine, instance_id: str) -> PullResult:
    with Session(engine) as s:
        inst = s.get(PluginInstance, instance_id)
        plugin = registry.get(inst.plugin_key) if inst else None
        if inst is not None and plugin is None and (why := remote.unavailable_reason(inst.plugin_key)):
            err = f"the plugin service is unavailable: {why}"
            _record_outcome(s, inst, False, err, utcnow())
            s.commit()
            return PullResult(error=err)
        if inst is None or plugin is None or plugin.info.kind not in {"source", "both"}:
            return PullResult(error="Not a source plugin instance.")
        counter = [0]
        try:
            ctx = build_context(inst, plugin, inbox=InboxWriter(_inbox_adder(engine, inst, counter)))
            if getattr(plugin, "remote", False):
                # Tell the service where we got to, but only after a clean run: after a failure it must start over,
                # or whatever it would have returned in between is silently skipped.
                ctx.extras["since"] = (
                    inst.last_run_at.isoformat() + "Z"
                    if inst.last_status == "ok" and inst.last_run_at
                    else None
                )
            secrets = list(ctx.secrets.values())
            _call(lambda: plugin.pull(ctx), PULL_TIMEOUT)
            err = None
        except Exception as e:  # noqa: BLE001
            secrets = list(resolve_secrets(plugin, inst).values())
            err = redact(f"{type(e).__name__}: {e}", secrets)
        _record_outcome(s, inst, err is None, err or "", utcnow())
        s.commit()
        return PullResult(counter[0], err)


def pulls_due(engine: Engine, now: datetime | None = None) -> list[str]:
    now = now or utcnow()
    out = []
    with Session(engine) as s:
        for inst in s.exec(select(PluginInstance).where(PluginInstance.enabled == True)).all():  # noqa: E712
            p = registry.get(inst.plugin_key)
            if p is None or p.info.kind not in {"source", "both"}:
                continue
            if inst.last_run_at is None or now - inst.last_run_at >= timedelta(
                minutes=max(inst.pull_interval_minutes, 5)
            ):
                out.append(inst.id)
    return out


# --- digest -------------------------------------------------------------------------------------------------


def build_digest(session: Session, inst: PluginInstance, now: datetime) -> dict:
    """Last week in memory, restricted to what this instance's allowlist (and its owner's access) permits."""
    owner = session.get(User, inst.owner_user_id)
    cutoff = now - DIGEST_EVERY
    vis = ev_mod.Visibility(session)
    p = principal_for_user(session, owner) if owner else None
    recs = list_records(session, p, status=None, limit=5000) if p else []

    def ok(r: MemoryRecord) -> bool:
        proj = session.get(Project, r.project_id).slug if r.project_id else None
        return ev_mod.instance_allows(
            inst, vis, scope=r.scope, project_slug=proj, team_slug=None, owner_user_id=r.user_id
        )

    recs = [r for r in recs if ok(r)]
    new = [r for r in recs if r.created_at >= cutoff]
    changed = [r for r in recs if r.updated_at >= cutoff and r.created_at < cutoff and r.status == "active"]
    superseded = [r for r in recs if r.status == "superseded" and r.valid_to and r.valid_to >= cutoff]
    stale = [r for r in recs if r.status == "stale" and r.updated_at >= cutoff]
    props = [x for x in session.exec(select(Proposal).where(Proposal.status == "pending")).all()]
    allowed_ids = {r.id for r in recs}
    pending = [x for x in props if x.target_ids and all(t in allowed_ids for t in x.target_ids)]
    inbox = (
        len(
            session.exec(
                select(InboxItem).where(
                    InboxItem.owner_user_id == inst.owner_user_id, InboxItem.status == "new"
                )
            ).all()
        )
        if "user" in (inst.scopes or []) and inst.user_scope_ack
        else 0
    )
    names = lambda rs: [r.name for r in rs[:8]]  # noqa: E731
    return {
        "period_days": 7,
        "new": len(new),
        "new_names": names(new),
        "changed": len(changed),
        "changed_names": names(changed),
        "superseded": len(superseded),
        "went_stale": len(stale),
        "pending_proposals": len(pending),
        "inbox_waiting": inbox,
        "link": "/review",
    }


def digests_due(engine: Engine, now: datetime | None = None) -> list[str]:
    now = now or utcnow()
    out = []
    with Session(engine) as s:
        for inst in s.exec(select(PluginInstance).where(PluginInstance.enabled == True)).all():  # noqa: E712
            wants = "digest.weekly" in (inst.events or []) or bool((inst.config or {}).get("export_digest"))
            if wants and now - (inst.last_digest_at or inst.created_at) >= DIGEST_EVERY:
                out.append(inst.id)
    return out


def send_digest(engine: Engine, instance_id: str, now: datetime | None = None) -> bool:
    now = now or utcnow()
    with Session(engine) as s:
        inst = s.get(PluginInstance, instance_id)
        plugin = registry.get(inst.plugin_key) if inst else None
        if inst is None or plugin is None:
            return False
        digest = build_digest(s, inst, now)
        if "digest.weekly" in (inst.events or []) and plugin.info.kind in {"sink", "both"}:
            ev_mod.emit(
                s,
                "digest.weekly",
                {**digest, "link": "/review"},
                instance_id=inst.id,
                owner_user_id=inst.owner_user_id,
            )
        if (inst.config or {}).get("export_digest") and plugin.info.kind in {"source", "both"}:
            try:
                ctx = build_context(inst, plugin)
                _call(lambda: plugin.export_digest(ctx, digest), PULL_TIMEOUT)
                _record_outcome(s, inst, True, "", now)
            except Exception as e:  # noqa: BLE001
                _record_outcome(
                    s,
                    inst,
                    False,
                    redact(
                        f"digest export failed: {type(e).__name__}: {e}",
                        resolve_secrets(plugin, inst).values(),
                    ),
                    now,
                )
        inst.last_digest_at = now
        s.add(inst)
        s.commit()
        return True


def run_scheduled(engine: Engine, now: datetime | None = None) -> dict:
    """One scheduler tick: deliver, pull what's due, send digests that are due."""
    stats = dispatch_once(engine, now=now)
    pulls = [run_pull(engine, i) for i in pulls_due(engine, now)]
    digests = [send_digest(engine, i, now) for i in digests_due(engine, now)]
    from . import search_sync  # late: search_sync uses this module's call/timeout helpers

    syncs = [search_sync.run_sync(engine, i, now=now) for i in search_sync.sync_due(engine, now)]
    return {"dispatch": stats, "pulls": pulls, "digests": sum(digests), "syncs": syncs}
