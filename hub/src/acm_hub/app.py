"""Application factory: one process serving the web UI, the human REST API, and the MCP endpoint."""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import text
from sqlmodel import Session, select

from . import __version__
from .auth import AuthError, authenticate_bearer, has_admin, issue_setup_code, purge_expired
from .config import Settings, get_settings
from .db import make_engine, migrate
from .mcp_server import current_principal, mcp
from .mcp_server import state as mcp_state
from .models import InstanceSettings
from .security import LoginThrottle, SlidingWindowLimiter

log = logging.getLogger("acm_hub")
WEB_DIR = Path(__file__).parent / "web"

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)


class McpAuthMiddleware:
    """Bearer-token auth in front of the MCP ASGI app. Sets the calling principal for tool handlers."""

    def __init__(self, app, hub: FastAPI):  # noqa: ANN001
        self.app = app
        self.hub = hub

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        client = scope.get("client") or (None, None)
        st = self.hub.state
        try:
            with Session(st.engine) as s:
                principal = authenticate_bearer(
                    s,
                    st.settings,
                    st.token_limiter,
                    headers.get("authorization"),
                    scheme=scope.get("scheme", "http"),
                    headers=headers,
                    client_host=client[0],
                )
        except AuthError as e:
            body = json.dumps({"error": e.message}).encode()
            hdrs = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
            if e.status == 401:
                hdrs.append((b"www-authenticate", b'Bearer realm="memory-hub"'))
            await send({"type": "http.response.start", "status": e.status, "headers": hdrs})
            await send({"type": "http.response.body", "body": body})
            return
        tok = current_principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            current_principal.reset(tok)


class Root:
    """Routes /mcp through token auth; everything else (and lifespan) to the FastAPI app."""

    def __init__(self, hub: FastAPI, mcp_asgi):  # noqa: ANN001
        self.hub = hub
        self.mcp = McpAuthMiddleware(mcp_asgi, hub)

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope["type"] == "http" and (scope["path"] == "/mcp" or scope["path"].startswith("/mcp/")):
            await self.mcp(scope, receive, send)
        else:
            await self.hub(scope, receive, send)


def create_app(settings: Settings | None = None, *, http_client_factory=None) -> Root:  # noqa: ANN001
    settings = settings or get_settings()
    engine = make_engine(settings)
    mcp_asgi = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host="0.0.0.0",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),  # we authenticate every request
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        migrate(engine)
        with Session(engine) as s:
            if s.get(InstanceSettings, 1) is None:
                s.add(InstanceSettings(id=1))
                s.commit()
            purge_expired(s)
            if not has_admin(s):
                inst = s.get(InstanceSettings, 1)
                if inst and not inst.setup_code_hash:
                    code = issue_setup_code(s)
                    log.warning(
                        "FIRST-RUN SETUP: open %s/setup and enter the setup code: %s",
                        settings.public_url,
                        code,
                    )
                else:
                    log.warning(
                        "First-run setup pending: open %s/setup (run `acm setup-code` for a fresh code).",
                        settings.public_url,
                    )
        if settings.secret_key_was_generated:
            log.warning(
                "MEMORY_HUB_SECRET_KEY isn't set; using a random per-process key (set it so OIDC logins survive restarts)."
            )
        async with mcp_asgi.router.lifespan_context(mcp_asgi):
            yield

    app = FastAPI(
        title="ai-core-memory hub",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.token_limiter = SlidingWindowLimiter(settings.token_rate_limit_per_min)
    app.state.login_throttle = LoginThrottle()
    app.state.http_client_factory = http_client_factory or (
        lambda: httpx.AsyncClient(timeout=10.0, follow_redirects=False)
    )
    app.state.import_stash = {}  # id -> (user_id, files, expires_at); dry-run uploads awaiting Apply
    mcp_state.engine = engine

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # noqa: ANN001
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if request.url.path.startswith(("/static/",)):
            response.headers.setdefault("Cache-Control", "public, max-age=3600")
        else:
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> JSONResponse:
        try:
            with Session(engine) as s:
                s.execute(text("SELECT 1"))
                ok = s.exec(select(InstanceSettings)).first() is not None
        except Exception:  # noqa: BLE001
            return JSONResponse({"status": "error"}, status_code=503)
        return JSONResponse(
            {"status": "ok" if ok else "starting", "version": __version__, "time": int(time.time())}
        )

    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    from .web import install  # imported late: web depends on the app state above

    install(app)
    root = Root(app, mcp_asgi)
    root.fastapi = app  # type: ignore[attr-defined]
    return root
