"""End to end: a real uvicorn server, driven by the official MCP SDK client over streamable HTTP."""

import socket
import threading
import time

import anyio
import httpx
import httpx2
import pytest
import uvicorn
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from sqlmodel import Session

from acm_hub.app import create_app
from acm_hub.auth import mint_token
from acm_hub.models import User


@pytest.fixture()
def live(settings):
    root = create_app(settings)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(root, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    with Session(root.fastapi.state.engine) as s:
        u = User(email="andy@example.com", is_admin=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        raw, _ = mint_token(
            s,
            u,
            label="e2e",
            project_ids=[],
            access_level="read_write",
            expires_days=7,
            include_user_scope=True,
        )
        s.commit()
    yield f"http://127.0.0.1:{port}", raw
    server.should_exit = True
    t.join(timeout=5)


def test_official_client_discovers_and_uses_the_tools(live):
    base, raw = live

    async def go():
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {raw}"})
        async with Client(streamable_http_client(f"{base}/mcp", http_client=http)) as client:
            tools = await client.list_tools()
            names = {t.name for t in tools.tools}
            assert {
                "memory_focus",
                "memory_write",
                "memory_get",
                "memory_search",
                "memory_sync",
                "inbox_add",
            } <= names
            w = await client.call_tool(
                "memory_write",
                {
                    "name": "terse-replies",
                    "description": "Prefers terse replies",
                    "type": "user",
                    "scope": "user",
                    "body": "Keep answers short.",
                },
            )
            assert not w.is_error
            f = await client.call_tool("memory_focus", {"task": "write a short reply to the user"})
            assert not f.is_error
            text = f.content[0].text
            assert "terse-replies" in text and "matched the task text" in text
            bad = await client.call_tool(
                "memory_write", {"name": "x", "description": "d", "type": "bogus", "scope": "user"}
            )
            assert bad.is_error and "type must be one of" in bad.content[0].text

    anyio.run(go)

    # and the same server refuses an unauthenticated client
    r = httpx.post(f"{base}/mcp", json={}, headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
