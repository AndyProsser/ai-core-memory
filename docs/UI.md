# Web UI

The hub's human interface (see [ARCHITECTURE.md § Web UI](ARCHITECTURE.md#web-ui)).
**Status:** Phase 1 is built (Memory, Record detail with history, Review's inbox and import
conflicts, Focus preview, Data, Settings, first-run setup, light/dark theme). the proposal queue with side-by-side conflict and merge cards, supersede and "Still true" on the record page, and the lifecycle settings (Phase 2). the Plugins screen (Phase 3). Projects, Teams and Users screens (Phase 4). The code is in
[`hub/src/acm_hub/web/`](../hub/src/acm_hub/web/); the screenshots-in-both-themes check used
during development is described under Testing below.

The UI exists for one reason: so a person can **see, correct, and move** what's
remembered about them and their work, without needing an AI to do it. It is a window
onto the store, not a second assistant.

## Principles

1. **Simple, clean, logical.** A handful of top-level sections, ordered by how people
   actually use the system. One primary action per screen. No dashboards of vanity
   metrics.
2. **No AI required, ever.** Every screen works with no model attached and no network
   beyond the hub itself. There is no chat box in the UI.
3. **Show the why.** Anywhere a record appears, its type, scope, tier, confidence, and
   status are visible and filterable, and its history is one click away. A memory you
   can't inspect is a memory you can't trust.
4. **Human edits are first-class, and accounted for.** Editing a record in the UI is
   normal; it creates a revision attributed to you. Changing an `established` record
   asks for explicit confirmation (see
   [ARCHITECTURE.md § Import / export](ARCHITECTURE.md#import--export--offline-human-operated)).
5. **Light and dark are both required.** Neither is an afterthought; both meet the
   contrast targets below.
6. **Boring technology.** Server-rendered HTML (Jinja2) with HTMX for partial updates
   and a few lines of vanilla JS (theme toggle, keyboard shortcuts). No SPA framework,
   no Node build step, no CDN dependencies — the UI must work on a LAN with no internet.
   Assets are served from the hub itself.

## Navigation

A single left rail (collapses to a top bar on narrow screens), five items, in workflow
order:

| Section    | What it's for                                                                                                  |
| ---------- | -------------------------------------------------------------------------------------------------------------- |
| **Memory** | Browse, search, filter, open, and edit records. The home screen.                                               |
| **Review** | The proposal queue and the inbox — everything waiting on a human decision. Shows a count badge.                |
| **Focus**  | "Focus preview": type a task, see exactly the context pack an AI would be handed, and why each record is in it. |
| **Data**   | Import, export, and backups. Offline-friendly by design.                                                       |
| **Settings** | Account & theme, API tokens, then (when relevant to the deployment mode) Teams & Users, Plugins, Instance.   |

A persistent **Quick capture** button (also `c`) opens a small form that adds an inbox
item — a snippet, an idea, a link — from anywhere. Scope/project default to the current
filter.

`solo` deployments hide everything team-related (Teams, visibility pickers); the screens
below simply don't show those controls. See
[SECURITY.md § Visibility](SECURITY.md#visibility--deployment-personas--one-schema-progressive-disclosure).

## Screens

### Memory

```text
┌────────────┬─────────────────────────────────────────────────────────────┐
│ ◆ Memory   │  Search records…                          [ + New record ]  │
│   Review ② │  Tier ▾  Type ▾  Scope ▾  Confidence ▾  Status ▾  Topic ▾   │
│   Focus    │ ─────────────────────────────────────────────────────────── │
│   Data     │  CORE (3 · 1.4k / 2k tokens)                                │
│   Settings │   ● never-merge-red-main     rule · project · established   │
│            │   ● prefers-terse-replies    user · user    · confirmed     │
│            │  ASSOCIATED (41)                                            │
│  ☀ ◐ ☾     │   ○ idempotent-retries       project · project · confirmed  │
│  Capture + │   ○ …                                                       │
└────────────┴─────────────────────────────────────────────────────────────┘
```

- List grouped by tier; core shows its budget usage.
- Filters are URL-addressable (bookmarkable, shareable inside the instance).
- Default view shows `active` records; Status filter reveals `superseded`, `stale`,
  `archived`.
- Search is FTS5 over name, description, body, topics.

### Record detail

Rendered markdown body with an **Edit** toggle (plain textarea + preview — no rich-text
editor). A sidebar shows the metadata (type, scope, tier, confidence, status, topics,
project), **Links** (related and supersedes/superseded-by), and a **History** tab: every
revision with author, source (`ui`/`cli`/`dream-cycle`/`mcp-write`/…), note, and a diff
against the previous revision. A **"How this changed"** timeline follows the
supersession chain, so you can read the evolution of a belief top to bottom.

Actions: promote/demote tier, change confidence, supersede with…, archive, export this
record. Anything gated (core over budget, `established` changes, scope widening) explains
why and, where it's a proposal-type action, offers to file a proposal instead.

### Review

One queue mindset, two sections: **Proposals** and **Inbox**.

- **Proposals** _(built in Phase 2)_ — each card says what would change in one sentence ("Merge
  `retry-policy` into `idempotent-retries`"), shows the diff, the rationale, who/what
  generated it (mechanical / dream skill / worker), and **Approve · Edit · Reject**.
  Batch-approve is offered only for `observed`-only proposals. Conflict cards (a new
  fragment vs. an `established` record) show both side by side and never have a
  one-click approve.
- **Inbox** — captured snippets with their source (UI, MCP, `obsidian`, `memos`) and
  link back to the original. Actions: **Turn into record**, **Keep as reference**,
  **Dismiss**. Un-actioned items are fine — the next dream cycle will classify them.

### Focus

One text box ("What are you working on?"), optional project and path pickers, and the
resulting context pack: core records first, then ranked associated records with their
`why` ("matched 'webhook' in body", "linked from core record X", "topic: payments"),
token total vs. budget. Same function the MCP `memory.focus` tool calls — so this is
also the debugging tool for "why didn't my AI know about X?"

### Data

Offline-first and explicit:

- **Export** — choose scope(s)/project(s)/filters, optionally include history and
  un-harvested inbox → download a `.zip` of markdown + `manifest.json`. A copy-paste
  `acm export …` equivalent is shown beside the button so the CLI path is discoverable.
- **Import** — upload a zip/markdown files → **dry-run report first** (create / update /
  unchanged / conflicts), then **Apply**. Conflicts land in Review as flagged
  revisions; nothing is silently overwritten.
- **Backups** — last export time, and the one-liner for scheduling `acm export` in cron
  or a Kubernetes `CronJob` (see [DEPLOYMENT.md](DEPLOYMENT.md#backups)).

### Settings

- **Account** — email, password (local accounts), linked SSO identity, **Theme**
  (System / Light / Dark).
- **Link single sign-on** (`/auth/oidc/link`, not a Settings tab) — shown when someone signs in with SSO
  and a local account already has their verified email: enter that account's password once to
  link the two. Reached only from the SSO callback; see SECURITY.md → OIDC details.
- **Encrypted private memory** — enable (passphrase + confirmation; the **recovery key** appears once on its own page,
  never in a URL), unlock/lock this session, change passphrase, regenerate the recovery key, recover, and turn off
  (needs the passphrase and typing "decrypt"). While your session is locked, a banner on every page says so and
  private records show a locked placeholder. The token form and the OAuth consent screen each gain an "including what
  I keep encrypted" checkbox, offered only when this session is unlocked and personal memory is included. See
  [SECURITY.md](SECURITY.md#encrypted-private-memory-optional).
- **API tokens** — create (label, project scope, read-only/read-write, expiry — default
  90 days), shown **once** in a copy box with a ready-to-paste `claude mcp add …` line
  and an `.mcp.json` snippet; list with prefix, scope, last used, expiry; **Revoke**
  (immediate). See [SECURITY.md](SECURITY.md#api-tokens-minimizing-the-blast-radius-of-a-leak).
- **Projects** — everyone: projects you can see, with visibility (`private`/`team`/`public`, limited to what
  the deployment mode allows) and owning team. Controls appear only where you may use them (a project's owner,
  or a team's owners). Deleting asks you to type the project's name and, if it holds archived records, tick a
  separate "permanently delete them" box; live records block deletion.
- **Connected apps** _(only when MCP OAuth is on)_ — apps you signed in to the hub (e.g. Claude.ai), each with
  the projects and access level you approved and a **Disconnect** button that ends it immediately. A separate
  **consent screen** (`/oauth/consent`) appears when an app asks to connect: it names the app (as inert text),
  shows where your browser will go, warns for loopback targets, and makes you choose projects (none ticked is
  refused), personal memory (off by default) and read-only/read-write before anything is shared.
- **Teams** _(hidden in `solo`)_ — teams you belong to with roles; an owner adds members by email, changes
  roles, removes people. An admin creates a team (naming its first owner) but sees only its name, and the page
  says so. Last owner is protected.
- **Users** _(admin)_ — create accounts (local password or SSO invite), deactivate/reactivate, grant or remove
  admin, reset a local password (shown once). Deactivation signs the person out and revokes their tokens.

Settings is a tab row — Account · Projects · Teams · Users · Plugins — showing only what your role and the
deployment mode allow.
- **Plugins** _(admin; built in Phase 3, reached from Settings)_ — a list of configured instances with
  on/off, last status and what each can see (nothing / which scopes / full text / personal memory are
  all flagged), then the available plugins to add. Each instance has a form generated from the plugin's
  config schema: secrets as environment-variable _names_ (with a "set / not set in the hub's environment"
  indicator, never the value), the scope and project allowlist, detail level, event checkboxes, and for
  sources a pull interval. **Send a test notification**, **Check now**, recent deliveries with errors, and
  **Remove**. Admin only. See [PLUGINS.md](PLUGINS.md).
- **Search plugins** — a plugin that adds semantic search shows as "adds semantic search" in the Plugins list. Its form has
  the usual allowlist and detail level but no notification events, and a sync interval; its page has **Rebuild index**
  (sends everything now, and says how many records are indexed). Focus preview shows `semantic match (<instance>)` as a
  reason when the plugin found something.
- **Instance** _(admin)_ — deployment mode, auth providers (local / OIDC: issuer, client
  ID, secret ref), core token budget, staleness windows, proposal auto-apply policy.

### First run

A one-page setup: create the first admin (local account — kept as the recovery path even
if OIDC is enabled later), pick a deployment mode (`solo` is preselected), optionally
configure OIDC, done. The same can be done headless via `acm` / environment variables
for container deployments.

## Theming: light and dark

Both themes are mandatory and defined once, as CSS custom properties on `:root`, with
dark values overridden under `[data-theme="dark"]`.

- **Three user choices:** System (default, follows `prefers-color-scheme`), Light, Dark.
  Stored per user in `users.theme_preference` and mirrored to `localStorage` so the
  login page and pre-auth screens match.
- **No flash of wrong theme:** a tiny inline script in `<head>` sets `data-theme` before
  first paint. The toggle (☀ ◐ ☾) lives in the left rail on every screen, and the page
  sets `color-scheme: light dark` so native controls, scrollbars, and form fields follow.
- **Tokens, not hard-coded colors.** Components reference tokens only; adding a theme
  later means adding one block of values.

Starting palette (contrast ratios checked against WCAG 2.2; text ≥ 4.5:1, UI components
≥ 3:1):

| Token             | Light     | Dark      | Use                                              |
| ----------------- | --------- | --------- | ------------------------------------------------ |
| `--bg`            | `#FAFAF9` | `#131211` | page background                                  |
| `--surface`       | `#FFFFFF` | `#1C1A19` | cards, rail, inputs                              |
| `--text`          | `#1C1917` | `#ECEAE7` | body text (16.7:1 / 15.6:1 on `--bg`)            |
| `--text-muted`    | `#57534E` | `#A8A29E` | secondary text (7.3:1 / 7.4:1 on `--bg`)         |
| `--border`        | `#D6D3D1` | `#3A3634` | dividers (decorative)                            |
| `--border-strong` | `#78716C` | `#78716C` | input and button outlines (≥ 3:1 on `--surface`) |
| `--accent`        | `#2748C8` | `#9DB2FF` | links, primary buttons, focus ring (7.1:1 / 9.1:1) |
| `--on-accent`     | `#FFFFFF` | `#0E1330` | text on accent                                   |
| `--ok`            | `#166534` | `#6EE7A0` | confirmed/applied                                |
| `--warn`          | `#92400E` | `#FBBF5E` | stale, needs review                              |
| `--danger`        | `#B42318` | `#FF8A80` | conflicts, revoke, destructive actions           |

Typography: system font stack (no webfont downloads), 16px base, 1.5 line height,
`ui-monospace` for ids/tokens/diffs. Generous spacing, single-column content max-width
of ~72ch for reading records.

Meaning is never carried by color alone: tier, confidence, and status are always text
badges (`CORE`, `confirmed`, `stale`), with color as reinforcement.

## Accessibility & responsiveness

- Keyboard-complete: logical tab order, visible focus ring (`--accent`, 2px offset),
  shortcuts (`/` search, `c` capture, `g m`/`g r`/`g f`/`g d` to navigate) that never
  trigger inside text fields, all listed in a `?` overlay.
- Semantic landmarks, labelled form controls, `aria-live` for HTMX updates (toasts like
  "Revision saved").
- Honors `prefers-reduced-motion` (no animation is required to use the UI) and browser
  zoom to 200% without horizontal scroll.
- Works on a phone-width screen: the rail becomes a top bar, tables become stacked
  cards. Review on a phone is a first-class use case (approving a proposal from a
  Slack/ntfy notification link).

## What the UI deliberately doesn't do

- No in-app chat or AI-generated content.
- No bulk free-text "edit everything as one big document" view — records are edited one
  at a time so revisions stay meaningful.
- No analytics, telemetry, or third-party requests. The only network traffic is to the
  hub itself.
- No path around the rules: scope isolation, confidence gating, and the core budget apply
  to the UI exactly as they do to MCP and import.

## Testing

Built and running today: integration tests that drive the real app (login, CSRF, every
form, the sanitized-markdown rendering, theme persistence), and a test that fails if any
template contains an inline style, script, or event handler (the CSP would silently drop
them — that is how a real bug, a core-budget meter that rendered full-width, was caught).
UI changes are also reviewed by screenshotting each screen in light, dark, and phone width.

**Real-browser checks.** In-process tests can't see what a browser does, and that mattered: until Phase 5 every plain
form returned 403 in a real browser — the `csrf()` template macro was imported without template context, so each
form's hidden token was empty — while every test passed, because the tests posted the token read from the page's meta
tag. It was found by driving the OAuth consent screen in Chromium. Now a regression test
(`tests/test_csrf_forms.py`) submits forms using only their own hidden field and fails if any is empty (it reproduces
the original failure when the fix is reverted), and two opt-in scripts under
[`hub/tests/e2e/`](../hub/tests/e2e/README.md) drive a running hub in a real browser: the main forms, and the
whole OAuth flow including the approval redirect through the consent page's CSP.

Still to add: an automated contrast check over the token table, keyboard-navigation tests
(Playwright), and a "works with the network off" test that loads every screen with all
external requests blocked.
