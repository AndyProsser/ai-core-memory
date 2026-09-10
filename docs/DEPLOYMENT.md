# Deployment

How the memory hub (see [ARCHITECTURE.md § Memory hub](ARCHITECTURE.md#memory-hub-cross-project-store))
gets run once it exists. This is a reference design, not backed by application code yet
— the Dockerfile/compose/Kubernetes snippets below describe the intended shape so
deployment doesn't have to be improvised after the fact.

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

volumes:
  hub-data:
```

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

The import/export endpoints (see
[ARCHITECTURE.md § Import / export](ARCHITECTURE.md#import--export--backup-only-never-the-routine-write-path))
exist for exactly this: schedule a job — a host cron entry alongside docker-compose, a
Kubernetes `CronJob` alongside k3s/k8s — that calls `GET /memories/export` and writes the
resulting markdown archive somewhere durable. That's the whole point of "store memories
in user-defined storage as markdown for backup" from the original design conversation.
This is ordinary infrastructure, not a feature the hub itself needs to implement.

## Status

None of this has application code behind it yet. See
[ARCHITECTURE.md § Memory hub](ARCHITECTURE.md#memory-hub-cross-project-store) for what's
designed and [Roadmap](ARCHITECTURE.md#roadmap) for sequencing. This document exists so
deployment isn't an afterthought once the hub is actually built.
