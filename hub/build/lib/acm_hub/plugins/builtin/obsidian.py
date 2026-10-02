"""Obsidian: a vault is just a folder of markdown, so this reads files directly — no Obsidian API, no network.
Notes you've put in chosen folders (or tagged) land in the inbox; nothing is written back except an optional digest note."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from ..base import BasePlugin, PluginContext, PluginInfo

_TAG_RE = re.compile(r"(?<![\w/])#([A-Za-z][\w/-]*)")
_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


class ObsidianConfig(BaseModel):
    vault_path: str = Field(
        description="Directory of the vault as the hub sees it (mount it into the container)."
    )
    folders: list[str] = Field(
        default_factory=lambda: ["Inbox"],
        description="Folders to scan, relative to the vault (comma-separated). Use . for the whole vault.",
    )
    tag: str = Field(
        default="",
        description="Optional: only notes with this tag (frontmatter `tags:` or inline #tag), e.g. memory.",
    )
    inbox_scope: Literal["user", "project", "team"] = "user"
    inbox_project: str = Field(default="", description="Project slug, when the inbox scope is project.")
    max_files: int = Field(default=200, ge=1, le=2000)
    max_file_kb: int = Field(default=256, ge=1, le=4096)
    export_digest: bool = Field(default=False, description="Also write a weekly digest note into the vault.")
    export_folder: str = Field(
        default="Memory hub", description="Where the digest note goes (relative to the vault)."
    )


def _inside(root: Path, p: Path) -> bool:
    try:
        p.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def _norm_tag(t: str) -> str:
    return t.strip().lstrip("#").lower()


def parse_note(text: str) -> tuple[dict[str, Any], str]:
    m = _FM_RE.match(text.replace("\r\n", "\n"))
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, m.group(2)
    return (fm if isinstance(fm, dict) else {}), m.group(2)


def note_tags(fm: dict[str, Any], body: str) -> set[str]:
    raw = fm.get("tags") or fm.get("tag") or []
    if isinstance(raw, str):
        raw = re.split(r"[,\s]+", raw)
    tags = {_norm_tag(str(t)) for t in raw if str(t).strip()}
    return tags | {_norm_tag(t) for t in _TAG_RE.findall(body)}


def _title(fm: dict[str, Any], body: str, path: Path) -> str:
    if fm.get("title"):
        return str(fm["title"])
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


class ObsidianPlugin(BasePlugin):
    info = PluginInfo(
        key="obsidian",
        name="Obsidian vault",
        kind="source",
        description="Captures notes from folders (or with a tag) in an Obsidian vault into your inbox. Reads files only; the dream cycle decides what becomes memory.",
        config_schema=ObsidianConfig,
    )

    def validate(self, config: BaseModel) -> None:
        c: ObsidianConfig = config  # type: ignore[assignment]
        vault = Path(c.vault_path)
        if not vault.is_dir():
            raise ValueError(
                f"The vault path {c.vault_path!r} isn't a directory the hub can see. In a container, mount the vault and use the in-container path."
            )
        for f in [*c.folders, c.export_folder]:
            if f and (Path(f).is_absolute() or ".." in Path(f).parts):
                raise ValueError(f"Folder {f!r} must be relative to the vault and can't contain '..'.")
        if c.inbox_scope == "project" and not c.inbox_project:
            raise ValueError("Pick an inbox project when the inbox scope is project.")

    def pull(self, ctx: PluginContext) -> None:
        c: ObsidianConfig = ctx.config  # type: ignore[assignment]
        root = Path(c.vault_path).resolve()
        want = _norm_tag(c.tag)
        files: list[Path] = []
        for folder in c.folders or ["."]:
            base = (root / (folder or ".")).resolve()
            if not _inside(root, base) or not base.is_dir():
                ctx.log.warning("folder %r isn't inside the vault; skipped", folder)
                continue
            for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]  # .obsidian, .git, .trash, ...
                for fn in filenames:
                    p = Path(dirpath) / fn
                    if (
                        fn.endswith(".md")
                        and not fn.startswith(".")
                        and not p.is_symlink()
                        and _inside(root, p)
                    ):
                        files.append(p)
        files = sorted(set(files), key=lambda p: p.stat().st_mtime, reverse=True)[: c.max_files]
        for p in files:
            if p.stat().st_size > c.max_file_kb * 1024:
                ctx.log.info("skipping %s: larger than %d KB", p.name, c.max_file_kb)
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            fm, body = parse_note(text)
            if want and want not in note_tags(fm, body):
                continue
            rel = p.relative_to(root).as_posix()
            ctx.inbox.add(_title(fm, body, p), body.strip(), external_ref=rel)  # type: ignore[union-attr]

    def export_digest(self, ctx: PluginContext, digest: dict[str, Any]) -> None:
        c: ObsidianConfig = ctx.config  # type: ignore[assignment]
        root = Path(c.vault_path).resolve()
        folder = (root / (c.export_folder or "Memory hub")).resolve()
        if not _inside(root, folder):
            raise ValueError("export folder escapes the vault")
        folder.mkdir(parents=True, exist_ok=True)
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        lines = [
            "---",
            f"title: Memory digest {day}",
            "tags: [memory-hub]",
            "---",
            "",
            f"# Memory digest {day}",
            "",
            f"- {digest['new']} new, {digest['changed']} changed, {digest['superseded']} replaced, {digest['went_stale']} went stale",
            f"- {digest['pending_proposals']} proposal(s) waiting for review; {digest['inbox_waiting']} inbox item(s)",
            "",
        ]
        if digest.get("new_names"):
            lines += ["## New", *[f"- {n}" for n in digest["new_names"]], ""]
        if digest.get("changed_names"):
            lines += ["## Changed", *[f"- {n}" for n in digest["changed_names"]], ""]
        lines.append(f"Review: {ctx.public_url}{digest.get('link', '/review')}")
        (folder / f"memory-digest-{day}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
