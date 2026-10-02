---
name: "{{short-kebab-case-slug}}"
description: "{{one-line summary of the direction being pursued — used to decide relevance}}"
metadata:
  id: "{{ULID — assigned on first write; omit when hand-drafting and the hub/dream pass will add it}}"
  type: intent
  scope: "{{project|team|user}}"
  confidence: "{{observed|confirmed|established}}" # usually confirmed or higher — intents are stated, not just noticed
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

{{The goal or direction being worked toward — not a fact that's already true, but where
things are heading and why. E.g. "migrating off the legacy auth service by Q3."}}

**Why:** {{the motivation for this direction.}}

**How to apply:** {{what this should steer future decisions toward or away from. If the
direction changes, update or prune this record rather than leaving a stale intent next
to a newer, contradicting one.}}
