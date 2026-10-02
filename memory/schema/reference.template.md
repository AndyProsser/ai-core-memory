---
name: "{{short-kebab-case-slug}}"
description: "{{one-line summary of what lives at this pointer and why it matters}}"
metadata:
  id: "{{ULID — assigned on first write; omit when hand-drafting and the hub/dream pass will add it}}"
  type: reference
  scope: "{{project|team|user}}"
  confidence: "{{observed|confirmed|established}}" # usually confirmed once the pointer's been checked
  tier: "{{core|associated}}" # core = loaded every session (small budget, human-gated); default associated
  status: active # active | superseded | stale | archived
  project_id: "{{repo or project name, if scope: project}}"
  topics: ["{{topic}}", "{{topic}}"]
  links: [] # ids of related records
  supersedes: [] # ids of records this one replaces
  created: "{{YYYY-MM-DD}}"
  last_reinforced: "{{YYYY-MM-DD, updated when a later session re-establishes this}}"
  source: dream-cycle
---

{{Where the live information actually lives (a Linear project, a Grafana dashboard, a
Slack channel, an internal wiki page) and what it's for. Store the pointer, not the
information itself — the information will go stale here and stay fresh at the source.}}
