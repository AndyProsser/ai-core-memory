---
name: "{{short-kebab-case-slug}}"
description: "{{one-line statement of the constraint — used to decide relevance}}"
metadata:
  id: "{{ULID — assigned on first write; omit when hand-drafting and the hub/dream pass will add it}}"
  type: rule
  scope: "{{project|team|user}}"
  confidence: "{{confirmed|established}}" # a rule that's merely observed once is really just feedback
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

{{The constraint itself, stated as a hard rule rather than a soft preference — e.g.
"never merge to main without a green build."}}

**Why:** {{the reason this is a rule and not just a preference — often a past incident
or a deliberate team/project decision.}}

**How to apply:** {{when this rule applies, and what "breaking" it would look like. If
this needs to change, that's a deliberate act (bump confidence down or discuss with the
user) — not something a dream cycle does on its own; see docs/ARCHITECTURE.md § Confidence & mutability.}}
