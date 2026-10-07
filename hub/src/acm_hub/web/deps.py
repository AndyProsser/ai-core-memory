"""Shared web plumbing: templates, session/CSRF dependencies, error handling."""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup
from sqlmodel import Session, func, select

from .. import __version__, crypto_store
from ..access import AccessError, NotFound, Principal, principal_for_user
from ..auth import cookie_should_be_secure, csrf_ok, has_admin, lookup_web_session
from ..models import InboxItem, InstanceSettings, User, WebSession
from ..proposals import pending_count
from ..records import Conflict, ValidationFailed

WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

_md = MarkdownIt("commonmark", {"html": False, "linkify": False}).disable(
    "image"
)  # no raw HTML, no remote images


def _link_open(self, tokens, idx, options, env):  # noqa: ANN001
    tokens[idx].attrSet("rel", "noopener noreferrer")
    return self.renderToken(tokens, idx, options, env)


_md.add_render_rule("link_open", _link_open)


def render_markdown(text: str) -> Markup:
    return Markup(_md.render(text or ""))  # noqa: S704 — html disabled; link schemes validated by markdown-it


templates.env.globals["app_version"] = __version__
templates.env.filters["md"] = render_markdown
templates.env.filters["date"] = lambda d: d.strftime("%Y-%m-%d") if d else "—"
templates.env.filters["datetime"] = lambda d: d.strftime("%Y-%m-%d %H:%M") if d else "—"


class RedirectTo(Exception):
    def __init__(self, url: str):
        self.url = url


def get_db(request: Request) -> Iterator[Session]:
    with Session(request.app.state.engine) as s:
        yield s


@dataclass
class Ctx:
    db: Session
    user: User
    ws: WebSession
    principal: Principal
    request: Request
    dek: bytes | None = None  # the data key for encrypted private memory, if this session is unlocked


def _client_host(request: Request) -> str | None:
    return request.client.host if request.client else None


def require_user(request: Request, db: Session = Depends(get_db)) -> Ctx:
    settings = request.app.state.settings
    found = lookup_web_session(db, settings, request.cookies.get(settings.session_cookie_name))
    if found is None:
        if not has_admin(db):
            raise RedirectTo("/setup")
        nxt = request.url.path if request.method == "GET" and request.url.path != "/" else ""
        raise RedirectTo("/login" + (f"?next={quote(nxt)}" if nxt else ""))
    user, ws = found
    principal = principal_for_user(db, user, kind="session")
    principal.dek = request.app.state.unlock_cache.get(
        ws.id, user.id
    )  # None unless they unlocked this session
    crypto_store.attach_keys(db, user.id, principal.dek)
    return Ctx(db, user, ws, principal, request, principal.dek)


async def user_csrf(request: Request, ctx: Ctx = Depends(require_user)) -> Ctx:
    supplied = request.headers.get("x-csrf-token")
    if not supplied:
        form = await request.form()
        supplied = str(form.get("csrf_token") or "")
    if not csrf_ok(
        ctx.ws,
        supplied,
        request.headers.get("origin"),
        request.headers.get("host"),
        request.headers.get("sec-fetch-site"),
    ):
        raise AccessError(
            "Your session expired or the form was tampered with. Reload the page and try again."
        )
    return ctx


def set_session_cookie(request: Request, response, raw: str) -> None:  # noqa: ANN001
    settings = request.app.state.settings
    secure = cookie_should_be_secure(request.url.scheme, _client_host(request), settings)
    response.set_cookie(
        settings.session_cookie_name,
        raw,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
        max_age=settings.session_absolute_days * 86400,
    )


def render(request: Request, name: str, ctx: Ctx | None = None, *, status: int = 200, **kw) -> HTMLResponse:  # noqa: ANN003
    base: dict = {
        "request": request,
        "user": None,
        "csrf_token": "",
        "theme": "system",
        "notice": request.query_params.get("notice", "")[:300],
    }
    if ctx:
        inst = ctx.db.get(InstanceSettings, 1)
        base |= {
            "user": ctx.user,
            "csrf_token": ctx.ws.csrf_token,
            "theme": ctx.user.theme_preference,
            "inbox_count": ctx.db.exec(
                select(func.count())
                .select_from(InboxItem)
                .where(InboxItem.owner_user_id == ctx.user.id, InboxItem.status == "new")
            ).one(),
            "proposal_count": pending_count(ctx.db, ctx.principal),
            "deployment_mode": inst.deployment_mode if inst else "solo",
            "locked_notice": bool(ctx.user.id) and ctx.dek is None and _has_private_keys(ctx.db, ctx.user.id),
        }
    base.update(kw)
    return templates.TemplateResponse(request, name, base, status_code=status)


def _has_private_keys(db: Session, user_id: str) -> bool:
    from ..keys import is_enabled

    return is_enabled(db, user_id)


def notice_url(path: str, message: str) -> str:
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}notice={quote(message[:300])}"


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RedirectTo)
    async def _redirect(request: Request, exc: RedirectTo):  # noqa: ANN202
        if request.headers.get("hx-request"):
            r = HTMLResponse("", status_code=200)
            r.headers["HX-Redirect"] = exc.url
            return r
        return RedirectResponse(exc.url, status_code=303)

    def page(request: Request, status: int, title: str, message: str, **extra):  # noqa: ANN003, ANN202
        if request.url.path.startswith("/api/"):  # the JSON API answers in JSON, never an HTML error page
            return JSONResponse({"error": message, **extra}, status_code=status)
        return render(request, "error.html", None, status=status, title=title, message=message)

    @app.exception_handler(AccessError)
    async def _forbidden(request: Request, exc: AccessError):  # noqa: ANN202
        return page(request, 403, "Not allowed", str(exc))

    @app.exception_handler(NotFound)
    async def _missing(request: Request, exc: NotFound):  # noqa: ANN202
        return page(request, 404, "Not found", str(exc))

    @app.exception_handler(ValidationFailed)
    async def _invalid(request: Request, exc: ValidationFailed):  # noqa: ANN202
        return page(request, 422, "That can't be saved", str(exc))

    @app.exception_handler(Conflict)
    async def _conflict(request: Request, exc: Conflict):  # noqa: ANN202
        return page(
            request,
            409,
            "Conflict",
            str(exc),
            needs_confirmation=bool(getattr(exc, "needs_confirmation", False)),
        )

    @app.exception_handler(404)
    async def _404(request: Request, exc):  # noqa: ANN001, ANN202
        return page(request, 404, "Not found", "There's nothing at this address.")

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException):  # noqa: ANN202
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        return page(request, exc.status_code, "Error", str(exc.detail))


STASH_PER_USER = 2
STASH_TOTAL = 12


def stash_put(request: Request, user_id: str, files: dict[str, bytes]) -> str:
    from ..security import new_csrf_token

    stash = request.app.state.import_stash
    now = time.time()
    for k in [k for k, v in stash.items() if v[2] < now]:
        stash.pop(k, None)
    # Each preview can hold tens of MB: keep a few per person and a few overall, oldest out first.
    mine = [k for k, v in stash.items() if v[0] == user_id]
    for k in mine[: max(0, len(mine) - (STASH_PER_USER - 1))]:
        stash.pop(k, None)
    while len(stash) >= STASH_TOTAL:
        stash.pop(next(iter(stash)))
    key = new_csrf_token()
    stash[key] = (user_id, files, now + 900)
    return key


def stash_take(request: Request, user_id: str, key: str) -> dict[str, bytes] | None:
    entry = request.app.state.import_stash.pop(key, None)
    if not entry or entry[0] != user_id or entry[2] < time.time():
        return None
    return entry[1]
