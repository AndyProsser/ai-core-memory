"""Application factory: one process serving the web UI, the human REST API, and the MCP endpoint."""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit

import anyio
import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import text
from sqlmodel import Session, select
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, dispatcher, keys
from .auth import AuthError, authenticate_bearer, has_admin, issue_setup_code, purge_expired
from .config import Settings, get_settings
from .consolidate import maybe_run_consolidation
from .db import make_engine, migrate
from .exportimport import MAX_TOTAL_BYTES
from .mcp_server import current_principal, mcp
from .mcp_server import state as mcp_state
from .models import InstanceSettings
from .plugins import remote as remote_plugins
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
                    client_host=client[0],
                )
        except AuthError as e:
            body = json.dumps({"error": e.message}).encode()
            hdrs = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
            if e.status == 401:
                challenge = 'Bearer realm="memory-hub"'
                if st.settings.oauth_enabled:  # tell OAuth clients where to start (RFC 9728 §5.1)
                    from .oauth import metadata_url

                    challenge += f', resource_metadata="{metadata_url(st.settings, scope["path"])}"'
                hdrs.append((b"www-authenticate", challenge.encode()))
            await send({"type": "http.response.start", "status": e.status, "headers": hdrs})
            await send({"type": "http.response.body", "body": body})
            return
        tok = current_principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            current_principal.reset(tok)


# Request bodies are capped before any route sees them (a declared Content-Length is refused outright; a chunked body is
# cut off as it is read). Everything here is small text except an import archive.
MAX_BODY_BYTES = 10 * 1024 * 1024
BODY_LIMITS = {"/data/import": MAX_TOTAL_BYTES + 1024 * 1024}

PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=(), bluetooth=()"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _host_of(value: str) -> str:
    """The hostname in a Host header, without port or IPv6 brackets."""
    value = value.strip().lower()
    if value.startswith("["):
        return value[1:].split("]", 1)[0]
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def security_headers(settings: Settings, path: str, scheme: str) -> list[tuple[bytes, bytes]]:
    """Headers every response gets unless the route already set its own (the OAuth consent page widens form-action)."""
    h = {
        "Content-Security-Policy": CSP,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "same-origin",
        "Permissions-Policy": PERMISSIONS_POLICY,
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Cache-Control": "public, max-age=3600" if path.startswith("/static/") else "no-store",
    }
    if settings.hsts_max_age > 0 and (settings.public_https or scheme == "https"):
        h["Strict-Transport-Security"] = f"max-age={settings.hsts_max_age}"
    return [(k.lower().encode(), v.encode()) for k, v in h.items()]


class Root:
    """The outermost ASGI layer. For every request: Host check (opt-in), body-size cap, security headers on whatever
    answers; then /mcp goes through token auth and everything else (and lifespan) to the FastAPI app."""

    def __init__(self, hub: FastAPI, mcp_asgi):  # noqa: ANN001
        self.hub = hub
        self.mcp = McpAuthMiddleware(mcp_asgi, hub)

    @staticmethod
    async def _reply(send, status: int, message: str) -> None:  # noqa: ANN001
        body = json.dumps({"error": message}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _host_allowed(self, settings: Settings, headers: dict[str, str], path: str) -> bool:
        extra = settings.extra_allowed_hosts
        if not extra or path == "/healthz":  # off unless the operator listed hosts; probes call by pod IP
            return True
        public = (urlsplit(settings.public_url).hostname or "").lower()
        return _host_of(headers.get("host", "")) in (extra | _LOOPBACK_HOSTS | {public})

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope["type"] != "http":
            await self.hub(scope, receive, send)
            return
        settings = self.hub.state.settings
        path = scope["path"]
        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope["headers"]}
        outer_send = send

        async def send(message):  # noqa: ANN001, ANN202
            if message["type"] == "http.response.start":
                have = {k.lower() for k, _ in message["headers"]}
                message = {
                    **message,
                    "headers": [
                        *message["headers"],
                        *(
                            (k, v)
                            for k, v in security_headers(settings, path, scope.get("scheme", "http"))
                            if k not in have
                        ),
                    ],
                }
            await outer_send(message)

        if not self._host_allowed(settings, headers, path):
            await self._reply(send, 400, "Unknown host.")
            return
        limit = BODY_LIMITS.get(path, MAX_BODY_BYTES)
        declared = headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            await self._reply(send, 413, "Request body is too large.")
            return
        seen = 0
        inner_receive = receive

        async def receive():  # noqa: ANN202
            nonlocal seen
            message = await inner_receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:  # a chunked body that never declared its size
                    raise StarletteHTTPException(413, "Request body is too large.")
            return message

        if path == "/mcp" or path.startswith("/mcp/"):
            await self.mcp(scope, receive, send)
        else:
            await self.hub(scope, receive, send)


async def _consolidation_loop(
    engine, hours: int, *, first_delay: float = 60.0, check_every: float = 900.0
) -> None:  # noqa: ANN001
    """Background scheduler: checks every 15 minutes whether the nightly mechanical pass is due."""
    await anyio.sleep(first_delay)
    while True:
        try:
            report = await anyio.to_thread.run_sync(maybe_run_consolidation, engine, hours)
            if report is not None:
                log.info("consolidation: %s", report.as_dict())
        except Exception:  # noqa: BLE001
            log.exception("consolidation pass failed; will retry at the next check")
        await anyio.sleep(check_every)


async def _remote_startup() -> None:
    """Fetch remote plugins' manifests in the background so a slow or absent service never delays startup."""
    try:
        await anyio.to_thread.run_sync(lambda: remote_plugins.refresh_due(force=True))
    except Exception:  # noqa: BLE001
        log.exception("remote plugin startup check failed; the scheduler will retry")


async def _plugin_loop(engine, *, first_delay: float = 15.0, every: float = 10.0) -> None:  # noqa: ANN001
    """Delivers queued events, pulls sources when due, sends weekly digests. Each tick is isolated from the last."""
    await anyio.sleep(first_delay)
    while True:
        try:
            await anyio.to_thread.run_sync(remote_plugins.refresh_due)  # retries downed services with backoff
            out = await anyio.to_thread.run_sync(dispatcher.run_scheduled, engine)
            s = out["dispatch"]
            if s.delivered or s.dead or out["digests"] or any(p.captured or p.error for p in out["pulls"]):
                log.info(
                    "plugins: delivered=%s retried=%s dead=%s digests=%s pulls=%s",
                    s.delivered,
                    s.retried,
                    s.dead,
                    out["digests"],
                    [(p.captured, p.error) for p in out["pulls"]],
                )
        except Exception:  # noqa: BLE001
            log.exception("plugin tick failed; will retry")
        await anyio.sleep(every)


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
        async with mcp_asgi.router.lifespan_context(mcp_asgi), anyio.create_task_group() as tg:
            if settings.plugins_enabled:
                tg.start_soon(_remote_startup)
                tg.start_soon(_plugin_loop, engine)
            if settings.consolidate_interval_hours > 0:
                tg.start_soon(_consolidation_loop, engine, settings.consolidate_interval_hours)
            yield
            tg.cancel_scope.cancel()

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
    remote_plugins.configure(settings)  # reads the operator's registration; contacts nothing yet
    app.state.token_limiter = SlidingWindowLimiter(settings.token_rate_limit_per_min)
    app.state.login_throttle = LoginThrottle()
    app.state.unlock_cache = keys.UnlockCache()  # data keys of unlocked web sessions: server memory only
    app.state.key_throttle = SlidingWindowLimiter(8, 600)  # passphrase attempts per person
    app.state.reauth_throttle = SlidingWindowLimiter(
        8, 600
    )  # current-password checks (change password, mint token) per person
    app.state.http_client_factory = http_client_factory or (
        lambda: httpx.AsyncClient(timeout=10.0, follow_redirects=False)
    )
    app.state.import_stash = {}  # id -> (user_id, files, expires_at); dry-run uploads awaiting Apply
    mcp_state.engine = engine

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

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(WEB_DIR / "static" / "favicon.ico", media_type="image/x-icon")

    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    from .web import install  # imported late: web depends on the app state above

    install(app)
    if settings.oauth_enabled:
        from . import oauth

        oauth.install(app, engine, settings)
    root = Root(app, mcp_asgi)
    root.fastapi = app  # type: ignore[attr-defined]
    return root
