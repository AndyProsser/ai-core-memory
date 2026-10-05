"""Human JSON API (`/api/v1`): session-cookie auth only, for scripts and tools a person runs as themselves.

Deliberately NOT a second door for AI clients (docs/SECURITY.md § What AI clients cannot do):
- an `Authorization: Bearer ...` header is refused outright, even a valid API token;
- unsafe methods need the session's CSRF token in `X-CSRF-Token` (and a matching Origin if one is sent);
- every call goes through the same access rules, confidence gating and revisions as the UI and MCP.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session

from ..access import Principal, principal_for_user, require_read
from ..auth import SESSION_COOKIE, csrf_ok, lookup_web_session
from ..focus import build_focus
from ..models import InstanceSettings, MemoryRecord
from ..orgs import visible_projects, visible_teams
from ..records import (
    RecordIn,
    get_record,
    history,
    list_records,
    project_slug,
    team_slug,
    write_record,
)
from .deps import Ctx, get_db

router = APIRouter(prefix="/api/v1")
MAX_REQUEST_BYTES = 1_000_000


def api_user(request: Request, db: Session = Depends(get_db)) -> Ctx:
    if request.headers.get("authorization"):
        # Not "ignore it and fall through to the cookie": a script that sends a token should be told plainly.
        raise HTTPException(
            401,
            "This API takes a signed-in session, not API tokens. Tokens are for the MCP endpoint (/mcp).",
        )
    found = lookup_web_session(db, request.app.state.settings, request.cookies.get(SESSION_COOKIE))
    if found is None:
        raise HTTPException(401, "Not signed in.")
    user, ws = found
    return Ctx(db, user, ws, principal_for_user(db, user, kind="session"), request)


def api_write(request: Request, ctx: Ctx = Depends(api_user)) -> Ctx:
    if int(request.headers.get("content-length") or 0) > MAX_REQUEST_BYTES:
        raise HTTPException(413, "Request body is too large.")
    if not csrf_ok(
        ctx.ws,
        request.headers.get("x-csrf-token"),
        request.headers.get("origin"),
        request.headers.get("host"),
    ):
        raise HTTPException(403, "Missing or wrong X-CSRF-Token (read it from GET /api/v1/me).")
    return ctx


def record_dict(db: Session, r: MemoryRecord, *, body: bool = True) -> dict:
    d = {
        "id": r.id,
        "name": r.name,
        "description": r.description,
        "type": r.type,
        "scope": r.scope,
        "project": project_slug(db, r),
        "team": team_slug(db, r),
        "confidence": r.confidence,
        "tier": r.tier,
        "status": r.status,
        "topics": list(r.topics),
        "created_at": r.created_at.isoformat(),
        "updated_at": r.updated_at.isoformat(),
        "last_reinforced": r.last_reinforced.isoformat() if r.last_reinforced else None,
    }
    if body:
        d["body"] = r.body
    return d


@router.get("/me")
def me(ctx: Ctx = Depends(api_user)) -> dict:
    inst = ctx.db.get(InstanceSettings, 1)
    return {
        "email": ctx.user.email,
        "is_admin": ctx.user.is_admin,
        "csrf_token": ctx.ws.csrf_token,
        "deployment_mode": inst.deployment_mode if inst else "solo",
    }


@router.get("/records")
def records_list(
    scope: str | None = None,
    type: str | None = None,
    tier: str | None = None,
    confidence: str | None = None,
    status: str | None = "active",
    topic: str | None = None,
    project: str | None = None,
    q: str | None = None,
    limit: int = 100,
    ctx: Ctx = Depends(api_user),
) -> dict:
    rows = list_records(
        ctx.db,
        ctx.principal,
        scope=scope,
        type=type,
        tier=tier,
        confidence=confidence,
        status=None if status == "all" else status,
        topic=topic,
        project=project,
        q=q,
        limit=max(1, min(limit, 500)),
    )
    return {"records": [record_dict(ctx.db, r, body=False) for r in rows]}


@router.get("/records/{ref}")
def record_get(ref: str, project: str | None = None, ctx: Ctx = Depends(api_user)) -> dict:
    return record_dict(ctx.db, get_record(ctx.db, ctx.principal, ref, project=project))


@router.get("/records/{ref}/history")
def record_history(ref: str, project: str | None = None, ctx: Ctx = Depends(api_user)) -> dict:
    rec = get_record(ctx.db, ctx.principal, ref, project=project)
    return {
        "revisions": [
            {
                "id": v.id,
                "changed_at": v.changed_at.isoformat(),
                "source": v.change_source,
                "note": v.change_note,
                "applied": v.applied,
                "flagged": v.flagged,
                "snapshot": {
                    f: getattr(v, f)
                    for f in (
                        "name",
                        "description",
                        "body",
                        "type",
                        "scope",
                        "confidence",
                        "tier",
                        "status",
                        "topics",
                    )
                },
            }
            for v in history(ctx.db, ctx.principal, rec)
        ]
    }


def _save(ctx: Ctx, data: RecordIn, note: str | None, confirm: bool) -> dict:
    result = write_record(
        ctx.db, ctx.principal, data, change_source="api", note=note, confirm_established=confirm
    )
    ctx.db.commit()
    return {
        "action": result.action,
        "notices": result.notices,
        "record": record_dict(ctx.db, result.record),
    }


@router.post("/records")
def record_create(data: RecordIn, ctx: Ctx = Depends(api_write)) -> dict:
    """Create — or, if one with this name already exists in the same place, update it (same as `memory_write`)."""
    return _save(ctx, data, None, False)


@router.patch("/records/{rid}")
def record_update(
    rid: str, data: RecordIn, note: str | None = None, confirm_established: bool = False,
    ctx: Ctx = Depends(api_write),
) -> dict:  # fmt: skip
    """Partial update. Changing an `established` record needs `confirm_established=true` — a person's call,
    so only a session (never a token) can make it."""
    rec = require_read(ctx.db, ctx.principal, ctx.db.get(MemoryRecord, rid))
    data = data.model_copy(update={"id": rec.id})
    return _save(ctx, data, note, confirm_established)


@router.get("/focus")
def focus(
    task: str,
    project: str | None = None,
    budget: int = 4000,
    include_other_projects: bool = False,
    ctx: Ctx = Depends(api_user),
) -> dict:
    inst = ctx.db.get(InstanceSettings, 1) or InstanceSettings()
    pack = build_focus(
        ctx.db,
        ctx.principal,
        task,
        project=project,
        budget=max(500, min(budget, 20000)),
        core_budget=inst.core_token_budget,
        include_other_projects=include_other_projects,
        touch=False,  # looking at a preview isn't a session being served
        use_semantic=True,  # the preview shows what an assistant would be handed, semantic hits included
    )
    return pack.to_dict()


@router.get("/projects")
def projects(ctx: Ctx = Depends(api_user)) -> dict:
    p: Principal = ctx.principal
    teams = {t.id: t.slug for t in visible_teams(ctx.db, p)}
    return {
        "projects": [
            {
                "slug": pr.slug,
                "visibility": pr.visibility,
                "team": teams.get(pr.team_id),
                "mine": pr.owner_user_id == p.user_id,
            }
            for pr in visible_projects(ctx.db, p)
        ]
    }


@router.get("/teams")
def teams_list(ctx: Ctx = Depends(api_user)) -> dict:
    return {"teams": [{"slug": t.slug, "name": t.name} for t in visible_teams(ctx.db, ctx.principal)]}
