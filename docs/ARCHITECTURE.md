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

**Tier** — a fourth: whether a record is `core` (loaded into every session) or
`associated` (pulled in only when the task calls for it). See "Core vs. associated".

**Status** — where a record is in its life: `active`, `superseded`, `stale`, or
`archived`. See "How memory changes over time".

Every record's frontmatter carries type, scope, confidence, tier, and status, plus
enough metadata to keep partitions honest and to let records relate to each other:

```markdown
---
name: idempotent-retries
description: Retries on the payments webhook must be idempotent — replay caused a double-charge incident.
metadata:
  id: 01JA3Z8K2M5Q7R9T1V3X5Y7Z9B # assigned by the hub/first dream pass; stable across renames
  type: project
  scope: project
  confidence: confirmed
  tier: associated # core | associated
  status: active # active | superseded | stale | archived
  project_id: ai-core-memory # or whichever repo/project this belongs to
  topics: [payments, webhooks, reliability]
  links: [01JA3Z8K2M5Q7R9T1V3X5Y7Z9C] # related records (ids) — "see also", not hierarchy
  supersedes: [] # ids this record replaced, if any
  created: 2026-02-11
  last_reinforced: 2026-03-02
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

## Core vs. associated

Not every memory deserves a place in every conversation. `tier` splits records into two
loading behaviors:

- **`core`** — loaded at the start of _every_ session in scope, no query needed: who the
  user is, the rules that must hold everywhere, the one or two project facts that change
  how all work gets done. Core is deliberately small and has a **hard token budget**
  (instance setting `core_token_budget`, default ≈2,000 tokens, counted approximately as
  characters ÷ 4 — no tokenizer dependency) because it costs context on every turn.
- **`associated`** — everything else. Retrieved by task relevance via
  [task focus](#task-focus), or fetched by id/link. Most records live here.

`tier` is orthogonal to `type` and `confidence`: a `rule` is _usually_ core but doesn't
have to be, and a core record can still be merely `observed`. Two guardrails keep core
from becoming a junk drawer:

1. **Core has a budget, so promotion is a trade.** Promoting a record into a full core
   set means demoting another — the proposal says which.
2. **Promotion to core is human-gated**, like scope promotion, for the same reason: it's
   a standing cost on every future session. The dream cycle may _propose_
   `promote_core` / `demote_core`; it never applies one on its own.

Core is resolved with the same narrow-wins precedence as everything else: a session
loads the core records of its user, its team(s), and its project, project winning
conflicts.

## Task focus

"Focus" is how a session gets the _right_ associated memories for what it's doing now,
instead of everything or nothing. It's a retrieval contract, not a storage feature, so
it works over plain files too (read core, read the scope `MEMORY.md` indexes, open
what's relevant) — the hub just does it faster and with ranking.

`memory.focus(task, project?, topics?, budget?, include_other_projects?)` returns a **context pack**:

1. All visible `core` records (always, within the core budget).
2. Candidate `associated` records, `status = active` only, found by SQLite FTS5 match of
   the task text against name/description/body, plus any records tagged with the given
   or inferred `topics`, plus a one-hop expansion across `links` from core records and
   top hits. **With a `project` in play, other projects' `project`-scope records stay out**
   unless `include_other_projects` is set — the cross-talk rule applies to retrieval too.
3. Candidates ranked by **relevance × scope weight (narrow wins) × confidence weight ×
   reinforcement/recency weight**, then packed to the budget (default ≈4,000 tokens):
   full body for the top few, `name + description` only for the rest (progressive
   disclosure — the client calls `memory.get(id)` for anything it wants in full).
4. Each returned record carries a short `why` (which match, link, or topic pulled it in),
   so focus is explainable and debuggable — the web UI's "Focus preview" runs this exact
   function so a person can see what an AI would be handed for a given task.

The core budget is enforced when a record is promoted, against the core records visible to
the person doing the promoting — exact per-session accounting across user, team, and project
core arrives with the Phase 2 proposal flow; focus reports (rather than hides) an
over-budget core set. Two deliberate limits. **No embeddings in the core path** — consistent with the
non-goals above; FTS5 + topics + links is enough for thousands of curated records, and a
vector index can arrive later as a [plugin](PLUGINS.md), not a dependency. And
**retrieval is not reinforcement**: `last_retrieved` is recorded (it feeds staleness
detection) but being served to a session never raises confidence — otherwise whatever
gets retrieved first becomes ever more "true" just by being retrieved.

## How memory changes over time

The premise: _both of us change as we learn._ A memory system that accretes without
revising is a log. So change is a first-class, visible part of the model, not an
edge case:

- **Reinforcement.** When a later session independently re-establishes a fact, the
  record's `last_reinforced` is bumped and its `reinforcement_count` incremented
  (tracked per distinct `source_ref`, so one chatty session can't reinforce itself).
  Two independent reinforcements promote `observed` → `confirmed`, matching the tier
  definitions above. `established` still requires the user to say so.
- **Supersession, not deletion.** When something genuinely changes — a decision
  reversed, a preference outgrown — the new record carries `supersedes: [old-id]`; the
  old record becomes `status: superseded` with a `valid_to` date and drops out of focus
  but stays in history. This is what lets the UI show _how a belief evolved_ ("used to
  prefer X, since March prefers Y") instead of only its latest value. It applies to
  records about the user and to records about how the AI should work (`feedback`,
  `rule`) alike.
- **Decay.** Unreinforced records go `stale`, scaled to confidence: `observed` after
  `stale_after_days.observed` (default 90), `confirmed` after
  `stale_after_days.confirmed` (default 365). `observed` records are marked stale
  automatically (logged as a revision, reversible, visible in the UI); `confirmed`
  records produce a `mark_stale` _proposal_ instead; `established` records never decay
  but produce a periodic `review_established` proposal ("still true?") — never a silent
  change. `stale` records are excluded from focus until revived or archived.
- **Everything is revisioned.** Every change — by a dream pass, an MCP write, a person
  in the UI, an import — lands in `memory_revisions` with who/what/why. Nothing is
  overwritten without a trail.

Status is a lifecycle, confidence is a mutability gate, and they interact only as
described above: status says whether a record is _in play_; confidence says how hard it
is to _change_.

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
   Also drains the **inbox**: raw snippets, ideas, and notes captured outside a chat
   (quick-capture in the UI, an MCP `inbox.add`, or a note-system
   [plugin](PLUGINS.md) such as Obsidian or Memos). Inbox items are _input_, never
   memory themselves — each is classified into a record, turned into a `reference`
   pointer back to where it lives, or dismissed.
2. **Classify** — sort fragments by type (user/feedback/project/reference/intent/rule)
   and by the scope they were learned in. Assign an initial confidence too: `observed`
   by default, `confirmed` if the user stated it directly or it corroborates an existing
   record. `rule` and `intent` records usually start at `confirmed` or higher — they're
   deliberately stated, not merely noticed.
3. **Merge/dedupe** — check each fragment against existing records at the same scope.
   Update in place rather than duplicating; a memory system that only ever appends is
   not a memory system, it's a log. A fragment that re-establishes an existing record
   _reinforces_ it (see "How memory changes over time"); one that replaces it
   _supersedes_ it. Also assign `topics` and `links` so the record can be found by
   [task focus](#task-focus) later.
4. **Flag conflicts** — if new information contradicts an existing record, treat it
   according to that record's confidence tier (see "Confidence & mutability"):
   `observed` records can be updated in place, `confirmed` records can be updated but the
   pass must say so in its summary, and `established` records must never be silently
   changed — surface the conflict and let the user decide. Memory that's silently wrong
   is worse than no memory.
5. **Promote (human-gated)** — if something learned at `project` scope looks like it
   actually belongs at `team` or `user` scope (a working-style preference surfaced while
   debugging, say), propose the promotion; don't execute it without confirmation.
6. **Prune** — supersede, mark stale, or archive records that later information has
   invalidated. Prefer supersession over deletion so the history of _how a belief
   changed_ survives.
7. **Reindex** — keep a short index (mirroring `MEMORY.md` in Claude Code's own memory
   tool) so a future session can see what exists without reading every file. Core
   records are marked in the index so a session knows what to load unconditionally.

### Who does the reasoning: the proposal queue

Consolidation splits into two kinds of work, and the split is what keeps the hub
vendor-neutral while still letting memory consolidate on its own schedule:

- **Mechanical** — needs no model: reinforcement counting, decay/staleness, duplicate
  _candidates_ (FTS similarity), conflict detection against confidence tiers, core
  budget checks. The hub runs this as a scheduled job (default nightly, also runnable
  on demand from the UI or CLI).
- **Judgement** — needs a model: is this a true duplicate or two related facts? Is this
  fragment a rule or a preference? What should the merged record say? This stays
  _outside_ the hub: the `dream` skill (or any MCP client with sampling) calls
  `memory.consolidate` to pull a work package of candidates, reasons over them, and
  writes back results.

Both produce **proposals** rather than direct edits, in a queue a human reviews (web UI
or `acm review`, no AI involved). A proposal has a `kind` — `merge`, `supersede`,
`promote_scope`, `promote_core`, `demote_core`, `mark_stale`, `archive`,
`review_established`, `conflict` — the records it touches, a proposed diff, and a
rationale. Approving one applies it through the same server-side confidence-tier
enforcement as any other write; low-risk `observed`-only proposals may be set to
auto-apply per instance, but anything touching `confirmed`/`established` records, scope,
or core always waits for a person. A hub with no model attached still works — it just
produces only the mechanical proposals.

## Memory hub (cross-project store)

Per-repo `memory/data/` answers "what does this project know." It doesn't answer "what
have I learned across every project I work in" — that requires seeing across repos you
may not even have cloned side by side, which is what the **memory hub** is for.

The hub is a **self-hostable platform**: one small service you run yourself, holding a
**replicated, queryable copy** of project- and team-scope records (and any user-scope
records a person opts to sync), aggregated from every repo that pushes to it, plus the
inbox, proposals, and plugin machinery around them. It is deliberately a copy, not the
source of truth for the plain-file format: every record it holds can also exist as a
plain markdown file, so losing the hub loses convenience, not data — it can be rebuilt
from an export or by re-syncing from the repos that feed it (see
[Import / export](#import--export--offline-human-operated)).

**Self-host first, scale second.** The default deployment is one container, one SQLite
file, one person or one small team. Nothing in the design requires a cloud service, and
nothing is built that only makes sense at scale. People who want to scale have a
documented path (Postgres via the same SQLAlchemy layer, see
[docs/DEPLOYMENT.md](DEPLOYMENT.md)) — but that's an option, never the baseline.

**Stack: Python + SQLite.** Python because it's the language every AI/MCP tooling
ecosystem already speaks natively — the dream skill's classification logic, the MCP SDK,
and any future ML-assisted dedupe all live in the same runtime with no cross-language
glue. FastAPI serves the human REST API, the web UI, and the MCP endpoint (via the
official MCP Python SDK's ASGI transport) from one process, backed by SQLite (WAL mode,
FTS5 for search) through SQLModel (SQLAlchemy + Pydantic, so the same models validate
API input and hit the DB) with Alembic for schema migrations. The web UI is
server-rendered (Jinja2 + HTMX, no JavaScript build step) — see
[docs/UI.md](UI.md). This isn't a big-data problem — a household's or team's memory
store is thousands of small records, not millions — so SQLite's limits are never the
binding constraint.

### Data model

| Table                | Purpose                                                                                                                                                                                                                                                         |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `instance_settings`  | singleton row: `deployment_mode` (`solo`/`team`/`multi_team`), enabled auth providers, default token expiry, `core_token_budget`, `stale_after_days`, auto-apply policy for proposals.                                                                          |
| `users`              | id, email, password_hash (nullable if OIDC-only), auth_provider, external_id, is_admin, theme_preference (`system`/`light`/`dark`), created_at.                                                                                                                 |
| `teams`              | id, name, slug, created_at.                                                                                                                                                                                                                                     |
| `team_members`       | team_id, user_id, role (`owner`/`member`) — who's on a team, opt-in per "Team scope is opt-in."                                                                                                                                                                 |
| `projects`           | id, slug, team_id (nullable), owner_user_id (nullable), visibility (`private`/`team`/`public`) — see SECURITY.md § Visibility.                                                                                                                                  |
| `api_tokens`         | id, user_id, token_hash, prefix, label, project_ids (scope; empty = all the user's projects), include_user_scope, access_level (`read_only`/`read_write`), expires_at, created_at, last_used_at, revoked_at.                                                                                                                 |
| `memory_records`     | id, scope, type, confidence, **tier**, **status**, name, description, body (markdown), **topics**, project_id/team_id/user_id (whichever applies to its scope), **valid_from/valid_to**, **last_reinforced**, **reinforcement_count**, **last_retrieved**, created_at, updated_at, source. |
| `memory_links`       | from_id, to_id, kind (`related`/`supersedes`) — the graph behind `links` and `supersedes`.                                                                                                                                                                      |
| `memory_revisions`   | id, memory_record_id, snapshot (name, description, body, type, scope, confidence, tier, status, topics), changed_by_user_id, changed_by_token_id, changed_by_label (OS user for CLI), changed_at, change_note, flagged, applied (false = a pending import/sync conflict awaiting a human), change_source (`dream-cycle`/`mcp-write`/`import`/`ui`/`cli`/`plugin`/`mechanical`).                                           |
| `web_sessions`       | id (SHA-256 of the cookie value), user_id, csrf_token, created_at, authenticated_at, last_seen_at, expires_at — server-side login sessions for the web UI (never used for token routes).                                                                                                                                          |
| `auth_flows`         | state_hash, nonce, code_verifier, binding_hash, reauth, created_at — short-lived in-flight OIDC logins (state/nonce/PKCE), single use.                                                                                                                                                                                                    |
| `inbox_items`        | id, source (`mcp`/`ui`/`cli`/`plugin:<key>`), scope, project_id, title, body, external_ref (URL/path in the originating system), captured_at, status (`new`/`harvested`/`dismissed`).                                                                           |
| `proposals`          | id, kind, target_record_ids, proposed_diff, rationale, generated_by (`mechanical`/`dream-skill`/`llm-worker`), status (`pending`/`approved`/`rejected`/`applied`/`expired`), decided_by_user_id, decided_at.                                                     |
| `plugin_instances`   | id, plugin_key, name, kind (`source`/`sink`/`both`), non-secret config, secret references (env var names, never values), enabled, scope allowlist, event filter, last_run_at, last_status. See [docs/PLUGINS.md](PLUGINS.md).                                  |
| `events`             | id, type, payload (minimal — ids and titles, not bodies), created_at — the outbox every sink plugin reads from.                                                                                                                                                 |
| `plugin_deliveries`  | event_id, plugin_instance_id, status, attempts, next_attempt_at, last_error — reliable, retryable, per-plugin delivery.                                                                                                                                         |

### Access control

Same partition rules as the rest of this document, just enforced in a multi-user
setting instead of by file location. Full detail — token security, the visibility
model behind `solo`/`team`/`multi_team` deployments, and exactly what `admin`/`owner`/
`member` can and can't do — lives in [docs/SECURITY.md](SECURITY.md); the essentials:

- **User-scope records are private to their owner**, full stop — not even an instance
  admin can browse another user's personal memory by default. This is the same
  principle as "user scope never enters a shared repo," just enforced at the database
  layer instead of by `.gitignore`.
- **Team-scope records are visible/writable to that team's members** (`team_members`),
  matching "team scope is opt-in" — joining a team is what grants access, nothing implicit.
- **Project-scope records** are visible/writable per the project's `visibility`
  (`private`/`team`/`public`) and its owning team or individual owner — see
  [docs/SECURITY.md § Visibility](SECURITY.md#visibility--deployment-personas--one-schema-progressive-disclosure).
- **`admin` is a platform role**, not a memory-access override — it manages users, teams,
  and instance settings, and sees project/team scope for teams it's actually a member of,
  same as anyone else.
- **Both human login and API tokens exist from the first release.** People sign in with
  a local account or OIDC SSO; AI clients and integrations authenticate with per-user,
  scoped, expiring API tokens. See
  [docs/SECURITY.md § Authentication](SECURITY.md#authentication-local-and-oidc-both-from-day-one).

### API surface

Three audiences, three ways in, one process, one set of access-control rules:

- **Web UI + human REST API (session-cookie or OIDC-backed auth)** — for people, not AI
  clients. Everything a person needs to _review, correct, export, import, and administer_
  memory works here with **no AI in the loop**: browsing and editing records with their
  revision history, the proposal review queue, inbox quick-capture, focus preview,
  token minting/revocation, teams and users, plugin configuration, import/export. The UI
  is specified in [docs/UI.md](UI.md). **Status:** in Phase 1 the web UI _is_ the human
  interface (server-rendered routes under `/memory`, `/review`, `/focus`, `/data`,
  `/settings`); a separate JSON REST API for people (`/api/memories`, `/api/tokens`, …) is
  not built yet — scripts and backups use the `acm` CLI, and AI clients use MCP. When it
  is added it will accept session auth only, never API tokens (see
  [docs/SECURITY.md § What AI clients cannot do](SECURITY.md#what-ai-clients-cannot-do)).
- **MCP surface (bearer API-token auth, `POST /mcp`)** — for AI clients, and the path meant for
  routine AI-driven writes: `memory.focus` (task-focused context pack — see
  [Task focus](#task-focus)), `memory.search`, `memory.get`, `memory.write`,
  `memory.sync` (bulk upsert from a repo's `memory/data/` after a dream pass),
  `memory.consolidate` (pull a work package of consolidation candidates, return
  proposals — Phase 2), `inbox.add`. **Tool names are underscored on the wire**
  (`memory_focus`, `memory_write`, `inbox_add`, …) because several MCP clients restrict
  tool names to `[A-Za-z0-9_-]`; this document keeps the dotted form for readability. The hub stays deliberately "dumb" about models: it enforces
  access control and the confidence-tier mutation rule server-side, but the
  classification/merge _reasoning_ stays in the `dream` skill running client-side, in
  whatever AI tool is driving it. That keeps the hub free of any dependency on a
  specific model or vendor.
- **Local CLI (`acm`, filesystem access, no network required)** — for the operator.
  See [Import / export](#import--export--offline-human-operated) below; this is also how
  a fresh instance is bootstrapped and how access is recovered.

MCP clients can read and write (within their token's scope), and in Phase 2 propose; they
cannot approve a proposal, mint tokens, change instance settings, run import/export, mark a
record `established`, change a record's tier, or write team-scope memory. Those are
human-only operations, enforced server-side.

### Import / export — offline, human-operated

Reviewing, exporting, and importing memory must work **without an AI and without the
hub being reachable** — your memory is yours, and "I can read, back up, and move it
myself" is the guarantee behind "own everything." Two ways in, both human-operated:

1. **The web UI** — the Data page (export download; import with a dry-run report you
   must Apply), plus per-record review and editing under Memory.
2. **The `acm` CLI** — operates **directly on the SQLite file or on a plain-file
   directory**, with the server stopped, with no network, and with no model: `acm export`,
   `acm import` (dry run unless `--apply`), `acm list`, `acm show`, `acm edit`, plus
   bootstrap/recovery commands (`acm setup-code`, `acm user`, `acm token`, `acm doctor`,
   `acm reindex`). `acm review` for proposals arrives with Phase 2. SQLite in WAL mode
   makes this safe to run even alongside a live server. The test suite runs the CLI with
   network connections blocked to keep that promise honest.

Export and import are deliberately **not reachable with an API token** — an AI client
can't dump or bulk-rewrite your memory (see
[docs/SECURITY.md § What AI clients cannot do](SECURITY.md#what-ai-clients-cannot-do)).

**Export format** is the same markdown + YAML frontmatter used everywhere in this repo,
laid out as one directory (or `.zip`) per export:

```text
export/
  manifest.json          # schema version, exported_at, instance id, counts, per-file sha256
  user/ MEMORY.md  *.md  # one file per record, grouped by scope, with scope index files
  team/<slug>/ ...
  project/<slug>/ ...
  _history/<id>.jsonl    # optional: full revision history per record (--with-history)
  _inbox/*.md            # optional: un-harvested inbox items
```

Records are identified by their frontmatter `id` (kept when restoring into a fresh
instance, so `links` between records survive a round trip); a file without one is matched on
(scope, owner, `name`). A team-scope record also carries `team_id` (the team slug), like
`project_id` for project scope. Import is **idempotent** (re-importing the same export is a
no-op) and always shows a **dry-run report first** — the dry run executes for real inside a
database savepoint and rolls back, so it reflects exactly what apply would do, permission and
validation outcomes included. Import can only write where the importer could write by hand.

**Humans are allowed to edit memory directly.** Earlier drafts framed hand-editing as
something to avoid; that was wrong for a system whose point is that you stay in control.
What stays true is the _accounting_, not the restriction: every UI/CLI/import change is a
revision with `change_source` of `ui`/`cli`/`import` and an author. The
confidence-tier rule still applies, just with the human as the authority — changing an
`established` record requires an explicit confirmation (a checkbox in the UI, `--confirm-established`
in the CLI) so it's never accidental, and an import that conflicts with an existing
record is **parked** as a flagged, unapplied revision (shown on the record's page and in
Review) rather than overwriting it. That happens when the hub copy is `established`, and also
when the hub copy is _newer_ than the file — so restoring an old backup over a live instance
can't silently clobber later edits. Export includes only records the exporter can read, so
`user`-scope export is only ever of your own records.

### Web UI

A small, calm, server-rendered interface that exists so a person can see and steer what's
remembered about them and their work. **Light and dark themes are required** (system
default plus a manual override), the navigation is a handful of logically ordered
sections, and nothing in it depends on an AI being available. Full spec — principles,
screens, theming tokens, accessibility — in [docs/UI.md](UI.md).

### Plugins

Everything that touches the outside world — pulling snippets from Obsidian or Memos,
posting a digest to Slack or Teams, sending a notification — is a **plugin**, not hub
core. Two kinds, built on a shared event outbox:

- **Sources** bring material _in_ to the **inbox** (never straight into memory — the
  dream cycle classifies it) or write records/digests _out_ to a note system.
- **Sinks** subscribe to events (`proposal.pending`, `conflict.flagged`,
  `digest.weekly`, …) and deliver notifications.

Plugins are operator-installed Python packages discovered via entry points, configured
per instance in the UI, with secrets referenced by environment variable name — never
stored in records or the database. Outbound plugins only ever see scopes the operator
explicitly allows and, by default, only titles and links, not record bodies. Interface,
event catalog, and the built-in plugins (Obsidian, Memos, Apprise/Slack/Teams/webhook) are
in [docs/PLUGINS.md](PLUGINS.md); the security rules are in
[docs/SECURITY.md § Plugins and egress](SECURITY.md#plugins-and-egress).

### Deployment stance

Self-hosted by the person or team that owns the memory — that's the point of "own
everything": your memory shouldn't live somewhere you don't control. A single Python
process plus one SQLite file is the whole deployment; Docker is a convenience, not a
requirement. It is sized for one person or one team first; scaling past that is
supported by swapping the database, not by redesigning anything.

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

Sequenced as phases; each phase is usable on its own. Self-host first throughout.

**Phase 0 — design (this change)**

- [x] Core/associated tier, task focus, lifecycle (reinforce/supersede/decay), proposal
      queue, inbox, plugin model, web UI and offline import/export specified; schema
      templates extended

**Phase 1 — usable hub, solo mode** (built: code in [`hub/`](../hub/README.md))

- [x] FastAPI + SQLModel + SQLite (WAL, FTS5) + Alembic skeleton; `/healthz`
- [x] Local auth and OIDC (both from the first release) and per-user scoped, expiring,
      hashed API tokens, per [docs/SECURITY.md](SECURITY.md) — no weaker interim scheme
- [x] MCP surface: `memory.focus` / `search` / `get` / `write` / `sync`, `inbox.add`,
      with server-side confidence-tier enforcement and revisions
- [x] `acm` CLI: bootstrap, export, `import` (dry run by default), list/show/edit — works offline
- [x] Web UI: Memory, Record detail + history, Review (inbox + import conflicts), Focus
      preview, Data (import/export), Settings (tokens, theme, instance); light/dark theme
- [x] Container image + docker-compose per [docs/DEPLOYMENT.md](DEPLOYMENT.md) (image
      definition written; not built in the authoring environment — see DEPLOYMENT.md)
- [ ] Session-authenticated JSON REST API for people (the UI and CLI cover every operation today)
- [ ] Team/membership screens (the data model and access rules are in place and tested; there's
      no UI to create teams or invite members yet — Phase 4)

**Phase 2 — memory that learns**

- [ ] Lifecycle: reinforcement, supersession, decay, `status`
- [ ] Mechanical consolidation job + proposal queue + Review screen; `memory.consolidate`
      work packages; `dream` skill updated to read/write proposals
- [ ] Core budget enforcement and `promote_core`/`demote_core` proposals
- [ ] Promotion workflow with an explicit human approval step (the proposal queue _is_ it)

**Phase 3 — plugins**

- [ ] Event outbox + plugin loader + Plugins screen
- [ ] Sinks: Apprise-based notifier (Slack, Teams, ntfy, email, webhook)
- [ ] Sources: Obsidian vault and Memos connectors (inbox in, optional digest out)

**Phase 4 — teams and distribution**

- [ ] Users/teams/membership + admin/owner/member roles, `deployment_mode`
      (`solo`/`team`/`multi_team`) and project `visibility`, UI for each
- [ ] Claude Code plugin (`.claude-plugin/`) per [docs/DISTRIBUTION.md](DISTRIBUTION.md)
      — blocked on confirming the current plugin skills-path convention first
- [ ] `hub-sync` skill or process to push repo records to the hub and pull cross-project
      context back into a session
- [ ] On-request "compile" step to generate a specific tool's instruction file
      (`.cursor/rules/`, `.windsurfrules`, etc.) from hub/repo memory
- [ ] Adapter notes for at least one non-Claude assistant that supports MCP
- [ ] k3s/k8s manifests (documented, genuinely optional)

**Later / optional**

- [ ] MCP OAuth backed by the hub's OIDC, for Claude.ai connectors that prefer it to
      bearer tokens
- [ ] Optional embedding index as a plugin (never a core dependency)
- [ ] Client-side encryption of `private`-scope bodies at rest (see SECURITY.md)
- [ ] Reference implementation of the dream pipeline outside a single chat session
      (e.g. run against exported transcripts from multiple tools in one pass)
- [ ] Postgres option for people who outgrow a single SQLite file
