"""Task focus: build the context pack an AI session is handed for the task at hand.

Implements docs/ARCHITECTURE.md § Task focus: core always, then FTS5 + topics + one-hop links,
ranked by relevance x scope x confidence x reinforcement/recency, packed to a token budget with
progressive disclosure, every record carrying a `why`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from sqlmodel import Session, select

from .access import Principal, can_read, visible_clause
from .models import MemoryLink, MemoryRecord, Project, utcnow
from .records import estimate_tokens, fts_search, fts_terms, project_slug, record_tokens, touch_retrieved

SCOPE_WEIGHT = {"project": 1.0, "team": 0.9, "user": 0.8}  # narrow wins
CONFIDENCE_WEIGHT = {"observed": 0.8, "confirmed": 1.0, "established": 1.1}
SCOPE_ORDER = {"project": 0, "team": 1, "user": 2}
DEFAULT_BUDGET = 4000
FULL_BODY_COUNT = 3  # top-N associated records get their full body; the rest are name + description


@dataclass
class FocusItem:
    record: MemoryRecord
    project: str | None
    why: list[str]
    score: float = 0.0
    full: bool = True

    @property
    def tokens(self) -> int:
        return (
            record_tokens(self.record)
            if self.full
            else estimate_tokens(f"{self.record.name}\n{self.record.description}")
        )


@dataclass
class FocusPack:
    task: str
    core: list[FocusItem] = field(default_factory=list)
    associated: list[FocusItem] = field(default_factory=list)
    omitted: int = 0
    core_tokens: int = 0
    associated_tokens: int = 0
    budget: int = DEFAULT_BUDGET
    core_budget: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        def item(i: FocusItem) -> dict:
            r = i.record
            d = {
                "id": r.id,
                "name": r.name,
                "description": r.description,
                "type": r.type,
                "scope": r.scope,
                "project": i.project,
                "confidence": r.confidence,
                "tier": r.tier,
                "topics": r.topics,
                "why": i.why,
            }
            if i.full:
                d["body"] = r.body
            else:
                d["body_omitted"] = "call memory_get with this id for the full text"
            return d

        return {
            "task": self.task,
            "core": [item(i) for i in self.core],
            "associated": [item(i) for i in self.associated],
            "omitted_for_budget": self.omitted,
            "tokens": {
                "core": self.core_tokens,
                "core_budget": self.core_budget,
                "associated": self.associated_tokens,
                "budget": self.budget,
            },
            "notes": self.notes,
        }


def _recency(rec: MemoryRecord) -> float:
    last = rec.last_reinforced or rec.updated_at or rec.created_at
    days = max((utcnow() - last).total_seconds() / 86400, 0)
    return 0.85 + 0.15 * math.pow(0.5, days / 365)  # 1.0 fresh -> 0.85 asymptotically


def build_focus(
    session: Session,
    p: Principal,
    task: str,
    *,
    project: str | None = None,
    topics: list[str] | None = None,
    budget: int = DEFAULT_BUDGET,
    core_budget: int = 2000,
    include_other_projects: bool = False,
    touch: bool = False,
) -> FocusPack:
    pack = FocusPack(task=task, budget=budget, core_budget=core_budget)
    visible = visible_clause(session, p)
    active = MemoryRecord.status == "active"
    proj = session.exec(select(Project).where(Project.slug == project)).first() if project else None
    if project and proj is None:
        pack.notes.append(
            f"Project {project!r} isn't known to the hub; showing user/team/other-project memory only."
        )

    def relevant_to_project(r: MemoryRecord) -> bool:
        # Partition rule: with a project in play, other projects' memory stays out unless explicitly asked for.
        return include_other_projects or r.scope != "project" or proj is None or r.project_id == proj.id

    # 1. Core: always, narrow-wins order (project, team, user).
    core_rows = session.exec(select(MemoryRecord).where(MemoryRecord.tier == "core", active, visible)).all()
    core_rows = [r for r in core_rows if relevant_to_project(r)]
    core_rows.sort(key=lambda r: (SCOPE_ORDER[r.scope], r.name))
    pack.core = [FocusItem(r, project_slug(session, r), ["core"]) for r in core_rows]
    pack.core_tokens = sum(i.tokens for i in pack.core)
    if pack.core_tokens > core_budget:
        pack.notes.append(
            f"Core is ~{pack.core_tokens} tokens, over its ~{core_budget} budget; consider demoting a record."
        )
    core_ids = {r.id for r in core_rows}

    # 2. Candidates: text match, topic match, then one-hop link expansion.
    cand: dict[str, FocusItem] = {}
    hits = fts_search(session, task, limit=40, mode="or")
    max_rel = max((r for _, r in hits), default=1.0) or 1.0
    ranked_text: dict[str, float] = {rid: rel / max_rel for rid, rel in hits}
    task_terms = set(fts_terms(task))
    wanted_topics = {t.lower() for t in (topics or [])}

    pool = {
        r.id: r
        for r in session.exec(
            select(MemoryRecord).where(active, visible, MemoryRecord.tier == "associated")
        ).all()
    }
    for rid, r in pool.items():
        if not relevant_to_project(r):
            continue
        why: list[str] = []
        base = 0.0
        if rid in ranked_text:
            base += ranked_text[rid]
            why.append("matched the task text")
        rec_topics = set(r.topics)
        topic_hit = (rec_topics & wanted_topics) or (rec_topics & task_terms)
        if topic_hit:
            base += 0.3
            why.append("topic: " + ", ".join(sorted(topic_hit)))
        if project and proj is not None and r.project_id == proj.id and base > 0:
            why.append(f"in project {project}")
        if base > 0:
            cand[rid] = FocusItem(r, project_slug(session, r), why, base)

    seeds = list(core_ids) + [i for i, _ in sorted(cand.items(), key=lambda kv: -kv[1].score)[:5]]
    for seed in seeds:
        edges = session.exec(
            select(MemoryLink).where((MemoryLink.from_id == seed) | (MemoryLink.to_id == seed))
        ).all()
        for e in edges:
            other = e.to_id if e.from_id == seed else e.from_id
            r = pool.get(other)
            if r is None or other in core_ids:
                continue
            note = "linked from " + ("a core record" if seed in core_ids else "a top match")
            if other in cand:
                cand[other].score += 0.15
                cand[other].why.append(note)
            else:
                cand[other] = FocusItem(r, project_slug(session, r), [note], 0.1)

    # 3. Rank, then pack to the budget: full bodies for the top few, name+description for the rest.
    for it in cand.values():
        r = it.record
        it.score *= (
            SCOPE_WEIGHT[r.scope]
            * CONFIDENCE_WEIGHT[r.confidence]
            * (1 + 0.05 * min(r.reinforcement_count, 4))
            * _recency(r)
        )
    ordered = sorted(cand.values(), key=lambda i: -i.score)
    spent = 0
    for n, it in enumerate(ordered):
        it.full = n < FULL_BODY_COUNT
        if spent + it.tokens > budget and it.full:
            it.full = False  # try the compact form before dropping it
        if spent + it.tokens > budget:
            pack.omitted += 1
            continue
        spent += it.tokens
        pack.associated.append(it)
    pack.associated_tokens = spent
    if pack.omitted:
        pack.notes.append(
            f"{pack.omitted} lower-ranked record(s) left out to fit the ~{budget}-token budget; use memory_search."
        )

    if touch:
        touch_retrieved(session, [i.record.id for i in pack.core + pack.associated])
    return pack


def visible_for_focus(session: Session, p: Principal, rec: MemoryRecord) -> bool:
    return can_read(session, p, rec)
