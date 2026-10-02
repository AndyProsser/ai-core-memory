# Plugins

How the memory hub (see [ARCHITECTURE.md § Memory hub](ARCHITECTURE.md#memory-hub-cross-project-store))
connects to the world outside itself: note systems like Obsidian and Memos, and
notification channels like Slack and Teams. Same status as the rest of the hub design —
decided, not yet implemented. Security rules for everything here live in
[SECURITY.md § Plugins and egress](SECURITY.md#plugins-and-egress).

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
| `core.budget_exceeded` | a core promotion was blocked by the token budget                   | proposal id                          |
| `inbox.new`            | an inbox item arrived                                              | id, source, title                    |
| `digest.weekly`        | schedule: summary of what changed, what's pending, what went stale | counts + titles, link to the Review screen |
| `plugin.failed`        | a plugin instance hit repeated errors                              | plugin instance, last error          |

Every payload carries a `link` to the relevant UI screen, so a notification is a pointer
back to the hub, not a copy of memory.

## Plugin interface

Discovered via the `acm.plugins` Python entry-point group. A plugin declares metadata, a
config schema (rendered as a form in the Plugins screen), and implements one or both
protocols. Sketch — final signatures are settled at implementation time:

```python
class PluginInfo(BaseModel):
    key: str                      # "obsidian", "memos", "apprise"
    name: str
    kind: Literal["source", "sink", "both"]
    config_schema: type[BaseModel]   # non-secret settings; secrets are env-var *names*
    secret_names: list[str] = []     # env vars this plugin expects, e.g. ["SLACK_WEBHOOK_URL"]
    subscribes_to: list[str] = []    # default event types (operator can narrow)

class SourcePlugin(Protocol):
    def pull(self, ctx: PluginContext) -> Iterable[InboxItem]: ...
        # Idempotent: set InboxItem.external_ref so a re-pull doesn't duplicate.
    def export(self, ctx: PluginContext, records: list[RecordView]) -> None: ...  # optional

class SinkPlugin(Protocol):
    def deliver(self, ctx: PluginContext, event: Event) -> DeliveryResult: ...
        # Raise or return DeliveryResult.retry(...) on transient failure.

class PluginContext:
    config: BaseModel             # validated, non-secret
    secrets: Mapping[str, str]    # resolved from env at call time, never persisted
    scopes: set[Scope]            # what this instance is allowed to see
    inbox: InboxWriter            # source plugins only
    log: Logger                   # secrets are redacted from log output
```

Rules the loader enforces, so a plugin author can't get them wrong:

- `RecordView`/`Event` objects are **pre-filtered** to the instance's scope allowlist and
  carry only the fields its `egress` level permits (`metadata` default, `full` opt-in
  per instance, per SECURITY.md).
- **Loop prevention.** Records and inbox items created by a plugin carry
  `source: plugin:<key>` and an `external_ref`; events caused by a plugin's own writes
  aren't redelivered to that same instance.
- Calls run with a timeout, in a worker thread, with exceptions caught and recorded —
  never on the request path.
- Plugins get **no database handle and no token**; the context is the whole API.

## Built-in plugins (planned)

| Plugin     | Kind   | What it does                                                                                                                                                                                                                                                                                                  |
| ---------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `obsidian` | source | Points at a vault directory on disk (no Obsidian API needed — a vault is a folder of markdown). Pulls notes matching a configurable folder or tag (e.g. `#memory`, `Inbox/`) into the inbox with `external_ref = file path`. Optional export: writes a weekly digest note and/or selected records into a folder, as plain markdown. |
| `memos`    | source | Uses the Memos REST API (base URL + access token via env var). Pulls memos with a chosen tag (e.g. `#memory`) into the inbox; `external_ref` = memo URL. Optional export: creates a memo for a digest.                                                                                                        |
| `apprise`  | sink   | One dependency that covers Slack, Microsoft Teams, ntfy, Discord, Telegram, email, and generic webhooks via Apprise URLs held in env vars. This is the recommended path for "notify me / post to a channel" — Slack and Teams don't each get a bespoke plugin unless Apprise proves insufficient.             |
| `webhook`  | sink   | Plain signed JSON POST (HMAC header) to any URL — the escape hatch for anything else, and a template for writing a new sink.                                                                                                                                                                                  |

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

- **Install:** add the package to the hub's environment (`pip install acm-plugin-foo`, or
  bake it into the container image); restart. Entry points make it appear in the
  Plugins screen.
- **Configure:** Plugins screen → add instance → fill the generated form → set scope
  allowlist, egress level, and event filter → **Send test event**. Secrets are env var
  names; the screen shows whether each resolved, never its value.
- **Watch:** each instance shows last run, last status, and a retry/failed-delivery
  count; `plugin.failed` can itself notify through a different sink.
- **Offline / no-network:** plugins are optional. A hub with none installed behaves
  identically to one with them, and the `acm` CLI never loads plugins.
