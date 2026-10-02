"""Plain-text rendering of events for sinks that post messages (Apprise, ntfy, ...)."""

from __future__ import annotations

from .base import Event


def render(event: Event) -> tuple[str, str]:
    p = event.payload
    t = event.type
    where = ", ".join(x for x in (p.get("scope"), p.get("project")) if x)
    link = f"\n{event.link}" if event.link else ""
    if t == "record.created":
        return f"New memory: {p.get('name')}", f"{p.get('type')} · {where}{link}"
    if t == "record.updated":
        return (
            f"Memory updated: {p.get('name')}",
            f"Changed: {', '.join(p.get('changed') or []) or 'details'} · {where}{link}",
        )
    if t == "record.superseded":
        return (
            f"Memory replaced: {p.get('name')}",
            f"Replaced by {p.get('replaced_by') or 'a newer record'} · {where}{link}",
        )
    if t == "proposal.pending":
        return (
            f"Review needed: {p.get('summary') or p.get('kind')}",
            f"{p.get('rationale', '')}{link}".strip(),
        )
    if t == "conflict.flagged":
        return (
            f"Conflict needs your decision: {p.get('name')}",
            f"A different version of an established record is waiting.{link}",
        )
    if t == "core.budget_exceeded":
        return (
            "Core memory is over budget",
            f"Over by about {p.get('over_by')} tokens; {p.get('proposals')} demotion(s) proposed.{link}",
        )
    if t == "inbox.new":
        return f"New in your inbox: {p.get('title')}", f"From {p.get('source')}{link}"
    if t == "digest.weekly":
        lines = [
            f"{p.get('new', 0)} new, {p.get('changed', 0)} changed, {p.get('superseded', 0)} replaced, {p.get('went_stale', 0)} went stale",
            f"{p.get('pending_proposals', 0)} proposal(s) and {p.get('inbox_waiting', 0)} inbox item(s) waiting",
        ]
        if p.get("new_names"):
            lines.append("New: " + ", ".join(p["new_names"]))
        return "Your memory this week", "\n".join(lines) + link
    if t == "plugin.failed":
        return f"Plugin problem: {p.get('instance')}", f"{p.get('error')}{link}"
    if t == "plugin.test":
        return "Memory hub test", f"{p.get('message', 'Test notification.')}{link}"
    return t, str(p)
