"""Settings → Plugins. Admin only. The UI configures plugins; it can never upload or run code."""

from __future__ import annotations

import os

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlmodel import col, select

from .. import dispatcher, search_sync
from .. import plugin_admin as pa
from ..access import AccessError, NotFound
from ..models import Event, PluginDelivery, PluginInstance, Project
from ..plugins import registry, remote
from ..plugins.base import EVENT_TYPES
from .deps import Ctx, notice_url, render, require_user, user_csrf

router = APIRouter()
SINK_EVENTS = [e for e in EVENT_TYPES]


def _admin(ctx: Ctx) -> None:
    if not ctx.user.is_admin:
        raise AccessError("Only an admin can manage plugins.")


def _plugin(key: str):  # noqa: ANN202
    p = registry.get(key)
    if p is None:
        raise NotFound("That plugin isn't installed.")
    return p


def _secret_status(plugin, inst: PluginInstance) -> list[dict]:  # noqa: ANN001
    """Which env var each secret points at, and whether it is set. Never the value."""
    out = []
    for s in plugin.info.secret_names:
        env = (inst.secret_refs or {}).get(s, "")
        out.append(
            {
                "name": s,
                "env": env,
                "is_set": bool(env and os.environ.get(env)),
                "help": plugin.info.secret_help.get(s, ""),
            }
        )
    return out


@router.get("/plugins")
def plugins_list(request: Request, ctx: Ctx = Depends(require_user)):  # noqa: ANN201
    _admin(ctx)
    instances = ctx.db.exec(select(PluginInstance).order_by(col(PluginInstance.created_at))).all()
    rows = []
    for i in instances:
        p = registry.get(i.plugin_key)
        rows.append(
            {
                "i": i,
                "plugin": p,
                "secrets": _secret_status(p, i) if p else [],
                "unreachable": None if p else remote.unavailable_reason(i.plugin_key),
            }
        )
    services = [
        {"key": st.spec.key, "url": st.spec.url, "ok": st.plugin is not None, "error": st.error}
        for st in remote.STATE.values()
    ]
    return render(
        request,
        "plugins.html",
        ctx,
        rows=rows,
        available=registry.all_plugins(),
        services=services,
        remote_errors=list(remote.CONFIG_ERRORS),
    )


@router.post("/plugins/remote/refresh")
def refresh_remote(ctx: Ctx = Depends(user_csrf)) -> RedirectResponse:
    _admin(ctx)
    remote.refresh_due(force=True)  # an admin's explicit request: contact every registered service now
    return RedirectResponse(notice_url("/plugins", "Checked the remote plugin services."), status_code=303)


def _form_ctx(ctx: Ctx, plugin, inst: PluginInstance, values: dict | None = None, errors=None):  # noqa: ANN001, ANN202
    return {
        "plugin": plugin,
        "inst": inst,
        "fields": pa.field_specs(plugin.info.config_schema, values if values is not None else inst.config),
        "secrets": _secret_status(plugin, inst),
        "errors": errors or [],
        "all_events": SINK_EVENTS,
        "is_sink": plugin.info.kind in {"sink", "both"},
        "is_source": plugin.info.kind in {"source", "both"},
        "is_search": plugin.info.kind == "search",
        "indexed": search_sync.indexed_count(ctx.db, inst.id)
        if plugin.info.kind == "search" and inst.id
        else 0,
        "projects": sorted(p.slug for p in ctx.db.exec(select(Project)).all()),
    }


@router.get("/plugins/new")
def plugin_new(request: Request, plugin: str, ctx: Ctx = Depends(require_user)):  # noqa: ANN201
    _admin(ctx)
    p = _plugin(plugin)
    return render(
        request,
        "plugin_form.html",
        ctx,
        mode="new",
        deliveries=[],
        **_form_ctx(ctx, p, pa.default_instance(ctx.user, p)),
    )


@router.post("/plugins")
async def plugin_create(request: Request, ctx: Ctx = Depends(user_csrf)):  # noqa: ANN201
    _admin(ctx)
    form = await request.form()
    p = _plugin(str(form.get("plugin") or ""))
    # validate() may call a remote service over the network: never on the event loop, which would stall the whole hub
    f = await anyio.to_thread.run_sync(pa.parse_instance_form, ctx.db, p, form)
    inst = pa.default_instance(ctx.user, p)
    pa.apply_form(inst, f)
    if f.errors:
        return render(
            request,
            "plugin_form.html",
            ctx,
            status=422,
            mode="new",
            deliveries=[],
            **_form_ctx(ctx, p, inst, f.config, f.errors),
        )
    ctx.db.add(inst)
    ctx.db.commit()
    return RedirectResponse(
        notice_url(
            f"/plugins/{inst.id}",
            "Plugin added. " + ("It is enabled." if inst.enabled else "It is off until you enable it."),
        ),
        status_code=303,
    )


def _get(ctx: Ctx, iid: str) -> PluginInstance:
    inst = ctx.db.get(PluginInstance, iid)
    if inst is None:
        raise NotFound("No such plugin instance.")
    return inst


@router.get("/plugins/{iid}")
def plugin_detail(request: Request, iid: str, ctx: Ctx = Depends(require_user)):  # noqa: ANN201
    _admin(ctx)
    inst = _get(ctx, iid)
    p = _plugin(inst.plugin_key)
    rows = ctx.db.exec(
        select(PluginDelivery)
        .where(PluginDelivery.instance_id == iid)
        .order_by(col(PluginDelivery.next_attempt_at).desc())
        .limit(20)
    ).all()
    deliveries = [{"d": d, "event": ctx.db.get(Event, d.event_id)} for d in rows]
    return render(
        request, "plugin_form.html", ctx, mode="edit", deliveries=deliveries, **_form_ctx(ctx, p, inst)
    )


@router.post("/plugins/{iid}")
async def plugin_update(request: Request, iid: str, ctx: Ctx = Depends(user_csrf)):  # noqa: ANN201
    _admin(ctx)
    inst = _get(ctx, iid)
    p = _plugin(inst.plugin_key)
    form = await request.form()
    # validate() may call a remote service over the network: never on the event loop, which would stall the whole hub
    f = await anyio.to_thread.run_sync(pa.parse_instance_form, ctx.db, p, form)
    if f.errors:
        shadow = PluginInstance(
            **{
                **inst.model_dump(),
                "name": f.name,
                "scopes": f.scopes,
                "events": f.events,
                "egress": f.egress,
                "enabled": f.enabled,
                "secret_refs": f.secret_refs,
                "projects": f.projects,
                "user_scope_ack": f.user_scope_ack,
            }
        )
        return render(
            request,
            "plugin_form.html",
            ctx,
            status=422,
            mode="edit",
            deliveries=[],
            **_form_ctx(ctx, p, shadow, f.config, f.errors),
        )
    pa.apply_form(inst, f)
    inst.consecutive_failures = 0 if f.enabled else inst.consecutive_failures
    ctx.db.add(inst)
    ctx.db.commit()
    return RedirectResponse(notice_url(f"/plugins/{iid}", "Saved."), status_code=303)


@router.post("/plugins/{iid}/toggle")
def plugin_toggle(iid: str, ctx: Ctx = Depends(user_csrf)) -> RedirectResponse:
    _admin(ctx)
    inst = _get(ctx, iid)
    inst.enabled = not inst.enabled
    ctx.db.add(inst)
    ctx.db.commit()
    return RedirectResponse(
        notice_url("/plugins", f"{inst.name} is now {'on' if inst.enabled else 'off'}."), status_code=303
    )


@router.post("/plugins/{iid}/test")
def plugin_test(iid: str, ctx: Ctx = Depends(user_csrf)) -> RedirectResponse:
    _admin(ctx)
    inst = _get(ctx, iid)
    if not inst.enabled:
        return RedirectResponse(
            notice_url(f"/plugins/{iid}", "Turn the plugin on first; a disabled instance never sends."),
            status_code=303,
        )
    res = dispatcher.deliver_test_event(ctx.db.get_bind(), iid)
    ctx.db.expire_all()
    msg = "Test sent. Check the destination." if res.ok else f"Test failed: {res.message or 'unknown error'}"
    return RedirectResponse(notice_url(f"/plugins/{iid}", msg), status_code=303)


@router.post("/plugins/{iid}/pull")
def plugin_pull(iid: str, ctx: Ctx = Depends(user_csrf)) -> RedirectResponse:
    _admin(ctx)
    inst = _get(ctx, iid)
    if not inst.enabled:
        return RedirectResponse(notice_url(f"/plugins/{iid}", "Turn the plugin on first."), status_code=303)
    res = dispatcher.run_pull(ctx.db.get_bind(), iid)
    ctx.db.expire_all()
    msg = f"Pull failed: {res.error}" if res.error else f"Pulled. {res.captured} new item(s) in your inbox."
    return RedirectResponse(notice_url(f"/plugins/{iid}", msg), status_code=303)


@router.post("/plugins/{iid}/rebuild")
def plugin_rebuild(iid: str, ctx: Ctx = Depends(user_csrf)) -> RedirectResponse:
    _admin(ctx)
    inst = _get(ctx, iid)
    if not inst.enabled:
        return RedirectResponse(notice_url(f"/plugins/{iid}", "Turn the plugin on first."), status_code=303)
    res = search_sync.run_sync(ctx.db.get_bind(), iid, rebuild=True)
    ctx.db.expire_all()
    msg = (
        f"Rebuild failed: {res.error}"
        if res.error
        else f"Rebuilt. {res.indexed} record(s) sent"
        + (f"; {res.remaining} more will follow." if res.remaining else ".")
    )
    return RedirectResponse(notice_url(f"/plugins/{iid}", msg), status_code=303)


@router.post("/plugins/{iid}/delete")
def plugin_delete(iid: str, ctx: Ctx = Depends(user_csrf)) -> RedirectResponse:
    _admin(ctx)
    inst = _get(ctx, iid)
    search_sync.wipe(
        ctx.db.get_bind(), iid
    )  # a search instance's index is forgotten (best effort at the service)
    ctx.db.expire_all()
    for d in ctx.db.exec(select(PluginDelivery).where(PluginDelivery.instance_id == iid)).all():
        ctx.db.delete(d)
    ctx.db.delete(inst)
    ctx.db.commit()
    return RedirectResponse(
        notice_url("/plugins", "Plugin removed. Items it already captured stay in your inbox."),
        status_code=303,
    )
