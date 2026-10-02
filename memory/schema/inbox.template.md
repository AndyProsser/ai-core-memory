---
# An inbox item is raw input, not a memory record. It has no type, tier, or confidence
# until the dream cycle classifies it into a record, a `reference` pointer, or a dismissal.
# See docs/ARCHITECTURE.md § The dream cycle and docs/PLUGINS.md.
title: "{{short title of the snippet, idea, or note}}"
source: "{{ui|cli|mcp|plugin:<key>}}"
scope: "{{project|team|user}}" # where it was captured; the dream cycle may propose a different one
project_id: "{{repo or project name, if scope: project}}"
external_ref: "{{URL or path in the originating system (Obsidian file, Memos URL), if any}}"
captured: "{{YYYY-MM-DD}}"
status: new # new | harvested | dismissed
---

{{The snippet, idea, or note as captured. Keep it raw — don't pre-classify.}}
