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
- Not a full session/transcript archive. Raw chat logs are the _input_ to a dream cycle,
  not the memory itself — the point of dreaming is to compress them away.
- Not a replacement for git history, code comments, or project docs. Memory records
  hold things that aren't derivable by reading the code or the commit log (see
  "What NOT to remember" below).

## Vocabulary

**Scope** — who/what a memory record is visible to. Scopes nest from narrowest to
broadest and are the primary defense against cross-talk:

| Scope     | Lives                                                   | Committed to git?            | Example                                                            |
| --------- | ------------------------------------------------------- | ---------------------------- | ------------------------------------------------------------------ |
| `session` | in-memory / a single chat only                          | never                        | "user is debugging the flaky test right now"                       |
| `project` | `memory/data/project/` in this repo                     | yes                          | "this service's retries must be idempotent — see incident 2026-02" |
| `team`    | `memory/data/team/` in this repo, or a shared team repo | yes (opt-in)                 | "we use trunk-based dev, no long-lived feature branches"           |
| `user`    | outside any repo, e.g. `~/.ai-memory/`                  | no (personal, machine-local) | "prefers terse responses, ten years of Go experience"              |

A record's scope is a fact about _where it's allowed to be read from_, not about who
wrote it. A user-scope fact can be learned while working in a specific project, but it
only becomes user-scope once the dream cycle promotes it there — see "Promotion" below.

**Type** — what kind of content the record holds, independent of scope. The first four
are reused from the memory taxonomy proven out in Claude Code's own memory tool; `intent`
and `rule` extend it for this project:

- `user` — role, expertise, preferences, how they like to work.
- `feedback` — corrections and confirmations about _how to do the work_ ("don't mock
  the DB in integration tests," "yes, one bundled PR was right here").
- `project` — decisions, ongoing initiatives, deadlines, incident context that isn't
  derivable from the code itself.
- `reference` — pointers to where live information already lives (a Linear project, a
  Grafana dashboard, a Slack channel) — not the information itself.
- `intent` — a goal or direction being worked toward ("migrating off the legacy auth
  service by Q3"), distinct from a fact that's already settled — records the _why we're
  heading this way_, so future sessions don't optimize for a direction that's since changed.
- `rule` — a constraint stated forcefully enough that it isn't just a preference ("never
  merge to `main` without a green build"). A rule that's only been observed once is
  really just `feedback`; `rule` is for things the user or team wants to hold the line on.

**Confidence** — a third, independent dimension: how much evidence it should take to
change a record. See "Confidence & mutability" below.

Every record's frontmatter carries type, scope, and confidence, plus enough metadata to
keep partitions honest:

```markdown
---
name: idempotent-retries
description: Retries on the payments webhook must be idempotent — replay caused a double-charge incident.
metadata:
  type: project
  scope: project
  confidence: confirmed
  project_id: ai-core-memory # or whichever repo/project this belongs to
  created: 2026-02-11
  source: dream-cycle
---

Retry logic on the payments webhook handler must be idempotent.

**Why:** a replayed webhook double-charged a customer in incident INC-1042 (2026-02-10).
**How to apply:** any new retry path on that handler needs an idempotency key check
before this can be closed as fixed.
```

See [`memory/schema/`](../memory/schema/) for one template per type.

## Confidence & mutability

A memory record is what we currently believe is true or best — not a fixed fact. Some
beliefs are cheap to revise; others should take real evidence to overturn. Every record
carries a `confidence` tier that governs how much friction the dream cycle applies
before changing it:

| Confidence    | Meaning                                                          | To change it                                                                                                                                             |
| ------------- | ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `observed`    | Seen once, or inferred rather than stated outright.              | The next dream cycle can freely revise or drop it — no confirmation needed.                                                                              |
| `confirmed`   | The user stated it directly, or it recurred across sessions.     | Can be updated by a later dream cycle, but the pass must call it out in its summary rather than changing it silently.                                    |
| `established` | Reinforced repeatedly, or explicitly marked durable by the user. | Requires explicit human confirmation before being changed or removed, even if new information seems to contradict it — flag it and ask, don't overwrite. |

New records default to `observed`. A record earns `confirmed` when the same fact is
independently reinforced in a later session, and `established` only when the user
explicitly says it should be treated as settled — still reversible, just never silently.

This tier is orthogonal to scope and type: an `established` record can exist at any
scope, and a `rule` doesn't automatically outrank a `feedback` record — confidence is
what governs mutability, not type. It's also distinct from git history: git tells you
_when_ a record changed, confidence tells you _how much it should take_ to change it again.

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

```text
harvest → classify → merge/dedupe → flag conflicts → promote (human-gated) → prune → reindex
```

1. **Harvest** — gather the session's residue: what was decided, corrected, learned, or
   discovered, plus any existing memory index so the pass knows what's already known.
2. **Classify** — sort fragments by type (user/feedback/project/reference/intent/rule)
   and by the scope they were learned in. Assign an initial confidence too: `observed`
   by default, `confirmed` if the user stated it directly or it corroborates an existing
   record. `rule` and `intent` records usually start at `confirmed` or higher — they're
   deliberately stated, not merely noticed.
3. **Merge/dedupe** — check each fragment against existing records at the same scope.
   Update in place rather than duplicating; a memory system that only ever appends is
   not a memory system, it's a log.
4. **Flag conflicts** — if new information contradicts an existing record, treat it
   according to that record's confidence tier (see "Confidence & mutability"):
   `observed` records can be updated in place, `confirmed` records can be updated but the
   pass must say so in its summary, and `established` records must never be silently
   changed — surface the conflict and let the user decide. Memory that's silently wrong
   is worse than no memory.
5. **Promote (human-gated)** — if something learned at `project` scope looks like it
   actually belongs at `team` or `user` scope (a working-style preference surfaced while
   debugging, say), propose the promotion; don't execute it without confirmation.
6. **Prune** — remove or mark stale records that later information has invalidated.
7. **Reindex** — keep a short index (mirroring `MEMORY.md` in Claude Code's own memory
   tool) so a future session can see what exists without reading every file.

## Memory hub (cross-project store)

Per-repo `memory/data/` answers "what does this project know." It doesn't answer "what
have I learned across every project I work in" — that requires seeing across repos you
may not even have cloned side by side, which is what the **memory hub** is for.

The hub is a small, self-hosted service — not a multi-tenant cloud product — holding a
**replicated, queryable copy** of project- and team-scope records (and any user-scope
records a person opts to sync), aggregated from every repo that pushes to it. It is
deliberately a copy, not the source of truth: every record it holds also exists as a
plain markdown file in some repo's `memory/data/`, so losing the hub loses convenience,
not data — it can be rebuilt by re-syncing from the repos that feed it.

**Stack: Python + SQLite.** Python because it's the language every AI/MCP tooling
ecosystem already speaks natively — the dream skill's classification logic, the MCP SDK,
and any future ML-assisted dedupe all live in the same runtime with no cross-language
glue. FastAPI serves both the human-facing REST API and the MCP endpoint (via the
official MCP Python SDK's ASGI transport) from one process, backed by SQLite through
SQLModel (SQLAlchemy + Pydantic, so the same models validate API input and hit the DB)
with Alembic for schema migrations. This isn't a big-data problem — a household's or
team's memory store is thousands of small records, not millions — so SQLite's limits
are never the binding constraint; Postgres remains a drop-in swap later via the same
SQLAlchemy layer if that ever changes.

### Data model

| Table              | Purpose                                                                                                                                                                             |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `users`            | id, email, password_hash, is_admin, created_at.                                                                                                                                     |
| `teams`            | id, name, slug, created_at.                                                                                                                                                         |
| `team_members`     | team_id, user_id, role (`owner`/`member`) — who's on a team, opt-in per "Team scope is opt-in."                                                                                     |
| `projects`         | id, slug, team_id (nullable), owner_user_id (nullable) — a project belongs to a team or a single person, never both.                                                                |
| `api_tokens`       | id, user_id, token_hash, label, created_at, last_used_at, revoked_at — the raw token is shown once at creation and only its hash is stored, the same pattern as a GitHub PAT.       |
| `memory_records`   | id, scope, type, confidence, name, description, body (markdown), project_id/team_id/user_id (whichever applies to its scope), created_at, updated_at, source.                       |
| `memory_revisions` | id, memory_record_id, body snapshot, confidence, changed_by, changed_at, change_note, change_source (`dream-cycle`/`mcp-write`/`import`) — the append-only history mentioned above. |

### Access control

Same partition rules as the rest of this document, just enforced in a multi-user
setting instead of by file location:

- **User-scope records are private to their owner**, full stop — not even a team or
  workspace admin can browse another user's personal memory by default. Admin rights
  cover accounts, teams, and projects; they are not a backdoor into personal scope. This
  is the same principle as "user scope never enters a shared repo," just enforced at the
  database layer instead of by `.gitignore`.
- **Team-scope records are visible/writable to that team's members** (`team_members`),
  matching "team scope is opt-in" — joining a team is what grants access, nothing implicit.
- **Project-scope records** are visible/writable to a project's team (if `team_id` is
  set) or its individual owner (if it's a personal project).
- **`admin` is a platform role**, not a memory-access override — it manages users, teams,
  and tokens, and can see project/team scope for teams it's actually a member of, same as
  anyone else.

### API surface

Two audiences, two auth methods, one process:

- **Human REST API (session-cookie auth)** — for people, not AI clients: login/logout,
  team and membership management, minting/revoking API tokens (this is how a human
  hands an AI client credentials), and browsing memories with their revision history so
  a person can actually review what's been remembered about them. `GET/POST /teams`,
  `GET/POST /tokens`, `GET /memories`, `GET /memories/{id}`, admin-only `GET/POST/DELETE
  /users`.
- **MCP surface (bearer API-token auth)** — for AI clients, and the only path meant for
  routine writes: `memory.search`, `memory.write`, `memory.sync` (bulk upsert from a
  repo's `memory/data/` after a dream pass), `memory.consolidate`. The hub stays
  deliberately "dumb": it enforces access control and the confidence-tier mutation rule
  server-side, but the actual classification/merge _reasoning_ stays in the `dream`
  skill running client-side, in whatever AI tool is driving it. That keeps the hub
  free of any dependency on a specific model or vendor.

### Import / export — backup only, never the routine write path

`POST /memories/import` and `GET /memories/export` move records in and out as the same
markdown + YAML frontmatter used everywhere else in this repo — for backing up the
store, seeding a fresh instance, or migrating between machines. They are explicitly
**not** meant as a routine editing path: the whole point of this project is that memory
changes happen through an AI reasoning about what's worth remembering, not through a
human hand-editing rows or files directly. To keep that true even during import, an
imported record that conflicts with an existing one still goes through the same
confidence-tier check as any other write (an import can't silently clobber an
`established` record any more than a careless MCP call can) — it lands as a flagged
revision for a human to resolve, not a blind overwrite. Both endpoints require
project/team ownership or admin rights.

### Deployment stance

Self-hosted by the person or team that owns the memory, sized for one person or one
team, not a shared multi-tenant SaaS product — that's the point of "own everything":
your memory shouldn't live somewhere you don't control. A single Python process plus one
SQLite file is the whole deployment; Docker is a convenience, not a requirement.

This section is architecture, not implementation yet — see Roadmap.

## Instruction-file placement across tools

Most AI coding tools already read a repo-local instructions file (`CLAUDE.md`,
`.cursor/rules/`, `.windsurfrules`, `.github/copilot-instructions.md`, or the emerging
generic `AGENTS.md`), and that maps directly onto **project scope** — it's exactly what
this repo already does with `CLAUDE.md` and `AGENTS.md`. No new mechanism needed there.

**User scope is where tools genuinely differ**, and mostly don't offer a file at all:
Claude Code reads a personal `~/.claude/CLAUDE.md` in addition to the repo-local one —
which conveniently is _already_ the project-vs-user split this document defines, just
expressed as two files instead of two records. Other tools (ChatGPT's custom
instructions, Copilot's account settings, most IDE-level AI settings) keep user-level
preferences in account settings, not a file at all, so there's nothing on disk for a
sync mechanism to target.

Given that, the practical approach is: don't chase every vendor's proprietary settings
surface automatically. Keep the canonical record in the hub / per-repo markdown, and
generate a specific tool's instruction file **on request, per tool, when it's actually
useful** — a "compile" step, not a background sync (see Roadmap). Where a tool has
nothing file-based to target (ChatGPT, Copilot account settings), the honest answer is
that user-scope memory reaches it through that tool's own MCP support querying the hub
directly, not through a materialized file.

## Interoperability strategy

Ordered by how much is built vs. planned:

1. **Plain files (today).** Markdown + YAML frontmatter under `memory/`. Any assistant
   with filesystem or repo access can read these with zero integration work.
2. **Claude Skills (today).** `.claude/skills/dream/SKILL.md` packages the dream-cycle
   instructions so Claude Code and Claude.ai can run it on request.
3. **Instruction-file adapters (today, thin).** `CLAUDE.md` for Claude, `AGENTS.md` as
   a pointer for other coding assistants that check that convention (Cursor, Windsurf,
   etc.) — both just point back at this document and `memory/schema/`.
4. **Memory hub + MCP (roadmap).** The self-hosted hub described above, reachable via
   MCP tools — the way a non-filesystem AI client, or a client working in a project that
   hasn't cloned every other project, gets access to memory beyond its own repo. See
   [docs/DEPLOYMENT.md](DEPLOYMENT.md) for how it's meant to run (Docker/Podman/k3s), and
   [docs/DISTRIBUTION.md](DISTRIBUTION.md) for how an AI client actually connects to it —
   a Claude Code plugin, a manual `claude mcp add`, or a Claude.ai custom connector.
5. **Personal cross-machine sync (roadmap, optional).** If a hub isn't running, plain
   `user` scope can still sync across a person's own machines the simple way — a
   personal git repo they own. The hub is for cross-project aggregation; it isn't
   required just to carry personal memory between two laptops.

## Roadmap

- [ ] Memory hub service: FastAPI + SQLModel + SQLite, per "Memory hub" above — stack is
      decided; schema, auth, and endpoints still need an actual implementation
- [ ] Users/teams/membership + admin role, with API-token issuance for MCP clients
- [ ] MCP surface (`memory.search` / `memory.write` / `memory.sync` / `memory.consolidate`)
      with server-side confidence-tier enforcement
- [ ] Import/export endpoints for backup/restore (markdown + frontmatter), gated to
      project/team owners and admins, still routed through confidence-tier conflict checks
- [ ] Container image + docker-compose per [docs/DEPLOYMENT.md](DEPLOYMENT.md); k3s/k8s
      manifests are documented but genuinely optional
- [ ] Claude Code plugin (`.claude-plugin/`) per [docs/DISTRIBUTION.md](DISTRIBUTION.md)
      — blocked on confirming the current plugin skills-path convention first
- [ ] `hub-sync` skill or process to push repo records to the hub and pull cross-project
      context back into a session
- [ ] Confidence-tier enforcement wired into the `dream` skill's conflict handling
      (documented above; the skill already assigns and checks tiers manually)
- [ ] Reference implementation of the dream pipeline outside a single chat session
      (e.g. run against exported transcripts from multiple tools in one pass)
- [ ] Promotion workflow with an explicit human approval step (not just "ask in chat")
- [ ] On-request "compile" step to generate a specific tool's instruction file
      (`.cursor/rules/`, `.windsurfrules`, etc.) from hub/repo memory
- [ ] Adapter notes for at least one non-Claude assistant that supports MCP
