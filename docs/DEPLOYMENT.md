# Deployment

How the memory hub (see [ARCHITECTURE.md § Memory hub](ARCHITECTURE.md#memory-hub-cross-project-store))
gets run once it exists. This is a reference design, not backed by application code yet
— the Dockerfile/compose/Kubernetes snippets below describe the intended shape so
deployment doesn't have to be improvised after the fact.

**Self-host first.** The baseline is one container (or one `pip install` + `uvicorn`),
one SQLite file, one volume, no external services — not even a database server, mail
server, or identity provider is required (local accounts work out of the box; OIDC is
optional but supported from the first release). Everything past that is optional.

## Container image

- Single Dockerfile, multi-stage: a builder stage installs Python dependencies, the
  runtime stage is a slim Python base image running as a non-root user under `uvicorn`.
- One HTTP port (default `8000`) serves both the REST API and the MCP endpoint — one
  FastAPI process, per ARCHITECTURE.md § Memory hub.
- A `/healthz` endpoint for container/orchestrator liveness and readiness checks.
- The SQLite file lives at a configurable path (`MEMORY_HUB_DB_PATH`, default
  `/data/hub.sqlite3`) so it can be mounted as a volume separate from the image, and the
  image itself stays stateless and disposable.

```dockerfile
FROM python:3.12-slim AS builder
WORKDIR /app
COPY pyproject.toml requirements.txt* ./
RUN pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim
RUN useradd -m -u 1000 hub
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY . .
USER hub
ENV MEMORY_HUB_DB_PATH=/data/hub.sqlite3
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

## docker-compose — the primary supported path

Most people running this are one person or one small team, on a home server, NAS, or a
single VM — not a cluster. docker-compose is the path this project optimizes for;
everything below it (k3s/k8s) is for people who already run a cluster, not a requirement.

```yaml
services:
  memory-hub:
    build: .
    image: ai-core-memory/hub:latest
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - hub-data:/data
    environment:
      MEMORY_HUB_DB_PATH: /data/hub.sqlite3
      MEMORY_HUB_SECRET_KEY: ${MEMORY_HUB_SECRET_KEY}
      MEMORY_HUB_ADMIN_EMAIL: ${MEMORY_HUB_ADMIN_EMAIL}
      # Optional SSO — omit for local accounts only. Secrets come from env/.env, never the image.
      MEMORY_HUB_OIDC_ISSUER: ${MEMORY_HUB_OIDC_ISSUER:-}
      MEMORY_HUB_OIDC_CLIENT_ID: ${MEMORY_HUB_OIDC_CLIENT_ID:-}
      MEMORY_HUB_OIDC_CLIENT_SECRET: ${MEMORY_HUB_OIDC_CLIENT_SECRET:-}
      MEMORY_HUB_PUBLIC_URL: ${MEMORY_HUB_PUBLIC_URL:-http://localhost:8000} # used for OIDC redirect URI
      # Plugin secrets are referenced by name in the UI and resolved from the environment, e.g.:
      # SLACK_WEBHOOK_URL: ${SLACK_WEBHOOK_URL:-}

volumes:
  hub-data:
```

### First run and the `acm` CLI

The image also contains the `acm` CLI, which works on the same volume with no network
and no AI (see [ARCHITECTURE.md § Import / export](ARCHITECTURE.md#import--export--offline-human-operated)):

```bash
docker compose run --rm memory-hub acm setup-code      # one-time code to claim first-run setup in the browser
docker compose run --rm memory-hub acm user create --admin you@example.com   # or create the admin headlessly
docker compose run --rm memory-hub acm export --out /data/export             # offline export, hub need not be running
docker compose run --rm memory-hub acm doctor                                # integrity + config check
```

There is no default password and no "first visitor becomes admin" window; see
[SECURITY.md § CLI access](SECURITY.md#cli-access-and-the-local-trust-boundary).

### Optional plugins

Plugins ([PLUGINS.md](PLUGINS.md)) are extra Python packages. To use one, extend the
image (`FROM ai-core-memory/hub` + `pip install acm-plugin-…`) or set
`MEMORY_HUB_EXTRA_PIP` for a startup install on a home server, then configure it in
Settings → Plugins. Obsidian's connector needs the vault directory mounted into the
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
exists for exactly this, and works without the hub running. Two options:

- **Offline, simplest:** a host cron entry (or Kubernetes `CronJob` mounting the same
  volume) running `acm export --with-history --out <durable location>`. No token, no
  network, no AI.
- **Over the API:** a job that calls `GET /memories/export` with a read-only token scoped
  to the projects to back up.

Either produces the same markdown + `manifest.json` layout, which `acm import --dry-run`
can restore into a fresh instance. Back up the SQLite volume too if you want
tokens, users, and plugin config (exports contain memory, not credentials) — use
`sqlite3 hub.sqlite3 ".backup …"` or snapshot the volume rather than copying a live file.
This is ordinary infrastructure, not a feature the hub itself needs to implement.

## Status

None of this has application code behind it yet. See
[ARCHITECTURE.md § Memory hub](ARCHITECTURE.md#memory-hub-cross-project-store) for what's
designed and [Roadmap](ARCHITECTURE.md#roadmap) for sequencing. This document exists so
deployment isn't an afterthought once the hub is actually built.
