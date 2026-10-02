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


def test_the_whole_learning_loop_over_real_http(live, settings):
    """AI client writes/reinforces/supersedes/proposes over MCP; a person then approves in the web UI."""
    import re

    base, raw = live
    state: dict = {}

    async def ai_side():
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {raw}"})
        async with Client(streamable_http_client(f"{base}/mcp", http_client=http)) as c:

            async def call(tool, **args):
                r = await c.call_tool(tool, args)
                import json

                return r.is_error, (json.loads(r.content[0].text) if not r.is_error else r.content[0].text)

            body = "Always send an idempotency key on webhook retries so a replay can never double charge a customer."
            e, a = await call(
                "memory_write",
                name="retry-keys",
                description="Webhook retries need idempotency keys",
                type="project",
                scope="project",
                project="payments",
                body=body,
                source_ref="sess-1",
            )
            assert not e and a["confidence"] == "observed"
            for ref in ("sess-2", "sess-3"):  # two independent sessions later: still true
                e, r = await call(
                    "memory_reinforce", id_or_name="retry-keys", project="payments", source_ref=ref
                )
            assert r["promoted_to_confirmed"] and r["confidence"] == "confirmed"
            e, b = await call(
                "memory_write",
                name="retry-keys-v2",
                description="Webhook retries need idempotency keys",
                type="project",
                scope="project",
                project="payments",
                body=body + " Also store the key for 7 days.",
                supersedes=[a["id"]],
            )
            assert not e
            e, got = await call("memory_get", id_or_name=a["id"])
            assert got["status"] == "superseded" and got["history"][0]["name"] == "retry-keys-v2"
            e, prop = await call(
                "memory_propose",
                kind="promote_core",
                payload={"record": b["id"]},
                rationale="Standing rule for every payments task.",
            )
            assert not e and prop["status"] == "pending"
            e, wp = await call("memory_consolidate", project="payments")
            assert [p["id"] for p in wp["pending_proposals"]] == [prop["proposal_id"]]
            state["record"], state["proposal"] = b["id"], prop["proposal_id"]
            e, focus = await call("memory_focus", task="fix webhook retries", project="payments")
            assert focus["core"] == []  # proposed, not applied: the AI cannot grant itself standing context

    anyio.run(ai_side)

    # the person: sign in, see the card, approve

    from acm_hub.models import User
    from acm_hub.security import hash_password

    with Session(create_engine_for(settings)) as s:
        u = s.exec(select(User)).one()
        u.password_hash = hash_password("correct horse battery staple")
        s.add(u)
        s.commit()
    web = httpx.Client(base_url=base, follow_redirects=False)
    assert (
        web.post(
            "/login", data={"email": "andy@example.com", "password": "correct horse battery staple"}
        ).status_code
        == 303
    )
    page = web.get("/review").text
    assert "Make retry-keys-v2 core" in page and "suggested by an AI" in page
    token = re.search(r'name="csrf" content="([^"]+)"', page).group(1)
    done = web.post(f"/review/proposals/{state['proposal']}", data={"csrf_token": token, "action": "approve"})
    assert done.status_code == 303 and "Applied" in done.headers["location"]

    async def after():
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {raw}"})
        async with Client(streamable_http_client(f"{base}/mcp", http_client=http)) as c:
            r = await c.call_tool("memory_focus", {"task": "anything", "project": "payments"})
            return r.content[0].text

    assert "retry-keys-v2" in anyio.run(after) and '"core": [' in anyio.run(
        after
    )  # now a person made it standing context


def create_engine_for(settings):
    from acm_hub.db import make_engine

    return make_engine(settings)


from sqlmodel import select  # noqa: E402
