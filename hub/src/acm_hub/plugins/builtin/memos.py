"""Memos (usememos.com): pulls tagged memos into the inbox through its REST API (v1).

Written against the documented v1 API shape (`GET /api/v1/memos`, bearer access token, `nextPageToken` paging) and
tested against a mock of it; it hasn't been run against every Memos release, and Memos has changed this API between
versions. If your instance differs, the error shown in the Plugins screen says what it returned."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from ..base import BasePlugin, PluginContext, PluginInfo

_TAG_RE = re.compile(r"(?<![\w/])#([A-Za-z][\w/-]*)")
MAX_PAGES = 10


class MemosConfig(BaseModel):
    base_url: str = Field(description="Your Memos address, e.g. https://memos.example.com")
    tag: str = Field(
        default="memory",
        description="Only memos with this tag are captured (set empty to capture all — noisy).",
    )
    inbox_scope: Literal["user", "project", "team"] = "user"
    inbox_project: str = Field(default="", description="Project slug, when the inbox scope is project.")
    max_items: int = Field(default=100, ge=1, le=1000)
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
        description="Captures memos with a chosen tag (default #memory) from a Memos server into your inbox.",
        config_schema=MemosConfig,
        secret_names=["token"],
        secret_help={
            "token": "Environment variable holding a Memos access token (Settings → My Account → Access Tokens)."
        },
    )

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
