# Distribution: getting skills and the hub into Claude

Two separate problems: getting the `dream` skill (and future skills) into whatever AI
tool someone is using, and getting that tool talking to a self-hosted memory hub over
MCP. Claude Code and Claude.ai solve both differently, so this document covers each.

## Claude Code

### Today: clone and go

Nothing to install. [`.claude/skills/dream/SKILL.md`](../.claude/skills/dream/SKILL.md)
is a project-local skill — open this repo (or copy `.claude/skills/` and
`memory/schema/` into another one) in Claude Code and the skill is already discoverable.
This is the zero-setup path and works today.

### Streamlined: the Claude Code plugin

This repo **is** a Claude Code plugin and its own marketplace — one checkout, no copies to drift.
The layout below was validated with `claude plugin validate` and installed end to end with the real
`claude` CLI (2.1.x) in a scratch profile:

```text
.claude-plugin/
  plugin.json         # name, version, userConfig (hub URL + token), inline MCP server
  marketplace.json    # one plugin, source "." — this repo is the marketplace root
.claude/skills/
  dream/SKILL.md      # the single copy: used in-repo AND by the installed plugin
  hub-sync/SKILL.md
```

How the single source of truth works: the manifest's `"skills": ["./.claude/skills/"]` points the plugin
at the same directory Claude Code already reads when you work inside this repo. No symlink (unreliable on
Windows) and no second `skills/` copy.

```text
/plugin marketplace add AndyProsser/ai-core-memory
/plugin install ai-core-memory@ai-core-memory
```

Installing prompts for two settings (`/plugin configure ai-core-memory@ai-core-memory`, or
`claude plugin install … --config hub_url=… --config hub_token=…`):

| Setting     | What it is                                         | Stored                                   |
| ----------- | -------------------------------------------------- | ---------------------------------------- |
| `hub_url`   | your hub's MCP endpoint, e.g. `https://hub/mcp/`   | plugin settings                          |
| `hub_token` | a per-user token from Settings → API tokens        | `sensitive: true` — Claude Code's secure storage, never in a file |

The plugin declares the MCP server **inline in `plugin.json`** rather than as a root `.mcp.json`, on purpose:
a root `.mcp.json` would also be loaded as _project_ config by anyone who opens this repo, where
`${user_config.*}` doesn't exist. `${user_config.hub_url}` and `${user_config.hub_token}` are substituted in
the server's `url` and `Authorization` header — verified by pointing an installed copy at a throwaway local
server and reading the request it received. The tools then appear as `mcp__plugin_ai-core-memory_memory__*`.

Notes from validation:

- `claude plugin validate` warns that `CLAUDE.md` at the plugin root isn't loaded as context. That is
  expected: it is contributor guidance for this repo, not something the plugin ships. Ship context as skills.
- A git-based install clones only tracked files, so users receive `hub/` and `docs/` along with the skills
  (about 2 MB). If that matters later, split the plugin into its own repo.
- Not tested: a live model session actually invoking the skills after install (that needs an authenticated
  Claude Code session). The skill path resolves and validates; treat first real use as the final check.

### MCP: how the hub gets connected

The hub serves MCP over streamable HTTP at `POST <hub-url>/mcp` with a per-user API token as the bearer
credential (see [ARCHITECTURE.md § Memory hub](ARCHITECTURE.md#memory-hub-cross-project-store)). Create a
token in the hub's **Settings → API tokens** (or `acm token create`); the UI hands back a ready-to-paste
command and `.mcp.json` snippet. Tokens are scoped to chosen projects, read-only by default, expire (90 days
by default), and are only accepted over HTTPS — or plain HTTP on localhost / a private LAN. Two ways to connect:

- **Plugin** (above) — URL and token are asked for once at install.
- **Manual** — `claude mcp add --transport http memory-hub https://your-hub/mcp --header
  "Authorization: Bearer $TOKEN"`, or the equivalent in a project's `.mcp.json` if the whole team should
  get it (token in `${MEMORY_HUB_TOKEN}`, never in the file).

This repo doesn't ship a root `.mcp.json`: the URL is specific to _your_ hub.

The tools the hub exposes are `memory_focus`, `memory_search`, `memory_get`,
`memory_write`, `memory_sync`, `memory_reinforce`, `memory_propose`, `memory_consolidate`,
`inbox_add`, and `inbox_resolve`; the server's built-in instructions tell a
connected assistant how to use them (focus at the start of a task, write narrowly, never claim
`established`).

## Other assistants

The hub is a plain MCP server, so any MCP-capable assistant can use it, and `acm compile` covers tools that
only read an instruction file.

### Connecting over MCP

> Config formats below come from search results and community write-ups, **not** the vendors' own docs (the
> docs sites were unreachable from the environment this was written in) and were not run against the real
> products. Check the vendor's current docs if something doesn't load.

**VS Code (GitHub Copilot agent mode)** — `.vscode/mcp.json`; the token is prompted for once and kept out of the file:

```json
{
  "inputs": [
    { "type": "promptString", "id": "memory-hub-token", "description": "Memory hub API token", "password": true }
  ],
  "servers": {
    "memory": {
      "type": "http",
      "url": "https://your-hub/mcp/",
      "headers": { "Authorization": "Bearer ${input:memory-hub-token}" }
    }
  }
}
```

**Cursor** — `.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global); read the token from an environment variable:

```json
{
  "mcpServers": {
    "memory": {
      "url": "https://your-hub/mcp/",
      "headers": { "Authorization": "Bearer ${env:MEMORY_HUB_TOKEN}" }
    }
  }
}
```

A Cursor forum report says `${env:…}` in remote-server headers was once sent literally and has since been
fixed; if you see `401`s, check that first.

What carries over and what doesn't: the tools (`memory_focus`, `memory_write`, …) and the hub's built-in
server instructions work anywhere. The `dream` and `hub-sync` _skills_ are Claude-specific; another assistant
gets the same behaviour by being told to call `memory_consolidate` first and act through the tools (the
server instructions already say how).

### Compile: instruction files for tools that don't speak MCP

```bash
acm compile claude   --project my-app            # CLAUDE.md
acm compile agents   --project my-app            # AGENTS.md
acm compile copilot  --project my-app            # .github/copilot-instructions.md
acm compile cursor   --project my-app            # .cursor/rules/ai-core-memory.mdc
acm compile windsurf --project my-app            # .windsurfrules
acm compile all --from-dir memory/data --project my-app --out .   # from plain files, no hub at all
```

It is a deliberate one-shot build, not a background sync, and deliberately narrow, because an instruction file
is trusted context for whatever loads it:

- only **active core and rule** records, and only **confirmed/established** ones (`--include-observed` to
  override — an observed record may have come from a plugin or a single inferred session);
- **no user-scope** records unless `--include-user-scope`, and then never into a git work tree unless
  `--allow-in-repo` (personal memory must not be committed by accident);
- project-scope records only for the `--project` slugs you name;
- output is deterministic; shared files (`CLAUDE.md`, `AGENTS.md`, …) get a marker-delimited block that is
  replaced on re-run while everything you wrote around it is left alone; Cursor's `.mdc` is a file the tool
  owns outright, and an existing one it didn't generate is never overwritten;
- record text can't forge or close the markers.

Windsurf's rules-file location is from memory of its conventions and unverified; `--stdout` prints any target
if you'd rather place it yourself.

## Claude.ai (the web app)

A different mechanism entirely — there's no plugin system here today:

- **Skills** — created or uploaded as a custom skill in-product (Pro/Max/Team/Enterprise
  plans), or pushed org-wide by a Team/Enterprise admin. No marketplace install path.
- **MCP ("Connectors")** — add the hub as a **custom connector**: Customize → Connectors
  → Add custom connector → the hub's HTTPS MCP URL, authenticating with either OAuth or
  a fixed bearer-token header.

Because the hub is a plain HTTP MCP server (per ARCHITECTURE.md § Memory hub's API
surface), the _same_ hub URL works for both Claude Code and Claude.ai — it's the
skill/plugin side that differs, not the hub itself.

## Summary

|                        | Claude Code                                               | Claude.ai                                                        |
| ---------------------- | --------------------------------------------------------- | ---------------------------------------------------------------- |
| Skill install          | Plugin (`/plugin install ai-core-memory@ai-core-memory`), or project-local | Custom skill upload, or org-wide push by a Team/Enterprise admin |
| MCP install            | `claude mcp add`, or via the plugin's `hub_url`/`hub_token` settings | Customize → Connectors → Add custom connector                    |
| Same hub URL for both? | Yes                                                       | Yes                                                              |
