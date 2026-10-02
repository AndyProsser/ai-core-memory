"""Record service: every create/update/read of memory goes through here.

Enforces, server-side, the rules in docs/ARCHITECTURE.md: scope partitioning, the confidence-tier
mutation rule, human-gated core promotion, the core token budget, and a revision for every change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, text
from sqlmodel import Session, col, select

from .access import (
    AccessError,
    NotFound,
    Principal,
    can_read,
    can_write,
    require_read,
    require_write,
    visible_clause,
    writable_project_ids,
)
from .ids import is_id
from .models import (
    InstanceSettings,
    MemoryLink,
    MemoryRecord,
    MemoryRevision,
    Project,
    Team,
    utcnow,
)

TYPES = ("user", "feedback", "project", "reference", "intent", "rule")
SCOPES = ("project", "team", "user")
CONFIDENCES = ("observed", "confirmed", "established")
TIERS = ("core", "associated")
STATUSES = ("active", "superseded", "stale", "archived")
CHANGE_SOURCES = ("dream-cycle", "mcp-write", "import", "ui", "cli", "plugin", "mechanical")

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
MAX_BODY = 20_000
MAX_DESCRIPTION = 400


class ValidationFailed(Exception):
    """The input is malformed or breaks a schema rule (maps to 422)."""


class Conflict(Exception):
    """The change collides with an `established` record or needs explicit human confirmation (409)."""

    def __init__(self, message: str, *, record_id: str | None = None, needs_confirmation: bool = False):
        super().__init__(message)
        self.record_id = record_id
        self.needs_confirmation = needs_confirmation


def estimate_tokens(text_: str) -> int:
    """Deliberately crude (chars / 4): no tokenizer dependency, per docs/ARCHITECTURE.md."""
    return len(text_) // 4 + 1


def record_tokens(rec: MemoryRecord) -> int:
    return estimate_tokens(f"{rec.name}\n{rec.description}\n{rec.body}")


class RecordIn(BaseModel):
    """A write request. Fields left as None keep their current value on update."""

    id: str | None = None
    name: str | None = None
    description: str | None = None
    body: str | None = None
    type: str | None = None
    scope: str | None = None
    confidence: str | None = None
    tier: str | None = None
    status: str | None = None
    topics: list[str] | None = None
    links: list[str] | None = None
    project: str | None = Field(default=None, description="project slug (scope=project)")
    team: str | None = Field(default=None, description="team slug (scope=team)")
    source: str | None = None
    source_ref: str | None = None

    @field_validator("topics", mode="before")
    @classmethod
    def _topics(cls, v):  # noqa: ANN001
        if v is None:
            return v
        return [str(t).strip().lower() for t in v if str(t).strip()]


@dataclass
class WriteResult:
    record: MemoryRecord
    action: str  # created | updated | unchanged
    notices: list[str] = field(default_factory=list)
    revision_id: str | None = None


# --- helpers -----------------------------------------------------------------------------------------


def _instance(session: Session) -> InstanceSettings:
    inst = session.get(InstanceSettings, 1)
    if inst is None:
        inst = InstanceSettings(id=1)
        session.add(inst)
        session.flush()
    return inst


def _validate_enum(value: str | None, allowed: tuple[str, ...], what: str) -> None:
    if value is not None and value not in allowed:
        raise ValidationFailed(f"{what} must be one of {', '.join(allowed)} (got {value!r}).")


def _validate_fields(data: RecordIn) -> None:
    _validate_enum(data.type, TYPES, "type")
    _validate_enum(data.scope, SCOPES, "scope")
    _validate_enum(data.confidence, CONFIDENCES, "confidence")
    _validate_enum(data.tier, TIERS, "tier")
    _validate_enum(data.status, STATUSES, "status")
    if data.name is not None and not SLUG_RE.match(data.name):
        raise ValidationFailed("name must be a short kebab-case slug (a-z, 0-9, hyphens; max 80 chars).")
    if data.description is not None:
        if not data.description.strip() or "\n" in data.description.strip():
            raise ValidationFailed("description must be a single non-empty line.")
        if len(data.description) > MAX_DESCRIPTION:
            raise ValidationFailed(f"description must be at most {MAX_DESCRIPTION} characters.")
    if data.body is not None and len(data.body) > MAX_BODY:
        raise ValidationFailed(f"body must be at most {MAX_BODY} characters.")
    if data.topics is not None:
        if len(data.topics) > 12 or any(not TOPIC_RE.match(t) for t in data.topics):
            raise ValidationFailed("topics: at most 12 short kebab-case tags.")
    if data.links is not None and (len(data.links) > 25 or any(not is_id(i) for i in data.links)):
        raise ValidationFailed("links: at most 25 record ids.")


def _owner_filter(scope: str, project_id: str | None, team_id: str | None, user_id: str | None):
    if scope == "project":
        return MemoryRecord.project_id == project_id
    if scope == "team":
        return MemoryRecord.team_id == team_id
    return MemoryRecord.user_id == user_id


def resolve_project(session: Session, p: Principal, slug: str, *, create: bool) -> Project | None:
    if not SLUG_RE.match(slug):
        raise ValidationFailed("project must be a kebab-case slug.")
    proj = session.exec(select(Project).where(Project.slug == slug)).first()
    if proj is not None:
        return proj
    if not create:
        return None
    # Auto-create on first write — only for a human, or a read-write token that isn't pinned to specific projects.
    if p.read_only or (p.is_token and p.token_project_ids):
        raise AccessError(f"Project {slug!r} doesn't exist and this credential can't create projects.")
    proj = Project(slug=slug, owner_user_id=p.user_id, visibility="private")
    session.add(proj)
    session.flush()
    return proj


def _find_existing(session: Session, p: Principal, data: RecordIn) -> MemoryRecord | None:
    if data.id:
        rec = session.get(MemoryRecord, data.id)
        return require_read(session, p, rec)
    if not data.name or not data.scope:
        return None
    project_id = team_id = user_id = None
    if data.scope == "project":
        if not data.project:
            raise ValidationFailed("scope=project requires `project` (a project slug).")
        proj = resolve_project(session, p, data.project, create=False)
        if proj is None:
            return None
        project_id = proj.id
    elif data.scope == "team":
        if not data.team:
            raise ValidationFailed("scope=team requires `team` (a team slug).")
        team = session.exec(select(Team).where(Team.slug == data.team)).first()
        if team is None:
            return None
        team_id = team.id
    else:
        user_id = p.user_id
    rec = session.exec(
        select(MemoryRecord).where(
            MemoryRecord.scope == data.scope,
            _owner_filter(data.scope, project_id, team_id, user_id),
            MemoryRecord.name == data.name,
        )
    ).first()
    return require_read(session, p, rec) if rec else None


def _fts_sync(session: Session, rec: MemoryRecord) -> None:
    session.execute(text("DELETE FROM memory_fts WHERE record_id = :id"), {"id": rec.id})
    session.execute(
        text("INSERT INTO memory_fts(record_id, name, description, body, topics) VALUES (:id,:n,:d,:b,:t)"),
        {
            "id": rec.id,
            "n": rec.name.replace("-", " "),
            "d": rec.description,
            "b": rec.body,
            "t": " ".join(rec.topics),
        },
    )


def _set_links(session: Session, p: Principal, rec: MemoryRecord, ids: list[str]) -> None:
    session.exec(delete(MemoryLink).where(MemoryLink.from_id == rec.id, MemoryLink.kind == "related"))  # type: ignore[call-overload]
    for target_id in dict.fromkeys(ids):
        if target_id == rec.id:
            continue
        target = session.get(MemoryRecord, target_id)
        if target is None or not can_read(session, p, target):
            raise ValidationFailed(f"links: no visible record {target_id}.")
        session.add(MemoryLink(from_id=rec.id, to_id=target_id, kind="related"))


def core_usage(session: Session, p: Principal, *, exclude_id: str | None = None) -> int:
    q = select(MemoryRecord).where(
        MemoryRecord.tier == "core", MemoryRecord.status == "active", visible_clause(session, p)
    )
    return sum(record_tokens(r) for r in session.exec(q).all() if r.id != exclude_id)


def _revision(
    session: Session,
    p: Principal,
    rec: MemoryRecord,
    *,
    source: str,
    note: str | None,
    flagged: bool = False,
    applied: bool = True,
) -> MemoryRevision:
    rev = MemoryRevision(
        memory_record_id=rec.id,
        name=rec.name,
        description=rec.description,
        body=rec.body,
        type=rec.type,
        scope=rec.scope,
        confidence=rec.confidence,
        tier=rec.tier,
        status=rec.status,
        topics=list(rec.topics),
        changed_by_user_id=p.user_id,
        changed_by_token_id=p.token_id,
        changed_by_label=p.label,
        change_note=note,
        change_source=source,
        flagged=flagged,
        applied=applied,
    )
    session.add(rev)
    session.flush()
    return rev


_SUBSTANTIVE = ("body", "description", "type", "confidence", "tier", "status")


# --- write -------------------------------------------------------------------------------------------


def write_record(
    session: Session,
    p: Principal,
    data: RecordIn,
    *,
    change_source: str,
    note: str | None = None,
    confirm_established: bool = False,
    fixed_id: str | None = None,
) -> WriteResult:
    if change_source not in CHANGE_SOURCES:
        raise ValidationFailed(f"unknown change_source {change_source!r}")
    if p.read_only:
        raise AccessError("This credential is read-only.")
    _validate_fields(data)
    existing = _find_existing(session, p, data)
    return (
        _update(session, p, existing, data, change_source, note, confirm_established)
        if existing
        else _create(session, p, data, change_source, note, fixed_id)
    )


def _create(
    session: Session,
    p: Principal,
    data: RecordIn,
    change_source: str,
    note: str | None,
    fixed_id: str | None = None,
) -> WriteResult:
    missing = [f for f in ("name", "description", "type", "scope") if not getattr(data, f)]
    if missing:
        raise ValidationFailed(f"Creating a record requires: {', '.join(missing)}.")
    assert data.name and data.description and data.type and data.scope  # for type checkers
    confidence = data.confidence or ("confirmed" if data.type == "rule" and p.is_human else "observed")
    tier = data.tier or "associated"
    status = data.status or "active"
    notices: list[str] = []

    if data.type == "rule" and confidence == "observed":
        raise ValidationFailed(
            "A rule can't be merely `observed` — that's feedback. Use type=feedback, or confidence=confirmed."
        )
    if p.is_token:
        if confidence == "established":
            raise AccessError(
                "Only a person can mark a record `established` (set it in the web UI or `acm edit`)."
            )
        if tier == "core":
            raise AccessError(
                "Promoting a record to core is human-gated: create it as `associated` and ask the user to promote it."
            )
        if data.scope == "team":
            raise AccessError(
                "AI clients can't write team-scope memory directly; team scope is a deliberate, human-gated promotion."
            )
        if data.scope == "user" and not p.token_include_user_scope:
            raise AccessError("This token isn't allowed to write user-scope memory.")

    project_id = team_id = user_id = None
    if data.scope == "project":
        if not data.project:
            raise ValidationFailed("scope=project requires `project` (a project slug).")
        proj = resolve_project(session, p, data.project, create=True)
        assert proj is not None
        if proj.id not in writable_project_ids(session, p):
            raise AccessError("You can't write to that project.")
        project_id = proj.id
    elif data.scope == "team":
        team = session.exec(select(Team).where(Team.slug == (data.team or ""))).first()
        if team is None or team.id not in p.team_ids:
            raise AccessError("You aren't a member of that team.")
        team_id = team.id
    else:
        user_id = p.user_id

    rec = MemoryRecord(
        scope=data.scope,
        type=data.type,
        confidence=confidence,
        tier=tier,
        status=status,
        name=data.name,
        description=data.description.strip(),
        body=(data.body or ""),
        topics=data.topics or [],
        project_id=project_id,
        team_id=team_id,
        user_id=user_id,
        source=data.source or change_source,
        source_trust="external" if change_source == "plugin" else "internal",
    )
    if fixed_id and session.get(MemoryRecord, fixed_id) is None:
        rec.id = fixed_id  # restores keep their ids so links between records survive a round trip
    if not can_write(session, p, rec):
        raise AccessError("You can't write there.")
    if rec.tier == "core":
        _enforce_core_budget(session, p, rec)
    session.add(rec)
    session.flush()
    _fts_sync(session, rec)
    if data.links:
        _set_links(session, p, rec, data.links)
    rev = _revision(session, p, rec, source=change_source, note=note or "created")
    return WriteResult(rec, "created", notices, rev.id)


def _enforce_core_budget(session: Session, p: Principal, rec: MemoryRecord) -> None:
    budget = _instance(session).core_token_budget
    used = core_usage(session, p, exclude_id=rec.id)
    need = record_tokens(rec)
    if used + need > budget:
        raise ValidationFailed(
            f"Core is limited to ~{budget} tokens (currently ~{used}; this record needs ~{need}). "
            "Demote or shorten another core record first."
        )


def _update(
    session: Session,
    p: Principal,
    rec: MemoryRecord,
    data: RecordIn,
    change_source: str,
    note: str | None,
    confirm_established: bool,
) -> WriteResult:
    require_write(session, p, rec)
    if data.scope and data.scope != rec.scope:
        raise ValidationFailed(
            "A record's scope can't be changed in place; create it in the new scope (promotion is human-gated)."
        )
    if data.project and rec.scope == "project":
        proj = session.get(Project, rec.project_id)
        if proj and proj.slug != data.project:
            raise ValidationFailed("A record can't be moved to a different project; create a new one there.")

    incoming: dict[str, object] = {}
    for f in ("name", "description", "body", "type", "confidence", "tier", "status", "topics"):
        v = getattr(data, f)
        if v is not None:
            incoming[f] = v.strip() if f == "description" else v
    if "name" in incoming and incoming["name"] != rec.name:
        clash = session.exec(
            select(MemoryRecord).where(
                MemoryRecord.scope == rec.scope,
                _owner_filter(rec.scope, rec.project_id, rec.team_id, rec.user_id),
                MemoryRecord.name == incoming["name"],
                MemoryRecord.id != rec.id,
            )
        ).first()
        if clash:
            raise ValidationFailed(f"A record named {incoming['name']!r} already exists here.")

    changed = {f: v for f, v in incoming.items() if getattr(rec, f) != v}
    links_changed = data.links is not None and set(data.links) != set(
        session.exec(
            select(MemoryLink.to_id).where(MemoryLink.from_id == rec.id, MemoryLink.kind == "related")
        ).all()
    )
    if not changed and not links_changed:
        return WriteResult(rec, "unchanged")

    substantive = [f for f in changed if f in _SUBSTANTIVE]
    new_type = changed.get("type", rec.type)
    new_conf = changed.get("confidence", rec.confidence)
    notices: list[str] = []
    flagged = False

    if new_type == "rule" and new_conf == "observed":
        raise ValidationFailed("A rule can't be merely `observed` — that's feedback.")
    if p.is_token:
        if new_conf == "established" and rec.confidence != "established":
            raise AccessError("Only a person can mark a record `established`.")
        if "tier" in changed:
            raise AccessError(
                "Changing a record's tier (core/associated) is human-gated; ask the user to do it."
            )
    if substantive:
        if rec.confidence == "established":
            if p.is_token:
                raise Conflict(
                    f"Record {rec.name!r} is `established`; it can't be changed by an AI client. Surface the conflict to the "
                    "user and let them decide (they can edit it in the web UI).",
                    record_id=rec.id,
                )
            if not confirm_established:
                raise Conflict(
                    f"Record {rec.name!r} is `established`. Confirm explicitly to change it.",
                    record_id=rec.id,
                    needs_confirmation=True,
                )
            flagged = True
            notices.append(f"Changed an established record ({rec.name}).")
        elif rec.confidence == "confirmed":
            flagged = True
            notices.append(
                f"Updated a confirmed record ({rec.name}): {', '.join(substantive)}. Call this out to the user."
            )

    for f, v in changed.items():
        setattr(rec, f, v)
    if "status" in changed and rec.status in {"superseded", "archived"}:
        rec.valid_to = utcnow()
    if "status" in changed and rec.status == "active":
        rec.valid_to = None
    if (
        rec.tier == "core"
        and rec.status == "active"
        and ({"tier", "body", "description", "name", "status"} & set(changed))
    ):
        _enforce_core_budget(session, p, rec)
    rec.updated_at = utcnow()
    session.add(rec)
    session.flush()
    _fts_sync(session, rec)
    if links_changed:
        _set_links(session, p, rec, data.links or [])
    rev = _revision(session, p, rec, source=change_source, note=note, flagged=flagged)
    return WriteResult(rec, "updated", notices, rev.id)


# --- conflicts (unapplied revisions) -------------------------------------------------------------------


def record_conflict(
    session: Session, p: Principal, rec: MemoryRecord, incoming: RecordIn, *, source: str, note: str
) -> MemoryRevision:
    """Park an incoming version of `rec` as a flagged, unapplied revision for a human to resolve."""
    ghost = MemoryRecord(
        **{
            **rec.model_dump(),
            **{
                k: v
                for k, v in incoming.model_dump().items()
                if v is not None
                and k in ("name", "description", "body", "type", "confidence", "tier", "status", "topics")
            },
        }
    )
    return _revision(session, p, ghost, source=source, note=note, flagged=True, applied=False)


def resolve_conflict(
    session: Session, p: Principal, revision_id: str, *, apply: bool, confirm_established: bool = False
) -> MemoryRecord:
    rev = session.get(MemoryRevision, revision_id)
    if rev is None or rev.applied:
        raise NotFound("No such pending conflict.")
    rec = require_write(session, p, session.get(MemoryRecord, rev.memory_record_id))  # type: ignore[arg-type]
    if not p.is_human:
        raise AccessError("Only a person can resolve a conflict.")
    if apply:
        write_record(
            session,
            p,
            RecordIn(
                id=rec.id,
                name=rev.name,
                description=rev.description,
                body=rev.body,
                type=rev.type,
                confidence=rev.confidence,
                tier=rev.tier,
                status=rev.status,
                topics=rev.topics,
            ),
            change_source="ui" if p.kind == "session" else "cli",
            note=f"applied pending conflict {rev.id}",
            confirm_established=confirm_established,
        )
    session.delete(rev)
    session.flush()
    return rec


def pending_conflicts(session: Session, rec_id: str) -> list[MemoryRevision]:
    return list(
        session.exec(
            select(MemoryRevision)
            .where(
                MemoryRevision.memory_record_id == rec_id,
                col(MemoryRevision.applied).is_(False),
            )
            .order_by(col(MemoryRevision.changed_at).desc())
        ).all()
    )


# --- reads ---------------------------------------------------------------------------------------------


def get_record(session: Session, p: Principal, ref: str, *, project: str | None = None) -> MemoryRecord:
    """Fetch by id, or by name (optionally disambiguated by project slug)."""
    if is_id(ref):
        return require_read(session, p, session.get(MemoryRecord, ref))
    q = select(MemoryRecord).where(MemoryRecord.name == ref, visible_clause(session, p))
    if project:
        proj = session.exec(select(Project).where(Project.slug == project)).first()
        q = q.where(MemoryRecord.project_id == (proj.id if proj else "-"))
    rows = session.exec(q).all()
    if not rows:
        raise NotFound("No such record.")
    if len(rows) > 1:
        raise ValidationFailed(f"{len(rows)} records are named {ref!r}; use the id or pass a project.")
    return rows[0]


def list_records(
    session: Session,
    p: Principal,
    *,
    scope: str | None = None,
    type: str | None = None,
    tier: str | None = None,
    confidence: str | None = None,
    status: str | None = "active",
    topic: str | None = None,
    project: str | None = None,
    q: str | None = None,
    limit: int = 500,
) -> list[MemoryRecord]:
    if q and q.strip():
        ids = [i for i, _ in fts_search(session, q, limit=limit * 2, mode="and")]
        stmt = select(MemoryRecord).where(MemoryRecord.id.in_(ids), visible_clause(session, p))  # type: ignore[attr-defined]
        rows = {r.id: r for r in session.exec(stmt).all()}
        out = [rows[i] for i in ids if i in rows]
    else:
        stmt = (
            select(MemoryRecord)
            .where(visible_clause(session, p))
            .order_by(col(MemoryRecord.updated_at).desc())
        )
        out = list(session.exec(stmt).all())
    if project:
        proj = session.exec(select(Project).where(Project.slug == project)).first()
        out = [r for r in out if proj and r.project_id == proj.id]
    for attr, val in (
        ("scope", scope),
        ("type", type),
        ("tier", tier),
        ("confidence", confidence),
        ("status", status),
    ):
        if val:
            out = [r for r in out if getattr(r, attr) == val]
    if topic:
        out = [r for r in out if topic in r.topics]
    return out[:limit]


_STOP = frozenset(
    "the a an and or of to in on for with is are was be this that it as at by from i we you my our your please can could would "
    "should just about into how what when where why which do does did have has had not no yes get got make made use using".split()
)


def fts_terms(query: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{1,}", query.lower())
    seen: dict[str, None] = {}
    for w in words:
        for part in w.replace("_", "-").split("-"):
            if len(part) >= 3 and part not in _STOP:
                seen[part] = None
    return list(seen)[:24]


def fts_search(session: Session, query: str, *, limit: int = 50, mode: str = "or") -> list[tuple[str, float]]:
    """(record_id, relevance>0) best-first. Terms are quoted, so user input can't inject FTS syntax."""
    terms = fts_terms(query)
    if not terms:
        return []
    joiner = " AND " if mode == "and" else " OR "
    match = joiner.join(f'"{t}"*' for t in terms)
    rows = session.execute(
        text(
            "SELECT record_id, bm25(memory_fts, 0.0, 5.0, 3.0, 1.0, 2.0) AS r FROM memory_fts WHERE memory_fts MATCH :m ORDER BY r LIMIT :n"
        ),
        {"m": match, "n": limit},
    ).all()
    return [(rid, -float(r)) for rid, r in rows]


def history(session: Session, p: Principal, rec: MemoryRecord) -> list[MemoryRevision]:
    require_read(session, p, rec)
    return list(
        session.exec(
            select(MemoryRevision)
            .where(MemoryRevision.memory_record_id == rec.id)
            .order_by(col(MemoryRevision.changed_at).desc(), col(MemoryRevision.id).desc())
        ).all()
    )


def linked_records(session: Session, p: Principal, rec: MemoryRecord) -> list[MemoryRecord]:
    ids = session.exec(
        select(MemoryLink.to_id).where(MemoryLink.from_id == rec.id, MemoryLink.kind == "related")
    ).all()
    ids += session.exec(
        select(MemoryLink.from_id).where(MemoryLink.to_id == rec.id, MemoryLink.kind == "related")
    ).all()
    out = [session.get(MemoryRecord, i) for i in dict.fromkeys(ids)]
    return [r for r in out if r and can_read(session, p, r)]


def count_records(session: Session, p: Principal) -> int:
    return session.exec(
        select(func.count()).select_from(MemoryRecord).where(visible_clause(session, p))
    ).one()


def project_slug(session: Session, rec: MemoryRecord) -> str | None:
    if rec.scope != "project" or not rec.project_id:
        return None
    proj = session.get(Project, rec.project_id)
    return proj.slug if proj else None


def team_slug(session: Session, rec: MemoryRecord) -> str | None:
    if rec.scope != "team" or not rec.team_id:
        return None
    team = session.get(Team, rec.team_id)
    return team.slug if team else None


def touch_retrieved(session: Session, ids: list[str], when: datetime | None = None) -> None:
    """Record that records were served (feeds staleness). Deliberately does NOT affect confidence."""
    if not ids:
        return
    for rid in ids:
        rec = session.get(MemoryRecord, rid)
        if rec:
            rec.last_retrieved = when or utcnow()
            session.add(rec)
