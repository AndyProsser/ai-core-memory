"""Settings → Projects, Teams, Users. Every rule lives in `acm_hub.orgs`; these routes only translate it to pages."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlmodel import col, func, select

from .. import orgs
from ..access import AccessError, NotFound
from ..models import MemoryRecord, Project, Team, TeamMember, User
from ..records import ValidationFailed
from .deps import Ctx, notice_url, render, require_user, user_csrf

router = APIRouter()


def _back(path: str, msg: str) -> RedirectResponse:
    return RedirectResponse(notice_url(path, msg), status_code=303)


def _owned_teams(ctx: Ctx) -> list[Team]:
    ids = [
        m.team_id
        for m in ctx.db.exec(
            select(TeamMember).where(TeamMember.user_id == ctx.user.id, TeamMember.role == "owner")
        ).all()
    ]
    return (
        list(ctx.db.exec(select(Team).where(Team.id.in_(ids)).order_by(col(Team.name))).all()) if ids else []
    )  # type: ignore[attr-defined]


def _require_teams_on(ctx: Ctx) -> None:
    if not orgs.teams_enabled(ctx.db):
        raise NotFound(
            "Teams are off in solo mode. An admin can turn them on in Settings → Account → Instance."
        )


# --- projects -------------------------------------------------------------------------------------------------


@router.get("/settings/projects")
def projects_page(request: Request, ctx: Ctx = Depends(require_user)):  # noqa: ANN201
    projects = orgs.visible_projects(ctx.db, ctx.principal)
    teams = {t.id: t for t in ctx.db.exec(select(Team)).all()}
    counts = dict(
        ctx.db.exec(
            select(MemoryRecord.project_id, func.count())
            .where(col(MemoryRecord.project_id).is_not(None))
            .group_by(MemoryRecord.project_id)
        ).all()
    )
    rows = [
        {
            "p": p,
            "team": teams.get(p.team_id) if p.team_id else None,
            "manage": orgs.can_manage_project(ctx.db, ctx.principal, p),
            "records": counts.get(p.id, 0),
            "mine": p.owner_user_id == ctx.user.id,
        }
        for p in projects
    ]
    return render(
        request,
        "projects.html",
        ctx,
        rows=rows,
        visibilities=orgs.allowed_visibilities(ctx.db),
        owned_teams=_owned_teams(ctx) if orgs.teams_enabled(ctx.db) else [],
    )


@router.post("/settings/projects")
def project_create(
    slug: str = Form(""),
    visibility: str = Form("private"),
    team: str = Form(""),
    ctx: Ctx = Depends(user_csrf),
) -> RedirectResponse:
    try:
        t = orgs.get_team(ctx.db, team) if team else None
        p = orgs.create_project(ctx.db, ctx.principal, slug, visibility=visibility, team=t)
        ctx.db.commit()
    except (ValidationFailed, AccessError, NotFound) as e:
        ctx.db.rollback()
        return _back("/settings/projects", str(e))
    return _back("/settings/projects", f"Project “{p.slug}” created.")


@router.post("/settings/projects/{slug}")
def project_update(
    slug: str, visibility: str = Form("private"), team: str = Form(""), ctx: Ctx = Depends(user_csrf)
) -> RedirectResponse:
    proj = ctx.db.exec(select(Project).where(Project.slug == slug)).first()
    if proj is None or proj.id not in {p.id for p in orgs.visible_projects(ctx.db, ctx.principal)}:
        raise NotFound("No such project.")
    try:
        orgs.update_project(
            ctx.db,
            ctx.principal,
            proj,
            visibility=visibility,
            team=orgs.get_team(ctx.db, team) if team else None,
        )
        ctx.db.commit()
    except (ValidationFailed, AccessError, NotFound) as e:
        ctx.db.rollback()
        return _back("/settings/projects", str(e))
    return _back("/settings/projects", f"“{slug}” updated.")


@router.post("/settings/projects/{slug}/delete")
def project_delete(
    slug: str, confirm: str = Form(""), purge: str = Form(""), ctx: Ctx = Depends(user_csrf)
) -> RedirectResponse:
    proj = ctx.db.exec(select(Project).where(Project.slug == slug)).first()
    if proj is None or proj.id not in {p.id for p in orgs.visible_projects(ctx.db, ctx.principal)}:
        raise NotFound("No such project.")
    if confirm.strip() != slug:
        return _back("/settings/projects", f"To delete “{slug}”, type its name to confirm.")
    try:
        n = orgs.delete_project(ctx.db, ctx.principal, proj, purge_archived=bool(purge))
        ctx.db.commit()
    except (ValidationFailed, AccessError) as e:
        ctx.db.rollback()
        return _back("/settings/projects", str(e))
    return _back("/settings/projects", f"Deleted “{slug}”" + (f" and {n} archived record(s)." if n else "."))


# --- teams ----------------------------------------------------------------------------------------------------


@router.get("/settings/teams")
def teams_page(request: Request, ctx: Ctx = Depends(require_user)):  # noqa: ANN201
    _require_teams_on(ctx)
    teams = orgs.visible_teams(ctx.db, ctx.principal)
    mine = {
        m.team_id: m.role
        for m in ctx.db.exec(select(TeamMember).where(TeamMember.user_id == ctx.user.id)).all()
    }
    sizes = dict(ctx.db.exec(select(TeamMember.team_id, func.count()).group_by(TeamMember.team_id)).all())
    users = (
        list(ctx.db.exec(select(User).where(col(User.is_active).is_(True)).order_by(col(User.email))).all())
        if ctx.user.is_admin
        else []
    )
    return render(request, "teams.html", ctx, teams=teams, mine=mine, sizes=sizes, users=users)


@router.post("/settings/teams")
def team_create(
    name: str = Form(""), slug: str = Form(""), owner: str = Form(""), ctx: Ctx = Depends(user_csrf)
) -> RedirectResponse:
    try:
        o = ctx.db.exec(select(User).where(User.email == owner.strip().lower())).first()
        if o is None:
            raise ValidationFailed("Pick the team's first owner.")
        t = orgs.create_team(ctx.db, ctx.principal, name, slug, o)
        ctx.db.commit()
    except (ValidationFailed, AccessError) as e:
        ctx.db.rollback()
        return _back("/settings/teams", str(e))
    return _back("/settings/teams", f"Team “{t.name}” created; {o.email} is its owner.")


def _team_for(ctx: Ctx, slug: str) -> tuple[Team, str | None]:
    team = orgs.get_team(ctx.db, slug)
    role = orgs.team_role(ctx.db, ctx.user.id, team.id)
    if role is None and not ctx.user.is_admin:
        raise NotFound("No such team.")  # non-members don't learn it exists
    return team, role


@router.get("/settings/teams/{slug}")
def team_detail(request: Request, slug: str, ctx: Ctx = Depends(require_user)):  # noqa: ANN201
    _require_teams_on(ctx)
    team, role = _team_for(ctx, slug)
    projects = (
        list(ctx.db.exec(select(Project).where(Project.team_id == team.id).order_by(col(Project.slug))).all())
        if role
        else []
    )
    candidates = []
    if role == "owner":
        on = {m.user_id for m in ctx.db.exec(select(TeamMember).where(TeamMember.team_id == team.id)).all()}
        candidates = [
            u
            for u in ctx.db.exec(
                select(User).where(col(User.is_active).is_(True)).order_by(col(User.email))
            ).all()
            if u.id not in on
        ]
    return render(
        request,
        "team_detail.html",
        ctx,
        team=team,
        role=role,
        members=orgs.members(ctx.db, team) if role else [],
        projects=projects,
        candidates=candidates,
    )


def _owner_action(ctx: Ctx, slug: str, fn) -> RedirectResponse:  # noqa: ANN001
    team, _ = _team_for(ctx, slug)
    try:
        msg = fn(team)
        ctx.db.commit()
    except (ValidationFailed, AccessError, NotFound) as e:
        ctx.db.rollback()
        return _back(f"/settings/teams/{slug}", str(e))
    return _back(f"/settings/teams/{slug}", msg)


@router.post("/settings/teams/{slug}/members")
def member_add(
    slug: str, email: str = Form(""), role: str = Form("member"), ctx: Ctx = Depends(user_csrf)
) -> RedirectResponse:
    def go(team: Team) -> str:
        u = ctx.db.exec(select(User).where(User.email == email.strip().lower())).first()
        if u is None:
            raise ValidationFailed(
                "No account with that email. An admin creates accounts (Settings → Users); you can then add them here."
            )
        orgs.add_member(ctx.db, ctx.principal, team, u, role)
        return f"Added {u.email} as {role}."

    return _owner_action(ctx, slug, go)


@router.post("/settings/teams/{slug}/members/{user_id}/role")
def member_role(
    slug: str, user_id: str, role: str = Form("member"), ctx: Ctx = Depends(user_csrf)
) -> RedirectResponse:
    def go(team: Team) -> str:
        u = ctx.db.get(User, user_id)
        if u is None:
            raise NotFound("No such person.")
        orgs.set_member_role(ctx.db, ctx.principal, team, u, role)
        return f"{u.email} is now {role}."

    return _owner_action(ctx, slug, go)


@router.post("/settings/teams/{slug}/members/{user_id}/remove")
def member_remove(slug: str, user_id: str, ctx: Ctx = Depends(user_csrf)) -> RedirectResponse:
    def go(team: Team) -> str:
        u = ctx.db.get(User, user_id)
        if u is None:
            raise NotFound("No such person.")
        orgs.remove_member(ctx.db, ctx.principal, team, u)
        return f"Removed {u.email}; their access to the team's memory ended immediately."

    return _owner_action(ctx, slug, go)


@router.post("/settings/teams/{slug}/delete")
def team_delete(
    slug: str, confirm: str = Form(""), purge: str = Form(""), ctx: Ctx = Depends(user_csrf)
) -> RedirectResponse:
    team = orgs.get_team(ctx.db, slug)
    if confirm.strip() != slug:
        return _back(
            f"/settings/teams/{slug}" if ctx.user.is_admin else "/settings/teams",
            f"To delete “{slug}”, type its short name to confirm.",
        )
    try:
        n = orgs.delete_team(ctx.db, ctx.principal, team, purge_archived=bool(purge))
        ctx.db.commit()
    except (ValidationFailed, AccessError) as e:
        ctx.db.rollback()
        return _back(f"/settings/teams/{slug}" if ctx.user.is_admin else "/settings/teams", str(e))
    return _back(
        "/settings/teams", f"Deleted team “{slug}”" + (f" and {n} archived record(s)." if n else ".")
    )


# --- users (admin) --------------------------------------------------------------------------------------------


def _admin(ctx: Ctx) -> None:
    if not ctx.user.is_admin:
        raise AccessError("Only an admin can manage accounts.")


@router.get("/settings/users")
def users_page(request: Request, ctx: Ctx = Depends(require_user)):  # noqa: ANN201
    _admin(ctx)
    users = ctx.db.exec(select(User).order_by(col(User.email))).all()
    return render(request, "users.html", ctx, users=users, temp_password=None, temp_for=None)


@router.post("/settings/users")
def user_create(
    email: str = Form(""),
    password: str = Form(""),
    sso: str = Form(""),
    admin: str = Form(""),
    ctx: Ctx = Depends(user_csrf),
) -> RedirectResponse:
    try:
        u = orgs.create_user(
            ctx.db,
            ctx.principal,
            email,
            password=password or None,
            sso_invite=bool(sso),
            is_admin=bool(admin),
        )
        ctx.db.commit()
    except (ValidationFailed, AccessError) as e:
        ctx.db.rollback()
        return _back("/settings/users", str(e))
    return _back(
        "/settings/users",
        f"Created {u.email}"
        + (
            " — they can sign in with single sign-on."
            if sso
            else ". Share the password with them securely; they can change it in Settings."
        ),
    )


def _user_action(ctx: Ctx, user_id: str, fn) -> RedirectResponse:  # noqa: ANN001
    u = ctx.db.get(User, user_id)
    if u is None:
        raise NotFound("No such account.")
    try:
        msg = fn(u)
        ctx.db.commit()
    except (ValidationFailed, AccessError) as e:
        ctx.db.rollback()
        return _back("/settings/users", str(e))
    return _back("/settings/users", msg)


@router.post("/settings/users/{user_id}/active")
def user_active(user_id: str, active: str = Form("1"), ctx: Ctx = Depends(user_csrf)) -> RedirectResponse:
    def go(u: User) -> str:
        orgs.set_user_active(ctx.db, ctx.principal, u, active == "1")
        return f"{u.email} " + (
            "can sign in again (their old API tokens stay revoked)."
            if active == "1"
            else "is deactivated: signed out, tokens revoked, plugins off."
        )

    return _user_action(ctx, user_id, go)


@router.post("/settings/users/{user_id}/admin")
def user_admin(user_id: str, admin: str = Form("0"), ctx: Ctx = Depends(user_csrf)) -> RedirectResponse:
    def go(u: User) -> str:
        orgs.set_user_admin(ctx.db, ctx.principal, u, admin == "1")
        return f"{u.email} is {'now an admin' if admin == '1' else 'no longer an admin'}."

    return _user_action(ctx, user_id, go)


@router.post("/settings/users/{user_id}/reset-password")
def user_reset(request: Request, user_id: str, ctx: Ctx = Depends(user_csrf)):  # noqa: ANN201
    _admin(ctx)
    u = ctx.db.get(User, user_id)
    if u is None:
        raise NotFound("No such account.")
    try:
        pw = orgs.reset_password(ctx.db, ctx.principal, u)
        ctx.db.commit()
    except (ValidationFailed, AccessError) as e:
        ctx.db.rollback()
        return _back("/settings/users", str(e))
    users = ctx.db.exec(select(User).order_by(col(User.email))).all()
    return render(
        request, "users.html", ctx, users=users, temp_password=pw, temp_for=u.email
    )  # shown once, never in a URL
