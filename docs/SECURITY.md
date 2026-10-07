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
  reverse proxy, set `MEMORY_HUB_TRUST_PROXY=true` **and** `MEMORY_HUB_TRUSTED_PROXIES` to the proxy's
  address so `X-Forwarded-Proto`/`-For` are honoured — and only from it (off by default: those headers are
  spoofable by anyone who can reach the hub directly; see [Network edge](#network-edge-proxy-trust-headers-and-limits)).
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

**As built (Phase 4), the admin boundary is deliberately tighter than "an owner with extra powers":**

- An admin **creates a team only together with its first owner**, and from then on has no membership rights
  over it: adding, removing or re-roling members is the team's owners' job. The admin sees a team's name, not
  who is on it (the Teams page says so rather than hiding the gap).
- An admin creates and **deactivates** accounts and resets local passwords (the temporary password is shown
  once, in the response body — never in a URL — and the user is signed out everywhere). Deactivation blocks
  sign-in, ends every session and revokes every token immediately, and disables the user's plugin instances;
  it never deletes memory. The last active admin can't be deactivated or demoted, and you can't deactivate
  yourself.
- A team's last owner can't be removed or demoted, so a team can't be orphaned.
- Project management follows the same rule: a personal project is managed by its owner, a team project by the
  team's owners — and **not** by an admin.
- Deleting a team or project never silently destroys memory: live records block deletion; archived-only
  records are removed only after an explicit, typed confirmation (`--confirm <slug>` plus `--purge-archived`,
  or the checkbox in the UI). Deleting a team is the one admin action that can purge content the admin can't
  read, which is why it is gated this way.
- Role changes take effect on the very next request: principals are loaded per request, not cached in the
  session, so removing a member or deactivating a user cuts access without waiting for expiry.
- API tokens can't perform any user, team or project administration, whatever the owning user's role.
- Every one of these has a test that fails if the protection is removed (`hub/tests/test_orgs*.py`,
  `test_org_ui.py`, `test_cli_orgs.py`).

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
only; AI/MCP clients authenticate with API tokens (see above), or — where an app can't take a pasted
token, such as Claude.ai connectors — with [MCP OAuth](#mcp-oauth-optional), whose consent step reuses
this same login.

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
- **Linking an SSO identity to an existing local account needs that account's password.**
  A verified-email match alone is never enough — whoever controls that address at the
  provider (a misconfigured or compromised IdP, a reused address) would otherwise own the
  account, which may be an admin. Instead the callback parks the verified identity in a
  signed, HttpOnly, 10-minute cookie scoped to `/auth/oidc/link` (HMAC with
  `MEMORY_HUB_SECRET_KEY`, bound to one purpose and one account; nothing is stored
  server-side), and the user proves the local password there. That form shares the login
  lockout (same per-account and per-address budget, so it is not a second guessing path),
  is same-origin only, and refuses deactivated accounts, accounts already linked to a
  different identity, and accounts whose local login is disabled. A successful link sets
  the identity key on the existing account; it never creates a second one, and local
  login keeps working alongside SSO.
- **Local admin stays as the recovery path.** The bootstrap admin keeps a local password
  even when OIDC is enabled, and `acm user` can create or reset a local admin (and `acm user set-email` change its address) from the
  host (see CLI access below), so a broken or misconfigured IdP never locks the operator
  out of their own data. An instance can disable local login for non-admin users.
- **Optional later:** map OIDC group claims to team membership/roles. Not in the first
  release — membership is managed in the hub.

## Web sessions

The web UI authenticates with a server-side session, not an API token:

- Session cookie: `HttpOnly`, `Secure`, `SameSite=Lax`, rotated on login, idle and absolute timeouts
  configurable. `Secure` is **always** on when `MEMORY_HUB_PUBLIC_URL` is `https://` — whatever the last hop to
  the hub looks like, since behind a TLS-terminating proxy that hop is plain http from a private address — and
  otherwise on except for plain HTTP from localhost/RFC1918. With an https public URL the cookie is also named
  `__Host-acm_session`, so a sibling subdomain or a plain-http origin can't plant or overwrite it.
- **CSRF protection** on every state-changing request (synchronizer token via HTMX
  headers); the UI never accepts API tokens, and the API never accepts session cookies
  for MCP/REST token routes — the two credential types can't be confused. A matching `Origin` is required when
  sent, and a browser that sends `Sec-Fetch-Site` must say `same-origin` or `none`: a valid token submitted from a
  `same-site` (sibling subdomain) or `cross-site` page is refused. Login, first-run setup and the SSO-link form
  have no session token to check, so they rely on the same two headers.
- Strict `Content-Security-Policy` — `script-src 'self'; style-src 'self'; default-src 'none'` —
  with **no inline scripts, styles, or event handlers at all** (a test fails the build if one
  appears in a template). The no-flash theme bootstrap is a same-origin _blocking_ script in
  `<head>`, not an inline one, so no nonce machinery is needed. No third-party origins: the UI
  loads nothing from a CDN (htmx is vendored). The rest of the response headers are listed under
  [Network edge](#network-edge-proxy-trust-headers-and-limits).
- Login throttling (budgets below); password-change and token-mint actions
  re-prompt for the password (local accounts) or recent OIDC re-authentication, and those password checks are
  themselves limited to 8 per 10 minutes per person, so a stolen session can't be used to guess the password.
- Record bodies are untrusted markdown: rendered through a sanitizing renderer (no raw
  HTML, no script, links `rel="noopener noreferrer"`) so a malicious memory can't attack
  the person reviewing it.

## Network edge: proxy trust, headers, and limits

Everything here sits in front of the routes, so it applies to the web UI, `/api/v1`, `/mcp` and the OAuth endpoints
alike. Each item has a test that fails if it is removed (`hub/tests/test_edge_hardening.py`).

**Proxy trust.** A hub on the internet sits behind a TLS-terminating proxy or ingress. The proxy tells the hub the
real client address and scheme in `X-Forwarded-For`/`-Proto`; anyone who can reach the hub *without* going through
the proxy could write those headers themselves. Three settings decide who is believed:

| Setting | Meaning |
| --- | --- |
| `MEMORY_HUB_TRUST_PROXY` (default `false`) | Honour `X-Forwarded-*` at all. |
| `MEMORY_HUB_TRUSTED_PROXIES` | Comma-separated IPs/CIDRs allowed to set them. Empty = loopback + RFC1918 + IPv6 ULA (a warning is logged). **Set it to just your proxy/ingress.** `*` is accepted but warned about. |
| `MEMORY_HUB_CLIENT_IP_HEADER` (default empty) | A header holding the real client address, for a CDN in front of the proxy (`CF-Connecting-IP` on Cloudflare). Needs `MEMORY_HUB_TRUST_PROXY`. |

**Behind a CDN.** A proxy that sits behind another hop (Cloudflare → Traefik → hub, with the load balancer SNATing)
sees only the nearest hop, so `X-Forwarded-For` can hold the proxy's own gateway address and every client shares one
per-IP bucket. `MEMORY_HUB_CLIENT_IP_HEADER` names a header the CDN overwrites at its edge. It is believed only when
the address uvicorn resolved is itself inside `MEMORY_HUB_TRUSTED_PROXIES`, and only if the value is a single IP
literal; otherwise the hub keeps what `X-Forwarded-For`/the peer gave. The resolved address replaces the ASGI client,
so the sign-in throttle, token limits, `Secure`-cookie check and uvicorn's access log all use it. Trade-offs: clients
that reach the proxy without the CDN (LAN, VPN) share one bucket, and one of them can write the header, which only
lets it choose its own bucket. Don't set this unless the CDN really does overwrite the header and the origin can't be
reached around it.

With a list, uvicorn takes the right-most address that is *not* a trusted proxy — the one the proxy itself saw, which
a client can't forge. With `*` it takes the left-most entry, which the client writes: that made every per-IP limit
bypassable and let `X-Forwarded-For: 127.0.0.1` pass for "local". The hub also no longer reads `X-Forwarded-Proto`
itself: only the scheme uvicorn derives, for the peers it trusts, counts — so the "no API tokens over plain http" rule
can't be dodged by a header from a direct caller. Even with a tight list, anything *inside* the trusted range can
still forge the header (another pod, another container on the proxy's network): keep the hub's port reachable only
from the proxy (a `NetworkPolicy` on Kubernetes, loopback binding or an internal network on Docker).

**Response headers** (added by the outermost layer, so `/mcp`, error pages and static files get them too; a route
that sets its own, like the OAuth consent page's CSP, keeps it):

| Header | Value |
| --- | --- |
| `Content-Security-Policy` | `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'` |
| `Strict-Transport-Security` | `max-age=31536000` — only when the public URL is https; `MEMORY_HUB_HSTS_MAX_AGE=0` omits it. No `includeSubDomains`/`preload`: those are commitments about *other* hosts and are the operator's call. |
| `X-Content-Type-Options` / `X-Frame-Options` | `nosniff` / `DENY` (the latter for browsers that predate `frame-ancestors`) |
| `Referrer-Policy` | `same-origin` |
| `Permissions-Policy` | camera, microphone, geolocation, payment, usb, serial, bluetooth all denied |
| `Cross-Origin-Opener-Policy` / `-Resource-Policy` | `same-origin` (CORP only affects no-cors loads; the OAuth metadata endpoints stay readable by CORS clients) |
| `Cache-Control` | `no-store` everywhere except `/static/` |

The `Server: uvicorn` header is not sent.

**Request size.** A declared `Content-Length` over 10 MiB is refused with `413` before any route runs, and a chunked
body is cut off as it is read, so an unauthenticated caller can't make the hub buffer an arbitrary upload. The one
exception is `/data/import` (an export archive, up to its own 64 MiB limit). Import previews held for "Apply" are
limited to 2 per person and 12 overall.

**Host header (opt-in).** Set `MEMORY_HUB_ALLOWED_HOSTS` and the hub answers `400` to any `Host` that isn't the public
URL's host, loopback, or one you listed (`/healthz` is always exempt, because kubelet probes by pod IP). Unset, nothing
is enforced — a LAN hub is reached by whatever name or IP works. Remember to list every in-cluster name other services
use to reach `/mcp`.

**Sign-in throttle.** Failed attempts are counted against three keys: *account + address* and *address* (8 failures
per 15 minutes each) and *account across all addresses* (40). So one address can lock *itself* out but can't lock a
person out of their own account from elsewhere, while a distributed guesser still hits the account-wide budget. A
success clears the address keys, never the account-wide one. Keys are length-capped and the tables are bounded, so
caller-chosen emails and addresses can't grow the hub's memory. The same budget covers the SSO account-link form.

**Plugin egress.** Link-local addresses (`169.254.0.0/16`, `fe80::/10`) are never a plain-http target for a plugin,
even though they pass the "private network" test used elsewhere: that range is where cloud metadata services answer.

**Limits.** The throttles, like the rest of the hub's state, live in process memory and reset on restart. Rate limits
keyed by address are only as good as the address: they assume `MEMORY_HUB_TRUSTED_PROXIES` is accurate. HSTS is sent
only after the first https response, so the very first visit to a new hostname is still protectable only by the
proxy redirecting http to https (do that at the proxy).

## MCP OAuth (optional)

Claude.ai custom connectors (and Claude Code, if you prefer) can sign a person in instead of being handed a
pasted token. This is **off by default** (`MEMORY_HUB_OAUTH_ENABLED=true` to switch on): a hub that doesn't need it
exposes none of these endpoints and advertises nothing. It refuses to start with OAuth on unless
`MEMORY_HUB_PUBLIC_URL` is `https://` (plain http is only tolerated for localhost or a private address) — codes
and tokens travel through the browser.

**The hub is its own authorization server** (the MCP spec requires the resource server's tokens to come from
somewhere it trusts, and Claude uses the first `authorization_servers` entry it's given). "Backed by the hub's
OIDC" means the person proves who they are with the hub's normal login — local password or your SSO — at the
consent screen; OIDC itself is not re-implemented.

**The one design rule: an OAuth access token is an ordinary hub API token.** It is an `ApiToken` row — project
scope, read-only/read-write, user-scope opt-in, expiry, revocation, "dead the moment the person is deactivated" —
so everything in [What AI clients cannot do](#what-ai-clients-cannot-do) applies unchanged and cannot drift.
OAuth adds only who chooses the scope (the person, at consent), a short access-token lifetime (60 minutes) and
rotating refresh tokens.

| Endpoint | Purpose |
| --- | --- |
| `GET /.well-known/oauth-protected-resource[/mcp[/]]` | RFC 9728 metadata; names the hub as its own authorization server |
| `GET /.well-known/oauth-authorization-server` | RFC 8414 metadata; advertises only `S256` PKCE and public clients (`none`) |
| `POST /register` | Dynamic Client Registration (RFC 7591), policed below |
| `GET /authorize` → `/oauth/consent` | Authorization request, then the consent screen |
| `POST /token` | Code exchange and refresh (form-encoded); refresh tokens rotate |
| `POST /revoke` | RFC 7009; revoking either token ends the whole grant |

An unauthenticated call to `/mcp` answers `401` with `WWW-Authenticate: Bearer resource_metadata="…"` so a
client knows where to start.

**What is enforced (each has a test that fails if it's removed — `hub/tests/test_oauth.py`):**

- **Registration is open but confined.** Dynamic registration is unauthenticated by nature, so what a
  registered client can *do* is what is limited. Redirect URIs must be exactly Claude's hosted callback
  (`https://claude.ai/api/mcp/auth_callback`), an RFC 8252 loopback address (`http://localhost`, `127.0.0.1` or
  `[::1]`, any port), or an exact URI the operator added in `MEMORY_HUB_OAUTH_EXTRA_REDIRECT_URIS`. Everything else
  — look-alike hosts, wrong scheme, fragments, userinfo, custom schemes, other private addresses — is refused, so a
  registered client can never receive an authorization code anywhere else. All clients are *public*: PKCE, no
  secret stored or issued. Registration is rate-limited per IP, capped (500 clients), and registrations that never
  led to a grant are swept after a day.
- **PKCE (`S256`) is mandatory**; `plain` and a missing challenge are refused. Redirect URIs match the registered
  value exactly, and an unregistered one gets an error page, never a redirect.
- **Resource binding.** A `resource` parameter must name this hub's MCP endpoint (`invalid_target` otherwise).
- **Codes** are 256-bit, single-use (claimed atomically), live 60 seconds, and are bound to the client and the
  redirect. Presenting a used code again revokes whatever its first exchange produced (RFC 6749 §4.1.2).
  Only hashes of codes, access tokens and refresh tokens are stored.
- **Consent is a human act.** It needs a signed-in session (a deactivated person can't), a CSRF token, and an
  explicit choice: which projects (none ticked is refused, never silently "everything"), whether personal memory
  is included (off by default), and read-only (default) or read-write — write is offered only if the app asked
  for it. The redirect target comes from the stored, already-validated request, never from the form. The page
  shows where the browser is about to go, warns that a loopback target means a program on this computer, renders
  the app's self-chosen name inert, and relaxes `form-action` only to that one origin.
- **Refresh tokens rotate on every use.** Replaying an already-rotated refresh token is treated as theft and ends
  the grant (the legitimate chain dies too; the person reconnects). A refresh can narrow scope, never widen it;
  the chain has an absolute lifetime (60 days).
- **The person stays in control.** Connected apps are listed in Settings with a Disconnect button that ends
  access immediately; they are deliberately *not* mixed into the hand-made token list, and revoking an app's token
  by any route ends its grant. Deactivating an account stops its tokens and refreshes at once.
- **No new door.** `/api/v1` still refuses every `Authorization` header, OAuth tokens included.

**Not supported, on purpose:** Client ID Metadata Documents (Claude falls back to DCR), confidential clients,
the `client_credentials` grant, and enterprise identity-assertion grants. Each would widen what an
unauthenticated caller can influence.

**Honest limits.** A self-registered client's name is its own claim; the consent screen says the hub can't verify
it, and the redirect allowlist — not the name — is what keeps a rogue client from collecting codes. Loopback
redirects can be claimed by any local process (the spec's known risk), hence the extra warning. This has been
exercised end to end against a client written from the spec and Claude's published requirements; a real
claude.ai connection needs a public HTTPS URL and has not been made.

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
  "RFC1918" means a literal private IP by default. `MEMORY_HUB_EGRESS_RESOLVE_PRIVATE=true` (off by
  default) extends it to a _hostname_ whose every DNS answer is loopback/private — a Kubernetes
  Service name, a LAN hostname — so in-cluster plain-http targets work without IP literals. Any public
  answer, or a failed lookup, still refuses. The egress client then connects to the exact address it
  checked (with the original `Host` header), so DNS that changes between the check and the request
  can't redirect plaintext traffic to a public host. Link-local addresses (cloud metadata) are excluded from plaintext targets altogether. Trade-off when enabled: whoever controls the
  hub's DNS decides what counts as "private" — appropriate when that's your own cluster/LAN resolver.
- **Per-user connections** ([PLUGINS.md § Connections](PLUGINS.md#connections-plugins-each-person-sets-up-for-themselves))
  let a *non-admin* make the hub call a URL *they* chose, with a credential *they* typed. That needs more than the
  rules above, and each rule below has a test that fails if it is removed:
  - **Credentials are sealed, not referenced.** A person's tokens are stored as AES-256-GCM ciphertext under a key
    derived (HKDF, own context label) from `MEMORY_HUB_SECRET_KEY`, with the connection id and secret name bound in as
    associated data, so a sealed value can't be moved to another connection or field. A copy of the database or a
    backup alone is therefore not enough to use them; the database *plus* the hub's environment is. This is the one
    deliberate exception to "never in the database", it applies only to connections, and system plugins still use
    environment-variable names. **Changing `MEMORY_HUB_SECRET_KEY` makes every sealed secret unreadable** (the
    connection then says "re-enter your token"); it does not corrupt anything else.
  - **Write-only.** No page, API, export, log or error message ever shows a stored secret; the form shows *set* /
    *not set*. Errors and logs are scrubbed of the secret values (and of the query-string token some APIs require).
  - **Connections can't name environment variables.** A system plugin's `secret_refs` are operator-chosen; on a
    connection they are ignored, so a person can never make the hub send `MEMORY_HUB_SECRET_KEY` (or any variable) to
    a server of their choosing.
  - **SSRF guard.** A person's URL must resolve to **public** addresses only. Loopback, private (RFC 1918 / ULA),
    link-local (cloud metadata), multicast and unspecified addresses are refused for both http and https. The hub
    resolves the name once, checks every answer, and then connects to *that* address (with the original `Host` header
    and TLS server name), so DNS can't change between the check and the request. No redirects. Responses are
    size-capped. To reach a Memos on your LAN or in your cluster, the **operator** lists the host (name, IP or CIDR)
    in `MEMORY_HUB_CONNECTION_PRIVATE_HOSTS`; only those private targets are then reachable by connections, and
    link-local/metadata addresses never are.
  - **Own data only.** A connection's owner is fixed at creation. Other users get "not found" for it (admins too:
    they can disable it, not open or edit it). It captures into its owner's inbox, its events are filtered by what its
    owner can read, and scope-less system events never reach it. Deactivating the owner turns it off and destroys its secrets.
  - **Bounded.** At most 10 connections per person, a 15-minute minimum pull interval, the same per-call
    timeouts, response caps and retry limits as every plugin, and an admin switch that disables connections
    instance-wide.
  - **Only connector types the operator allows.** A connection can only use a plugin that declares itself safe
    for that (`personal_ok`): never `apprise`, never the vault-folder source, never a third-party or remote plugin
    unless its own definition says so.
- **Auditable.** Every delivery and every inbound pull is recorded (`plugin_deliveries`,
  last status/error on the instance) and visible in the Plugins screen. (Source plugins only add inbox
  items, so there are no `plugin`-sourced revisions to audit; what they capture is marked `plugin:<key>`.)

**What is enforced, and where the limits are** (Phase 3):

- _Enforced and tested:_ the scope/project allowlist is deny-by-default; an instance only receives events
  its **owner could read themselves**; `user` scope needs the acknowledgement _and_ is only ever the owner's
  own; payloads never contain record bodies (full text is fetched at delivery time, only at `egress: full`,
  only if the record is itself allowed); secrets are env-var _names_ in the database and are scrubbed from
  stored errors and logs; the egress client refuses non-HTTPS (except localhost/private LAN, and — only
  with `MEMORY_HUB_EGRESS_RESOLVE_PRIVATE` — hostnames resolving solely to private addresses, connected to
  by the checked IP), follows no redirects, honours the host allowlist and caps response size; an instance never receives events its own
  activity caused; plugins are admin-configured only; the offline CLI never loads plugin code; plugin
  failures, hangs and floods are isolated and rate-limited.
- _Tested by mutation:_ removing the user-scope acknowledgement check, the owner-visibility check, secret
  redaction, or the Obsidian path guards makes the corresponding tests fail.
- _Limits, stated plainly:_ in-process plugins are **trusted code running in the hub process** — an installed
  plugin could do anything the hub process can; the guards above constrain plugins that use the provided context,
  not a malicious one. **Remote plugins** (below) remove that limit for code you don't trust. `apprise` does its own HTTP (the hub validates the URLs it is given, but doesn't proxy
  the traffic — so with `MEMORY_HUB_EGRESS_RESOLVE_PRIVATE` on, an Apprise plaintext target's hostname is checked
  but not pinned; Apprise resolves it again itself). A hung plugin call is abandoned, not killed. The plugin scheduler is in-process, so run a
  single hub process. Nothing stops an operator from choosing `egress: full` and a public channel, so the
  Plugins screen flags full-text and personal-memory instances loudly.

### Remote plugins: running code you don't trust

A remote plugin (see [PLUGINS.md § Remote plugins](PLUGINS.md#remote-out-of-process-plugins)) is a separate service the
hub calls over HTTP, so a hostile or buggy one has no way into the hub's process, memory, environment or database:

- **Operator-registered only.** Services come from `MEMORY_HUB_REMOTE_PLUGINS[_FILE]`; the UI and database can't add
  one, and a registration that collides with a built-in or installed plugin is refused.
- **Mutual authentication.** Every request carries a timestamped HMAC-SHA256 signature the service verifies (5-minute
  replay window), and every response must carry a valid signature back, so a service on a plain-HTTP LAN can't be
  impersonated or its answers altered undetected. The signing key is a ≥32-character secret referenced by
  environment-variable name; it is the only secret the hub holds for a remote plugin.
- **The hub, not the service, decides what the service sees.** The same deny-by-default scope allowlist, `metadata`
  egress, owner-visibility check and user-scope acknowledgement run before an event is serialised. A source's
  service returns items; the hub writes them, and the service never receives a token, inbox writer or database handle.
- **Untrusted answers.** Responses are size-capped, parsed against a strict schema, and bounded (200 items per pull,
  capped field lengths). Captured items are marked external and can't be promoted automatically.
- **No credentials in the hub.** The service's own third-party credentials live in its environment, never in the hub's.
- **Same egress rules.** The URL must be https or a local/private address, redirects are never followed.

**Search plugins** (an optional embedding index; see [PLUGINS.md § Search plugins](PLUGINS.md#search-plugins-an-optional-embedding-index))
add two properties on top of the above, each with a test that fails if it is removed:

- **A service can add nothing the caller couldn't already read.** It returns ids; the focus builder only admits an id
  that is in the caller's own visible, active, associated, project-scoped candidate pool. An id belonging to another
  person's private memory, an archived record, a record in another project, or one that doesn't exist is dropped.
- **An index serves only its owner.** A search instance is queried only by the person who owns it, so one person's
  index never answers another's `memory_focus`, even in a project they share.
- **What it holds is what the allowlist permits, kept current.** Reconciliation sends each instance only the active
  records it may see (user-scope only with the acknowledgement, and only the owner's own) and removes anything that is
  edited out of scope, archived, superseded or deleted — and everything when the instance is deleted. At `metadata`
  egress the service gets a record's name, description and topics, never its body.
- **It can never hurt retrieval.** Calls have a 2-second budget; a down, slow, unsigned or malformed service costs a
  note in the pack and nothing else — the plain full-text result is unchanged.

_Limits:_ a service that you point at `egress: full` still receives that text — isolation protects the hub from the
plugin, not the data you chose to send it. A service can lie about its own results (a feed reader can invent items);
that is why they land in the inbox as untrusted input.

## Encrypted private memory (optional)

A person can turn on encryption for their own **private (user-scope) memory**. The body of each of their user-scope
records, and of every revision of those records, is then stored as AES-256-GCM ciphertext, under a key only they can
unlock. This is **encryption at rest with a key the hub does not keep**, not end-to-end encryption: while a request is
being served with an unlocked credential, the running hub holds the plaintext.

**What it protects against**

- Someone who obtains the database file or a backup of it — including an admin or operator who can read the SQLite
  file but has no passphrase. Roles never gave an admin access to private memory; this makes the storage agree.
- Leaks through the hub's own side doors: the full-text index, plugin events, search-plugin indexing, exports,
  consolidation and `acm compile` never receive an encrypted record's body unless the person unlocked it themselves.

**What it does not protect against** (stated plainly)

- A compromised or malicious *running* hub, or anyone who can read its process memory or change its code: they can
  see plaintext whenever a credential is unlocked. Treat the host as trusted while you are using it.
- The fields that stay in plaintext so that listing, search and focus keep working: a record's **name, description and
  topics**, its type/tier/confidence/status, timestamps and links. Don't put secrets there.
- Inbox items (transient captures, plaintext until they are turned into records, which are then encrypted) and every
  non-private scope. Project and team memory are not encrypted by this feature.
- Losing both the passphrase and the recovery key. There is no back door; the data is unrecoverable by design.

**Keys.** Enabling generates a random 256-bit data key (DEK). The DEK is stored only *wrapped*: once under a key derived
from the person's **memory passphrase** with Argon2id (a separate secret from their login password, so SSO users have
one too) and once under a random **recovery key** shown a single time. Records are sealed with the DEK, with the record
(or revision) id bound in as associated data, so ciphertext can't be moved between rows. The hub never stores the
passphrase, the recovery key or the DEK.

**Unlocking is explicit and scoped.**

| Credential | How it gets the DEK |
| --- | --- |
| Web session | The person enters the passphrase; the DEK is held **in server memory only**, tied to that session, and dropped on logout, session expiry, deactivation, a restart, or 30 idle minutes. |
| API token / OAuth app | Only if the person ticks "can read my encrypted memory" **when creating the token or approving the app** (while unlocked). The DEK is wrapped under a key derived from the token's own secret, so the database alone can't unwrap it; revoking the token or grant destroys the wrap. |
| `acm` CLI | The passphrase, prompted for (or supplied by `ACM_PASSPHRASE`), for that command only. |
| Everything else — plugins, the scheduler, consolidation, search sync | Never. They see a locked placeholder. |

**Locked means locked, everywhere, and fails closed.** A record whose key isn't available reads as a fixed placeholder
(`🔒 …`), never ciphertext. Writing to private memory with a credential that can't encrypt is **refused** — it never
silently falls back to plaintext. A body equal to the placeholder can never be written (so an export, merge or
promotion can't copy "locked" over real content). Exports skip locked records and say so. Locked records are left out
of duplicate detection and plugin payloads.

**Plaintext written earlier.** Switching encryption on seals existing records and then rewrites the database file
(WAL checkpoint + `VACUUM`) so the old plaintext doesn't linger in freed pages. That covers the live file only: backups
and copies made *before* enabling still contain the plaintext, and promoting a private record to project or team scope
writes its body in plaintext by design (it is no longer private).

**Operations** (web: Settings → Account; CLI: `acm key …`): enable (shows the recovery key once), unlock/lock, change
passphrase, regenerate the recovery key, recover with the recovery key, and disable (decrypts everything back to plaintext).

**Not built:** rotating the DEK itself (changing the passphrase or recovery key re-wraps it without re-encrypting
records), encrypting team/project memory, names/descriptions or inbox items, and sharing one key cache across several
hub processes (the hub is single-process).

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
- Records are stored in plaintext unless a person opts into
  [encrypted private memory](#encrypted-private-memory-optional), which covers only their user-scope bodies. The DB
  file and its directory are created `0600`/`0700`.
- Tokens and sessions are looked up by the SHA-256 of a 256-bit random value; there's no
  per-secret salt because there's nothing low-entropy to protect. Passwords use Argon2id.
