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

### Optional plugins

Plugins ([PLUGINS.md](PLUGINS.md)) are extra Python packages. To use one, extend the
image (`FROM ai-core-memory/hub` + `pip install acm-plugin-…`), then configure it in
Settings → Plugins. (The plugin framework is Phase 3; nothing here exists yet.) Obsidian's connector needs the vault directory mounted into the
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

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: memory-hub
spec:
  serviceName: memory-hub
  replicas: 1
  selector:
    matchLabels: { app: memory-hub }
  template:
    metadata:
      labels: { app: memory-hub }
    spec:
      containers:
        - name: memory-hub
          image: ai-core-memory/hub:latest
          ports: [{ containerPort: 8000 }]
          envFrom:
            - secretRef: { name: memory-hub-secrets }
          volumeMounts:
            - name: data
              mountPath: /data
          readinessProbe:
            httpGet: { path: /healthz, port: 8000 }
  volumeClaimTemplates:
    - metadata: { name: data }
      spec:
        accessModes: ["ReadWriteOnce"]
        resources: { requests: { storage: 1Gi } }
---
apiVersion: v1
kind: Service
metadata:
  name: memory-hub
spec:
  selector: { app: memory-hub }
  ports: [{ port: 80, targetPort: 8000 }]
```

Secrets (`MEMORY_HUB_SECRET_KEY`, admin bootstrap credentials) belong in a Kubernetes
`Secret`, not the manifest — same environment variables as the compose file, just
sourced differently per platform.

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

The Phase 1 hub is built (see [`hub/README.md`](../hub/README.md)); the container image and the
k3s/k8s sections below are as described above — compose validated, image not yet built here,
Kubernetes manifests unverified. See [Roadmap](ARCHITECTURE.md#roadmap) for what's next.
