# Security: tokens, visibility, roles, and auth

This document covers the parts of the [memory hub](ARCHITECTURE.md#memory-hub-cross-project-store)
that exist specifically to limit blast radius: how API tokens are minted and revoked,
how visibility scales from one person to many teams without becoming three different
systems, who's allowed to do what, and how people actually log in. Same status as the
rest of the hub design — decided, not yet implemented.

## API tokens: minimizing the blast radius of a leak

The threat model here is concrete: a token ends up somewhere it shouldn't — a public
repo, a CI log, a compromised runner. The goal is that this is an annoyance, not a
breach. Every point below exists for that one reason.

- **Format & entropy.** Generated with a CSPRNG, at least 256 bits, with a recognizable
  prefix (`acm_live_<random>`). The prefix matters as much as the entropy: it's what
  lets secret scanners (GitHub secret scanning, gitleaks, truffleHog) actually find a
  leaked token automatically, and what lets a human glancing at a config file recognize
  it for what it is.
- **Hashed at rest, shown once.** The database stores `SHA-256(token)`, never the raw
  value. A fast hash is _correct_ here, not a shortcut — unlike a password, a
  256-bit random token isn't guessable, so there's nothing for a slow hash (Argon2id,
  bcrypt) to defend against; those are for the local-auth password path instead (see
  Authentication below). The raw token is displayed exactly once, at creation, and
  never retrievable again — only its prefix, label, and usage metadata are shown after that.
- **Scoped, not blanket.** A token is minted against an explicit scope: specific
  project ID(s) — defaulting to the one project it was created for, not "everything the
  user can see" — and an access level (`read_only` or `read_write`). A token that leaks
  out of one project's CI pipeline shouldn't expose every project its creator has access to.
  **Personal (`user`-scope) memory is a separate, explicit grant** (`include_user_scope`,
  off by default): a CI token has no business reading how its owner likes to work, and a
  token without the grant can neither read nor write user scope. Team scope reaches a
  project-limited token only for teams that own one of its projects, and is read-only for it.
- **Expiring by default.** Tokens carry an expiry (a sensible instance-level default —
  90 days is reasonable — configurable per token up to an instance maximum). "Forever"
  is exactly how a token from two years ago becomes the thing that leaks quietly.
- **Revocation is immediate.** `revoked_at` is checked on every request; there's no
  caching window where a revoked token still works.
- **Never logged, never in a URL.** Always `Authorization: Bearer <token>`; redacted
  from access and error logs; rejected outright over plaintext HTTP except from
  localhost or an RFC1918 address (self-hosted instances still need to work on a LAN
  without forcing a TLS setup just to try it out). "Private" is an explicit list —
  loopback, `10/8`, `172.16/12`, `192.168/16`, link-local, IPv6 ULA — not Python's broader
  `is_private`, which also covers documentation and other reserved ranges. Behind a TLS-terminating
  reverse proxy, set `MEMORY_HUB_TRUST_PROXY=true` so `X-Forwarded-Proto` is honoured (off by
  default: those headers are spoofable by anyone who can reach the hub directly).
- **Owned by a user, minted by a person.** A token acts as the user who created it,
  never with more access than that user has, and can only be created from a logged-in web
  session or the host CLI (never by another token). Each token is created with a label,
  project scope, access level, and expiry — and the UI hands back a ready-to-paste
  `claude mcp add` line and `.mcp.json` snippet at that moment, since it's the only time
  the raw value exists.
- **Traceable per token, not just per user.** `memory_revisions` records
  `changed_by_token_id` alongside `changed_by_user_id`. If a specific token turns out to
  be compromised, revoking it is enough — you don't have to distrust everything that
  human ever wrote.
- **Rate-limited per token**, independently of the human's own limit, so abuse of a
  leaked token is both slower to exploit and easier to notice in the audit trail.

## Visibility & deployment personas — one schema, progressive disclosure

The three personas from the original design conversation — indie developer, small team,
multiple teams — don't need three different systems. They need one schema that's
capable of the most complex case, with a `deployment_mode` instance setting that decides
how much of it is actually exposed:

| `deployment_mode` | What's true                                                                                                                                 | Visibility exposed            |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------- |
| `solo`            | No teams exist. Every project has `team_id = NULL`, owned by a single user. Additional users can still be added — they just aren't grouped. | `private` / `public`          |
| `team`            | One team (occasionally a couple), everyone's a member.                                                                                      | `private` / `team`            |
| `multi_team`      | Multiple teams, project isolation between them, cross-team sharing is opt-in.                                                               | `private` / `team` / `public` |

The underlying model is identical at every level — a `projects.visibility` enum
(`private`, `team`, `public`) plus the existing `scope`/`type`/`confidence` model from
[ARCHITECTURE.md](ARCHITECTURE.md#vocabulary). Moving from `solo` to `team` to
`multi_team` is a one-way reveal of more UI/API surface on the same tables, decided
during first-run setup (or changed later by an admin) — never a data migration. That's
the whole trick: build for `multi_team` once, and `solo` is just `multi_team` with the
team-related screens hidden and no team rows in the database.

**What each visibility level means, precisely:**

- `private` — visible only to the project's owning user. Equivalent to `user` scope for
  the project itself, though records tied to it can still be `project`-scope and shared
  with nobody else.
- `team` — visible to members of the project's owning team (`team_id` must be set;
  `visibility = 'team'` with `team_id IS NULL` isn't a valid state). This is what a
  small-team instance's "public projects" really means when there's only one team — the
  UI can label it either way, the data model doesn't care.
- `public` — visible to **every authenticated user of this hub instance**, across every
  team. Not anonymous, not internet-exposed — "public" means "public within your own
  self-hosted instance." Read visibility only: being able to _see_ a public project
  never implies being able to _write_ to it — writes still follow ownership/team
  membership rules regardless of visibility.

This composes with, and doesn't replace, the scope rules already in ARCHITECTURE.md: a
project's `visibility` governs who can see `project`-scope records tied to it. `team`-
and `user`-scope records follow the partition rules defined there regardless of their
project's visibility flag — a `user`-scope record is never visible to anyone but its
owner, even inside a `public` project.

## Roles

| Role     | Level         | Can                                                                                                                                 | Cannot                                                                                                                                                               |
| -------- | ------------- | ----------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `admin`  | instance-wide | Manage user accounts, configure auth providers, set `deployment_mode`, create/delete teams.                                         | Read any user's `private`-scope memory, or a team's `team`-scope memory for a team they aren't a member of — admin is a platform role, not a memory-access override. |
| `owner`  | per-team      | Invite/remove members, promote/demote member↔owner within their own team, create/delete projects, set a project's visibility.       | Manage another team's membership or projects, override another user's private memory.                                                                                |
| `member` | per-team      | Read/write `team`- and `project`-scope memory for projects their team can see, manage their own `user`-scope memory and API tokens. | Manage team membership or project visibility.                                                                                                                        |

Admin sees exactly what a regular user with the same team memberships would see, plus
account/team/auth administration — it's an operational role, not a backdoor into
content. That said, be honest about the limit of any software-enforced boundary: on a
self-hosted instance, whoever has filesystem or database access to the SQLite file can
read it regardless of API-level roles — no self-hosted system can prevent that, and for
a solo or trusted-small-team deployment that's an acceptable, well-understood tradeoff.
It matters more once a `multi_team` instance is run by an operator who isn't equally
trusted by every team on it; if that's a real deployment target, encrypting `private`-
scope record bodies at rest (client-side, keyed per user) is the natural next step —
noted as a roadmap idea, not built now, since it isn't needed until someone actually
needs it.

## Authentication: local and OIDC, both from day one

Two built-in auth providers, chosen per instance (an instance can enable both at once —
e.g. an admin with a local account, everyone else via OIDC):

- **Local accounts** — email + password. Passwords are hashed with Argon2id (or bcrypt
  as a fallback), which is the opposite tradeoff from API tokens above: passwords are
  low-entropy and human-guessable, so the hash needs to be deliberately slow.
- **OIDC** — standard OpenID Connect authorization-code flow with PKCE, configured per
  instance (issuer URL, client ID/secret, redirect URI). This is what lets a team plug
  into whatever SSO they already run — Google Workspace, Okta, Authentik, Keycloak,
  GitHub OIDC — instead of maintaining a second set of credentials.

Both providers resolve to the same `users` row (`auth_provider` + `external_id` for
OIDC-provisioned accounts), so everything downstream — teams, tokens, memory access —
is identical regardless of how someone logged in. OIDC governs the human web-UI login
only; AI/MCP clients still authenticate with API tokens (see above), since an
interactive OAuth flow doesn't make sense for a long-running agent.

First-OIDC-login provisioning is an instance setting, not a fixed behavior: either
auto-create a `member` account on first successful login, or require an admin to have
pre-invited that email — the right default depends on whether the instance is `solo`
(auto-create is fine) or `multi_team` inside an org that wants to control who gets an
account (pre-invite only).

### OIDC details

- **Flow:** authorization code with PKCE, plus `state` and `nonce` validated on return;
  ID token signature, issuer, audience, and expiry verified against the provider's JWKS
  (cached, refreshed on key rotation). Only `https` issuers are accepted outside
  localhost/RFC1918.
- **Identity key is `(issuer, sub)`**, not email — an email change or reuse at the
  provider can't silently hand one person another's account. Email is used to match a
  pre-invited account on first login, and only when the provider reports
  `email_verified`.
- **Local admin stays as the recovery path.** The bootstrap admin keeps a local password
  even when OIDC is enabled, and `acm user` can create or reset a local admin from the
  host (see CLI access below), so a broken or misconfigured IdP never locks the operator
  out of their own data. An instance can disable local login for non-admin users.
- **Optional later:** map OIDC group claims to team membership/roles. Not in the first
  release — membership is managed in the hub.

## Web sessions

The web UI authenticates with a server-side session, not an API token:

- Session cookie: `HttpOnly`, `Secure` (except on localhost/RFC1918 plain HTTP),
  `SameSite=Lax`, rotated on login, idle and absolute timeouts configurable.
- **CSRF protection** on every state-changing request (synchronizer token via HTMX
  headers); the UI never accepts API tokens, and the API never accepts session cookies
  for MCP/REST token routes — the two credential types can't be confused.
- Strict `Content-Security-Policy` — `script-src 'self'; style-src 'self'; default-src 'none'` —
  with **no inline scripts, styles, or event handlers at all** (a test fails the build if one
  appears in a template). The no-flash theme bootstrap is a same-origin _blocking_ script in
  `<head>`, not an inline one, so no nonce machinery is needed. No third-party origins: the UI
  loads nothing from a CDN (htmx is vendored).
- Login throttling per account and per IP; password-change and token-mint actions
  re-prompt for the password (local accounts) or recent OIDC re-authentication.
- Record bodies are untrusted markdown: rendered through a sanitizing renderer (no raw
  HTML, no script, links `rel="noopener noreferrer"`) so a malicious memory can't attack
  the person reviewing it.

## What AI clients cannot do

Tokens authenticate **AI clients and integrations**, and the capability boundary is part
of the security model, not just convenience:

- A token can read, write, and _propose_ within its scope. It **cannot** approve or
  reject a proposal, mint or revoke tokens, change instance settings, manage users or
  teams, install/configure plugins, or run import/export. Concretely, as enforced in the
  record service: it can't set a record `established`, can't change a record's tier
  (core promotion is human-gated), can't write `team` scope, can't change a record's scope,
  and can't change an `established` record at all (it gets a conflict to surface to the user).
  `memory_sync` / `memory_write` from a token that collides with an `established` or newer
  record parks the incoming version as a flagged, unapplied revision _and_ a `conflict`
  proposal for a person to resolve. Those are human-session-only
  operations. A compromised token can therefore at worst add `observed` records and
  proposals within its scope — and every one of them is attributed to the token and
  reviewable.
- Writes from a token are subject to the same confidence-tier and core-budget
  enforcement as everything else; there is no "trusted" token tier that skips them.
- Prompt-injection hygiene: record bodies and inbox items are _data_. The hub marks
  content from `plugin:*` and unattributed sources (`source_trust: external`) so a client
  or the UI can treat it with suspicion — a note scraped from the web shouldn't be able
  to promote itself to a `rule` without a person approving it, which the proposal gate
  already guarantees.

## CLI access and the local trust boundary

The `acm` CLI (see [ARCHITECTURE.md § Import / export](ARCHITECTURE.md#import--export--offline-human-operated))
operates directly on the SQLite file or a plain-file memory directory. That makes it
exactly as trusted as **filesystem access to the host**, which — as noted under Roles —
is already the real boundary on a self-hosted instance. The CLI therefore:

- needs no token, but refuses to run unless it can read/write the database file (OS
  permissions are the gate; the file and data directory are created `0600`/`0700`);
- records `change_source = 'cli'` and the OS username on every revision it writes;
- applies the same confidence-tier and `--confirm-established` rules as the UI;
- is how the first admin and any recovery account are created, so there is no default
  password and no "first visitor to the URL becomes admin" window — the first-run UI
  setup is only available while no admin exists **and** requires a one-time setup code
  printed in the server log / returned by `acm setup-code`.

## Plugins and egress

Plugins (see [PLUGINS.md](PLUGINS.md)) are the one place memory-adjacent data can leave
the hub, so they get explicit rules. Cross-talk is a one-way-door risk: a record posted
to the wrong Slack channel can't be un-posted.

- **Operator-installed, in-process, trusted.** A plugin is code the operator chose to
  install; the hub doesn't sandbox it. The UI configures plugins but can never upload or
  run code. Treat installing one like installing any dependency: pin and review it.
- **Deny by default.** A new plugin instance has an empty scope allowlist. The operator
  must explicitly allow each scope (and project) it may see.
- **`user` scope never egresses by default**, and a per-instance setting to include it
  requires an admin-confirmed acknowledgement; even then it can only ever include the
  _operator's own_ user-scope records, never another user's.
- **Metadata-only egress by default.** Sinks receive ids, names, types, and a link back
  to the hub — not record bodies. `egress: full` is a per-instance opt-in, shown in the
  Plugins screen so nobody forgets it's on.
- **Secrets by reference.** Plugin config stores environment variable _names_; values are
  resolved at call time, held only in memory, and redacted from logs. They are never
  written to records, events, revisions, exports, or the database.
- **No ambient authority.** A plugin gets a narrow context object — no DB handle, no API
  token, no ability to call other plugins. Inbound (source) plugins can only add inbox
  items; they cannot write records or approve anything.
- **Inbound content is untrusted.** Inbox items from plugins are marked external, never
  auto-promoted, and subject to the normal dream-cycle classification and human-gated
  promotion — a hostile note in a synced Obsidian vault or Memos instance cannot become
  a `rule` or a core record on its own.
- **Bounded and isolated.** Per-call timeouts, bounded retries with backoff, per-instance
  rate limits, and failures that can't block writes or other plugins. Outbound HTTP
  from plugins goes through one egress helper that refuses non-HTTPS targets (except
  localhost/RFC1918) and can be restricted by an operator-configured host allowlist.
- **Auditable.** Every delivery and every inbound pull is recorded (`plugin_deliveries`,
  last status/error on the instance) and visible in the Plugins screen. (Source plugins only add inbox
  items, so there are no `plugin`-sourced revisions to audit; what they capture is marked `plugin:<key>`.)

**What is enforced, and where the limits are** (Phase 3):

- _Enforced and tested:_ the scope/project allowlist is deny-by-default; an instance only receives events
  its **owner could read themselves**; `user` scope needs the acknowledgement _and_ is only ever the owner's
  own; payloads never contain record bodies (full text is fetched at delivery time, only at `egress: full`,
  only if the record is itself allowed); secrets are env-var _names_ in the database and are scrubbed from
  stored errors and logs; the egress client refuses non-HTTPS (except localhost/private LAN), follows no
  redirects, honours the host allowlist and caps response size; an instance never receives events its own
  activity caused; plugins are admin-configured only; the offline CLI never loads plugin code; plugin
  failures, hangs and floods are isolated and rate-limited.
- _Tested by mutation:_ removing the user-scope acknowledgement check, the owner-visibility check, secret
  redaction, or the Obsidian path guards makes the corresponding tests fail.
- _Limits, stated plainly:_ plugins are **trusted code running in the hub process** — an installed plugin
  could do anything the hub process can; the guards above constrain plugins that use the provided context,
  not a malicious one. `apprise` does its own HTTP (the hub validates the URLs it is given, but doesn't proxy
  the traffic). A hung plugin call is abandoned, not killed. The plugin scheduler is in-process, so run a
  single hub process. Nothing stops an operator from choosing `egress: full` and a public channel, so the
  Plugins screen flags full-text and personal-memory instances loudly.

## Proposals: AI suggestions, human decisions

The proposal queue (see [ARCHITECTURE.md](ARCHITECTURE.md#who-does-the-reasoning-the-proposal-queue))
is where AI-suggested change meets a person, so its rules are part of the security model:

- **A token can propose; only a session or the host CLI can decide.** `memory_propose` needs a
  read-write token; there is no tool to approve or reject. The mechanical job's system
  principal can't decide either, and it can never change an `established` record, a tier, or a
  scope (a test asserts each of these).
- **An AI can't pose as the hub.** It cannot raise `review_established` or `conflict`, its
  `generated_by` can never be `mechanical`, and so its proposals are never eligible for
  auto-apply or the "approve all low-risk" batch, which cover hub-generated, all-`observed`
  proposals only.
- **Visibility follows the records.** A proposal is visible only to people who can read _every_
  record it touches; approving needs write access to them. Proposals about someone's
  user-scope memory are invisible to everyone else, admins included.
- **Rationales and payload text are untrusted.** They're rendered escaped in the UI, capped in
  size (1,000 characters of rationale, 20 KB of payload), and never interpreted.
- **Spam resistance.** Identical open proposals are reused, and one a person rejected isn't
  re-raised for 30 days — a misbehaving client can't bury the review queue.
- **Approval is atomic and re-validated.** On approval the hub re-checks that the records still
  fit the proposal (closing it as `expired` if not) and applies it in a savepoint, so a failure
  half-way leaves nothing changed.

## Implementation notes

What is built, and the limits of it, stated plainly:

- Login throttling and the per-token rate limit are **in-process** (the hub is a single
  process by design). They reset on restart and wouldn't be shared across replicas — fine
  for self-hosting, something to move to the database before any multi-replica deployment.
- The first-run setup code is written to the server log and stored only as a hash; whoever
  can read the log or run `acm setup-code` can claim setup. That's the same trust boundary as
  filesystem access (see CLI access above) and closes permanently once an admin exists.
- There is no email, so there is no emailed password reset: a locked-out local admin
  recovers with `acm user set-password` on the host. OIDC users recover through their IdP.
- The consolidation scheduler runs in the hub process; if you run several hub processes against one
  database (not supported with SQLite), each would schedule its own pass.
- Records and the database are not encrypted at rest (see Roles above for the
  roadmap idea). The DB file and its directory are created `0600`/`0700`.
- Tokens and sessions are looked up by the SHA-256 of a 256-bit random value; there's no
  per-secret salt because there's nothing low-entropy to protect. Passwords use Argon2id.
