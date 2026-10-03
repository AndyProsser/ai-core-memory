"""Users, teams and projects: the role model from docs/SECURITY.md § Roles, enforced in one place for the web UI and
the CLI alike.

  admin   (instance-wide)  manage accounts, set deployment_mode, create/delete teams — and nothing about memory content.
  owner   (per team)       add/remove members, promote/demote, create/delete that team's projects, set their visibility.
  member  (per team)       read/write the team's memory; manage their own tokens.

Admin is deliberately *not* an owner of anything and sees exactly what a regular user with the same memberships sees.
"""

from __future__ import annotations

import re
import secrets

from sqlalchemy import delete, text
from sqlmodel import Session, col, select

from .access import AccessError, NotFound
from .models import (
    ApiToken,
    InboxItem,
    InstanceSettings,
    MemoryLink,
    MemoryRecord,
    MemoryRevision,
    PluginInstance,
    Project,
    Proposal,
    Reinforcement,
    Team,
    TeamMember,
    User,
    WebSession,
    utcnow,
)
from .records import ValidationFailed
from .security import check_password_policy, hash_password

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ROLES = ("owner", "member")
VISIBILITY_BY_MODE = {
    "solo": ("private", "public"),
    "team": ("private", "team"),
    "multi_team": ("private", "team", "public"),
}


def deployment_mode(session: Session) -> str:
    inst = session.get(InstanceSettings, 1)
    return inst.deployment_mode if inst else "solo"


def allowed_visibilities(session: Session) -> tuple[str, ...]:
    return VISIBILITY_BY_MODE[deployment_mode(session)]


def teams_enabled(session: Session) -> bool:
    return deployment_mode(session) != "solo"


def _require_human(actor) -> None:  # noqa: ANN001
    if not actor.is_human:
        raise AccessError("Only a person can do that; API tokens can't manage users, teams or projects.")


def _require_admin(actor) -> None:  # noqa: ANN001
    _require_human(actor)
    if not actor.is_admin:
        raise AccessError("Only an admin can do that.")


# --- users (admin) --------------------------------------------------------------------------------------------


def _admins(session: Session) -> list[User]:
    return [u for u in session.exec(select(User)).all() if u.is_admin and u.is_active]


def create_user(
    session: Session,
    actor,
    email: str,
    *,
    password: str | None = None,
    sso_invite: bool = False,
    is_admin: bool = False,
) -> User:  # noqa: ANN001
    _require_admin(actor)
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise ValidationFailed("Enter a valid email address.")
    if session.exec(select(User).where(User.email == email)).first():
        raise ValidationFailed(f"{email} already has an account.")
    if sso_invite:
        user = User(
            email=email, auth_provider="oidc", is_admin=is_admin
        )  # links on their first verified SSO login
    else:
        if not password:
            raise ValidationFailed("Set an initial password, or invite them through single sign-on.")
        if problem := check_password_policy(password):
            raise ValidationFailed(problem)
        user = User(email=email, password_hash=hash_password(password), is_admin=is_admin)
    session.add(user)
    session.flush()
    return user


def new_temporary_password() -> str:
    return "-".join(secrets.token_urlsafe(6) for _ in range(3))  # ~barely guessable, readable, shown once


def reset_password(session: Session, actor, user: User) -> str:  # noqa: ANN001
    _require_admin(actor)
    if user.auth_provider != "local":
        raise ValidationFailed(
            "That account signs in through single sign-on; there's no local password to reset."
        )
    pw = new_temporary_password()
    user.password_hash = hash_password(pw)
    session.add(user)
    for ws in session.exec(select(WebSession).where(WebSession.user_id == user.id)).all():
        session.delete(ws)  # whoever had the old password is signed out
    session.flush()
    return pw


def set_user_active(session: Session, actor, user: User, active: bool) -> None:  # noqa: ANN001
    _require_admin(actor)
    if not active:
        if user.id == actor.user_id:
            raise ValidationFailed("You can't deactivate your own account.")
        if user.is_admin and user.is_active and len(_admins(session)) <= 1:
            raise ValidationFailed("That's the last active admin; make someone else an admin first.")
        # Cut every way in at once: sessions, API tokens, and any plugin that acts for them.
        for ws in session.exec(select(WebSession).where(WebSession.user_id == user.id)).all():
            session.delete(ws)
        for tok in session.exec(
            select(ApiToken).where(ApiToken.user_id == user.id, col(ApiToken.revoked_at).is_(None))
        ).all():
            tok.revoked_at = utcnow()
            session.add(tok)
        from .models import OAuthGrant
        from .oauth import revoke_grant

        for grant in session.exec(
            select(OAuthGrant).where(OAuthGrant.user_id == user.id, col(OAuthGrant.revoked_at).is_(None))
        ).all():
            revoke_grant(session, grant)  # connected apps (e.g. Claude.ai) lose their refresh tokens too
        for inst in session.exec(select(PluginInstance).where(PluginInstance.owner_user_id == user.id)).all():
            inst.enabled = False
            session.add(inst)
    user.is_active = active
    session.add(user)
    session.flush()


def set_user_admin(session: Session, actor, user: User, is_admin: bool) -> None:  # noqa: ANN001
    _require_admin(actor)
    if not is_admin and user.is_admin and user.is_active and len(_admins(session)) <= 1:
        raise ValidationFailed("That's the last active admin; make someone else an admin first.")
    user.is_admin = is_admin
    session.add(user)
    session.flush()


# --- teams ----------------------------------------------------------------------------------------------------


def team_role(session: Session, user_id: str, team_id: str) -> str | None:
    m = session.get(TeamMember, (team_id, user_id))
    return m.role if m else None


def get_team(session: Session, slug: str) -> Team:
    team = session.exec(select(Team).where(Team.slug == slug)).first()
    if team is None:
        raise NotFound("No such team.")
    return team


def visible_teams(session: Session, actor) -> list[Team]:  # noqa: ANN001
    """Members see their teams. An admin additionally *lists* every team (to administer them), but only as a name:
    membership and content stay with the members."""
    if actor.is_admin:
        return list(session.exec(select(Team).order_by(col(Team.name))).all())
    ids = [
        m.team_id for m in session.exec(select(TeamMember).where(TeamMember.user_id == actor.user_id)).all()
    ]
    return (
        list(session.exec(select(Team).where(Team.id.in_(ids)).order_by(col(Team.name))).all()) if ids else []
    )  # type: ignore[attr-defined]


def create_team(session: Session, actor, name: str, slug: str, owner: User) -> Team:  # noqa: ANN001
    _require_admin(actor)
    if not teams_enabled(session):
        raise ValidationFailed(
            "Teams are off in solo mode. Change the deployment mode in Settings → Instance first."
        )
    name, slug = name.strip(), slug.strip().lower()
    if not name or len(name) > 80:
        raise ValidationFailed("Give the team a name (up to 80 characters).")
    if not SLUG_RE.match(slug):
        raise ValidationFailed("The team's short name may use a-z, 0-9 and hyphens (up to 64 characters).")
    if session.exec(select(Team).where(Team.slug == slug)).first():
        raise ValidationFailed(f"A team called “{slug}” already exists.")
    if not owner.is_active:
        raise ValidationFailed("The first owner must be an active user.")
    team = Team(name=name, slug=slug)
    session.add(team)
    session.flush()
    session.add(
        TeamMember(team_id=team.id, user_id=owner.id, role="owner")
    )  # every team starts with an owner
    session.flush()
    return team


def _purge_records(session: Session, ids: list[str]) -> int:
    """Permanently remove records and everything that hangs off them. Only ever called for *archived* records,
    after an explicit, confirmed request — there is no other hard-delete path in the hub."""
    if not ids:
        return 0
    for model, col_ in (
        (Reinforcement, Reinforcement.record_id),
        (MemoryRevision, MemoryRevision.memory_record_id),
    ):
        session.exec(delete(model).where(col_.in_(ids)))  # type: ignore[call-overload,attr-defined]
    session.exec(delete(MemoryLink).where(col(MemoryLink.from_id).in_(ids) | col(MemoryLink.to_id).in_(ids)))  # type: ignore[call-overload]
    for rid in ids:
        session.execute(text("DELETE FROM memory_fts WHERE record_id = :id"), {"id": rid})
    gone = set(ids)
    for prop in session.exec(select(Proposal)).all():
        if gone & set(prop.target_ids):
            session.delete(prop)
    session.exec(delete(MemoryRecord).where(MemoryRecord.id.in_(ids)))  # type: ignore[call-overload,attr-defined]
    return len(ids)


def _check_deletable(records: list[MemoryRecord], what: str, purge_archived: bool) -> None:
    if not records:
        return
    live = [r for r in records if r.status != "archived"]
    if live:
        raise ValidationFailed(
            f"{what} still has {len(live)} record(s) that aren't archived. Export it (acm export) and archive them first; deleting never silently destroys live memory."
        )
    if not purge_archived:
        raise ValidationFailed(
            f"{what} has {len(records)} archived record(s). Deleting it permanently deletes them and their history — export first, then confirm the purge."
        )


def delete_team(session: Session, actor, team: Team, *, purge_archived: bool = False) -> int:  # noqa: ANN001
    """Returns how many archived records were permanently deleted (0 unless purge_archived was confirmed)."""
    _require_admin(actor)
    if session.exec(select(Project).where(Project.team_id == team.id)).first():
        raise ValidationFailed("This team still owns projects. Move or delete them first.")
    recs = list(session.exec(select(MemoryRecord).where(MemoryRecord.team_id == team.id)).all())
    _check_deletable(recs, "This team", purge_archived)
    n = _purge_records(session, [r.id for r in recs])
    for m in session.exec(select(TeamMember).where(TeamMember.team_id == team.id)).all():
        session.delete(m)
    session.flush()  # members first: no ORM relationships here, so the unit of work won't order these for us
    session.delete(team)
    session.flush()
    return n


def _require_team_owner(session: Session, actor, team: Team) -> None:  # noqa: ANN001
    _require_human(actor)
    if team_role(session, actor.user_id, team.id) != "owner":
        raise AccessError(
            "Only an owner of this team can do that. (Instance admins manage accounts and create teams, not team membership.)"
        )


def _owners(session: Session, team: Team) -> list[TeamMember]:
    return [
        m
        for m in session.exec(select(TeamMember).where(TeamMember.team_id == team.id)).all()
        if m.role == "owner"
    ]


def add_member(session: Session, actor, team: Team, user: User, role: str = "member") -> TeamMember:  # noqa: ANN001
    _require_team_owner(session, actor, team)
    if role not in ROLES:
        raise ValidationFailed("Role must be owner or member.")
    if not user.is_active:
        raise ValidationFailed("That account is deactivated.")
    if session.get(TeamMember, (team.id, user.id)):
        raise ValidationFailed(f"{user.email} is already on this team.")
    m = TeamMember(team_id=team.id, user_id=user.id, role=role)
    session.add(m)
    session.flush()
    return m


def set_member_role(session: Session, actor, team: Team, user: User, role: str) -> None:  # noqa: ANN001
    _require_team_owner(session, actor, team)
    if role not in ROLES:
        raise ValidationFailed("Role must be owner or member.")
    m = session.get(TeamMember, (team.id, user.id))
    if m is None:
        raise NotFound("That person isn't on this team.")
    if m.role == "owner" and role != "owner" and len(_owners(session, team)) <= 1:
        raise ValidationFailed("A team needs at least one owner; promote someone else first.")
    m.role = role
    session.add(m)
    session.flush()


def remove_member(session: Session, actor, team: Team, user: User) -> None:  # noqa: ANN001
    _require_team_owner(session, actor, team)
    m = session.get(TeamMember, (team.id, user.id))
    if m is None:
        raise NotFound("That person isn't on this team.")
    if m.role == "owner" and len(_owners(session, team)) <= 1:
        raise ValidationFailed(
            "A team needs at least one owner; promote someone else before removing this one."
        )
    session.delete(m)
    session.flush()


def members(session: Session, team: Team) -> list[tuple[User, str]]:
    rows = session.exec(select(TeamMember).where(TeamMember.team_id == team.id)).all()
    out = [(session.get(User, m.user_id), m.role) for m in rows]
    return sorted([(u, r) for u, r in out if u], key=lambda t: (t[1] != "owner", t[0].email))


# --- projects -------------------------------------------------------------------------------------------------


def can_manage_project(session: Session, actor, project: Project) -> bool:  # noqa: ANN001
    """A personal project: its owner. A team project: that team's owners. Admin gets nothing extra."""
    if not actor.is_human:
        return False
    if project.team_id:
        return team_role(session, actor.user_id, project.team_id) == "owner"
    return project.owner_user_id == actor.user_id


def _check_visibility(
    session: Session, visibility: str, team: Team | None, *, current: str | None = None
) -> None:
    if visibility not in ("private", "team", "public"):
        raise ValidationFailed("Visibility must be private, team or public.")
    if visibility != current and visibility not in allowed_visibilities(session):
        raise ValidationFailed(
            f"“{visibility}” isn't available in {deployment_mode(session).replace('_', ' ')} mode (available: {', '.join(allowed_visibilities(session))})."
        )
    if visibility == "team" and team is None:
        raise ValidationFailed("A team-visible project needs a team.")


def create_project(
    session: Session, actor, slug: str, *, visibility: str = "private", team: Team | None = None
) -> Project:  # noqa: ANN001
    _require_human(actor)
    slug = slug.strip().lower()
    if not SLUG_RE.match(slug):
        raise ValidationFailed("A project's short name may use a-z, 0-9 and hyphens (up to 64 characters).")
    if session.exec(select(Project).where(Project.slug == slug)).first():
        raise ValidationFailed(f"A project called “{slug}” already exists.")
    if visibility == "private":
        team = None  # private means just you; a team can't be attached
    if team is not None:
        _require_team_owner(session, actor, team)  # creating a team's project is an owner's job
    _check_visibility(session, visibility, team)
    proj = Project(
        slug=slug, owner_user_id=actor.user_id, team_id=team.id if team else None, visibility=visibility
    )
    session.add(proj)
    session.flush()
    return proj


def update_project(
    session: Session, actor, project: Project, *, visibility: str, team: Team | None
) -> Project:  # noqa: ANN001
    if not can_manage_project(session, actor, project):
        raise AccessError("Only the project's owner (or its team's owners) can change its visibility.")
    if visibility == "private":
        team = None
    if team is not None and (project.team_id != team.id):
        _require_team_owner(
            session, actor, team
        )  # moving a project into a team needs ownership of that team too
    _check_visibility(session, visibility, team, current=project.visibility)
    project.visibility, project.team_id = visibility, team.id if team else None
    session.add(project)
    session.flush()
    return project


def delete_project(session: Session, actor, project: Project, *, purge_archived: bool = False) -> int:  # noqa: ANN001
    """Returns how many archived records were permanently deleted (0 unless purge_archived was confirmed)."""
    if not can_manage_project(session, actor, project):
        raise AccessError("Only the project's owner (or its team's owners) can delete it.")
    recs = list(session.exec(select(MemoryRecord).where(MemoryRecord.project_id == project.id)).all())
    _check_deletable(recs, "This project", purge_archived)
    n = _purge_records(session, [r.id for r in recs])
    for item in session.exec(select(InboxItem).where(InboxItem.project_id == project.id)).all():
        item.project_id = None  # a captured snippet outlives the project it was filed under
        session.add(item)
    session.delete(project)
    session.flush()
    return n


def visible_projects(session: Session, actor) -> list[Project]:  # noqa: ANN001
    from .access import readable_project_ids

    ids = readable_project_ids(session, actor)
    return (
        list(session.exec(select(Project).where(Project.id.in_(ids)).order_by(col(Project.slug))).all())
        if ids
        else []
    )  # type: ignore[attr-defined]
