---
name: "{{short-kebab-case-slug}}"
description: "{{one-line summary of the guidance — used to decide relevance}}"
metadata:
  id: "{{ULID — assigned on first write; omit when hand-drafting and the hub/dream pass will add it}}"
  type: feedback
  scope: "{{project|team|user}}"
  confidence: "{{observed|confirmed|established}}"
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

{{The rule itself, stated as an instruction.}}

**Why:** {{the reason this guidance was given — often a past incident or a strong,
non-obvious preference. This is what lets a future session judge edge cases instead of
following the rule blindly.}}

**How to apply:** {{when/where this guidance kicks in.}}
