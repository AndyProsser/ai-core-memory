"""Joplin: pulls notes into the inbox through the Data API of the Joplin desktop app (Options → Web Clipper).

Joplin's API authenticates with a token in the query string, so the token is redacted from every log and error. The
service only runs while the desktop app is open and must be reachable from the hub (LAN / VPN). Written against the
documented Data API (`/ping`, `/notes`, `/search`, `has_more` paging) and tested against a mock of that shape."""

from __future__ import annotations

from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, Field

from ..base import BasePlugin, PluginContext, PluginInfo

MAX_PAGES = 20
PAGE = 50
FIELDS = "id,title,body,updated_time"


class JoplinConfig(BaseModel):
    base_url: str = Field(
        default="http://localhost:41184",
        description="Where Joplin's Web Clipper service answers, as the hub can reach it (the port is 41184 by default).",
    )
    tag: str = Field(
        default="memory",
        description="Only notes with this Joplin tag are captured (empty: all notes — noisy).",
    )
    inbox_scope: Literal["user", "project", "team"] = "user"
    inbox_project: str = Field(default="", description="Project slug, when the inbox scope is project.")
    max_items: int = Field(default=100, ge=1, le=1000)
    allowed_hosts: list[str] = Field(default_factory=list)


class JoplinPlugin(BasePlugin):
    info = PluginInfo(
        key="joplin",
        name="Joplin",
        kind="source",
        description="Captures notes with a chosen tag (default 'memory') from the Joplin desktop app into your inbox.",
        config_schema=JoplinConfig,
        secret_names=["token"],
        secret_help={"token": "Environment variable holding the Joplin Web Clipper token."},
        personal_ok=True,
        personal_help={"token": "Joplin → Options → Web Clipper → Advanced options → Authorisation token."},
    )

    def validate(self, config: BaseModel) -> None:
        c: JoplinConfig = config  # type: ignore[assignment]
        if not c.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        if c.inbox_scope == "project" and not c.inbox_project:
            raise ValueError("Pick an inbox project when the inbox scope is project.")

    def check(self, ctx: PluginContext) -> str:
        c: JoplinConfig = ctx.config  # type: ignore[assignment]
        token = ctx.secrets.get("token")
        if not token:
            raise RuntimeError("No token is saved for this connection.")
        base = c.base_url.rstrip("/")
        ping = ctx.http.get(f"{base}/ping")
        if ping.status_code != 200 or "JoplinClipperServer" not in ping.text:
            raise RuntimeError("That address doesn't answer like Joplin's Web Clipper service.")
        r = ctx.http.get(f"{base}/notes", params={"token": token, "limit": 1, "fields": "id"})
        if r.status_code in (401, 403):
            raise RuntimeError("Joplin rejected the token.")
        if r.status_code != 200:
            raise RuntimeError(f"Joplin returned HTTP {r.status_code} for /notes")
        return "Connected to Joplin."

    def pull(self, ctx: PluginContext) -> None:
        c: JoplinConfig = ctx.config  # type: ignore[assignment]
        token = ctx.secrets.get("token")
        if not token:
            raise RuntimeError("No token is saved for this connection.")
        base = c.base_url.rstrip("/")
        tag = c.tag.strip().lstrip("#")
        seen = 0
        for page in range(1, MAX_PAGES + 1):
            params: dict = {"token": token, "limit": PAGE, "page": page, "fields": FIELDS}
            if tag:
                url, params = f"{base}/search", {**params, "type": "note", "query": f'tag:"{tag}"'}
            else:
                url, params = (
                    f"{base}/notes",
                    {**params, "order_by": "updated_time", "order_dir": "DESC"},
                )
            r = ctx.http.get(url, params=params)
            if r.status_code in (401, 403):
                raise RuntimeError("Joplin rejected the token.")
            if r.status_code != 200:
                raise RuntimeError(f"Joplin returned HTTP {r.status_code} for {url.removeprefix(base)}")
            data = r.json()
            for note in data.get("items", []):
                seen += 1
                nid = str(note.get("id") or "")
                title = str(note.get("title") or "").strip()
                if nid and title:
                    ctx.inbox.add(  # type: ignore[union-attr]
                        title[:100],
                        str(note.get("body") or "").strip(),
                        external_ref=f"joplin://x-callback-url/openNote?id={quote(nid)}",
                    )
                if seen >= c.max_items:
                    return
            if not data.get("has_more"):
                return
