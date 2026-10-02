---
name: "{{short-kebab-case-slug}}"
description: "{{one-line summary of this fact about the person — used to decide relevance}}"
metadata:
  id: "{{ULID — assigned on first write; omit when hand-drafting and the hub/dream pass will add it}}"
  type: user
  scope: user
  confidence: "{{observed|confirmed|established}}"
  tier: "{{core|associated}}" # core = loaded every session (small budget, human-gated); default associated
  status: active # active | superseded | stale | archived
  topics: ["{{topic}}", "{{topic}}"]
  links: [] # ids of related records
  supersedes: [] # ids of records this one replaces
  created: "{{YYYY-MM-DD}}"
  last_reinforced: "{{YYYY-MM-DD, updated when a later session re-establishes this}}"
  source: dream-cycle
---

{{The fact itself: role, expertise, working preferences, how they like to collaborate.
Written to help tailor future responses to who this person is — not a judgement of them,
and not something derivable from the code or project they happen to be working in.}}
