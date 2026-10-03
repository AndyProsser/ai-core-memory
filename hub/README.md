# Memory hub

The self-hostable memory hub: a central store for AI-assistant memory with a web UI, an MCP
endpoint, and an offline CLI. Design: [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) ·
security: [`docs/SECURITY.md`](../docs/SECURITY.md) · UI: [`docs/UI.md`](../docs/UI.md) ·
deployment: [`docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md).

One Python process, one SQLite file. No external services.

## What's in Phase 1

| Area | Status |
| --- | --- |
| Records, tiers (core/associated), confidence-tier enforcement, revisions | built |
| Task focus (`memory_focus`): core + FTS5 + topics + links, budget-packed, explainable | built |
| MCP: `memory_focus` `memory_search` `memory_get` `memory_write` `memory_sync` `memory_reinforce` `memory_propose` `memory_consolidate` `inbox_add` `inbox_resolve` | built |
| Local login (Argon2id) + OIDC SSO (PKCE) | built |
| API tokens: hashed, scoped, expiring, revocable, rate-limited, HTTPS-or-LAN | built |
| Web UI: Memory, Review (inbox + import conflicts), Focus, Data, Settings; light/dark | built |
| `acm` CLI: setup, users, tokens, list/show/edit, export, import, consolidate, review, doctor, reindex | built |
| Lifecycle: reinforcement, supersession + timeline, decay; proposal queue + Review screen; `acm consolidate` / `acm review`; scheduler | built (Phase 2) |
| Plugins: event outbox + dispatcher, Apprise (Slack/Teams/ntfy/email) + signed webhook sinks, Obsidian + Memos sources, Plugins screen, `acm plugins` | built (Phase 3) |
| Teams/users/projects roles + UI + CLI, Claude Code plugin, `hub-sync` skill, `acm compile`, k8s manifests | built (Phase 4) |

## Run it

```bash
cd hub
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

export MEMORY_HUB_DB_PATH=./data/hub.sqlite3
export MEMORY_HUB_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
acm serve                         # http://127.0.0.1:8000 ; the first-run setup code is printed in the log
```

Open the URL, enter the setup code, create the admin. Or headless: `acm user create --admin you@example.com`.

Connect Claude Code (the UI's **Settings → API tokens** gives you this line with a fresh token):

```bash
claude mcp add --transport http memory-hub http://127.0.0.1:8000/mcp \
  --header "Authorization: Bearer $MEMORY_HUB_TOKEN"
```

Containers: see `docker-compose.yml` and [`docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md).

## Offline, no AI

```bash
acm list -q retry                 # search
acm show idempotent-retries --history
acm edit idempotent-retries --tier core --confirm-established
acm export --out ./backup --with-history
acm import ./backup               # dry run; add --apply to write
acm consolidate --dry-run         # what would decay / look duplicated / exceed the core budget
acm review                        # the proposal queue; `acm review approve ID [--confirm-established]`
acm plugins                       # configured plugin instances; `acm plugins disable ID` is the offline off-switch
acm doctor
```

`acm` reads the SQLite file directly (the server can be stopped, or running). It never opens a
network connection — the test suite enforces that.

## Develop

```bash
pytest                            # unit + web + MCP + OIDC (fake IdP) + CLI + a real-server MCP-client test
ruff check . && ruff format .
```

Schema changes: edit `src/acm_hub/models.py`, then
`alembic revision --autogenerate -m "what changed"` (uses `alembic.ini`), review the generated
migration, and commit it. Migrations run automatically at startup and from every `acm` command.

Layout: `models.py` (tables) · `access.py` (who can see/change what) · `records.py` (the only
write path, enforcing the confidence/tier rules) · `focus.py` · `exportimport.py` · `mcp_server.py` ·
`auth.py`/`oidc.py`/`security.py` · `web/` (routes, templates, static) · `cli.py`.
