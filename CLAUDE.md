# Working in this repo

`ai-core-memory` defines an open, cross-platform memory format and consolidation
process for AI assistants (the "dream cycle" — see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)).
This repo is spec-and-skill first, with a working Phase 1 hub in [`hub/`](hub/README.md)
(FastAPI + SQLite + MCP + web UI + `acm` CLI). The design docs and the `dream` skill remain
the source of truth; code in `hub/` implements them, it doesn't redefine them.

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before changing anything about the
memory record schema or the scope model (session/project/team/user) — those two ideas
are load-bearing for everything else in the repo.

## Conventions

- **Line endings: LF everywhere, no exceptions.** Enforced via `.gitattributes` and
  `.editorconfig`. Don't introduce CRLF files or fight the normalization.
- **Memory records are Markdown + YAML frontmatter**, per the templates in
  [`memory/schema/`](memory/schema/). Every record declares a `type`
  (user/feedback/project/reference/intent/rule), a `scope` (session/project/team/user),
  a `confidence` tier (observed/confirmed/established), a loading `tier`
  (core/associated), and a lifecycle `status` (active/superseded/stale/archived) — see
  ARCHITECTURE.md for what each means and how they interact.
- **Design commitments that apply to everything built here:** self-hosting comes first
  (no required cloud service); OIDC SSO and per-user API tokens ship from the first
  release; review/export/import must work offline and without an AI (`acm` CLI + web UI);
  the web UI must support light and dark themes. Don't build around these.
- **Don't write real personal memory data into this repo.** `memory/data/user/` and
  `memory/data/session/` are gitignored on purpose — user-scope memory is machine-local,
  not project-shared. `memory/data/project/` and `memory/data/team/` are meant to be
  committed; that's the point of those scopes.
- **This repo is documentation-first.** A conceptual change (a new scope, a new memory
  type, a change to the dream pipeline) belongs in `docs/ARCHITECTURE.md` before or
  alongside any skill/code change that implements it — don't let the two drift apart.

## Where things go

| Adding...                                            | Goes in                                                                        |
| ---------------------------------------------------- | ------------------------------------------------------------------------------ |
| A new Claude Skill                                   | `.claude/skills/<name>/SKILL.md`                                               |
| Hub application code, tests, Dockerfile              | `hub/` (`src/acm_hub/`, `tests/`) — see `hub/README.md`                        |
| A new memory type or scope                           | `docs/ARCHITECTURE.md` first, then a template in `memory/schema/`              |
| A design/process change to the dream cycle           | `docs/ARCHITECTURE.md`                                                         |
| Anything about running the memory hub                | `docs/DEPLOYMENT.md` (containers, k3s/k8s, backups)                            |
| Anything about installing skills/MCP into an AI tool | `docs/DISTRIBUTION.md` (plugins, `.mcp.json`, Claude.ai connectors)            |
| Anything about tokens, roles, visibility, or auth    | `docs/SECURITY.md`                                                             |
| Anything about the web UI, screens, or theming       | `docs/UI.md`                                                                   |
| Anything about plugins (Obsidian, Memos, Slack, …)   | `docs/PLUGINS.md` (egress/trust rules go in `docs/SECURITY.md`)                |
| Editor/tooling config                                | `.vscode/`, `.editorconfig`, `.gitattributes` — keep these boring and standard |

## Scope of changes

Phase 1 of the hub is built (see ARCHITECTURE.md → Roadmap for what is and isn't). Don't
start on later roadmap phases (proposal queue, plugins, teams UI, the Claude Code plugin)
unless explicitly asked — "the design is decided" and "go build it" are separate asks. When
working in `hub/`, run `pip install -e '.[dev]'` and `pytest` there; every change needs
tests, and security-relevant behaviour (auth, tokens, access, import/export) needs a test
that fails if the protection is removed. This matters more for anything security-related than anywhere else in
this repo: don't improvise a simplified auth/token scheme "for now" — follow
`docs/SECURITY.md` as written, or raise the discrepancy, rather than shipping something
weaker. If the code and the docs disagree, fix the docs in the same change. Prefer extending the plain-file
format, the schema templates, and the `dream` skill, since those are what's actually in
use today.
