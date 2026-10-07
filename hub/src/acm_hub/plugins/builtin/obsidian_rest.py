"""Obsidian through the community "Local REST API" plugin: pulls notes from a vault the person has open.

Unlike the `obsidian` vault-folder source (an operator mounting a synced folder), this talks to the app itself, so it
only works while Obsidian is open and the plugin's address is reachable from the hub (LAN / VPN). The plugin serves
HTTPS with a self-signed certificate by default; the hub never skips certificate checks, so use the plugin's plain-HTTP
port on a network the operator has allowed, or give it a trusted certificate. Written against the plugin's documented
API (`GET /`, `GET /vault/<dir>/`, `GET /vault/<file>` with the note+json media type) and tested against a mock."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, Field

from ..base import BasePlugin, PluginContext, PluginInfo

NOTE_JSON = "application/vnd.olrapi.note+json"
MAX_DEPTH = 5
MAX_FILES_SCANNED = 500
_INLINE_TAG = re.compile(r"(?<![\w/])#([A-Za-z][\w/-]*)")


class ObsidianRestConfig(BaseModel):
    base_url: str = Field(
        description="The Local REST API address as the hub can reach it, e.g. http://192.168.1.20:27123",
    )
    folder: str = Field(
        default="Inbox", description="Vault folder to read (empty: the whole vault, recursively)."
    )
    tag: str = Field(default="", description="Only notes with this tag (frontmatter or #inline). Empty: all.")
    vault_name: str = Field(
        default="",
        description="Your vault's name, so captured items link back with obsidian://open (optional).",
    )
    inbox_scope: Literal["user", "project", "team"] = "user"
    inbox_project: str = Field(default="", description="Project slug, when the inbox scope is project.")
    max_items: int = Field(default=100, ge=1, le=500)
    allowed_hosts: list[str] = Field(default_factory=list)


def _path(p: str) -> str:
    return "/".join(quote(seg, safe="") for seg in p.split("/"))


class ObsidianRestPlugin(BasePlugin):
    info = PluginInfo(
        key="obsidian-rest",
        name="Obsidian (Local REST API)",
        kind="source",
        description="Captures notes from an open Obsidian vault into your inbox, through the Local REST API community plugin.",
        config_schema=ObsidianRestConfig,
        secret_names=["api_key"],
        secret_help={"api_key": "Environment variable holding the Local REST API key."},
        personal_ok=True,
        personal_help={"api_key": "Obsidian → Settings → Local REST API → API Key."},
    )

    def validate(self, config: BaseModel) -> None:
        c: ObsidianRestConfig = config  # type: ignore[assignment]
        if not c.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        if ".." in c.folder.split("/"):
            raise ValueError("folder can't contain '..'")
        if c.inbox_scope == "project" and not c.inbox_project:
            raise ValueError("Pick an inbox project when the inbox scope is project.")

    def _headers(self, ctx: PluginContext, accept: str = "application/json") -> dict[str, str]:
        key = ctx.secrets.get("api_key")
        if not key:
            raise RuntimeError("No API key is saved for this connection.")
        return {"Authorization": f"Bearer {key}", "Accept": accept}

    def check(self, ctx: PluginContext) -> str:
        c: ObsidianRestConfig = ctx.config  # type: ignore[assignment]
        r = ctx.http.get(c.base_url.rstrip("/") + "/", headers=self._headers(ctx))
        if r.status_code in (401, 403):
            raise RuntimeError("Obsidian rejected the API key.")
        if r.status_code != 200:
            raise RuntimeError(f"Obsidian returned HTTP {r.status_code}")
        if r.json().get("authenticated") is False:
            raise RuntimeError("Obsidian rejected the API key.")
        return "Connected to Obsidian."

    def _walk(self, ctx: PluginContext, base: str, folder: str, depth: int, out: list[str]) -> None:
        if depth > MAX_DEPTH or len(out) >= MAX_FILES_SCANNED:
            return
        url = f"{base}/vault/{_path(folder)}/" if folder else f"{base}/vault/"
        r = ctx.http.get(url, headers=self._headers(ctx))
        if r.status_code == 404 and depth == 0:
            raise RuntimeError(f"There is no folder {folder!r} in the vault.")
        if r.status_code in (401, 403):
            raise RuntimeError("Obsidian rejected the API key.")
        if r.status_code != 200:
            raise RuntimeError(f"Obsidian returned HTTP {r.status_code} listing {folder or '/'}")
        for name in r.json().get("files", []):
            name = str(name)
            if name.startswith("."):
                continue  # hidden: .obsidian, .trash, …
            full = f"{folder}/{name}" if folder else name
            if name.endswith("/"):
                self._walk(ctx, base, full.rstrip("/"), depth + 1, out)
            elif name.lower().endswith(".md"):
                out.append(full)
            if len(out) >= MAX_FILES_SCANNED:
                return

    def pull(self, ctx: PluginContext) -> None:
        c: ObsidianRestConfig = ctx.config  # type: ignore[assignment]
        base = c.base_url.rstrip("/")
        files: list[str] = []
        self._walk(ctx, base, c.folder.strip().strip("/"), 0, files)
        want = c.tag.strip().lstrip("#").lower()
        added = 0
        for path in sorted(files):
            r = ctx.http.get(f"{base}/vault/{_path(path)}", headers=self._headers(ctx, NOTE_JSON))
            if r.status_code != 200:
                continue  # one unreadable note doesn't stop the rest
            note = r.json()
            content = str(note.get("content") or "")
            tags = {str(t).lstrip("#").lower() for t in (note.get("tags") or [])}
            fm = note.get("frontmatter") or {}
            fm_tags = fm.get("tags") or fm.get("tag") or []
            tags |= {str(t).lstrip("#").lower() for t in ([fm_tags] if isinstance(fm_tags, str) else fm_tags)}
            tags |= {t.lower() for t in _INLINE_TAG.findall(content)}
            if want and want not in tags:
                continue
            title = path.rsplit("/", 1)[-1].removesuffix(".md")
            ref = (
                f"obsidian://open?vault={quote(c.vault_name, safe='')}&file={quote(path, safe='')}"
                if c.vault_name
                else f"obsidian-rest:{path}"
            )
            ctx.inbox.add(title[:100], content.strip(), external_ref=ref)  # type: ignore[union-attr]
            added += 1
            if added >= c.max_items:
                return
