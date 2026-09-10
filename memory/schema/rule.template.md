---
name: "{{short-kebab-case-slug}}"
description: "{{one-line statement of the constraint — used to decide relevance}}"
metadata:
  type: rule
  scope: "{{project|team|user}}"
  confidence: "{{confirmed|established}}" # a rule that's merely observed once is really just feedback
  project_id: "{{repo or project name, if scope: project}}"
  created: "{{YYYY-MM-DD}}"
  source: dream-cycle
---

{{The constraint itself, stated as a hard rule rather than a soft preference — e.g.
"never merge to main without a green build."}}

**Why:** {{the reason this is a rule and not just a preference — often a past incident
or a deliberate team/project decision.}}

**How to apply:** {{when this rule applies, and what "breaking" it would look like. If
this needs to change, that's a deliberate act (bump confidence down or discuss with the
user) — not something a dream cycle does on its own; see docs/ARCHITECTURE.md § Confidence & mutability.}}
