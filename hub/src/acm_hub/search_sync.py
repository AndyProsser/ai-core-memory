"""Search plugins: keeping an external index in step with what an instance may see, and asking it for candidates.

See docs/PLUGINS.md § Search plugins. The index lives in the plugin service. The hub decides, on every sync, which
active records the instance is *allowed* to hold (the same deny-by-default allowlist, owner-visibility check and
user-scope acknowledgement as any plugin), compares a content hash with what it last sent, and sends only the
difference — so a record that is edited, archived, moved out of the allowlist, or that its owner lost access to, is
removed from the service rather than lingering there. At query time it asks the caller's own instances for ids,
re-checks every id against the caller's access in `focus`, and treats a failure as "no semantic results".
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.engine import Engine
from sqlmodel import Session, col, delete, select

from . import events as ev_mod
from .access import Principal, principal_for_user
from .models import MemoryRecord, PluginInstance, Project, SearchIndexEntry, User, utcnow
from .plugins import registry, remote
from .plugins.egress import redact
from .records import list_records, team_slug

log = logging.getLogger("acm_hub.search_sync")
BATCH_INDEX = 50
BATCH_REMOVE = 200
MAX_BODY = 8000  # characters of body sent at egress=full
SYNC_BUDGET_SECONDS = 90.0
QUERY_TIMEOUT = 2.0  # per instance
QUERY_BUDGET = 3.0  # in total, per focus call
MAX_QUERY_INSTANCES = 2
DEFAULT_SYNC_MINUTES = 15

MORE_TO_DO: set[str] = set()  # instances whose last run ran out of time: sync again at the next tick


@dataclass
class SyncResult:
    indexed: int = 0
    removed: int = 0
    remaining: int = 0
    error: str | None = None


def _chunks(items: list, n: int):  # noqa: ANN202
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _doc(session: Session, inst: PluginInstance, r: MemoryRecord) -> dict:
    proj = session.get(Project, r.project_id).slug if r.project_id else None
    doc = {
        "id": r.id,
        "name": r.name,
        "description": r.description,
        "topics": list(r.topics),
        "type": r.type,
        "scope": r.scope,
        "project": proj,
        "tier": r.tier,
        "confidence": r.confidence,
    }
    if (
        inst.egress == "full" and not r.encrypted
    ):  # text only on an explicit opt-in, and never an encrypted body
        doc["body"] = r.body[:MAX_BODY]
    return doc


def desired_documents(session: Session, inst: PluginInstance, owner: User) -> dict[str, tuple[str, dict]]:
    """{record id: (content hash, document)} for every active record this instance is allowed to hold."""
    vis = ev_mod.Visibility(session)
    p = principal_for_user(session, owner)
    out: dict[str, tuple[str, dict]] = {}
    for r in list_records(session, p, status="active", limit=100_000):
        proj = session.get(Project, r.project_id).slug if r.project_id else None
        if not ev_mod.instance_allows(
            inst,
            vis,
            scope=r.scope,
            project_slug=proj,
            team_slug=team_slug(session, r),
            owner_user_id=r.user_id,
        ):
            continue
        doc = _doc(session, inst, r)
        out[r.id] = (hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest(), doc)
    return out


def _search_plugin(inst: PluginInstance | None):  # noqa: ANN202
    plugin = registry.get(inst.plugin_key) if inst else None
    return plugin if plugin is not None and plugin.info.kind == "search" else None


def run_sync(
    engine: Engine,
    instance_id: str,
    *,
    rebuild: bool = False,
    now: datetime | None = None,
    budget_seconds: float = SYNC_BUDGET_SECONDS,
) -> SyncResult:
    from . import (
        dispatcher,  # late: dispatcher imports a lot, and we only need its call/timeout/outcome helpers
    )

    now = now or utcnow()
    with Session(engine) as s:
        inst = s.get(PluginInstance, instance_id)
        if inst is None:
            return SyncResult(error="No such instance.")
        plugin = _search_plugin(inst)
        if plugin is None:
            why = remote.unavailable_reason(inst.plugin_key)
            err = f"the plugin service is unavailable: {why}" if why else "Not a search plugin instance."
            if why:
                dispatcher._record_outcome(s, inst, False, err, now)  # noqa: SLF001
                s.commit()
            return SyncResult(error=err)
        owner = s.get(User, inst.owner_user_id)
        if owner is None or not owner.is_active:
            return SyncResult(error="The instance's owner is no longer active.")
        res = SyncResult()
        try:
            ctx = dispatcher.build_context(inst, plugin)
            if rebuild:
                dispatcher._call(lambda: plugin.reset(ctx), dispatcher.CALL_TIMEOUT)  # noqa: SLF001
                s.exec(delete(SearchIndexEntry).where(SearchIndexEntry.instance_id == inst.id))  # type: ignore[call-overload]
                s.commit()
            desired = desired_documents(s, inst, owner)
            state = {
                e.record_id: e
                for e in s.exec(select(SearchIndexEntry).where(SearchIndexEntry.instance_id == inst.id))
            }
            to_remove = [rid for rid in state if rid not in desired]
            to_index = [
                rid for rid, (h, _) in desired.items() if rid not in state or state[rid].content_hash != h
            ]
            deadline = time.monotonic() + budget_seconds
            for batch in _chunks(to_remove, BATCH_REMOVE):
                dispatcher._call(lambda b=batch: plugin.remove(ctx, b), dispatcher.CALL_TIMEOUT)  # noqa: SLF001
                for rid in batch:
                    s.delete(state[rid])
                s.commit()
                res.removed += len(batch)
            for batch in _chunks(to_index, BATCH_INDEX):
                if time.monotonic() > deadline:
                    break
                dispatcher._call(  # noqa: SLF001
                    lambda b=batch: plugin.index(ctx, [desired[rid][1] for rid in b]), dispatcher.CALL_TIMEOUT
                )
                for rid in batch:
                    entry = state.get(rid) or SearchIndexEntry(
                        instance_id=inst.id, record_id=rid, content_hash=""
                    )
                    entry.content_hash, entry.indexed_at = desired[rid][0], utcnow()
                    s.add(entry)
                s.commit()
                res.indexed += len(batch)
            res.remaining = len(to_index) - res.indexed
        except Exception as e:  # noqa: BLE001 — isolation: any plugin failure is just a failed run on this instance
            res.error = redact(f"{type(e).__name__}: {e}", [])
        (MORE_TO_DO.add if res.remaining and not res.error else MORE_TO_DO.discard)(inst.id)
        dispatcher._record_outcome(s, inst, res.error is None, res.error or "", now)  # noqa: SLF001
        s.commit()
        return res


def sync_due(engine: Engine, now: datetime | None = None) -> list[str]:
    now = now or utcnow()
    out = []
    with Session(engine) as s:
        for inst in s.exec(select(PluginInstance).where(PluginInstance.enabled == True)).all():  # noqa: E712
            if _search_plugin(inst) is None:
                continue
            every = timedelta(minutes=max(inst.pull_interval_minutes, 5))
            if inst.id in MORE_TO_DO or inst.last_run_at is None or now - inst.last_run_at >= every:
                out.append(inst.id)
    return out


def indexed_count(session: Session, instance_id: str) -> int:
    return len(
        session.exec(select(SearchIndexEntry).where(SearchIndexEntry.instance_id == instance_id)).all()
    )


def wipe(engine: Engine, instance_id: str) -> None:
    """Forget an instance's index (on delete). Best effort at the service: it may be down; our bookkeeping always goes."""
    from . import dispatcher

    with Session(engine) as s:
        inst = s.get(PluginInstance, instance_id)
        plugin = _search_plugin(inst)
        if inst is not None and plugin is not None:
            try:
                ctx = dispatcher.build_context(inst, plugin)
                dispatcher._call(lambda: plugin.reset(ctx), dispatcher.CALL_TIMEOUT)  # noqa: SLF001
            except Exception:  # noqa: BLE001
                log.warning(
                    "couldn't ask %s to forget the index of %s; delete it on the service",
                    inst.plugin_key,
                    inst.name,
                )
        s.exec(delete(SearchIndexEntry).where(SearchIndexEntry.instance_id == instance_id))  # type: ignore[call-overload]
        s.commit()
    MORE_TO_DO.discard(instance_id)


# --- query time --------------------------------------------------------------------------------------------------------


def semantic_candidates(
    session: Session, p: Principal, task: str, *, limit: int = 20
) -> tuple[list[tuple[str, int, str]], list[str]]:
    """Ranked ids from the caller's own search instances: ([(record id, rank from 0, instance name)], notes).

    An instance serves only its owner, so one person's index never answers another's query; whatever comes back is
    still re-checked against the caller's access by the focus builder. Anything that goes wrong is a note, not an error.
    """
    from . import dispatcher

    notes: list[str] = []
    best: dict[str, tuple[int, str]] = {}
    if not p.user_id or not (task or "").strip():
        return [], notes
    started = time.monotonic()
    insts = session.exec(
        select(PluginInstance).where(
            PluginInstance.owner_user_id == p.user_id, col(PluginInstance.enabled).is_(True)
        )
    ).all()
    asked = 0
    for inst in insts:
        plugin = _search_plugin(inst)
        if plugin is None:
            if remote.known_kind(inst.plugin_key) == "search" and (
                why := remote.unavailable_reason(inst.plugin_key)
            ):
                notes.append(f"Semantic search ({inst.name}) is unavailable: {why}")
            continue
        if asked >= MAX_QUERY_INSTANCES or time.monotonic() - started > QUERY_BUDGET:
            break
        asked += 1
        try:
            ctx = dispatcher.build_context(inst, plugin)
            hits = plugin.search(
                ctx,
                task[:500],
                limit,
                min(QUERY_TIMEOUT, max(0.2, QUERY_BUDGET - (time.monotonic() - started))),
            )
        except Exception as e:  # noqa: BLE001
            notes.append(f"Semantic search ({inst.name}) didn't answer in time or failed: {type(e).__name__}")
            continue
        for rank, (rid, _score) in enumerate(hits):
            if rid not in best or rank < best[rid][0]:
                best[rid] = (rank, inst.name)
    ranked = sorted(((rid, r, name) for rid, (r, name) in best.items()), key=lambda t: t[1])
    return ranked[:limit], notes
