"""Data: offline-friendly export, dry-run-first import."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from ..exportimport import MAX_TOTAL_BYTES, export_files, import_files, new_export_name, read_zip, to_zip
from ..records import ValidationFailed
from .deps import Ctx, notice_url, render, require_user, stash_put, stash_take, user_csrf
from .routes_memory import _project_choices

router = APIRouter()


@router.get("/data")
def data_page(request: Request, ctx: Ctx = Depends(require_user)):  # noqa: ANN201
    return render(request, "data.html", ctx, projects=_project_choices(ctx))


@router.get("/data/export")
def data_export(
    scope: str = "", project: str = "", history: int = 0, inbox: int = 0, ctx: Ctx = Depends(require_user)
) -> Response:
    files = export_files(
        ctx.db,
        ctx.principal,
        scope=scope or None,
        project=project or None,
        with_history=bool(history),
        with_inbox=bool(inbox),
    )
    return Response(
        to_zip(files),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{new_export_name()}.zip"'},
    )


@router.post("/data/import")
async def data_import(
    request: Request, file: UploadFile = File(...), csrf_token: str = Form(""), ctx: Ctx = Depends(user_csrf)
):  # noqa: ANN201, B008, ARG001
    raw = await file.read(MAX_TOTAL_BYTES + 1)
    if len(raw) > MAX_TOTAL_BYTES:
        raise ValidationFailed("That file is too large to import.")
    name = (file.filename or "").lower()
    files = (
        read_zip(raw)
        if name.endswith(".zip") or raw[:2] == b"PK"
        else {f"project/_/{file.filename or 'record.md'}": raw}
    )
    if not any(k.endswith(".md") for k in files):
        raise ValidationFailed("No markdown records found in that file.")
    report = import_files(
        ctx.db, ctx.principal, files, apply=False
    )  # dry run: executes in a savepoint, then rolls back
    ctx.db.rollback()
    return render(
        request, "import_report.html", ctx, report=report, stash=stash_put(request, ctx.user.id, files)
    )


@router.post("/data/import/apply")
def data_import_apply(
    request: Request, stash: str = Form(""), ctx: Ctx = Depends(user_csrf)
) -> RedirectResponse:
    files = stash_take(request, ctx.user.id, stash)
    if files is None:
        return RedirectResponse(
            notice_url("/data", "That import preview expired. Upload the file again."), status_code=303
        )
    report = import_files(ctx.db, ctx.principal, files, apply=True, change_source="import")
    ctx.db.commit()
    s = report.summary
    msg = f"Imported: {s['create']} created, {s['update']} updated, {s['unchanged']} unchanged, {s['conflict']} need review, {s['error']} skipped."
    return RedirectResponse(notice_url("/review" if s["conflict"] else "/data", msg), status_code=303)
