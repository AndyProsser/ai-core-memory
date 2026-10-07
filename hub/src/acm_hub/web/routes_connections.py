"""Settings → Connections: a person's own notes apps and webhooks. Every route acts on the signed-in user's own
connections only (connections.get_owned); there is deliberately no admin path to another person's."""

from __future__ import annotations

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlmodel import select

from .. import connections, dispatcher
from ..access import AccessError, NotFound, readable_project_ids
from ..models import Project
from ..plugins.base import EVENT_TYPES
from .deps import Ctx, notice_url, render, require_user, user_csrf

router = APIRouter()
LABELS = {
    "record.created": "A memory is added",
    "record.updated": "A memory changes",
    "record.superseded": "A memory is replaced",
    "proposal.pending": "Something needs my review",
    "conflict.flagged": "A conflict needs my decision",
    "core.budget_exceeded": "Core memory is over budget",
    "inbox.new": "Something lands in my inbox",
    "digest.weekly": "Weekly digest",
    "plugin.failed": "A connection keeps failing",
}


def _plugin(key: str):  # noqa: ANN202
    p = connections.available(key)
    if p is None:
        raise NotFound("That connector isn't available.")
    return p


def _require_on(ctx: Ctx) -> None:
    if not connections.enabled(ctx.db):
        raise AccessError("An admin has turned connections off.")


def _form_ctx(ctx: Ctx, plugin, inst, values=None, errors=None):  # noqa: ANN001, ANN202
    pids = readable_project_ids(ctx.db, ctx.principal)
    return {
        "plugin": plugin,
        "inst": inst,
        "fields": connections.pa.field_specs(
            plugin.info.config_schema, values if values is not None else inst.config
        ),
        "secrets": connections.secret_states(plugin, inst),
        "errors": errors or [],
        "all_events": EVENT_TYPES,
        "labels": LABELS,
        "is_sink": plugin.info.kind in {"sink", "both"},
        "is_source": plugin.info.kind in {"source", "both"},
        "projects": sorted(p.slug for p in ctx.db.exec(select(Project).where(Project.id.in_(pids))).all())
        if pids
        else [],
        "min_interval": connections.MIN_INTERVAL_MINUTES,
    }


@router.get("/settings/connections")
def connections_list(request: Request, ctx: Ctx = Depends(require_user)):  # noqa: ANN201
    rows = []
    for i in connections.list_for(ctx.db, ctx.user):
        p = connections.available(i.plugin_key)
        rows.append({"i": i, "plugin": p, "secrets": connections.secret_states(p, i) if p else []})
    return render(
        request,
        "connections.html",
        ctx,
        rows=rows,
        available=connections.available_plugins(),
        on=connections.enabled(ctx.db),
        limit=connections.MAX_PER_USER,
    )


@router.get("/settings/connections/new")
def connection_new(request: Request, plugin: str, ctx: Ctx = Depends(require_user)):  # noqa: ANN201
    _require_on(ctx)
    p = _plugin(plugin)
    if why := connections.can_create(ctx.db, ctx.user):
        raise AccessError(why)
    return render(
        request,
        "connection_form.html",
        ctx,
        mode="new",
        **_form_ctx(ctx, p, connections.new_instance(ctx.user, p)),
    )


@router.post("/settings/connections")
async def connection_create(request: Request, ctx: Ctx = Depends(user_csrf)):  # noqa: ANN201
    _require_on(ctx)
    form = await request.form()
    p = _plugin(str(form.get("plugin") or ""))
    if why := connections.can_create(ctx.db, ctx.user):
        raise AccessError(why)
    # parse_form resolves the person's URL (DNS): never on the event loop
    f = await anyio.to_thread.run_sync(connections.parse_form, ctx.db, ctx.user, p, form, None)
    inst = connections.new_instance(ctx.user, p)
    if f.errors:
        connections.apply_preview(inst, f)
        return render(
            request,
            "connection_form.html",
            ctx,
            status=422,
            mode="new",
            **_form_ctx(ctx, p, inst, f.config, f.errors),
        )
    connections.apply(ctx.db, inst, f)
    ctx.db.commit()
    return RedirectResponse(
        notice_url(
            f"/settings/connections/{inst.id}", "Connection added. Use “Test connection”, then turn it on."
        ),
        status_code=303,
    )


@router.get("/settings/connections/{iid}")
def connection_detail(request: Request, iid: str, ctx: Ctx = Depends(require_user)):  # noqa: ANN201
    inst = connections.get_owned(ctx.db, ctx.user, iid)
    p = _plugin(inst.plugin_key)
    return render(request, "connection_form.html", ctx, mode="edit", **_form_ctx(ctx, p, inst))


@router.post("/settings/connections/{iid}")
async def connection_update(request: Request, iid: str, ctx: Ctx = Depends(user_csrf)):  # noqa: ANN201
    _require_on(ctx)
    inst = connections.get_owned(ctx.db, ctx.user, iid)
    p = _plugin(inst.plugin_key)
    form = await request.form()
    f = await anyio.to_thread.run_sync(connections.parse_form, ctx.db, ctx.user, p, form, inst)
    if f.errors:
        shadow = type(inst)(**{**inst.model_dump(), "sealed_secrets": dict(inst.sealed_secrets or {})})
        connections.apply_preview(shadow, f)
        return render(
            request,
            "connection_form.html",
            ctx,
            status=422,
            mode="edit",
            **_form_ctx(ctx, p, shadow, f.config, f.errors),
        )
    connections.apply(ctx.db, inst, f)
    inst.consecutive_failures = 0 if f.enabled else inst.consecutive_failures
    ctx.db.commit()
    return RedirectResponse(notice_url(f"/settings/connections/{iid}", "Saved."), status_code=303)


@router.post("/settings/connections/{iid}/toggle")
def connection_toggle(iid: str, ctx: Ctx = Depends(user_csrf)) -> RedirectResponse:
    inst = connections.get_owned(ctx.db, ctx.user, iid)
    if not inst.enabled:
        _require_on(ctx)  # turning off is always allowed; turning on needs the feature on
    inst.enabled = not inst.enabled
    ctx.db.add(inst)
    ctx.db.commit()
    return RedirectResponse(
        notice_url("/settings/connections", f"{inst.name} is now {'on' if inst.enabled else 'off'}."),
        status_code=303,
    )


@router.post("/settings/connections/{iid}/test")
def connection_test(iid: str, ctx: Ctx = Depends(user_csrf)) -> RedirectResponse:
    _require_on(ctx)
    connections.get_owned(ctx.db, ctx.user, iid)
    ok, msg = dispatcher.check_instance(ctx.db.get_bind(), iid)
    ctx.db.expire_all()
    return RedirectResponse(
        notice_url(f"/settings/connections/{iid}", msg if ok else f"Test failed: {msg}"), status_code=303
    )


@router.post("/settings/connections/{iid}/pull")
def connection_pull(iid: str, ctx: Ctx = Depends(user_csrf)) -> RedirectResponse:
    _require_on(ctx)
    inst = connections.get_owned(ctx.db, ctx.user, iid)
    if not inst.enabled:
        return RedirectResponse(
            notice_url(f"/settings/connections/{iid}", "Turn it on first."), status_code=303
        )
    res = dispatcher.run_pull(ctx.db.get_bind(), iid)
    ctx.db.expire_all()
    msg = f"Pull failed: {res.error}" if res.error else f"Pulled. {res.captured} new item(s) in your inbox."
    return RedirectResponse(notice_url(f"/settings/connections/{iid}", msg), status_code=303)


@router.post("/settings/connections/{iid}/delete")
def connection_delete(iid: str, ctx: Ctx = Depends(user_csrf)) -> RedirectResponse:
    inst = connections.get_owned(ctx.db, ctx.user, iid)
    connections.delete(ctx.db, inst)
    ctx.db.commit()
    return RedirectResponse(
        notice_url(
            "/settings/connections",
            "Connection removed and its saved token destroyed. Items it captured stay in your inbox.",
        ),
        status_code=303,
    )
