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

### Streamlined: a Claude Code plugin

To use the `dream` skill — and later, the hub connection — in _any_ project without
copying files around, Claude Code supports **plugins**: a bundle of skills and MCP
config, installed once and available everywhere. The shape, based on Claude Code's
current plugin/marketplace docs:

```text
.claude-plugin/
  plugin.json          # name, version, description, author
  marketplace.json     # lists this plugin so people can `/plugin marketplace add` it
skills/
  dream/SKILL.md        # plugin-scope skills live at the plugin root, not .claude/skills/
.mcp.json              # MCP server(s) the plugin wires up, once the hub exists
```

```json
// .claude-plugin/plugin.json
{
  "name": "ai-core-memory",
  "description": "Dream consolidation skill with a self-hosted memory-hub MCP connection",
  "version": "0.1.0",
  "author": { "name": "Andy Prosser" }
}
```

```json
// .claude-plugin/marketplace.json — lets this same repo be its own marketplace
{
  "name": "ai-core-memory",
  "owner": { "name": "Andy Prosser", "url": "https://github.com/<owner>/ai-core-memory" },
  "description": "Open, self-hosted memory for AI assistants",
  "plugins": [
    {
      "name": "ai-core-memory",
      "source": { "source": "github", "repo": "<owner>/ai-core-memory", "ref": "main" },
      "description": "Dream consolidation skill + memory hub MCP connection",
      "version": "0.1.0"
    }
  ]
}
```

Once published, installing it is two commands:

```text
/plugin marketplace add <owner>/ai-core-memory
/plugin install ai-core-memory@ai-core-memory
```

A single GitHub repo can be both the plugin and its own marketplace — no central
approval needed for self-hosting; only submitting to Anthropic's community/official
marketplace requires review, and that's optional, not required for this to work.

**Open question, deliberately not resolved yet:** Claude Code's plugin system is a
newer, fast-moving surface, and the exact rule for where skills must live relative to
`.claude-plugin/` — a fixed `skills/` at plugin root vs. a configurable path — should be
re-verified against current docs before this is actually published; treat the layout
above as a plan, not a pinned fact. It also raises a real question worth resolving
first: `.claude/skills/dream/` (this repo's working copy, used when you're developing
_inside_ this repo) and a plugin's `skills/dream/` (used when the plugin is installed
into _someone else's_ repo) may need to be the same file, not two copies that drift —
and symlinks are unreliable on Windows, which this project explicitly wants to support.
That's why no `.claude-plugin/` directory exists in this repo yet: publishing a plugin
manifest that points at a path structure we haven't confirmed, or that quietly forks
from the working skill, would be worse than not publishing one yet.

### MCP: how the hub gets connected

Two ways, once the hub exists and is reachable over HTTP (see
[ARCHITECTURE.md § Memory hub](ARCHITECTURE.md#memory-hub-cross-project-store)):

- **Manual** — `claude mcp add --transport http memory-hub https://your-hub/mcp --header
  "Authorization: Bearer $TOKEN"`, or the equivalent committed to a project's `.mcp.json`
  if the whole team should get it.
- **Bundled in the plugin** — the plugin's own `.mcp.json` declares the same server, with
  the URL and token supplied via environment variables (`${MEMORY_HUB_URL}`,
  `${MEMORY_HUB_TOKEN}`) so installing the plugin both gives you the skill and prompts
  you to point it at your own hub.

This repo doesn't ship an `.mcp.json` yet — there's no hub for it to point at. An MCP
entry for a server that doesn't exist is worse than no entry; it gets added, in both
places above, together with the hub itself (see
[ARCHITECTURE.md § Roadmap](ARCHITECTURE.md#roadmap)).

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
| Skill install          | Project-local today; plugin (roadmap) for use in any repo | Custom skill upload, or org-wide push by a Team/Enterprise admin |
| MCP install            | `claude mcp add`, or bundled in the plugin's `.mcp.json`  | Customize → Connectors → Add custom connector                    |
| Same hub URL for both? | Yes                                                       | Yes                                                              |
