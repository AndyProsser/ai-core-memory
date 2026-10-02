"""Who can see and change what. Enforces the partition rules in docs/ARCHITECTURE.md and the
visibility/role model in docs/SECURITY.md. Everything that touches records goes through here."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import and_, false, or_, true
from sqlmodel import Session, select

from .models import MemoryRecord, Project, TeamMember, User


class AccessError(Exception):
    """The caller isn't allowed to do this (maps to 403)."""


class NotFound(Exception):
    """No such record, or it isn't visible to the caller (maps to 404 — we don't leak existence)."""


@dataclass
class Principal:
    user_id: str
    email: str = ""
    is_admin: bool = False
    kind: str = "session"  # session | token | cli
    token_id: str | None = None
    token_project_ids: list[str] = field(default_factory=list)  # empty = unrestricted
    token_include_user_scope: bool = False
    read_only: bool = False
    label: str | None = None  # OS username for CLI actors
    team_ids: set[str] = field(default_factory=set)

    @property
    def is_human(self) -> bool:
        return self.kind in {"session", "cli"}

    @property
    def is_system(self) -> bool:
        return self.kind == "system"

    @property
    def is_token(self) -> bool:
        return self.kind == "token"


def system_principal() -> Principal:
    """The mechanical consolidation job. Not reachable from any network path; it is only constructed inside
    the hub (scheduler, `acm consolidate`). It can see everything but is NOT human: it can never change
    `established` records, core, or scope — those always wait for a person."""
    return Principal(user_id="", email="", kind="system", label="mechanical")


def principal_for_user(
    session: Session, user: User, kind: str = "session", label: str | None = None
) -> Principal:
    teams = set(session.exec(select(TeamMember.team_id).where(TeamMember.user_id == user.id)).all())
    return Principal(
        user_id=user.id, email=user.email, is_admin=user.is_admin, kind=kind, label=label, team_ids=teams
    )


# --- projects --------------------------------------------------------------------------------------


def _token_limited(p: Principal, project_ids: set[str]) -> set[str]:
    return project_ids & set(p.token_project_ids) if p.token_project_ids else project_ids


def readable_project_ids(session: Session, p: Principal) -> set[str]:
    """Visibility: private=owner only; team=members of the owning team; public=any authenticated user."""
    rows = session.exec(select(Project)).all()
    ids = set()
    for pr in rows:
        if pr.owner_user_id == p.user_id:
            ids.add(pr.id)
        elif pr.visibility == "public":
            ids.add(pr.id)
        elif pr.visibility == "team" and pr.team_id and pr.team_id in p.team_ids:
            ids.add(pr.id)
    return _token_limited(p, ids)


def writable_project_ids(session: Session, p: Principal) -> set[str]:
    """Seeing a public project never implies writing to it: writes follow ownership / team membership."""
    if p.read_only:
        return set()
    ids = set()
    for pr in session.exec(select(Project)).all():
        if pr.owner_user_id == p.user_id or (pr.team_id and pr.team_id in p.team_ids):
            ids.add(pr.id)
    return _token_limited(p, ids)


def readable_team_ids(session: Session, p: Principal) -> set[str]:
    teams = set(p.team_ids)
    if p.token_project_ids:  # a project-limited token only reaches the teams that own those projects
        owning = set(
            session.exec(select(Project.team_id).where(Project.id.in_(p.token_project_ids))).all()  # type: ignore[attr-defined]
        )
        teams &= {t for t in owning if t}
    return teams


# --- records ---------------------------------------------------------------------------------------


def visible_clause(session: Session, p: Principal):
    """SQL condition selecting the records `p` may read (all statuses)."""
    if p.is_system:
        return true()
    parts = []
    if not p.is_token or p.token_include_user_scope:
        parts.append(and_(MemoryRecord.scope == "user", MemoryRecord.user_id == p.user_id))
    teams = readable_team_ids(session, p)
    if teams:
        parts.append(and_(MemoryRecord.scope == "team", MemoryRecord.team_id.in_(teams)))  # type: ignore[attr-defined]
    projects = readable_project_ids(session, p)
    if projects:
        parts.append(and_(MemoryRecord.scope == "project", MemoryRecord.project_id.in_(projects)))  # type: ignore[attr-defined]
    return or_(*parts) if parts else false()


def can_read(session: Session, p: Principal, rec: MemoryRecord) -> bool:
    if p.is_system:
        return True
    if rec.scope == "user":
        return rec.user_id == p.user_id and (not p.is_token or p.token_include_user_scope)
    if rec.scope == "team":
        return rec.team_id in readable_team_ids(session, p)
    if rec.scope == "project":
        return rec.project_id in readable_project_ids(session, p)
    return False


def can_write(session: Session, p: Principal, rec: MemoryRecord) -> bool:
    if p.is_system:
        return True
    if p.read_only:
        return False
    if rec.scope == "user":
        return rec.user_id == p.user_id and (not p.is_token or p.token_include_user_scope)
    if rec.scope == "team":
        # Team-scope is opt-in and promoted deliberately: AI clients never write it directly.
        return p.is_human and rec.team_id in p.team_ids
    if rec.scope == "project":
        return rec.project_id in writable_project_ids(session, p)
    return False


def require_read(session: Session, p: Principal, rec: MemoryRecord | None) -> MemoryRecord:
    if rec is None or not can_read(session, p, rec):
        raise NotFound("No such record.")
    return rec


def require_write(session: Session, p: Principal, rec: MemoryRecord | None) -> MemoryRecord:
    if rec is None or not can_read(session, p, rec):
        raise NotFound("No such record.")
    if not can_write(session, p, rec):
        raise AccessError("You can read this record but not change it.")
    return rec
