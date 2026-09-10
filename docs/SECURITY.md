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
- **Expiring by default.** Tokens carry an expiry (a sensible instance-level default —
  90 days is reasonable — configurable per token up to an instance maximum). "Forever"
  is exactly how a token from two years ago becomes the thing that leaks quietly.
- **Revocation is immediate.** `revoked_at` is checked on every request; there's no
  caching window where a revoked token still works.
- **Never logged, never in a URL.** Always `Authorization: Bearer <token>`; redacted
  from access and error logs; rejected outright over plaintext HTTP except from
  localhost or an RFC1918 address (self-hosted instances still need to work on a LAN
  without forcing a TLS setup just to try it out).
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
