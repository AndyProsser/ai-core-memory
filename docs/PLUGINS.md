# Plugins

How the memory hub (see [ARCHITECTURE.md § Memory hub](ARCHITECTURE.md#memory-hub-cross-project-store))
connects to the world outside itself: note systems like Obsidian and Memos, and
notification channels like Slack and Teams. Security rules for everything here live in
[SECURITY.md § Plugins and egress](SECURITY.md#plugins-and-egress).

**Status: built (Phase 3).** The framework, the event outbox and dispatcher, and four built-in plugins
(`apprise`, `webhook`, `obsidian`, `memos`) are in [`hub/src/acm_hub/plugins/`](../hub/src/acm_hub/plugins/).
Where the shipped behaviour differs from the original sketch, this document describes what shipped.

## Principles

- **The hub core knows nothing about any specific service.** No Slack code, no Obsidian
  code in the core. If it talks to the outside world, it's a plugin.
- **External material is input, not memory.** A note captured in Obsidian is an _inbox
  item_ until the [dream cycle](ARCHITECTURE.md#the-dream-cycle) classifies it — into a
  record, into a `reference` pointer back to the note, or into the bin. Mirroring a whole
  note system into memory would violate
  [What NOT to remember](ARCHITECTURE.md#what-not-to-remember).
- **Outbound is minimal and opt-in.** A plugin only sees the scopes an operator allowed
  for it and, by default, only event metadata (ids, titles, links), not record bodies.
- **Failure is isolated.** A broken Slack webhook must never block a memory write, a
  dream pass, or another plugin.
- **Self-hosted means trusted code.** Plugins are Python packages the operator installs.
  The UI configures them; it never uploads or executes code.

## Two kinds, one outbox

```text
                 ┌────────────── hub core ──────────────┐
 Obsidian ─┐     │  inbox ──► dream cycle ──► records   │     ┌─► Slack
 Memos    ─┼─►source plugins                  │         │     ├─► Teams
 (others) ─┘     │                       events outbox ──┼──►sink plugins ─► ntfy / email
                 └──────────────────────────────────────┘     └─► webhook
```

- **Source plugin** — pulls (on a schedule or on demand) from an external system and
  calls `ctx.inbox.add(...)`. Optionally also _exports_: writes a record or periodic
  digest out to that system (e.g. a markdown note in an Obsidian vault).
- **Sink plugin** — receives events from the outbox and delivers them somewhere.
- A plugin may be both (a Slack plugin that posts digests and also captures messages
  reacted to with a 🧠).

### Event outbox

Core writes an `events` row inside the same transaction as the change that caused it, so
nothing is lost if the process dies. A dispatcher fans each event out to enabled sink
instances whose event filter and scope allowlist match, tracking each delivery in
`plugin_deliveries` with retry/backoff (see the
[data model](ARCHITECTURE.md#data-model)). Delivery is **at-least-once**; sinks should be
idempotent on `event.id`.

| Event                  | Fires when                                                         | Payload (minimal)                    |
| ---------------------- | ------------------------------------------------------------------ | ------------------------------------ |
| `record.created`       | a record is written                                                | id, name, scope, type, tier          |
| `record.updated`       | a record's body, confidence, tier, or status changes               | id, name, what changed               |
| `record.superseded`    | a record is replaced by a newer one                                | old id, new id, names                |
| `proposal.pending`     | consolidation produced a proposal awaiting review                  | id, kind, one-line rationale         |
| `conflict.flagged`     | new information contradicts a `confirmed`/`established` record     | record id, name, proposal id         |
| `core.budget_exceeded` | the consolidation pass found core over its token budget and proposed demotions | over by, number of proposals |
| `inbox.new`            | an inbox item arrived                                              | id, source, title                    |
| `digest.weekly`        | schedule: summary of what changed, what's pending, what went stale | counts + titles, link to the Review screen |
| `plugin.failed`        | a plugin instance failed 3 times in a row                          | plugin instance, last error          |

Every payload carries a `link` to the relevant UI screen, so a notification is a pointer
back to the hub, not a copy of memory. (A `plugin.test` event, sent by the Plugins screen's
test button, goes only to the instance being tested.)

**How delivery works** (`hub/src/acm_hub/dispatcher.py`). A scheduler tick (every 10 s, inside the
hub process; `MEMORY_HUB_PLUGINS=false` switches it off entirely) does three things: _fans out_
new events into one `plugin_deliveries` row per matching instance; _delivers_ due rows, each in a
worker thread under a 30 s timeout, committing per delivery so one slow plugin never holds the others
up; and _purges_ events older than 30 days. Delivery is at-least-once. A failure is retried after
1 min, 5 min, 30 min, 2 h, 6 h and then marked `dead`; a plugin that reports a definitive rejection
(HTTP 4xx, a policy refusal) dies immediately instead of retrying. After three failures in a row the
hub raises a `plugin.failed` event — delivered to _other_ sinks, never to the failing one. Each
instance is also rate-limited (60 deliveries a minute); over the limit, deliveries wait without
burning an attempt. A disabled instance's pending deliveries wait until it is enabled again.

**Who gets which event.** An instance receives an event only if all of these hold: it is enabled and
its plugin can send; it subscribed to that event type; the event's scope is in its scope allowlist
(and, for project scope, its project list if it has one); **its owner can actually read that
record** (an admin's Slack instance doesn't learn about another user's private project); for `user`
scope it carries the explicit acknowledgement _and_ the record is the owner's own; and the event
wasn't caused by that same instance's own activity (loop prevention). Scope-less events such as
`plugin.failed` carry no memory content, so they skip the scope list but still stay with their owner.

## Plugin interface

Discovered via the `acm.plugins` Python entry-point group. A plugin declares metadata, a
config schema (rendered as a form in the Plugins screen), and implements one or both
protocols. This is the shape as built (see `hub/src/acm_hub/plugins/base.py`; the _Writing a plugin_ section below has a runnable example):

```python
class PluginInfo(BaseModel):
    key: str                          # "obsidian", "memos", "apprise", "webhook"
    name: str
    description: str = ""
    kind: Literal["source", "sink", "both"]
    config_schema: type[BaseModel]    # non-secret settings; the Plugins form is generated from it
    secret_names: list[str] = []      # secrets it needs; the operator maps each to an env var *name*
    secret_help: dict[str, str] = {}
    default_events: list[str] = []    # events a new sink instance subscribes to (the operator can narrow)

class BasePlugin:                     # implement what applies; everything runs off the request path, under a timeout
    info: PluginInfo
    def validate(self, config) -> None: ...                                  # raise ValueError("human message")
    def deliver(self, ctx: PluginContext, event: Event) -> DeliveryResult: ...   # sinks
    def pull(self, ctx: PluginContext) -> None: ...                           # sources: call ctx.inbox.add(...)
    def export_digest(self, ctx: PluginContext, digest: dict) -> None: ...    # sources, optional

class DeliveryResult:                 # .success() | .retry_later(msg) | .failed(msg)  (failed = don't retry)

class Event:                          # already filtered to this instance's allowlist and egress level
    id: str; type: str; created_at: datetime; payload: dict; link: str | None   # link is absolute

class PluginContext:
    instance_id: str; instance_name: str
    config: BaseModel                 # validated, non-secret
    secrets: Mapping[str, str]        # resolved from the environment for this call only; never persisted
    scopes: frozenset[str]            # what this instance may see
    egress: str                       # "metadata" | "full"
    log: LoggerAdapter                # secret values are redacted
    http: EgressClient                # the only way out: https (or LAN), host allowlist, no redirects, size cap
    public_url: str
    inbox: InboxWriter | None         # sources only: .add(title, body, external_ref=...) -> bool
```

Rules the loader enforces, so a plugin author can't get them wrong:

- `RecordView`/`Event` objects are **pre-filtered** to the instance's scope allowlist and
  carry only the fields its `egress` level permits (`metadata` default, `full` opt-in
  per instance, per SECURITY.md).
- **Loop prevention.** Records and inbox items created by a plugin carry
  `source: plugin:<key>` and an `external_ref`; events caused by a plugin's own writes
  aren't redelivered to that same instance.
- Calls run with a timeout, in a worker thread, with exceptions caught and recorded —
  never on the request path. (A thread can't be killed, so a plugin that hangs is _abandoned_ and
  counted as a failure; it can't block the dispatcher or other plugins, but it does keep its own thread.)
- Plugins get **no database handle and no token**; the context is the whole API.

## Built-in plugins

| Plugin     | Kind   | What it does                                                                                                                                                                                                                                                                                                  |
| ---------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `obsidian` | source | Points at a vault directory on disk (no Obsidian API needed — a vault is a folder of markdown). Pulls notes matching a configurable folder or tag (e.g. `#memory`, `Inbox/`) into the inbox with `external_ref = file path`. Optional export: writes a weekly digest note and/or selected records into a folder, as plain markdown. |
| `memos`    | source | Uses the Memos REST API (base URL + access token via env var). Pulls memos with a chosen tag (e.g. `#memory`) into the inbox; `external_ref` = memo URL. Optional export: creates a memo for a digest.                                                                                                        |
| `apprise`  | sink   | One dependency that covers Slack, Microsoft Teams, ntfy, Discord, Telegram, email, and generic webhooks via Apprise URLs held in env vars. This is the recommended path for "notify me / post to a channel" — Slack and Teams don't each get a bespoke plugin unless Apprise proves insufficient.             |
| `webhook`  | sink   | Plain signed JSON POST (HMAC header) to any URL — the escape hatch for anything else, and a template for writing a new sink.                                                                                                                                                                                  |

Honest notes on each:

- **`apprise`** does its own HTTP, so it doesn't go through the shared egress client. The hub instead
  checks the _URLs_ before handing them over: plaintext targets (`json://`, `form://`, `xml://`,
  `http://`) are refused unless they point at localhost or a private network (use `jsons://` etc.), and
  generic http(s)/json targets honour the instance's `allowed_hosts`. Chat-service URLs
  (`slack://`, `msteams://`, `ntfys://`, …) are passed through. Install with the `notify` extra
  (`pip install 'ai-core-memory-hub[notify]'`; the container image includes it). It is tested against a
  real local HTTP server, not a mock.
- **`webhook`** signs the compact, key-sorted JSON body with HMAC-SHA256 in `X-ACM-Signature:
  sha256=<hex>`, and sends `X-ACM-Event` and `X-ACM-Delivery` (the event id — dedupe on it, since delivery is
  at-least-once). Both the URL and the signing key come from environment variables you name.
- **`obsidian`** reads the vault directory directly and never writes into your notes. It scans the
  folders you list (default `Inbox`; `.` for the whole vault), optionally only notes with a tag
  (frontmatter `tags:` or inline `#tag`), skips hidden folders (`.obsidian`, `.git`, `.trash`), skips
  symlinks and anything that resolves outside the vault, and skips files over a size limit. A re-pull is
  idempotent (`external_ref` = the note's relative path): a note edited _before_ you reviewed it updates its
  inbox item; one you've already harvested or dismissed is left alone. The optional digest export writes
  `memory-digest-YYYY-MM-DD.md` into one folder inside the vault. In a container, mount the vault read-only
  (see [DEPLOYMENT.md](DEPLOYMENT.md)) and use the in-container path.
- **`memos`** is written against the documented v1 REST API (`GET /api/v1/memos`, bearer access token,
  `nextPageToken` paging) and tested against a mock of that shape — **it has not been run against every
  Memos release**, and Memos has changed this API between versions. If your server differs, the error shown
  in the Plugins screen says what it returned (e.g. `Memos returned HTTP 404 for /api/v1/memos`). Tag
  matching works on either the API's `tags` field or inline `#tags` in the content. Digest export to Memos is
  not built.

Ideas that fit the same interface without a new design: a `readwise`/`pocket` source, a
`git` source that watches a notes repo, an `embeddings` plugin offering vector search
over records, a `calendar` sink for review reminders.

## Plugins vs. AI clients that already have connectors

An AI client with its own Obsidian or Memos MCP server can already read those systems
and call `inbox.add`/`memory.write` itself — no hub plugin involved. Hub plugins exist for
the **headless, scheduled** case: capture that happens whether or not an AI session is
open, and notifications that go out on a schedule or in response to an event. Both paths
end in the same inbox and the same dream cycle.

## Operating plugins

- **Install:** the four built-ins ship with the hub. A third-party plugin is a Python package that
  exposes a `BasePlugin` subclass (or instance) under the `acm.plugins` entry-point group; install it into
  the hub's environment (or bake it into the image) and restart. A plugin that fails to load is logged and
  skipped; it can't stop the hub starting.
- **Configure** (Settings → Plugins, admin only): add an instance → the form is generated from the plugin's
  config schema → name the environment variable for each secret → choose the scopes, projects, detail level
  (titles-and-links by default) and events → **Send a test notification** / **Check now**. The screen shows
  whether each secret's variable is set, never its value. An instance is created _off_ with _no_ scopes.
  Removing an instance keeps whatever it already captured in your inbox.
- **Watch:** each instance shows its last run, last status, consecutive failures and its recent deliveries
  with errors. Errors are stored with secret values scrubbed out.
- **Emergency off-switch:** `acm plugins` lists instances and `acm plugins disable <id>` turns one off
  straight in the database — it works with the hub stopped. `MEMORY_HUB_PLUGINS=false` stops every plugin
  from running at all.
- **Offline / no-network:** plugins are optional. A hub with none installed behaves identically to one with
  them, and the `acm` CLI **never loads or runs plugin code** (a test enforces it).

## Writing a plugin

```python
from pydantic import BaseModel
from acm_hub.plugins.base import BasePlugin, DeliveryResult, Event, PluginContext, PluginInfo


class MyConfig(BaseModel):
    room: str                                  # becomes a text field in the Plugins screen


class MyChat(BasePlugin):
    info = PluginInfo(key="mychat", name="My chat", kind="sink", config_schema=MyConfig,
                      secret_names=["token"], default_events=["proposal.pending"])

    def deliver(self, ctx: PluginContext, event: Event) -> DeliveryResult:
        r = ctx.http.post("https://chat.example.com/api", json={"room": ctx.config.room, "text": event.type},
                          headers={"Authorization": f"Bearer {ctx.secrets['token']}"})
        return DeliveryResult.success() if r.is_success else DeliveryResult.retry_later(f"HTTP {r.status_code}")
```

Register it in your package's `pyproject.toml`: `[project.entry-points."acm.plugins"] mychat = "my_pkg:MyChat"`.
Use `ctx.http` for network access (it enforces the egress rules) and `ctx.log` (secrets are redacted). A source
plugin implements `pull(ctx)` and calls `ctx.inbox.add(title, body, external_ref=...)`; keep `external_ref`
stable so re-pulls don't duplicate. You get no database handle and no API token — the context is the whole API.
