"""The event outbox. Core code calls `emit()` inside the same transaction as the change that caused it, so a
rolled-back change leaves no event behind and a committed one can't lose its event. Payloads are minimal:
ids, names, types, links — never record bodies. See docs/PLUGINS.md § Event outbox."""

from __future__ import annotations

from sqlmodel import Session, select

from .access import principal_for_user, readable_project_ids
from .models import Event, InboxItem, MemoryRecord, PluginInstance, Project, Team, User
from .plugins.base import EVENT_TYPES

INTERNAL_EVENT_TYPES = ("plugin.test",)  # deliverable only to the instance it targets


def emit(
    session: Session,
    type_: str,
    payload: dict,
    *,
    scope: str | None = None,
    project_slug: str | None = None,
    team_slug: str | None = None,
    owner_user_id: str | None = None,
    instance_id: str | None = None,
    origin_instance_id: str | None = None,
) -> Event | None:
    """Queue an event if any enabled plugin instance could want it. Returns None (and writes nothing) otherwise."""
    if type_ not in EVENT_TYPES and type_ not in INTERNAL_EVENT_TYPES:
        raise ValueError(f"unknown event type {type_!r}")
    instances = session.exec(select(PluginInstance).where(PluginInstance.enabled == True)).all()  # noqa: E712
    if instance_id:
        if not any(i.id == instance_id for i in instances):
            return None
    elif not any(type_ in (i.events or []) for i in instances):
        return None
    ev = Event(
        type=type_,
        payload=payload,
        scope=scope,
        project_slug=project_slug,
        team_slug=team_slug,
        owner_user_id=owner_user_id,
        instance_id=instance_id,
        origin_instance_id=origin_instance_id,
    )
    session.add(ev)
    session.flush()
    return ev


# --- payload builders ---------------------------------------------------------------------------------------


def _slugs(session: Session, rec: MemoryRecord) -> tuple[str | None, str | None]:
    proj = session.get(Project, rec.project_id) if rec.project_id else None
    team = session.get(Team, rec.team_id) if rec.team_id else None
    return (proj.slug if proj else None, team.slug if team else None)


def record_payload(session: Session, rec: MemoryRecord, **extra) -> dict:  # noqa: ANN003
    project, _ = _slugs(session, rec)
    return {
        "id": rec.id,
        "name": rec.name,
        "scope": rec.scope,
        "type": rec.type,
        "tier": rec.tier,
        "confidence": rec.confidence,
        "project": project,
        "link": f"/memory/{rec.id}",
        **extra,
    }


def emit_record_event(session: Session, type_: str, rec: MemoryRecord, **extra) -> Event | None:  # noqa: ANN003
    project, team = _slugs(session, rec)
    return emit(
        session,
        type_,
        record_payload(session, rec, **extra),
        scope=rec.scope,
        project_slug=project,
        team_slug=team,
        owner_user_id=rec.user_id,
    )


def emit_inbox_new(
    session: Session, item: InboxItem, *, origin_instance_id: str | None = None
) -> Event | None:
    proj = session.get(Project, item.project_id) if item.project_id else None
    return emit(
        session,
        "inbox.new",
        {"id": item.id, "title": item.title, "source": item.source, "scope": item.scope, "link": "/review"},
        scope=item.scope,
        project_slug=proj.slug if proj else None,
        owner_user_id=item.owner_user_id,
        origin_instance_id=origin_instance_id,
    )


# --- who may receive what -----------------------------------------------------------------------------------


class Visibility:
    """Per-fan-out cache: which projects/teams an instance's owner can read."""

    def __init__(self, session: Session):
        self.session = session
        self._projects: dict[str, set[str]] = {}
        self._teams: dict[str, set[str]] = {}

    def _load(self, owner_id: str) -> None:
        if owner_id in self._projects:
            return
        user = self.session.get(User, owner_id)
        if user is None:
            self._projects[owner_id], self._teams[owner_id] = set(), set()
            return
        p = principal_for_user(self.session, user)
        ids = readable_project_ids(self.session, p)
        self._projects[owner_id] = (
            {pr.slug for pr in self.session.exec(select(Project).where(Project.id.in_(ids))).all()}
            if ids
            else set()
        )  # type: ignore[attr-defined]
        teams = self.session.exec(select(Team).where(Team.id.in_(p.team_ids))).all() if p.team_ids else []  # type: ignore[attr-defined]
        self._teams[owner_id] = {t.slug for t in teams}

    def project(self, owner_id: str, slug: str | None) -> bool:
        self._load(owner_id)
        return slug is not None and slug in self._projects[owner_id]

    def team(self, owner_id: str, slug: str | None) -> bool:
        self._load(owner_id)
        return slug is not None and slug in self._teams[owner_id]


def instance_allows(
    inst: PluginInstance,
    vis: Visibility,
    *,
    scope: str | None,
    project_slug: str | None,
    team_slug: str | None,
    owner_user_id: str | None,
) -> bool:
    """The allowlist, deny by default (docs/SECURITY.md § Plugins and egress)."""
    if (
        scope is None
    ):  # scope-less events carry no memory content (plugin.failed, ...); owner-tagged ones stay with their owner
        return owner_user_id is None or owner_user_id == inst.owner_user_id
    if scope not in (inst.scopes or []):
        return False
    if scope == "user":  # only the operator's own personal memory, and only with an explicit acknowledgement
        return bool(inst.user_scope_ack) and owner_user_id == inst.owner_user_id
    if scope == "project":
        if inst.projects and project_slug not in inst.projects:
            return False
        return vis.project(inst.owner_user_id, project_slug)
    if scope == "team":
        return vis.team(inst.owner_user_id, team_slug)
    return False


def event_matches(inst: PluginInstance, ev: Event, vis: Visibility, *, sink_capable: bool) -> bool:
    if not inst.enabled or not sink_capable:
        return False
    if ev.origin_instance_id == inst.id:  # loop prevention: never echo an instance's own activity back to it
        return False
    if ev.instance_id is not None:
        return ev.instance_id == inst.id
    if ev.type not in (inst.events or []):
        return False
    return instance_allows(
        inst,
        vis,
        scope=ev.scope,
        project_slug=ev.project_slug,
        team_slug=ev.team_slug,
        owner_user_id=ev.owner_user_id,
    )
