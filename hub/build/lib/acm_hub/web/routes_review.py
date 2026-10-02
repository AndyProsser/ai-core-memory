"""Review: proposals from the consolidation pass (and from AI clients) plus the inbox. Humans decide here."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlmodel import col, select

from .. import proposals as pr
from ..access import readable_project_ids
from ..consolidate import run_consolidation
from ..models import InboxItem, InstanceSettings, Project
from ..records import Conflict, ValidationFailed
from .deps import Ctx, notice_url, render, require_user, user_csrf

router = APIRouter()


@router.get("/review")
def review(request: Request, show: str = "pending", ctx: Ctx = Depends(require_user)):  # noqa: ANN201
    status = None if show == "all" else "pending"
    props = pr.list_proposals(ctx.db, ctx.principal, status=status)
    views = [pr.view(ctx.db, x) for x in props]
    items = ctx.db.exec(
        select(InboxItem)
        .where(InboxItem.owner_user_id == ctx.user.id, InboxItem.status == "new")
        .order_by(col(InboxItem.captured_at).desc())
    ).all()
    inst = ctx.db.get(InstanceSettings, 1) or InstanceSettings()
    pids = readable_project_ids(ctx.db, ctx.principal)
    projects = (
        sorted(p.slug for p in ctx.db.exec(select(Project).where(Project.id.in_(pids))).all()) if pids else []
    )  # type: ignore[attr-defined]
    return render(
        request,
        "review.html",
        ctx,
        views=views,
        items=items,
        show=show,
        projects=projects,
        low_risk=[v for v in views if v.proposal.status == "pending" and v.low_risk and not v.stale_reason],
        last_run=inst.last_consolidation_at,
        auto_apply=inst.auto_apply_proposals,
    )


@router.post("/review/proposals/{proposal_id}")
def decide(
    proposal_id: str,
    action: str = Form("reject"),
    note: str = Form(""),
    confirm_established: str = Form(""),
    ctx: Ctx = Depends(user_csrf),
) -> RedirectResponse:
    try:
        pr.decide(
            ctx.db,
            ctx.principal,
            proposal_id,
            approve=action == "approve",
            note=note,
            confirm_established=bool(confirm_established),
        )
        ctx.db.commit()
    except pr.ProposalExpired as e:
        ctx.db.commit()  # keep the closure
        return RedirectResponse(notice_url("/review", str(e)), status_code=303)
    except Conflict as e:
        ctx.db.rollback()
        msg = (
            "This touches an established record. Tick the confirmation box to go ahead."
            if e.needs_confirmation
            else str(e)
        )
        return RedirectResponse(notice_url("/review", msg), status_code=303)
    except ValidationFailed as e:
        ctx.db.rollback()
        return RedirectResponse(notice_url("/review", f"Couldn't apply that: {e}"), status_code=303)
    return RedirectResponse(
        notice_url(
            "/review",
            "Applied." if action == "approve" else "Rejected. It won't be raised again for 30 days.",
        ),
        status_code=303,
    )


@router.post("/review/approve-low-risk")
def approve_low_risk(ctx: Ctx = Depends(user_csrf)) -> RedirectResponse:
    """Batch-approve only what's safe to batch: hub-generated, all-observed, merge/archive/stale. Never anything else."""
    done = failed = 0
    for prop in pr.list_proposals(ctx.db, ctx.principal, status="pending"):
        v = pr.view(ctx.db, prop)
        if not v.low_risk or v.stale_reason:
            continue
        try:
            pr.decide(ctx.db, ctx.principal, prop.id, approve=True, note="batch-approved (low risk)")
            ctx.db.commit()
            done += 1
        except (ValidationFailed, Conflict):
            ctx.db.rollback()
            failed += 1
    return RedirectResponse(
        notice_url(
            "/review",
            f"Approved {done} low-risk proposal(s)." + (f" {failed} couldn't be applied." if failed else ""),
        ),
        status_code=303,
    )


@router.post("/review/consolidate")
def consolidate_now(ctx: Ctx = Depends(user_csrf)) -> RedirectResponse:
    """Run the mechanical pass over what *you* can edit (the scheduled run covers the whole instance)."""
    rep = run_consolidation(ctx.db, scope_to=ctx.principal)
    ctx.db.commit()
    d = rep.as_dict()
    new = sum(d["proposed"].values())
    msg = (
        f"Checked {d['scanned']} record(s): {new} new proposal(s), {d['auto_staled']} marked stale (unused observed records)"
        + (f", {d['auto_applied']} auto-applied" if d["auto_applied"] else "")
        + "."
    )
    return RedirectResponse(notice_url("/review", msg), status_code=303)
