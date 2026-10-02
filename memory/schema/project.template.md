---
name: "{{short-kebab-case-slug}}"
description: "{{one-line summary of the fact/decision — used to decide relevance}}"
metadata:
  id: "{{ULID — assigned on first write; omit when hand-drafting and the hub/dream pass will add it}}"
  type: project
  scope: "{{project|team}}"
  confidence: "{{observed|confirmed|established}}"
  tier: "{{core|associated}}" # core = loaded every session (small budget, human-gated); default associated
  status: active # active | superseded | stale | archived
  project_id: "{{repo or project name}}"
  topics: ["{{topic}}", "{{topic}}"]
  links: [] # ids of related records
  supersedes: [] # ids of records this one replaces
  created: "{{YYYY-MM-DD}}"
  last_reinforced: "{{YYYY-MM-DD, updated when a later session re-establishes this}}"
  source: dream-cycle
---

{{The fact or decision itself — something not derivable from the code or git history.}}

**Why:** {{the motivation — a constraint, deadline, incident, or stakeholder ask.}}

**How to apply:** {{how this should shape future suggestions or decisions. Note that
project memory decays fast — if this stops being true, prune or update it rather than
leaving it stale.}}
