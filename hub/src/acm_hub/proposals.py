"""The proposal queue: suggested changes (merge, supersede, promote, stale, ...) that wait for a human.

Both the mechanical consolidation job and AI clients (the `dream` skill, via MCP) *propose*; only a person
approves. Approving applies the change through the normal record service, so every confidence-tier rule,
the core budget and access control hold exactly as they do for any other write. See
docs/ARCHITECTURE.md § Who does the reasoning: the proposal queue.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta

from sqlmodel import Session, col, select

from .access import AccessError, NotFound, Principal, can_read, can_write
from .models import MemoryRecord, MemoryRevision, Proposal, utcnow
from .records import (
    Conflict,
    RecordIn,
    ValidationFailed,
    record_conflict,
    reinforce_review,
    resolve_conflict,
    supersede,
    write_record,
)

KINDS = (
    "merge",
    "supersede",
    "promote_scope",
    "promote_core",
    "demote_core",
    "mark_stale",
    "archive",
    "review_established",
    "conflict",
)
# What an AI client may propose. `review_established` and `conflict` are raised by the hub itself.
AI_KINDS = frozenset(
    {"merge", "supersede", "promote_scope", "promote_core", "demote_core", "mark_stale", "archive"}
)
GENERATORS = ("mechanical", "dream-skill", "llm-worker")
REJECTION_COOLDOWN = timedelta(
    days=30
)  # an identical proposal a person already rejected isn't re-raised for this long
MAX_PAYLOAD_BYTES = 20_000
SCOPE_RANK = {"project": 0, "team": 1, "user": 2}  # narrow -> broad; promotion only ever goes broader

_PAYLOAD_RECORD_KEYS = {
    "merge": ("keep", "retire"),
    "supersede": ("old", "new"),
    "promote_scope": ("source",),
    "promote_core": ("record",),
    "demote_core": ("record",),
    "mark_stale": ("record",),
    "archive": ("record",),
    "review_established": ("record",),
    "conflict": ("record",),
}


class ProposalExpired(ValidationFailed):
    """The proposal no longer applies; it has been closed (the caller should commit that)."""


# --- payloads ---------------------------------------------------------------------------------------------


def targets_of(kind: str, payload: dict) -> list[str]:
    ids = [payload.get(k) for k in _PAYLOAD_RECORD_KEYS[kind]]
    if kind == "promote_core":
        ids += list(payload.get("demote") or [])
    return [i for i in dict.fromkeys(ids) if i]


def dedupe_key(kind: str, payload: dict) -> str:
    extra = ""
    if kind == "conflict":
        extra = str(payload.get("revision"))
    elif kind == "promote_scope":
        extra = f"{payload.get('to_scope')}:{payload.get('team') or ''}"
    return f"{kind}:{'|'.join(sorted(targets_of(kind, payload)))}:{extra}"


def _validate_payload_shape(kind: str, payload: dict) -> None:
    if kind not in KINDS:
        raise ValidationFailed(f"kind must be one of {', '.join(KINDS)}.")
    if not isinstance(payload, dict) or len(json.dumps(payload, default=str)) > MAX_PAYLOAD_BYTES:
        raise ValidationFailed("payload must be a JSON object under 20 KB.")
    for k in _PAYLOAD_RECORD_KEYS[kind]:
        if not isinstance(payload.get(k), str) or not payload[k]:
            raise ValidationFailed(f"{kind} needs payload.{k} (a record id).")
    if kind == "promote_core":
        demote = payload.get("demote") or []
        if not isinstance(demote, list) or len(demote) > 10 or not all(isinstance(x, str) for x in demote):
            raise ValidationFailed("promote_core payload.demote must be a list of at most 10 record ids.")
    if kind == "merge":
        m = payload.get("merged")
        if m is not None and (not isinstance(m, dict) or set(m) - {"description", "body"}):
            raise ValidationFailed("merge payload.merged may only contain description and body.")
    if kind == "promote_scope":
        if payload.get("to_scope") not in {"team", "user"}:
            raise ValidationFailed("promote_scope needs payload.to_scope = team or user.")
        if payload["to_scope"] == "team" and not payload.get("team"):
            raise ValidationFailed("promote_scope to team needs payload.team (a team slug).")
        for f in ("name", "description", "body"):
            if payload.get(f) is not None and not isinstance(payload[f], str):
                raise ValidationFailed(f"promote_scope payload.{f} must be text.")


def _records(session: Session, ids: list[str]) -> dict[str, MemoryRecord | None]:
    return {i: session.get(MemoryRecord, i) for i in ids}


def precondition(session: Session, kind: str, payload: dict) -> str | None:
    """Why this proposal can't (or no longer should) be applied, or None. Used at creation, expiry, and approval."""
    recs = _records(session, targets_of(kind, payload))
    if any(r is None for r in recs.values()):
        return "a record it refers to no longer exists"
    g = lambda k: recs[payload[k]]  # noqa: E731
    if kind == "merge":
        keep, retire = g("keep"), g("retire")
        if keep.id == retire.id:
            return "keep and retire are the same record"
        if keep.status != "active" or retire.status != "active":
            return "one of the records is no longer active"
        if (keep.scope, keep.project_id, keep.team_id, keep.user_id) != (
            retire.scope,
            retire.project_id,
            retire.team_id,
            retire.user_id,
        ):
            return "the records are in different scopes or projects"
    elif kind == "supersede":
        old, new = g("old"), g("new")
        if old.id == new.id:
            return "a record can't supersede itself"
        if old.status == "superseded" or new.status not in {"active"}:
            return "the old record is already superseded, or the new one isn't active"
        if (old.scope, old.project_id, old.team_id, old.user_id) != (
            new.scope,
            new.project_id,
            new.team_id,
            new.user_id,
        ):
            return "the records are in different scopes or projects"
    elif kind == "promote_scope":
        src = g("source")
        if src.status != "active":
            return "the source record is no longer active"
        if SCOPE_RANK[payload["to_scope"]] <= SCOPE_RANK[src.scope]:
            return f"promotion only goes to a broader scope than {src.scope}"
    elif kind == "promote_core":
        rec = g("record")
        if rec.status != "active" or rec.tier == "core":
            return "the record is no longer active, or is already core"
        for d in payload.get("demote") or []:
            if recs[d].tier != "core":
                return f"{recs[d].name!r} isn't core any more"
    elif kind == "demote_core":
        if g("record").tier != "core" or g("record").status != "active":
            return "the record isn't an active core record any more"
    elif kind == "mark_stale":
        if g("record").status != "active":
            return "the record is no longer active"
    elif kind == "archive":
        if g("record").status == "archived":
            return "the record is already archived"
    elif kind == "review_established":
        if g("record").confidence != "established" or g("record").status != "active":
            return "the record is no longer an active established record"
    elif kind == "conflict":
        rev = session.get(MemoryRevision, payload.get("revision") or "")
        if rev is None or rev.applied:
            return "the pending version was already resolved"
    return None


# --- creating ---------------------------------------------------------------------------------------------


def create_proposal(
    session: Session,
    p: Principal,
    kind: str,
    payload: dict,
    *,
    rationale: str,
    generated_by: str | None = None,
) -> Proposal | None:
    """File a proposal. Returns the existing pending one if identical, or None if a person recently rejected it."""
    _validate_payload_shape(kind, payload)
    if p.read_only:
        raise AccessError("This credential is read-only; it can't propose changes.")
    if p.is_token and kind not in AI_KINDS:
        raise AccessError(f"AI clients can propose: {', '.join(sorted(AI_KINDS))}.")
    if p.is_system:
        gen = "mechanical"
    else:
        gen = generated_by if generated_by in GENERATORS and generated_by != "mechanical" else "dream-skill"
    rationale = (rationale or "").strip()
    if not rationale or len(rationale) > 1000:
        raise ValidationFailed("Give a rationale (1-1000 characters) so the reviewer knows why.")
    for rid in targets_of(kind, payload):
        rec = session.get(MemoryRecord, rid)
        if rec is None or not can_read(session, p, rec):
            raise NotFound(f"No such record {rid}.")
    if (problem := precondition(session, kind, payload)) is not None:
        raise ValidationFailed(f"Can't propose that: {problem}.")
    key = dedupe_key(kind, payload)
    same = session.exec(
        select(Proposal).where(Proposal.dedupe_key == key).order_by(col(Proposal.created_at).desc())
    ).all()
    for prev in same:
        if prev.status == "pending":
            return prev
    now = utcnow()
    for prev in same:
        if prev.status == "rejected" and prev.decided_at and now - prev.decided_at < REJECTION_COOLDOWN:
            return None
    prop = Proposal(
        kind=kind,
        payload=payload,
        target_ids=targets_of(kind, payload),
        rationale=rationale,
        generated_by=gen,
        generated_by_token_id=p.token_id,
        dedupe_key=key,
    )
    session.add(prop)
    session.flush()
    return prop


def park_conflict(
    session: Session, p: Principal, rec: MemoryRecord, incoming: RecordIn, *, source: str, note: str
) -> Proposal | None:
    """Hold an incoming version that can't be applied (established / newer record) and ask a person to resolve it."""
    rev = record_conflict(session, p, rec, incoming, source=source, note=note)
    prop = create_proposal(
        session,
        p if not p.is_token else _as_system(),
        "conflict",
        {"record": rec.id, "revision": rev.id},
        rationale=f"{note}. Compare the incoming version with the current one and choose.",
        generated_by=None,
    )
    if prop is not None and p.is_token:
        prop.generated_by_token_id = p.token_id
        prop.generated_by = "dream-skill"
    return prop


def _as_system() -> Principal:
    from .access import system_principal

    return system_principal()


def close_conflict_proposals(session: Session, revision_id: str, *, applied: bool, by: Principal) -> None:
    for prop in session.exec(
        select(Proposal).where(Proposal.kind == "conflict", Proposal.status == "pending")
    ).all():
        if prop.payload.get("revision") == revision_id:
            prop.status = "applied" if applied else "rejected"
            prop.decided_at = utcnow()
            prop.decided_by_user_id = by.user_id or None
            prop.decided_by_label = by.label
            prop.decision_note = "resolved from the record page"
            session.add(prop)
    session.flush()


# --- reading ----------------------------------------------------------------------------------------------


def _visible(session: Session, p: Principal, prop: Proposal) -> bool:
    for rid in prop.target_ids:
        rec = session.get(MemoryRecord, rid)
        if rec is None or not can_read(session, p, rec):
            return False
    return True


def list_proposals(
    session: Session,
    p: Principal,
    *,
    status: str | None = "pending",
    kind: str | None = None,
    limit: int = 200,
) -> list[Proposal]:
    q = select(Proposal).order_by(col(Proposal.created_at).desc())
    if status:
        q = q.where(Proposal.status == status)
    if kind:
        q = q.where(Proposal.kind == kind)
    out = []
    for prop in session.exec(q).all():
        if _visible(session, p, prop):
            out.append(prop)
            if len(out) >= limit:
                break
    return out


def pending_count(session: Session, p: Principal) -> int:
    return len(list_proposals(session, p, status="pending", limit=99))


def get_proposal(session: Session, p: Principal, proposal_id: str) -> Proposal:
    prop = session.get(Proposal, proposal_id)
    if prop is None or not _visible(session, p, prop):
        raise NotFound("No such proposal.")
    return prop


@dataclass
class ProposalView:
    proposal: Proposal
    summary: str
    records: dict[str, MemoryRecord] = field(default_factory=dict)
    needs_confirm: bool = False  # touches an established record: approving needs an explicit confirmation
    low_risk: bool = False  # all-observed targets, kinds that can be batch-approved
    incoming: MemoryRevision | None = None
    stale_reason: str | None = None


def view(session: Session, prop: Proposal) -> ProposalView:
    recs = {i: r for i in prop.target_ids if (r := session.get(MemoryRecord, i)) is not None}
    n = lambda k: recs[prop.payload[k]].name if prop.payload.get(k) in recs else "(deleted)"  # noqa: E731
    pl = prop.payload
    summary = {
        "merge": lambda: f"Merge {n('retire')} into {n('keep')}",
        "supersede": lambda: f"{n('new')} replaces {n('old')}",
        "promote_scope": lambda: (
            f"Promote {n('source')} to {pl.get('to_scope')} scope"
            + (f" (team {pl['team']})" if pl.get("team") else "")
        ),
        "promote_core": lambda: (
            f"Make {n('record')} core"
            + (
                f", demoting {', '.join(recs[d].name for d in pl.get('demote') or [] if d in recs)}"
                if pl.get("demote")
                else ""
            )
        ),
        "demote_core": lambda: f"Move {n('record')} out of core",
        "mark_stale": lambda: f"Mark {n('record')} stale",
        "archive": lambda: f"Archive {n('record')}",
        "review_established": lambda: f"Is {n('record')} still true?",
        "conflict": lambda: f"A different version of {n('record')} is waiting",
    }[prop.kind]()
    needs_confirm = (
        any(r.confidence == "established" for r in recs.values()) and prop.kind != "review_established"
    )
    low_risk = (
        prop.kind in {"merge", "archive", "mark_stale"}
        and prop.generated_by == "mechanical"
        and all(r.confidence == "observed" for r in recs.values())
    )
    incoming = session.get(MemoryRevision, pl.get("revision")) if prop.kind == "conflict" else None
    stale = precondition(session, prop.kind, pl) if prop.status == "pending" else None
    return ProposalView(prop, summary, recs, needs_confirm, low_risk, incoming, stale)


# --- deciding ---------------------------------------------------------------------------------------------


def expire_outdated(session: Session) -> int:
    n = 0
    for prop in session.exec(select(Proposal).where(Proposal.status == "pending")).all():
        why = precondition(session, prop.kind, prop.payload)
        if why:
            prop.status = "expired"
            prop.decided_at = utcnow()
            prop.decision_note = f"out of date: {why}"
            session.add(prop)
            n += 1
    session.flush()
    return n


def decide(
    session: Session,
    p: Principal,
    proposal_id: str,
    *,
    approve: bool,
    note: str | None = None,
    confirm_established: bool = False,
) -> Proposal:
    """Approve or reject. Human only. On a failed apply nothing changes and the proposal stays pending."""
    if not p.is_human:
        raise AccessError("Only a person can approve or reject a proposal.")
    prop = get_proposal(session, p, proposal_id)
    if prop.status != "pending":
        raise ValidationFailed(f"This proposal was already {prop.status}.")
    if (why := precondition(session, prop.kind, prop.payload)) is not None:
        prop.status = "expired"
        prop.decided_at = utcnow()
        prop.decision_note = f"out of date: {why}"
        session.add(prop)
        session.flush()
        raise ProposalExpired(f"This proposal is out of date: {why}. It has been closed.")
    cs = "ui" if p.kind == "session" else "cli"
    if approve:
        with (
            session.begin_nested()
        ):  # all-or-nothing: a failure leaves the records and the proposal untouched
            _apply(session, p, prop, cs, confirm_established)
    elif prop.kind == "conflict":
        resolve_conflict(session, p, prop.payload["revision"], apply=False)
    _finish(session, p, prop, "applied" if approve else "rejected", note)
    return prop


def _finish(
    session: Session, p: Principal, prop: Proposal, status: str, note: str | None, *, label: str | None = None
) -> None:
    prop.status = status
    prop.decided_at = utcnow()
    prop.decided_by_user_id = p.user_id or None
    prop.decided_by_label = label or p.label
    prop.decision_note = (note or "").strip()[:500] or None
    session.add(prop)
    session.flush()


def auto_apply(session: Session, prop: Proposal) -> bool:
    """Instance opt-in (auto_apply_proposals): apply a low-risk, mechanically generated, all-observed proposal."""
    v = view(session, prop)
    if not v.low_risk or v.stale_reason:
        return False
    sysp = _as_system()
    try:
        with session.begin_nested():
            _apply(session, sysp, prop, "mechanical", False)
    except (ValidationFailed, AccessError, Conflict):
        return False
    _finish(session, sysp, prop, "applied", "auto-applied (observed-only, mechanical)", label="auto")
    return True


def _set(
    session: Session, p: Principal, rec_id: str, cs: str, note: str, confirm: bool, **fields
) -> MemoryRecord:  # noqa: ANN003
    return write_record(
        session, p, RecordIn(id=rec_id, **fields), change_source=cs, note=note, confirm_established=confirm
    ).record


def _apply(session: Session, p: Principal, prop: Proposal, cs: str, confirm: bool) -> None:
    pl, k = prop.payload, prop.kind
    note = f"proposal {prop.id[-8:]}: {k}"
    get = lambda key: session.get(MemoryRecord, pl[key])  # noqa: E731
    if k == "merge":
        keep, retire = get("keep"), get("retire")
        merged = pl.get("merged") or {}
        if merged:
            _set(
                session,
                p,
                keep.id,
                cs,
                note,
                confirm,
                description=merged.get("description"),
                body=merged.get("body"),
            )
        supersede(
            session,
            p,
            retire,
            keep,
            change_source=cs,
            note=f"merged into {keep.name} ({note})",
            confirm_established=confirm,
        )
    elif k == "supersede":
        supersede(
            session, p, get("old"), get("new"), change_source=cs, note=note, confirm_established=confirm
        )
    elif k == "promote_scope":
        src = get("source")
        new_in = RecordIn(
            name=pl.get("name") or src.name,
            description=pl.get("description") or src.description,
            body=pl["body"] if pl.get("body") is not None else src.body,
            type=src.type,
            scope=pl["to_scope"],
            team=pl.get("team"),
            confidence=src.confidence,
            tier="associated",
            topics=list(src.topics),
        )
        new = write_record(
            session, p, new_in, change_source=cs, note=f"promoted from {src.scope} scope ({note})"
        ).record
        supersede(
            session,
            p,
            src,
            new,
            change_source=cs,
            note=f"promoted to {new.scope} scope ({note})",
            confirm_established=confirm,
            cross_scope=True,
        )
    elif k == "promote_core":
        for d in pl.get("demote") or []:  # make room first; the budget check below is then exact
            _set(session, p, d, cs, f"{note} (making room)", confirm, tier="associated")
        _set(session, p, pl["record"], cs, note, confirm, tier="core")
    elif k == "demote_core":
        _set(session, p, pl["record"], cs, note, confirm, tier="associated")
    elif k == "mark_stale":
        _set(session, p, pl["record"], cs, note, confirm, status="stale")
    elif k == "archive":
        _set(session, p, pl["record"], cs, note, confirm, status="archived")
    elif k == "review_established":
        reinforce_review(session, p, get("record"), change_source=cs, note=note)
    elif k == "conflict":
        resolve_conflict(session, p, pl["revision"], apply=True, confirm_established=confirm)


def can_decide(session: Session, p: Principal, prop: Proposal) -> bool:
    return p.is_human and all(
        (r := session.get(MemoryRecord, i)) is not None and can_write(session, p, r) for i in prop.target_ids
    )
