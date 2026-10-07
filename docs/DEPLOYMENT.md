# Deployment

How the memory hub (see [ARCHITECTURE.md § Memory hub](ARCHITECTURE.md#memory-hub-cross-project-store))
gets run. The Dockerfile and compose file are real
(under [`hub/`](../hub/)); the Kubernetes manifests below are a reference design.

**Self-host first.** The baseline is one container (or one `pip install` + `uvicorn`),
one SQLite file, one volume, no external services — not even a database server, mail
server, or identity provider is required (local accounts work out of the box; OIDC is
optional but supported from the first release). Everything past that is optional.

## Container image

The real definition is [`hub/Dockerfile`](../hub/Dockerfile) (build context: `hub/`):

- Multi-stage: a builder stage builds a wheel of the `ai-core-memory-hub` package; the
  runtime stage is `python:3.12-slim`, installs only that wheel (no compiler, no source
  tree), and runs as a non-root user (`uid 1000`) under `uvicorn` via `acm serve`.
- One HTTP port (`8000`) serves the web UI, the MCP endpoint (`/mcp`), and `/healthz` —
  one FastAPI process, per ARCHITECTURE.md § Memory hub. The image `HEALTHCHECK` calls `/healthz`.
- The SQLite file lives at `MEMORY_HUB_DB_PATH` (default `/data/hub.sqlite3`) on a volume,
  so the image stays stateless and disposable. Migrations run automatically on startup.
- Templates, static assets (htmx is vendored — no CDN) and Alembic migrations ship inside the
  wheel, so the container needs no network access at runtime other than what you configure
  (OIDC discovery, plugins).

### Published image (GitHub Container Registry)

[`.github/workflows/hub.yml`](../.github/workflows/hub.yml) tests the hub, builds this image, starts it as a smoke test
(`/healthz` healthy, runs as uid 1000, `acm doctor` clean, unauthenticated `/mcp` refused) and only then publishes it
to **`ghcr.io/andyprosser/ai-core-memory-hub`** for `linux/amd64` and `linux/arm64` (so Raspberry Pi k3s nodes work),
with a provenance attestation and SBOM. It authenticates with the workflow's own `GITHUB_TOKEN`; no personal token
or other secret is involved.

| Trigger | Tags pushed |
| --- | --- |
| push to `main` | `latest`, `sha-<short commit>` |
| tag `vX.Y.Z` | `X.Y.Z`, `X.Y`, `sha-<short commit>` |
| pull request | nothing pushed; the image is still built and smoke-tested |

```bash
docker pull ghcr.io/andyprosser/ai-core-memory-hub:latest   # or podman pull
docker pull ghcr.io/andyprosser/ai-core-memory-hub:0.1.0    # a released version
```

**Releasing.** The version lives in the root [`VERSION`](../VERSION) file. To release, edit it, run `python scripts/sync-version.py`
(it copies the number into `hub/pyproject.toml` and `acm_hub.__version__`, since the hub builds from `hub/` alone; a test
and the release workflow fail if they disagree), and push to `main`: [`release.yml`](../.github/workflows/release.yml) skips if `vX.Y.Z` already
exists, otherwise runs the whole hub workflow with that version (tests, smoke test, push `:X.Y.Z`, `:X.Y`, `:latest`),
and only if that succeeds creates the `vX.Y.Z` tag and a GitHub Release with generated notes. A failed run leaves no tag
behind, so fix and re-run. *Actions → release → Run workflow* releases the current version on demand. Pushing a
`vX.Y.Z` tag by hand also still publishes the image, but creates no GitHub Release.

Pin a version or `sha-…` tag in production rather than `latest`. **First publish:** GitHub creates the package as
*private*. If you want to pull it without credentials, open the package under the repository's *Packages*, then
*Package settings → Change visibility → Public*; otherwise create an image-pull secret (a token with
`read:packages`) and reference it from the StatefulSet's `imagePullSecrets`.

> **Verification status:** the image has been **built and run** (Docker 29, vfs storage): it runs as uid 1000,
> its `HEALTHCHECK` reports healthy, the database files are created `0600`, `acm setup-code` works inside it,
> and `/mcp` rejects unauthenticated calls. Its pip steps needed this sandbox's TLS-intercepting proxy CA to be
> injected — the only difference from the shipped Dockerfile, and irrelevant on a normal network. The compose
> file is validated with `docker compose config` but was not brought up (the sandbox daemon had no bridge
> networking), so run `docker compose up --build` once on your own host.

## docker-compose — the primary supported path

Most people running this are one person or one small team, on a home server, NAS, or a
single VM — not a cluster. docker-compose is the path this project optimizes for;
everything below it (k3s/k8s) is for people who already run a cluster, not a requirement.

```bash
cd hub
cp .env.example .env          # set MEMORY_HUB_SECRET_KEY (required) and MEMORY_HUB_PUBLIC_URL
docker compose up -d --build
docker compose logs memory-hub | grep -i "setup code"   # first-run code; then open the URL
```

[`hub/docker-compose.yml`](../hub/docker-compose.yml) is one service and one named volume,
with these deliberate defaults:

- **Bound to loopback** (`127.0.0.1:8000`). To serve your LAN, set `MEMORY_HUB_BIND=0.0.0.0`;
  to serve anything beyond a LAN, put a TLS reverse proxy in front and set
  `MEMORY_HUB_TRUST_PROXY=true` (API tokens are refused over plain HTTP from non-private
  addresses — see [SECURITY.md](SECURITY.md)).
- `MEMORY_HUB_SECRET_KEY` is **required** — compose refuses to start without it.
- `MEMORY_HUB_PLUGINS` (default `true`) is the master switch for plugin execution.
- `MEMORY_HUB_EGRESS_RESOLVE_PRIVATE` (default `false`) lets plugins use plain `http://` to a hostname whose every
  DNS answer is private (compose service names, Kubernetes Services, LAN hostnames) instead of only literal private
  IPs. See [SECURITY.md § Plugins and egress](SECURITY.md#plugins-and-egress).
- `MEMORY_HUB_CONSOLIDATE_INTERVAL_HOURS` (default `24`, `0` disables) sets how often the hub runs its
  mechanical consolidation pass (decay, duplicate candidates, core budget). It's restart-safe (the last-run
  time is in the database) and you can always run it by hand: `acm consolidate [--dry-run]`.
- Optional SSO via `MEMORY_HUB_OIDC_ISSUER` / `_CLIENT_ID` / `_CLIENT_SECRET`; register
  `<MEMORY_HUB_PUBLIC_URL>/auth/oidc/callback` as the redirect URI. Secrets come from `.env`,
  never the image.

### First run and the `acm` CLI

The image also contains the `acm` CLI, which works on the same volume with no network
and no AI (see [ARCHITECTURE.md § Import / export](ARCHITECTURE.md#import--export--offline-human-operated)):

```bash
docker compose exec memory-hub acm setup-code      # fresh one-time code to claim first-run setup in the browser
docker compose exec memory-hub acm user create --admin you@example.com   # or create the admin headlessly (prompts for a password)
docker compose exec memory-hub acm export --out /data/export             # offline export; works with the hub stopped too (`run --rm`)
docker compose exec memory-hub acm doctor                                # integrity + config check
```

There is no default password and no "first visitor becomes admin" window; see
[SECURITY.md § CLI access](SECURITY.md#cli-access-and-the-local-trust-boundary).

### Plugins

The four built-in plugins ([PLUGINS.md](PLUGINS.md)) are in the image, including Apprise
(Slack, Teams, ntfy, email, …). Configure them in Settings → Plugins. Three deployment details:

- **Secrets** (a Slack/Teams webhook URL, a Memos token) are referenced in the UI by environment-variable
  _name_. Put the values in `hub/.env`; the compose file passes that file into the container with
  `env_file`. They are never written to the database.
- **Obsidian** needs your vault inside the container: uncomment the `OBSIDIAN_VAULT_DIR` mount in
  `docker-compose.yml` (read-only is enough — the plugin only reads, except for the optional digest note, which
  needs a writable mount) and use `/vault` as the vault path.
- **Third-party plugins**: `FROM ai-core-memory/hub` + `pip install acm-plugin-…`, restart. Setting
  `MEMORY_HUB_PLUGINS=false` switches every plugin off, and `acm plugins disable <id>` turns one off
  with the hub stopped. Run a **single** hub process: the plugin scheduler lives inside it. Obsidian's connector needs the vault directory mounted into the
container (read-only unless you want digest export).

## Podman

The same Dockerfile and compose file work under Podman as-is (`podman build`,
`podman-compose up`, or `podman play kube` against a generated manifest) — Podman
targets Docker/OCI compatibility, so nothing project-specific is needed. Worth calling
out explicitly for people who prefer rootless containers, which fits "own everything"
at least as well as requiring a Docker daemon does.

## k3s / Kubernetes — with an important caveat

SQLite is single-writer, local-disk storage. It does not tolerate multiple pods writing
the same file over a network filesystem, so:

- Run the hub as a **StatefulSet with exactly one replica**, never a horizontally-scaled
  Deployment. A single replica on a `PersistentVolumeClaim` is a perfectly good outcome
  — the win from containerizing this is repeatable deploys and cluster-native
  config/secrets, not horizontal scale.
- If real multi-replica scale is ever actually needed, that's the signal to swap SQLite
  for Postgres through the same SQLAlchemy layer (already a documented drop-in path in
  ARCHITECTURE.md) — don't try to get there by putting SQLite on shared network storage;
  it will lock up or corrupt.

Ready-to-edit manifests live in [`hub/deploy/k8s/`](../hub/deploy/k8s/): `configmap.yaml`,
`statefulset.yaml` (one replica, non-root, probes on `/healthz`), `service.yaml`, `backup-cronjob.yaml`,
plus `secret.example.yaml` and `ingress.example.yaml` (TLS — tokens and sessions must only travel over it).

```bash
kubectl create secret generic memory-hub-secrets \
  --from-literal=MEMORY_HUB_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
# edit configmap.yaml (MEMORY_HUB_PUBLIC_URL) and pin the image tag in statefulset.yaml + backup-cronjob.yaml, then:
kubectl apply -k hub/deploy/k8s
kubectl logs statefulset/memory-hub | grep "setup code"   # then finish first-run setup in the browser
```

Secrets stay in a Kubernetes `Secret` (the manifests never contain one); the same variable names as the
compose file. The nightly `CronJob` takes a consistent online copy of the whole database (SQLite's backup API)
onto its own volume and keeps 14; it is scheduled next to the hub pod because the hub's volume is
ReadWriteOnce. Copy backups off the cluster as well.

**Verification status, honestly:** every manifest passes `kubernetes-validate --strict` against Kubernetes
1.28, 1.31 and 1.34 (`kustomization.yaml` has no schema in that tool), selectors/names/PVC references were
checked by script, and the backup CronJob's exact script was run inside the built image against a live hub
volume (as uid 1000, with the backup volume owned by the pod's `fsGroup` — a bare Docker volume is root-owned and
fails, which is what `fsGroup: 1000` is for), producing a `0600` copy that passes `PRAGMA integrity_check`. The
manifests were **not** applied to a real cluster. Treat them as a validated starting point.

### MCP OAuth (optional)

To let Claude.ai connectors sign in rather than paste a token, set `MEMORY_HUB_OAUTH_ENABLED=true` and make
`MEMORY_HUB_PUBLIC_URL` the exact `https://` address people use (the hub refuses to start otherwise). The ingress
or reverse proxy must forward `/.well-known/*`, `/authorize`, `/token`, `/register`, `/revoke` and `/oauth/consent`
to the hub unchanged. Optional: `MEMORY_HUB_OAUTH_EXTRA_REDIRECT_URIS` (exact URIs, comma-separated, on top of
Claude's callback and loopback), `MEMORY_HUB_OAUTH_ACCESS_TOKEN_MINUTES` (60) and `MEMORY_HUB_OAUTH_REFRESH_DAYS`
(60). Inspect and cut access offline with `acm oauth`. See [SECURITY.md § MCP OAuth](SECURITY.md#mcp-oauth-optional).

## Backups

Export (see
[ARCHITECTURE.md § Import / export](ARCHITECTURE.md#import--export--offline-human-operated))
exists for exactly this, and works without the hub running: a host cron entry (or Kubernetes
`CronJob` mounting the same volume) running `acm export --with-history --out <durable location>`.
No token, no network, no AI. (Export isn't available to API tokens — see
[SECURITY.md](SECURITY.md#what-ai-clients-cannot-do) — so a leaked token can't dump your memory.)
The same markdown + `manifest.json` layout restores into a fresh instance with
`acm import <dir>` (a dry run) and then `acm import <dir> --apply`. Back up the SQLite volume too if you want
tokens, users, and plugin config (exports contain memory, not credentials) — use
`sqlite3 hub.sqlite3 ".backup …"` or snapshot the volume rather than copying a live file.
This is ordinary infrastructure, not a feature the hub itself needs to implement.

## Status

The hub is built (see [`hub/README.md`](../hub/README.md)). The image was built and run in the authoring
environment (see above), and the GitHub workflow that publishes it has run green on `main` (tests, image smoke
test, multi-arch push). See [Roadmap](ARCHITECTURE.md#roadmap) for what's next.
