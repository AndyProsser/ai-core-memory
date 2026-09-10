---
name: "{{short-kebab-case-slug}}"
description: "{{one-line summary of the direction being pursued — used to decide relevance}}"
metadata:
  type: intent
  scope: "{{project|team|user}}"
  confidence: "{{observed|confirmed|established}}" # usually confirmed or higher — intents are stated, not just noticed
  project_id: "{{repo or project name, if scope: project}}"
  created: "{{YYYY-MM-DD}}"
  source: dream-cycle
---

{{The goal or direction being worked toward — not a fact that's already true, but where
things are heading and why. E.g. "migrating off the legacy auth service by Q3."}}

**Why:** {{the motivation for this direction.}}

**How to apply:** {{what this should steer future decisions toward or away from. If the
direction changes, update or prune this record rather than leaving a stale intent next
to a newer, contradicting one.}}
