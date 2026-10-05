"""Helpers for writing a remote plugin service (docs/PLUGINS.md § Remote plugins).

Standard library only, so a service can vendor this one file. It implements the wire format — HMAC-SHA256 over a
timestamp and the exact body bytes, in both directions — and a tiny ASGI app that dispatches the protocol's
operations to plain functions. Nothing here is specific to Python on the other end: any service that follows the
table in the docs is a valid plugin.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from typing import Any

PROTOCOL = 1
MAX_SKEW_SECONDS = 300
MIN_SECRET_LENGTH = 32
PREFIX = "/acm/v1/"


def sign(secret: str, timestamp: str | int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify(
    secret: str,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    *,
    now: float | None = None,
    max_skew: int = MAX_SKEW_SECONDS,
) -> bool:
    """True only for a well-formed, fresh timestamp and a matching signature (constant-time comparison)."""
    if not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((time.time() if now is None else now) - ts) > max_skew:
        return False
    return hmac.compare_digest(sign(secret, ts, body), signature)


Handler = Callable[..., dict[str, Any]]


def create_app(
    *,
    secret: str,
    manifest: dict[str, Any],
    deliver: Handler | None = None,
    pull: Handler | None = None,
    digest: Handler | None = None,
    validate: Handler | None = None,
    index: Handler | None = None,
    remove: Handler | None = None,
    reset: Handler | None = None,
    search: Handler | None = None,
):  # noqa: ANN201
    """Build an ASGI app. Handlers are plain synchronous functions (run in a worker thread):

    deliver(instance, event) -> {"ok": bool, "retry": bool, "message": str}
    pull(instance, config, since, limit) -> {"items": [{"title", "body", "external_ref"}]}
    digest(instance, config, digest) -> {"ok": bool}
    validate(instance, config) -> {"ok": True} or {"ok": False, "error": "..."}

    A `search` service (manifest kind "search") instead implements:
    index(instance, records) -> {"ok": True}        upsert records the hub may let it see
    remove(instance, ids) -> {"ok": True}           forget records (edited out of scope, archived, deleted)
    reset(instance) -> {"ok": True}                 forget everything for this instance
    search(instance, query, limit) -> {"results": [{"id": "...", "score": 0.83}]}
    """
    if len(secret) < MIN_SECRET_LENGTH:
        raise ValueError(f"the signing secret must be at least {MIN_SECRET_LENGTH} characters")
    manifest = {"protocol": PROTOCOL, **manifest}
    ops: dict[str, Handler | None] = {
        "deliver": deliver,
        "pull": pull,
        "digest": digest,
        "validate": validate,
        "index": index,
        "remove": remove,
        "reset": reset,
        "search": search,
    }

    async def respond(send, status: int, payload: dict[str, Any], timestamp: str) -> None:  # noqa: ANN001
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"x-acm-response-signature", sign(secret, timestamp, body).encode()),
        ]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def app(scope, receive, send):  # noqa: ANN001
        if scope["type"] != "http":
            return
        hdr = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        ts = hdr.get("x-acm-timestamp", "")
        if not verify(secret, ts, hdr.get("x-acm-signature"), body):
            await respond(send, 401, {"error": "bad or stale signature"}, ts or "0")
            return
        path = scope["path"]
        if not path.startswith(PREFIX):
            await respond(send, 404, {"error": "not found"}, ts)
            return
        op = path[len(PREFIX) :]
        try:
            if op == "manifest" and scope["method"] == "GET":
                await respond(send, 200, manifest, ts)
                return
            handler = ops.get(op)
            if handler is None or scope["method"] != "POST":
                await respond(send, 404, {"error": f"unsupported operation {op!r}"}, ts)
                return
            data = json.loads(body or b"{}")
            if op == "deliver":
                out = await asyncio.to_thread(handler, data.get("instance", {}), data.get("event", {}))
            elif op == "pull":
                out = await asyncio.to_thread(
                    handler,
                    data.get("instance", {}),
                    data.get("config", {}),
                    data.get("since"),
                    data.get("limit", 100),
                )
            elif op == "digest":
                out = await asyncio.to_thread(
                    handler, data.get("instance", {}), data.get("config", {}), data.get("digest", {})
                )
            elif op == "index":
                out = await asyncio.to_thread(handler, data.get("instance", {}), data.get("records", []))
            elif op == "remove":
                out = await asyncio.to_thread(handler, data.get("instance", {}), data.get("ids", []))
            elif op == "reset":
                out = await asyncio.to_thread(handler, data.get("instance", {}))
            elif op == "search":
                out = await asyncio.to_thread(
                    handler, data.get("instance", {}), data.get("query", ""), data.get("limit", 20)
                )
            else:
                out = await asyncio.to_thread(handler, data.get("instance", {}), data.get("config", {}))
            await respond(send, 200, out, ts)
        except Exception as e:  # noqa: BLE001 — a service error is a 500 the hub will treat as retryable
            await respond(send, 500, {"error": f"{type(e).__name__}: {e}"[:300]}, ts)

    return app
