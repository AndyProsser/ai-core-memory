"""Markdown + YAML-frontmatter export and import (docs/ARCHITECTURE.md § Import / export).

Human-operated and offline: no network, no model. Import is idempotent and dry-run capable; a
conflicting incoming version never silently overwrites — it's parked as a flagged revision.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path, PurePosixPath

import yaml
from sqlmodel import Session, col, select

from . import __version__, proposals
from .access import AccessError, NotFound, Principal, can_read, visible_clause
from .ids import is_id
from .models import (
    InboxItem,
    InstanceSettings,
    MemoryLink,
    MemoryRecord,
    MemoryRevision,
    utcnow,
)
from .records import (
    Conflict,
    RecordIn,
    ValidationFailed,
    _find_existing,
    project_slug,
    team_slug,
    write_record,
)

SCHEMA_VERSION = 1
MAX_FILES = 20_000
MAX_TOTAL_BYTES = 64 * 1024 * 1024
_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)
_UNSAFE_NAME = re.compile(r"[^a-z0-9-]")


# --- frontmatter ----------------------------------------------------------------------------------


def _iso(v: datetime | None) -> str | None:
    return v.date().isoformat() if v else None


def render_record(session: Session, rec: MemoryRecord) -> str:
    links = session.exec(
        select(MemoryLink.to_id).where(MemoryLink.from_id == rec.id, MemoryLink.kind == "related")
    ).all()
    meta: dict = {
        "id": rec.id,
        "type": rec.type,
        "scope": rec.scope,
        "confidence": rec.confidence,
        "tier": rec.tier,
        "status": rec.status,
    }
    if rec.scope == "project":
        meta["project_id"] = project_slug(session, rec)
    if rec.scope == "team":
        meta["team_id"] = team_slug(session, rec)
    meta.update(
        topics=list(rec.topics),
        links=sorted(links),
        supersedes=[],
        created=_iso(rec.created_at),
        updated=rec.updated_at.isoformat(timespec="seconds"),
        last_reinforced=_iso(rec.last_reinforced),
        source=rec.source,
    )
    fm = yaml.safe_dump(
        {"name": rec.name, "description": rec.description, "metadata": meta},
        sort_keys=False,
        allow_unicode=True,
        width=1000,
    )
    return f"---\n{fm}---\n\n{rec.body.rstrip()}\n"


@dataclass
class ParsedRecord:
    data: RecordIn
    fixed_id: str | None
    created: datetime | None
    updated: datetime | None
    link_ids: list[str]


def _as_datetime(v) -> datetime | None:  # noqa: ANN001
    if isinstance(v, datetime):
        return v.replace(tzinfo=None)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def parse_record(text: str, *, path_hint: str = "") -> ParsedRecord:
    m = _FM_RE.match(text.replace("\r\n", "\n"))
    if not m:
        raise ValidationFailed("missing YAML frontmatter")
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise ValidationFailed(f"invalid YAML frontmatter: {e}") from e
    if not isinstance(fm, dict) or not fm.get("name") or not fm.get("description"):
        raise ValidationFailed("frontmatter needs `name` and `description`")
    meta = fm.get("metadata") or {}
    if not isinstance(meta, dict) or not meta.get("type") or not meta.get("scope"):
        raise ValidationFailed("frontmatter metadata needs `type` and `scope`")
    parts = PurePosixPath(path_hint).parts
    project = meta.get("project_id") or (parts[1] if len(parts) >= 3 and parts[0] == "project" else None)
    team = meta.get("team_id") or (parts[1] if len(parts) >= 3 and parts[0] == "team" else None)
    rid = meta.get("id") if is_id(meta.get("id")) else None  # template placeholders aren't ids
    links = [str(i) for i in (meta.get("links") or []) if is_id(i)]
    data = RecordIn(
        name=str(fm["name"]),
        description=str(fm["description"]).strip(),
        body=m.group(2).strip("\n"),
        type=str(meta["type"]),
        scope=str(meta["scope"]),
        confidence=meta.get("confidence"),
        tier=meta.get("tier"),
        status=meta.get("status"),
        topics=[str(t) for t in (meta.get("topics") or [])],
        project=str(project) if project else None,
        team=str(team) if team else None,
        source=str(meta["source"]) if meta.get("source") else None,
    )
    return ParsedRecord(
        data, rid, _as_datetime(meta.get("created")), _as_datetime(meta.get("updated")), links
    )


# --- export ---------------------------------------------------------------------------------------


def _index_line(rec: MemoryRecord, filename: str) -> str:
    star = "★ " if rec.tier == "core" else ""
    hook = rec.description if len(rec.description) <= 110 else rec.description[:107] + "..."
    return f"- {star}[{rec.name}]({filename}) — {hook}"


def export_files(
    session: Session,
    p: Principal,
    *,
    scope: str | None = None,
    project: str | None = None,
    with_history: bool = False,
    with_inbox: bool = False,
) -> dict[str, bytes]:
    """Build the export as {relative path: bytes}. Only records `p` can read are included."""
    stmt = (
        select(MemoryRecord)
        .where(visible_clause(session, p))
        .order_by(col(MemoryRecord.scope), col(MemoryRecord.name))
    )
    recs = list(session.exec(stmt).all())
    if scope:
        recs = [r for r in recs if r.scope == scope]
    if project:
        recs = [r for r in recs if project_slug(session, r) == project]
    files: dict[str, bytes] = {}
    groups: dict[str, list[tuple[MemoryRecord, str]]] = {}
    for r in recs:
        base = {
            "user": "user",
            "team": f"team/{team_slug(session, r)}",
            "project": f"project/{project_slug(session, r)}",
        }[r.scope]
        files[f"{base}/{r.name}.md"] = render_record(session, r).encode()
        groups.setdefault(base, []).append((r, f"{r.name}.md"))
    for base, items in groups.items():
        lines = [f"# Memory index — {base}", "", "★ = core (loaded in every session)", ""]
        lines += [_index_line(r, fn) for r, fn in items if r.status == "active"]
        files[f"{base}/MEMORY.md"] = ("\n".join(lines) + "\n").encode()
    if with_history:
        for r in recs:
            revs = session.exec(
                select(MemoryRevision)
                .where(MemoryRevision.memory_record_id == r.id)
                .order_by(col(MemoryRevision.changed_at))
            ).all()
            lines = [
                json.dumps(
                    {
                        "id": v.id,
                        "changed_at": v.changed_at.isoformat(timespec="seconds"),
                        "change_source": v.change_source,
                        "change_note": v.change_note,
                        "changed_by": v.changed_by_label or v.changed_by_user_id,
                        "flagged": v.flagged,
                        "applied": v.applied,
                        "name": v.name,
                        "description": v.description,
                        "body": v.body,
                        "confidence": v.confidence,
                        "tier": v.tier,
                        "status": v.status,
                        "topics": v.topics,
                    },
                    ensure_ascii=False,
                )
                for v in revs
            ]
            files[f"_history/{r.id}.jsonl"] = ("\n".join(lines) + "\n").encode()
    n_inbox = 0
    if with_inbox:
        for it in session.exec(
            select(InboxItem).where(InboxItem.owner_user_id == p.user_id, InboxItem.status == "new")
        ).all():
            fm = yaml.safe_dump(
                {
                    "title": it.title,
                    "source": it.source,
                    "scope": it.scope,
                    "external_ref": it.external_ref,
                    "captured": _iso(it.captured_at),
                    "status": it.status,
                },
                sort_keys=False,
                allow_unicode=True,
            )
            files[f"_inbox/{it.id}.md"] = f"---\n{fm}---\n\n{it.body.rstrip()}\n".encode()
            n_inbox += 1
    inst = session.get(InstanceSettings, 1)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "exported_at": utcnow().isoformat(timespec="seconds") + "Z",
        "hub_version": __version__,
        "instance_id": inst.instance_id if inst else None,
        "exported_by": p.email or p.user_id,
        "counts": {"records": len(recs), "inbox": n_inbox},
        "filters": {"scope": scope, "project": project},
        "files": {path: hashlib.sha256(b).hexdigest() for path, b in sorted(files.items())},
    }
    files["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    return files


def write_dir(files: dict[str, bytes], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    for rel, data in files.items():
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)


def to_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, data in files.items():
            z.writestr(rel, data)
    return buf.getvalue()


# --- reading an export back in --------------------------------------------------------------------


def _safe_rel(name: str) -> str | None:
    pp = PurePosixPath(name.replace("\\", "/"))
    if pp.is_absolute() or ".." in pp.parts or not pp.parts:
        return None
    return str(pp)


def read_zip(data: bytes) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    total = 0
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ValidationFailed("not a valid zip file") from e
    with z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
            raise ValidationFailed("archive is too large")
        for info in infos:
            rel = _safe_rel(info.filename)
            if rel is None:
                raise ValidationFailed(f"unsafe path in archive: {info.filename!r}")
            content = z.read(info)
            total += len(content)
            if total > MAX_TOTAL_BYTES:
                raise ValidationFailed("archive is too large")
            files[rel] = content
    return files


def read_dir(path: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    total = 0
    for f in sorted(path.rglob("*")):
        if f.is_file() and not f.is_symlink():
            data = f.read_bytes()
            total += len(data)
            if len(files) >= MAX_FILES or total > MAX_TOTAL_BYTES:
                raise ValidationFailed("directory is too large")
            files[f.relative_to(path).as_posix()] = data
    return files


# --- import ---------------------------------------------------------------------------------------


@dataclass
class ImportItem:
    path: str
    name: str
    action: str  # create | update | unchanged | conflict | error | skipped
    detail: str = ""


@dataclass
class ImportReport:
    applied: bool
    items: list[ImportItem] = field(default_factory=list)
    inbox_created: int = 0

    def count(self, action: str) -> int:
        return sum(1 for i in self.items if i.action == action)

    @property
    def summary(self) -> dict[str, int]:
        keys = ("create", "update", "unchanged", "conflict", "error", "skipped")
        return {k: self.count(k) for k in keys} | {"inbox": self.inbox_created}


_COMPARE = ("description", "body", "type", "confidence", "tier", "status", "topics")


def _same(existing: MemoryRecord, d: RecordIn) -> bool:
    for f in _COMPARE:
        v = getattr(d, f)
        if v is not None and getattr(existing, f) != v:
            return False
    return True


def import_files(
    session: Session,
    p: Principal,
    files: dict[str, bytes],
    *,
    apply: bool,
    change_source: str = "import",
    via_sync: bool = False,
) -> ImportReport:
    """Dry-run by default at every call site. The dry run executes for real inside a savepoint and rolls
    back, so its report reflects exactly what apply would do — permissions and validation included."""
    if not p.is_human and not via_sync:
        raise AccessError("Import is a human-only operation.")
    report = ImportReport(applied=apply)
    nested = session.begin_nested()
    try:
        parsed: list[tuple[str, ParsedRecord, MemoryRecord]] = []
        for path in sorted(files):
            if not path.endswith(".md") or path.endswith("MEMORY.md") or path.startswith("_history/"):
                continue
            if path.startswith("_inbox/"):
                report.inbox_created += _import_inbox(session, p, path, files[path])
                continue
            name = PurePosixPath(path).stem
            try:
                pr = parse_record(files[path].decode("utf-8"), path_hint=path)
                rec_item = _import_one(session, p, pr, change_source)
            except (ValidationFailed, AccessError, NotFound, Conflict, UnicodeDecodeError) as e:
                report.items.append(ImportItem(path, name, "error", str(e)))
                continue
            report.items.append(ImportItem(path, pr.data.name or name, rec_item[0], rec_item[1]))
            if rec_item[2] is not None:
                parsed.append((path, pr, rec_item[2]))
        # Second pass: links, now that every record in the archive exists.
        for _path, pr, rec in parsed:
            if pr.link_ids:
                good = [
                    i
                    for i in pr.link_ids
                    if (t := session.get(MemoryRecord, i)) is not None and can_read(session, p, t)
                ]
                if good:
                    try:
                        write_record(
                            session,
                            p,
                            RecordIn(id=rec.id, links=good),
                            change_source=change_source,
                            note="import: links",
                        )
                    except (ValidationFailed, AccessError, Conflict):
                        pass
        session.flush()
        if apply:
            nested.commit()
        else:
            nested.rollback()
    except Exception:
        nested.rollback()
        raise
    return report


def _import_one(
    session: Session, p: Principal, pr: ParsedRecord, change_source: str
) -> tuple[str, str, MemoryRecord | None]:
    d = pr.data
    existing = session.get(MemoryRecord, pr.fixed_id) if pr.fixed_id else None
    if existing is not None and not can_read(session, p, existing):
        # Someone else's record happens to carry this id: treat it as not matching, and don't reuse the id.
        existing = None
        pr.fixed_id = None
    if existing is None:
        existing = _find_existing(session, p, RecordIn(**d.model_dump(exclude={"id"})))
    if existing is None:
        res = write_record(session, p, d, change_source=change_source, note="imported", fixed_id=pr.fixed_id)
        if pr.created:
            res.record.created_at = pr.created
            session.add(res.record)
        return "create", "", res.record
    if _same(existing, d):
        return "unchanged", "", existing
    hub_is_newer = bool(pr.updated and existing.updated_at.replace(microsecond=0) > pr.updated)
    if existing.confidence == "established" or hub_is_newer:
        why = (
            "the hub copy is `established`"
            if existing.confidence == "established"
            else "the hub copy is newer than the import"
        )
        proposals.park_conflict(session, p, existing, d, source=change_source, note=f"import conflict: {why}")
        return "conflict", f"not applied — {why}; review it on the record's page", existing
    d2 = d.model_copy(update={"id": existing.id, "scope": None, "project": None, "team": None})
    res = write_record(session, p, d2, change_source=change_source, note="imported update")
    return "update", "; ".join(res.notices), res.record


def _import_inbox(session: Session, p: Principal, path: str, raw: bytes) -> int:
    m = _FM_RE.match(raw.decode("utf-8", "replace").replace("\r\n", "\n"))
    if not m:
        return 0
    fm = yaml.safe_load(m.group(1)) or {}
    if not isinstance(fm, dict) or not fm.get("title"):
        return 0
    ref = fm.get("external_ref")
    source = str(fm.get("source") or "import")[:60]
    if (
        ref
        and session.exec(
            select(InboxItem).where(
                InboxItem.owner_user_id == p.user_id,
                InboxItem.source == source,
                InboxItem.external_ref == ref,
            )
        ).first()
    ):
        return 0
    session.add(
        InboxItem(
            owner_user_id=p.user_id,
            source=source,
            scope=str(fm.get("scope") or "user"),
            title=str(fm["title"])[:200],
            body=m.group(2).strip("\n")[:20000],
            external_ref=str(ref) if ref else None,
        )
    )
    return 1


def new_export_name() -> str:
    return f"acm-export-{utcnow().strftime('%Y%m%d-%H%M%S')}"
