---
name: "{{short-kebab-case-slug}}"
description: "{{one-line summary of the fact/decision — used to decide relevance}}"
metadata:
  type: project
  scope: "{{project|team}}"
  confidence: "{{observed|confirmed|established}}"
  project_id: "{{repo or project name}}"
  created: "{{YYYY-MM-DD}}"
  source: dream-cycle
---

{{The fact or decision itself — something not derivable from the code or git history.}}

**Why:** {{the motivation — a constraint, deadline, incident, or stakeholder ask.}}

**How to apply:** {{how this should shape future suggestions or decisions. Note that
project memory decays fast — if this stops being true, prune or update it rather than
leaving it stale.}}
