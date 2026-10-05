"""Shared support for the remote- and search-plugin tests: an in-process ASGI transport, hostile fake services and
the `start` fixture that boots a hub with a remote plugin registered."""

import asyncio
import json
import time  # noqa: F401
from datetime import timedelta  # noqa: F401

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from acm_hub.app import create_app
from acm_hub.config import Settings
from acm_hub.models import InboxItem, PluginInstance, User
from acm_hub.plugins import egress, remote
from acm_hub.plugins import remote_sdk as sdk

from .conftest import setup_admin

SECRET = "k" * 40
URL = "http://127.0.0.1:9000"

SOURCE = {
    "key": "feed",
    "name": "Feed",
    "description": "A test feed",
    "kind": "source",
    "config": [
        {"name": "feed_url", "type": "string", "required": True, "help": "where"},
        {"name": "max_items", "type": "integer", "default": 20},
        {"name": "inbox_scope", "type": "select", "options": ["user", "project", "team"], "default": "user"},
    ],
}
SINK = {
    "key": "feed",
    "name": "Pager",
    "kind": "sink",
    "default_events": ["record.created", "proposal.pending"],
    "config": [],
}


def asgi_transport(app) -> httpx.BaseTransport:
    """Route the hub's outbound HTTP into an ASGI app in-process, so the real wire format is exercised."""

    class T(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = request.read()
            scope = {
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": request.method,
                "scheme": request.url.scheme, "path": request.url.path, "raw_path": request.url.raw_path,
                "query_string": request.url.query, "headers": list(request.headers.raw),
                "server": ("test", 80), "client": ("127.0.0.1", 1),
            }  # fmt: skip
            sent: list[dict] = []

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(m):
                sent.append(m)

            asyncio.run(app(scope, receive, send))
            start = next(m for m in sent if m["type"] == "http.response.start")
            content = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
            return httpx.Response(start["status"], headers=start["headers"], content=content)

    return T()


def raw_service(status=200, body=b"{}", sign_with=SECRET, sig_ts_offset=0, signed=True, extra_headers=None):
    """A hand-made, possibly hostile, service: lets a test choose exactly what comes back and how it is signed."""

    def app(scope, receive, send):
        async def run():
            hdr = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            ts = hdr.get("x-acm-timestamp", "0")
            while (await receive()).get("more_body"):
                pass
            headers = [(b"content-type", b"application/json")] + [
                (k.encode(), v.encode()) for k, v in (extra_headers or {}).items()
            ]
            if signed:
                sig = sdk.sign(sign_with, int(ts) + sig_ts_offset, body)
                headers.append((b"x-acm-response-signature", sig.encode()))
            await send({"type": "http.response.start", "status": status, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        return run()

    return app


class Down(httpx.BaseTransport):
    def handle_request(self, request):
        raise httpx.ConnectError("connection refused")


@pytest.fixture(autouse=True)
def _clean():
    yield
    egress.set_transport(None)
    remote.reset()


@pytest.fixture()
def start(settings, monkeypatch):
    """start(service_app_or_transport) -> (client, app) with a remote plugin 'feed' registered and contacted."""
    stack: list[TestClient] = []

    def go(service, *, spec=None, secret=SECRET, refresh=True):
        monkeypatch.setenv(
            "MEMORY_HUB_REMOTE_PLUGINS",
            json.dumps([spec or {"key": "feed", "url": URL, "secret_env": "FEED_SECRET"}]),
        )
        if secret is not None:
            monkeypatch.setenv("FEED_SECRET", secret)
        egress.set_transport(service if isinstance(service, httpx.BaseTransport) else asgi_transport(service))
        root = create_app(Settings())
        client = TestClient(root)
        client.__enter__()
        stack.append(client)
        setup_admin(client, root.fastapi)
        if refresh:
            remote.refresh_due(force=True)
        return client, root.fastapi

    yield go
    for c in stack:
        c.__exit__(None, None, None)


def admin_id(app):
    with Session(app.state.engine) as s:
        return s.exec(select(User).where(User.email == "admin@example.com")).one().id


def add_instance(app, **kw):
    base = dict(
        plugin_key="feed",
        name="my feed",
        owner_user_id=admin_id(app),
        enabled=True,
        config={"feed_url": "https://f.example/x"},
        scopes=[],
        events=[],
    )
    with Session(app.state.engine) as s:
        inst = PluginInstance(**(base | kw))
        s.add(inst)
        s.commit()
        return inst.id


def inbox(app):
    with Session(app.state.engine) as s:
        return s.exec(select(InboxItem)).all()
