# Architecture

This document is the source of truth for the two ideas everything else in this repo
depends on: the **scope/type model** for a memory record, and the **dream cycle** that
produces and maintains them. If you're proposing a change to either, start here.

## Goals

- Memory that survives past a single chat, code session, or tool.
- Memory that can be shared deliberately (a team convention, a project decision) without
  leaking things that shouldn't travel (one user's personal habits, one project's
  internal details bleeding into another).
- A storage format any AI platform can read, not just Claude — plain files first,
  protocol adapters (Skills, MCP) on top.

## Non-goals

- Not a vector database or embedding-based retrieval layer. Memory here is small,
  curated, and human-legible by design — retrieval is "read the relevant files," not
  semantic search over a large corpus. Nothing stops a future MCP server from adding a
  search index on top of these files, but the source of truth stays plain text.
- Not a full session/transcript archive. Raw chat logs are the *input* to a dream cycle,
  not the memory itself — the point of dreaming is to compress them away.
- Not a replacement for git history, code comments, or project docs. Memory records
  hold things that aren't derivable by reading the code or the commit log (see
  "What NOT to remember" below).

## Vocabulary

**Scope** — who/what a memory record is visible to. Scopes nest from narrowest to
broadest and are the primary defense against cross-talk:

| Scope     | Lives                                   | Committed to git? | Example |
|-----------|------------------------------------------|--------------------|---------|
| `session` | in-memory / a single chat only           | never              | "user is debugging the flaky test right now" |
| `project` | `memory/data/project/` in this repo      | yes                | "this service's retries must be idempotent — see incident 2026-02" |
| `team`    | `memory/data/team/` in this repo, or a shared team repo | yes (opt-in) | "we use trunk-based dev, no long-lived feature branches" |
| `user`    | outside any repo, e.g. `~/.ai-memory/`   | no (personal, machine-local) | "prefers terse responses, ten years of Go experience" |

A record's scope is a fact about *where it's allowed to be read from*, not about who
wrote it. A user-scope fact can be learned while working in a specific project, but it
only becomes user-scope once the dream cycle promotes it there — see "Promotion" below.

**Type** — what kind of content the record holds, independent of scope. Reused from the
memory taxonomy proven out in Claude Code's own memory tool:

- `user` — role, expertise, preferences, how they like to work.
- `feedback` — corrections and confirmations about *how to do the work* ("don't mock
  the DB in integration tests," "yes, one bundled PR was right here").
- `project` — decisions, ongoing initiatives, deadlines, incident context that isn't
  derivable from the code itself.
- `reference` — pointers to where live information already lives (a Linear project, a
  Grafana dashboard, a Slack channel) — not the information itself.

Every record's frontmatter carries both fields, plus enough metadata to keep partitions
honest:

```markdown
---
name: idempotent-retries
description: Retries on the payments webhook must be idempotent — replay caused a double-charge incident.
metadata:
  type: project
  scope: project
  project_id: ai-core-memory        # or whichever repo/project this belongs to
  created: 2026-02-11
  source: dream-cycle
---

Retry logic on the payments webhook handler must be idempotent.

**Why:** a replayed webhook double-charged a customer in incident INC-1042 (2026-02-10).
**How to apply:** any new retry path on that handler needs an idempotency key check
before this can be closed as fixed.
```

See [`memory/schema/`](../memory/schema/) for one template per type.

## What NOT to remember

Same discipline as any good memory system — if it's cheap to re-derive, don't store it:

- Code structure, conventions, file layout — read the repo.
- Who changed what and when — `git log` / `git blame`.
- Bug fixes and how they were solved — the diff and commit message have that.
- Anything already written down in a `CLAUDE.md` / `AGENTS.md` / project doc.
- In-progress task state — that's what a todo list or a plan is for, not memory.

## Partition & cross-talk rules

1. **Read precedence is narrow-wins.** When multiple scopes have something to say,
   session > project > team > user — the more specific context overrides the general
   one, the same way git config resolves local > global > system.
2. **Nothing is promoted automatically.** A dream cycle running inside Project A never
   writes to Project B's `memory/data/project/`, and never writes to `team` or `user`
   scope without flagging the promotion for a human to confirm. Cross-talk is a one-way
   door risk (a leaked project detail can't be un-leaked), so the default is to keep a
   fact at the narrowest scope it was learned in.
3. **User scope never enters a shared repo.** It lives outside git entirely (a
   machine-local directory), because it's about the person, not the project — a shared
   `ai-core-memory` repo used by a team should never accumulate one person's personal
   working style as if it were project fact. `memory/data/user/` and
   `memory/data/session/` are gitignored for exactly this reason.
4. **Team scope is opt-in, not default.** Promoting a project-level finding to
   `team` means "everyone on every project this team touches should know this" — that's
   a deliberate, occasional action, not something every dream cycle should do on its own.

## The dream cycle

The consolidation pass that turns short-term residue into the records above. Implemented
today as a Claude Skill ([`.claude/skills/dream/SKILL.md`](../.claude/skills/dream/SKILL.md))
that runs inside a single conversation; the pipeline it follows is platform-agnostic and
is meant to be re-implemented by other adapters later (see Roadmap).

```
harvest → classify → merge/dedupe → flag conflicts → promote (human-gated) → prune → reindex
```

1. **Harvest** — gather the session's residue: what was decided, corrected, learned, or
   discovered, plus any existing memory index so the pass knows what's already known.
2. **Classify** — sort fragments by type (user/feedback/project/reference) and by the
   scope they were learned in.
3. **Merge/dedupe** — check each fragment against existing records at the same scope.
   Update in place rather than duplicating; a memory system that only ever appends is
   not a memory system, it's a log.
4. **Flag conflicts** — if new information contradicts an existing record, surface it
   rather than silently overwriting. Memory that's silently wrong is worse than no memory.
5. **Promote (human-gated)** — if something learned at `project` scope looks like it
   actually belongs at `team` or `user` scope (a working-style preference surfaced while
   debugging, say), propose the promotion; don't execute it without confirmation.
6. **Prune** — remove or mark stale records that later information has invalidated.
7. **Reindex** — keep a short index (mirroring `MEMORY.md` in Claude Code's own memory
   tool) so a future session can see what exists without reading every file.

## Interoperability strategy

Ordered by how much is built vs. planned:

1. **Plain files (today).** Markdown + YAML frontmatter under `memory/`. Any assistant
   with filesystem or repo access can read these with zero integration work.
2. **Claude Skills (today).** `.claude/skills/dream/SKILL.md` packages the dream-cycle
   instructions so Claude Code and Claude.ai can run it on request.
3. **Instruction-file adapters (today, thin).** `CLAUDE.md` for Claude, `AGENTS.md` as
   a pointer for other coding assistants that check that convention (Cursor, Windsurf,
   etc.) — both just point back at this document and `memory/schema/`.
4. **MCP server (roadmap).** A server exposing `memory.search`, `memory.write`, and
   `memory.consolidate` as tools, backed by the same on-disk format, so any MCP client —
   not just Claude — can read and write the store without repo/filesystem access.
5. **Sync/merge across machines (roadmap).** Team- and project-scope memory already
   travels via the repo's own git remote. User-scope memory is machine-local by design
   today; syncing it across a person's own machines is a later, separate problem
   (likely another git repo the user owns, not this one).

## Roadmap

- [ ] MCP server (`memory.search` / `memory.write` / `memory.consolidate`)
- [ ] Reference implementation of the dream pipeline outside a single chat session
      (e.g. run against exported transcripts from multiple tools in one pass)
- [ ] Promotion workflow with an explicit human approval step (not just "ask in chat")
- [ ] Adapter notes for at least one non-Claude assistant that supports MCP
