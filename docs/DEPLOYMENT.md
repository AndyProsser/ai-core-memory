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

> **Verification status:** the compose file is validated with `docker compose config`, and the
> image's build-and-install steps were reproduced outside Docker (build the wheel, install it
> into a clean environment from that wheel only, run `acm migrate` and `acm serve`, hit
> `/healthz`). The image itself has **not** been built in the authoring environment, which had
> no Docker daemon — run `docker compose up --build` once and report anything that differs.

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
# edit configmap.yaml (MEMORY_HUB_PUBLIC_URL), the image name, then:
kubectl apply -k hub/deploy/k8s
kubectl logs statefulset/memory-hub | grep "setup code"   # then finish first-run setup in the browser
```

Secrets stay in a Kubernetes `Secret` (the manifests never contain one); the same variable names as the
compose file. The nightly `CronJob` takes a consistent online copy of the whole database (SQLite's backup API)
onto its own volume and keeps 14; it is scheduled next to the hub pod because the hub's volume is
ReadWriteOnce. Copy backups off the cluster as well.

**Verification status, honestly:** the YAML parses and the selectors, names and PVC references were checked by
script, and the backup script was run against a live WAL database. The manifests were **not** applied to a
cluster or schema-validated (`kubectl`/`kubeconform` weren't available), and the image they reference has not
been built here. Treat them as a reviewed starting point.

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

The hub is built (see [`hub/README.md`](../hub/README.md)); the container image has not been built in the
authoring environment — compose config validated, Kubernetes manifests structure-checked only. See [Roadmap](ARCHITECTURE.md#roadmap) for what's next.
