"""Memory browsing/editing, the inbox (Review), and Focus preview."""

from __future__ import annotations

import difflib
import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlmodel import select

from ..access import NotFound, readable_project_ids, require_read, require_write
from ..events import emit_inbox_new
from ..focus import DEFAULT_BUDGET, build_focus
from ..models import InboxItem, InstanceSettings, MemoryRecord, Project, Team
from ..records import (
    CONFIDENCES,
    STATUSES,
    TIERS,
    TYPES,
    Conflict,
    RecordIn,
    ValidationFailed,
    core_usage,
    history,
    linked_records,
    list_records,
    pending_conflicts,
    project_slug,
    reinforce_review,
    resolve_conflict,
    supersession_chain,
    team_slug,
    write_record,
)
from .deps import Ctx, notice_url, render, require_user, user_csrf

router = APIRouter()
_SLUG_JUNK = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    return _SLUG_JUNK.sub("-", text.lower()).strip("-")[:60]


def _project_choices(ctx: Ctx) -> list[str]:
    ids = readable_project_ids(ctx.db, ctx.principal)
    return (
        sorted(p.slug for p in ctx.db.exec(select(Project).where(Project.id.in_(ids))).all()) if ids else []
    )  # type: ignore[attr-defined]


def _team_choices(ctx: Ctx) -> list[Team]:
    return (
        list(ctx.db.exec(select(Team).where(Team.id.in_(ctx.principal.team_ids))).all())
        if ctx.principal.team_ids
        else []
    )  # type: ignore[attr-defined]


def _csv(value: str) -> list[str]:
    return [t.strip().lower() for t in value.split(",") if t.strip()]


def _names_to_ids(ctx: Ctx, names: str, own_id: str | None = None) -> list[str]:
    ids: list[str] = []
    for n in [x.strip() for x in names.split(",") if x.strip()]:
        matches = [
            r for r in list_records(ctx.db, ctx.principal, status=None) if r.name == n and r.id != own_id
        ]
        if not matches:
            raise ValidationFailed(f"Related record {n!r} wasn't found.")
        ids.append(matches[0].id)
    return ids


def _record_view(ctx: Ctx, r: MemoryRecord) -> dict:
    return {"r": r, "project": project_slug(ctx.db, r), "team": team_slug(ctx.db, r)}


# --- list ---------------------------------------------------------------------------------------------


@router.get("/memory")
def memory_list(
    request: Request,
    q: str = "",
    tier: str = "",
    type: str = "",
    scope: str = "",
    confidence: str = "",
    status: str = "active",  # noqa: A002
    topic: str = "",
    project: str = "",
    ctx: Ctx = Depends(require_user),
):  # noqa: ANN201
    rows = list_records(
        ctx.db,
        ctx.principal,
        q=q or None,
        tier=tier or None,
        type=type or None,
        scope=scope or None,
        confidence=confidence or None,
        status=(None if status == "all" else status) or None,
        topic=topic or None,
        project=project or None,
    )
    views = [_record_view(ctx, r) for r in rows]
    inst = ctx.db.get(InstanceSettings, 1) or InstanceSettings()
    used = core_usage(ctx.db, ctx.principal)
    all_topics = sorted({t for r in list_records(ctx.db, ctx.principal, status=None) for t in r.topics})
    page = "_memory_results.html" if request.headers.get("hx-request") else "memory_list.html"
    return render(
        request,
        page,
        ctx,
        core=[v for v in views if v["r"].tier == "core"],
        associated=[v for v in views if v["r"].tier != "core"],
        total=len(views),
        core_used=used,
        core_budget=inst.core_token_budget,
        core_pct=min(100, round(100 * used / max(inst.core_token_budget, 1))),
        f={
            "q": q,
            "tier": tier,
            "type": type,
            "scope": scope,
            "confidence": confidence,
            "status": status,
            "topic": topic,
            "project": project,
        },
        types=TYPES,
        confidences=CONFIDENCES,
        statuses=STATUSES,
        topics=all_topics,
        projects=_project_choices(ctx),
    )


# --- create -------------------------------------------------------------------------------------------


def _form_defaults(ctx: Ctx, **over) -> dict:  # noqa: ANN003
    base = {
        "name": "",
        "description": "",
        "body": "",
        "type": "project",
        "scope": "project",
        "project": "",
        "team": "",
        "confidence": "observed",
        "tier": "associated",
        "topics": "",
        "links": "",
        "inbox_id": "",
        "supersedes": "",
    }
    return base | over


def _new_form(
    request: Request,
    ctx: Ctx,
    values: dict,
    error: str = "",
    status: int = 200,
    old: MemoryRecord | None = None,
):  # noqa: ANN202
    if old is None and values.get("supersedes"):
        old = ctx.db.get(MemoryRecord, values["supersedes"])
    return render(
        request,
        "record_form.html",
        ctx,
        status=status,
        mode="new",
        rec=None,
        old=old,
        v=values,
        error=error,
        types=TYPES,
        confidences=CONFIDENCES,
        tiers=TIERS,
        statuses=STATUSES,
        projects=_project_choices(ctx),
        teams=_team_choices(ctx),
        needs_confirm=False,
    )


@router.get("/memory/new")
def memory_new(
    request: Request,
    inbox_id: str = "",
    scope: str = "",
    project: str = "",
    supersedes: str = "",
    ctx: Ctx = Depends(require_user),
):  # noqa: ANN201
    vals = _form_defaults(ctx, scope=scope or "project", project=project)
    old = None
    if supersedes:
        old = require_write(ctx.db, ctx.principal, ctx.db.get(MemoryRecord, supersedes))
        vals |= {
            "name": f"{old.name[:73]}-v2",
            "description": old.description,
            "body": old.body,
            "type": old.type,
            "scope": old.scope,
            "project": project_slug(ctx.db, old) or "",
            "confidence": "confirmed" if old.confidence != "observed" else "observed",
            "tier": old.tier,
            "topics": ", ".join(old.topics),
            "supersedes": old.id,
        }
    if inbox_id:
        item = ctx.db.get(InboxItem, inbox_id)
        if item is None or item.owner_user_id != ctx.user.id:
            raise NotFound("No such inbox item.")
        vals |= {
            "name": _slugify(item.title),
            "description": item.title[:300],
            "body": item.body,
            "inbox_id": item.id,
            "scope": item.scope,
        }
        if item.project_id and (proj := ctx.db.get(Project, item.project_id)):
            vals["project"] = proj.slug
    return _new_form(request, ctx, vals, old=old)


@router.post("/memory/new")
def memory_create(
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    body: str = Form(""),
    type: str = Form("project"),  # noqa: A002
    scope: str = Form("project"),
    project: str = Form(""),
    team: str = Form(""),
    confidence: str = Form("observed"),
    tier: str = Form("associated"),
    topics: str = Form(""),
    links: str = Form(""),
    inbox_id: str = Form(""),
    supersedes: str = Form(""),
    confirm_established: str = Form(""),
    ctx: Ctx = Depends(user_csrf),
):  # noqa: ANN201
    vals = _form_defaults(
        ctx,
        name=name,
        description=description,
        body=body,
        type=type,
        scope=scope,
        project=project,
        team=team,
        confidence=confidence,
        tier=tier,
        topics=topics,
        links=links,
        inbox_id=inbox_id,
        supersedes=supersedes,
    )
    try:
        res = write_record(
            ctx.db,
            ctx.principal,
            RecordIn(
                name=name.strip(),
                description=description,
                body=body,
                type=type,
                scope=scope,
                project=project.strip() or None,
                team=team or None,
                confidence=confidence,
                tier=tier,
                topics=_csv(topics),
                links=_names_to_ids(ctx, links) or None,
                supersedes=[supersedes] if supersedes else None,
            ),
            change_source="ui",
            note="created in the web UI" if not supersedes else "created to replace an older record",
            confirm_established=bool(confirm_established),
        )
        if inbox_id and (item := ctx.db.get(InboxItem, inbox_id)) and item.owner_user_id == ctx.user.id:
            item.status = "harvested"
            ctx.db.add(item)
        ctx.db.commit()
    except (ValidationFailed, Conflict) as e:
        ctx.db.rollback()
        return _new_form(request, ctx, vals, str(e), 422)
    except Exception:
        ctx.db.rollback()
        raise
    return RedirectResponse(notice_url(f"/memory/{res.record.id}", "Record created."), status_code=303)


# --- detail / edit ------------------------------------------------------------------------------------


def _detail_context(ctx: Ctx, rec: MemoryRecord) -> dict:
    revs = [r for r in history(ctx.db, ctx.principal, rec) if r.applied][:30]
    diffs: dict[str, list[str]] = {}
    for i, rev in enumerate(revs):
        older = revs[i + 1].body if i + 1 < len(revs) else ""
        diffs[rev.id] = [
            ln
            for ln in difflib.unified_diff(older.splitlines(), rev.body.splitlines(), lineterm="", n=1)
            if not ln.startswith(("---", "+++"))
        ][:60]
    return {
        "rec": rec,
        "project": project_slug(ctx.db, rec),
        "team": team_slug(ctx.db, rec),
        "revisions": revs,
        "diffs": diffs,
        "related": linked_records(ctx.db, ctx.principal, rec),
        "chain": supersession_chain(ctx.db, ctx.principal, rec),
        "replaced_by": [
            r
            for r in supersession_chain(ctx.db, ctx.principal, rec)
            if r.created_at > rec.created_at and r.status != "superseded"
        ][-1:]
        if rec.status == "superseded"
        else [],
        "conflicts": pending_conflicts(ctx.db, rec.id),
        "tokens": core_usage(ctx.db, ctx.principal),
    }


@router.get("/memory/{rid}")
def memory_detail(request: Request, rid: str, edit: int = 0, tab: str = "", ctx: Ctx = Depends(require_user)):  # noqa: ANN201
    rec = require_read(ctx.db, ctx.principal, ctx.db.get(MemoryRecord, rid))
    related_names = ", ".join(sorted(r.name for r in linked_records(ctx.db, ctx.principal, rec)))
    d = _detail_context(ctx, rec)
    if edit:
        require_write(ctx.db, ctx.principal, rec)
        vals = _form_defaults(
            ctx,
            name=rec.name,
            description=rec.description,
            body=rec.body,
            type=rec.type,
            scope=rec.scope,
            project=d["project"] or "",
            confidence=rec.confidence,
            tier=rec.tier,
            topics=", ".join(rec.topics),
            links=related_names,
        )
        return render(
            request,
            "record_form.html",
            ctx,
            mode="edit",
            v=vals | {"status": rec.status},
            error="",
            types=TYPES,
            confidences=CONFIDENCES,
            tiers=TIERS,
            statuses=STATUSES,
            projects=[],
            teams=[],
            needs_confirm=rec.confidence == "established",
            **d,
        )
    return render(request, "record.html", ctx, tab=tab or "record", **d)


@router.post("/memory/{rid}/edit")
def memory_edit(
    request: Request,
    rid: str,
    description: str = Form(""),
    body: str = Form(""),
    name: str = Form(""),
    type: str = Form(""),  # noqa: A002
    confidence: str = Form(""),
    tier: str = Form(""),
    status: str = Form(""),
    topics: str = Form(""),
    links: str = Form(""),
    note: str = Form(""),
    confirm_established: str = Form(""),
    ctx: Ctx = Depends(user_csrf),
):  # noqa: ANN201
    rec = require_write(ctx.db, ctx.principal, ctx.db.get(MemoryRecord, rid))
    d = _detail_context(ctx, rec)
    vals = _form_defaults(
        ctx,
        name=name,
        description=description,
        body=body,
        type=type,
        confidence=confidence,
        tier=tier,
        topics=topics,
        links=links,
        scope=rec.scope,
        project=d["project"] or "",
    ) | {"status": status}
    try:
        write_record(
            ctx.db,
            ctx.principal,
            RecordIn(
                id=rec.id,
                name=name.strip(),
                description=description,
                body=body,
                type=type,
                confidence=confidence,
                tier=tier,
                status=status or None,
                topics=_csv(topics),
                links=_names_to_ids(ctx, links, rec.id),
            ),
            change_source="ui",
            note=note.strip() or None,
            confirm_established=bool(confirm_established),
        )
        ctx.db.commit()
    except (ValidationFailed, Conflict) as e:
        ctx.db.rollback()
        rec = ctx.db.get(MemoryRecord, rid)
        d = _detail_context(ctx, rec)  # type: ignore[arg-type]
        return render(
            request,
            "record_form.html",
            ctx,
            status=409 if isinstance(e, Conflict) else 422,
            mode="edit",
            v=vals,
            error=str(e),
            types=TYPES,
            confidences=CONFIDENCES,
            tiers=TIERS,
            statuses=STATUSES,
            projects=[],
            teams=[],
            needs_confirm=rec.confidence == "established",
            **d,
        )  # type: ignore[union-attr]
    return RedirectResponse(notice_url(f"/memory/{rid}", "Saved."), status_code=303)


@router.post("/memory/{rid}/quick")
def memory_quick(
    rid: str,
    tier: str = Form(""),
    status: str = Form(""),
    confirm_established: str = Form(""),
    ctx: Ctx = Depends(user_csrf),
) -> RedirectResponse:
    rec = require_write(ctx.db, ctx.principal, ctx.db.get(MemoryRecord, rid))
    try:
        write_record(
            ctx.db,
            ctx.principal,
            RecordIn(id=rec.id, tier=tier or None, status=status or None),
            change_source="ui",
            note=f"quick action: {tier or status}",
            confirm_established=bool(confirm_established),
        )
        ctx.db.commit()
    except (ValidationFailed, Conflict) as e:
        ctx.db.rollback()
        return RedirectResponse(notice_url(f"/memory/{rid}", str(e)), status_code=303)
    return RedirectResponse(notice_url(f"/memory/{rid}", "Updated."), status_code=303)


@router.post("/memory/{rid}/still-true")
def memory_still_true(rid: str, ctx: Ctx = Depends(user_csrf)) -> RedirectResponse:
    """A person confirms the record still holds: restarts its decay clock; changes nothing else."""
    rec = require_write(ctx.db, ctx.principal, ctx.db.get(MemoryRecord, rid))
    if rec.status == "stale":
        write_record(
            ctx.db,
            ctx.principal,
            RecordIn(id=rec.id, status="active"),
            change_source="ui",
            note="marked as still true",
        )
    reinforce_review(ctx.db, ctx.principal, rec, change_source="ui")
    ctx.db.commit()
    return RedirectResponse(notice_url(f"/memory/{rid}", "Marked as still true."), status_code=303)


@router.post("/memory/{rid}/conflicts/{rev_id}")
def memory_resolve(
    rid: str,
    rev_id: str,
    action: str = Form("dismiss"),
    confirm_established: str = Form(""),
    ctx: Ctx = Depends(user_csrf),
) -> RedirectResponse:
    try:
        resolve_conflict(
            ctx.db,
            ctx.principal,
            rev_id,
            apply=action == "apply",
            confirm_established=bool(confirm_established),
        )
        ctx.db.commit()
    except (ValidationFailed, Conflict) as e:
        ctx.db.rollback()
        return RedirectResponse(notice_url(f"/memory/{rid}", str(e)), status_code=303)
    return RedirectResponse(notice_url(f"/memory/{rid}", "Conflict resolved."), status_code=303)


# --- Review (inbox) -----------------------------------------------------------------------------------


@router.post("/inbox")
def inbox_add(
    request: Request,
    title: str = Form(""),
    body: str = Form(""),
    scope: str = Form("user"),
    project: str = Form(""),
    next: str = Form("/review"),  # noqa: A002
    ctx: Ctx = Depends(user_csrf),
) -> RedirectResponse:
    if not title.strip() or len(title) > 200 or len(body) > 20000 or scope not in {"project", "team", "user"}:
        raise ValidationFailed("Give the capture a title (max 200 characters).")
    pid = None
    if project:
        proj = ctx.db.exec(select(Project).where(Project.slug == project)).first()
        if proj is None or proj.id not in readable_project_ids(ctx.db, ctx.principal):
            raise NotFound("No such project.")
        pid = proj.id
    item = InboxItem(
        owner_user_id=ctx.user.id, source="ui", scope=scope, project_id=pid, title=title.strip(), body=body
    )
    ctx.db.add(item)
    ctx.db.flush()
    emit_inbox_new(ctx.db, item)
    ctx.db.commit()
    dest = next if next.startswith("/") and not next.startswith("//") else "/review"
    return RedirectResponse(notice_url(dest, "Captured to the inbox."), status_code=303)


@router.post("/inbox/{item_id}/dismiss")
def inbox_dismiss(item_id: str, ctx: Ctx = Depends(user_csrf)) -> RedirectResponse:
    item = ctx.db.get(InboxItem, item_id)
    if item is None or item.owner_user_id != ctx.user.id:
        raise NotFound("No such inbox item.")
    item.status = "dismissed"
    ctx.db.add(item)
    ctx.db.commit()
    return RedirectResponse(notice_url("/review", "Dismissed."), status_code=303)


# --- Focus preview ------------------------------------------------------------------------------------


@router.get("/focus")
def focus_preview(
    request: Request,
    task: str = "",
    project: str = "",
    topics: str = "",
    budget: int = DEFAULT_BUDGET,
    cross: int = 0,
    ctx: Ctx = Depends(require_user),
):  # noqa: ANN201
    pack = None
    if task.strip():
        inst = ctx.db.get(InstanceSettings, 1) or InstanceSettings()
        pack = build_focus(
            ctx.db,
            ctx.principal,
            task,
            project=project or None,
            topics=_csv(topics),
            budget=max(500, min(budget, 20000)),
            core_budget=inst.core_token_budget,
            include_other_projects=bool(cross),
            touch=False,
        )  # preview never counts as retrieval
    page = "_focus_results.html" if request.headers.get("hx-request") else "focus.html"
    return render(
        request,
        page,
        ctx,
        pack=pack,
        task=task,
        project=project,
        topics=topics,
        budget=budget,
        cross=cross,
        projects=_project_choices(ctx),
    )
