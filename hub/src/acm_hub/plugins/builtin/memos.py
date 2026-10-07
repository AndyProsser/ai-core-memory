"""Memos (usememos.com): pulls tagged memos into the inbox through its REST API (v1).

Written against the documented v1 API shape (`GET /api/v1/memos`, bearer access token, `nextPageToken` paging) and
tested against a mock of it; it hasn't been run against every Memos release, and Memos has changed this API between
versions. If your instance differs, the error shown in the Plugins screen says what it returned."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..base import BasePlugin, PluginContext, PluginInfo

_TAG_RE = re.compile(r"(?<![\w/])#([A-Za-z][\w/-]*)")
MAX_PAGES = 10
DIGEST_MARKER = "# Memory digest"  # the heading of memos we write ourselves: never pulled back in as input


class MemosConfig(BaseModel):
    base_url: str = Field(description="Your Memos address, e.g. https://memos.example.com")
    tag: str = Field(
        default="memory",
        description="Only memos with this tag are captured (set empty to capture all — noisy).",
    )
    inbox_scope: Literal["user", "project", "team"] = "user"
    inbox_project: str = Field(default="", description="Project slug, when the inbox scope is project.")
    max_items: int = Field(default=100, ge=1, le=1000)
    export_digest: bool = Field(
        default=False,
        description="Also post the weekly memory digest as a private memo (tagged #memory-hub).",
    )
    allowed_hosts: list[str] = Field(default_factory=list)


def _title(content: str) -> str:
    for line in content.splitlines():
        t = _TAG_RE.sub("", line).strip(" #\t")
        if t:
            return t[:100]
    return "Memo"


class MemosPlugin(BasePlugin):
    info = PluginInfo(
        key="memos",
        name="Memos",
        kind="source",
        description="Captures memos with a chosen tag (default #memory) from a Memos server into your inbox, and can post the weekly digest back as a private memo.",
        config_schema=MemosConfig,
        secret_names=["token"],
        secret_help={
            "token": "Environment variable holding a Memos access token (Settings → My Account → Access Tokens)."
        },
        personal_ok=True,
        personal_help={"token": "A Memos access token (Memos → Settings → My Account → Access Tokens)."},
    )

    def check(self, ctx: PluginContext) -> str:
        c: MemosConfig = ctx.config  # type: ignore[assignment]
        token = ctx.secrets.get("token")
        if not token:
            raise RuntimeError("No access token is saved for this connection.")
        r = ctx.http.get(
            f"{c.base_url.rstrip('/')}/api/v1/memos",
            params={"pageSize": 1},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        if r.status_code in (401, 403):
            raise RuntimeError(f"Memos rejected the token (HTTP {r.status_code}).")
        if r.status_code != 200:
            raise RuntimeError(f"Memos returned HTTP {r.status_code} for /api/v1/memos")
        return "Connected to Memos."

    def validate(self, config: BaseModel) -> None:
        c: MemosConfig = config  # type: ignore[assignment]
        if not c.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        if c.inbox_scope == "project" and not c.inbox_project:
            raise ValueError("Pick an inbox project when the inbox scope is project.")

    def pull(self, ctx: PluginContext) -> None:
        c: MemosConfig = ctx.config  # type: ignore[assignment]
        token = ctx.secrets.get("token")
        if not token:
            raise RuntimeError("The access-token environment variable isn't set.")
        base = c.base_url.rstrip("/")
        want = c.tag.strip().lstrip("#").lower()
        seen, page_token = 0, ""
        for _ in range(MAX_PAGES):
            params = {"pageSize": 50}
            if page_token:
                params["pageToken"] = page_token
            r = ctx.http.get(
                f"{base}/api/v1/memos",
                params=params,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            if r.status_code != 200:
                raise RuntimeError(f"Memos returned HTTP {r.status_code} for /api/v1/memos")
            data = r.json()
            for memo in data.get("memos", []):
                seen += 1
                content = str(memo.get("content") or "")
                if content.lstrip().startswith(DIGEST_MARKER):
                    continue  # our own digest: don't capture the hub's output as if it were a note
                tags = {str(t).lstrip("#").lower() for t in (memo.get("tags") or [])} | {
                    t.lower() for t in _TAG_RE.findall(content)
                }
                if want and want not in tags:
                    continue
                name = str(memo.get("name") or "")
                ref = f"{base}/m/{name.split('/')[-1]}" if name else f"{base}/memo/{seen}"
                ctx.inbox.add(_title(content), content.strip(), external_ref=ref)  # type: ignore[union-attr]
                if seen >= c.max_items:
                    return
            page_token = data.get("nextPageToken") or data.get("next_page_token") or ""
            if not page_token:
                return

    def export_digest(self, ctx: PluginContext, digest: dict[str, Any]) -> None:
        c: MemosConfig = ctx.config  # type: ignore[assignment]
        token = ctx.secrets.get("token")
        if not token:
            raise RuntimeError("The access-token environment variable isn't set.")
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        lines = [
            f"{DIGEST_MARKER} {day}",
            "",
            f"- {digest['new']} new, {digest['changed']} changed, {digest['superseded']} replaced, {digest['went_stale']} went stale",
            f"- {digest['pending_proposals']} proposal(s) waiting for review; {digest['inbox_waiting']} inbox item(s)",
        ]
        if digest.get("new_names"):
            lines += ["", "New: " + ", ".join(digest["new_names"])]
        if digest.get("changed_names"):
            lines += ["Changed: " + ", ".join(digest["changed_names"])]
        lines += ["", f"Review: {ctx.public_url}{digest.get('link', '/review')}", "", "#memory-hub"]
        r = ctx.http.post(
            f"{c.base_url.rstrip('/')}/api/v1/memos",
            json={
                "content": "\n".join(lines),
                "visibility": "PRIVATE",
            },  # a digest is for its owner, never public
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Memos returned HTTP {r.status_code} when posting the digest")
