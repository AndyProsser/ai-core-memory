# Working in this repo

`ai-core-memory` defines an open, cross-platform memory format and consolidation
process for AI assistants (the "dream cycle" — see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)).
This repo is currently spec-and-skill stage, not a running service: most of the value is
in the design docs and the `dream` skill, not in application code.

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before changing anything about the
memory record schema or the scope model (session/project/team/user) — those two ideas
are load-bearing for everything else in the repo.

## Conventions

- **Line endings: LF everywhere, no exceptions.** Enforced via `.gitattributes` and
  `.editorconfig`. Don't introduce CRLF files or fight the normalization.
- **Memory records are Markdown + YAML frontmatter**, per the templates in
  [`memory/schema/`](memory/schema/). Every record declares a `type`
  (user/feedback/project/reference/intent/rule), a `scope` (session/project/team/user),
  and a `confidence` tier (observed/confirmed/established) — see ARCHITECTURE.md for
  what each means and how they interact.
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
| A new memory type or scope                           | `docs/ARCHITECTURE.md` first, then a template in `memory/schema/`              |
| A design/process change to the dream cycle           | `docs/ARCHITECTURE.md`                                                         |
| Anything about running the memory hub                | `docs/DEPLOYMENT.md` (containers, k3s/k8s, backups)                            |
| Anything about installing skills/MCP into an AI tool | `docs/DISTRIBUTION.md` (plugins, `.mcp.json`, Claude.ai connectors)            |
| Editor/tooling config                                | `.vscode/`, `.editorconfig`, `.gitattributes` — keep these boring and standard |

## Scope of changes

Don't start implementing the memory hub service or other roadmap items (see
ARCHITECTURE.md → Roadmap) unless explicitly asked. The hub's design — Python, FastAPI,
SQLite/SQLModel, the data model, the user/team/token access model — is decided (see
ARCHITECTURE.md § Memory hub), but no code exists yet; treat "the design is decided" and
"go build it" as two separate asks. Prefer extending the plain-file format, the schema
templates, and the `dream` skill, since those are what's actually in use today.
