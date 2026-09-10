---
name: dream
description: Consolidate this session's decisions, corrections, and discoveries into durable long-term memory records under memory/data/, following the scope and type model in docs/ARCHITECTURE.md. Use when the user asks to "dream", "dream on this", "consolidate memory", "commit this to long-term memory", or at the natural end of a session worth remembering.
---

# Dream cycle

A short-term-to-long-term memory consolidation pass, modeled on how sleep consolidates a
day's experience into durable memory: harvest what happened, classify it, merge it with
what's already known, flag what conflicts, promote what generalizes, prune what's stale,
and leave a clean index behind. Full design: [docs/ARCHITECTURE.md](../../../docs/ARCHITECTURE.md).

Run this on request, not automatically — memory changes are visible to every future
session that reads them, so a human should be able to see what got written.

## Pipeline

### 1. Harvest

Review the current conversation (and any other session material the user points you
to — exported transcripts, notes) for residue worth keeping:

- Decisions made and why.
- Corrections the user gave about *how* to do the work, or confirmations that an
  approach worked.
- Facts about the project (initiatives, deadlines, incidents) not derivable from the
  code or git history.
- Pointers to external systems worth remembering the location of.
- Anything the user explicitly asked to be remembered.

Also read the existing index (`memory/data/<scope>/MEMORY.md`) for each scope so you
know what's already captured — the point of this pass is to update memory, not append
to it forever.

### 2. Classify

For each fragment, assign:

- **Type** — `user`, `feedback`, `project`, `reference`, `intent`, or `rule` (see
  [docs/ARCHITECTURE.md § Vocabulary](../../../docs/ARCHITECTURE.md#vocabulary) for
  what each means).
- **Scope** — `session`, `project`, `team`, or `user`. Default to the narrowest scope
  the fragment was actually learned at. A working-style preference noticed while
  debugging this project is still `user` scope if it's about the person, not the
  project — scope is about *who it's about / who should see it*, not where it happened.
- **Confidence** — `observed`, `confirmed`, or `established` (see
  [docs/ARCHITECTURE.md § Confidence & mutability](../../../docs/ARCHITECTURE.md#confidence--mutability)).
  Default to `observed` for a first-time, inferred, or one-off fragment. Use `confirmed`
  when the user stated it directly or it corroborates something already on record.
  `rule` and `intent` records almost always start at `confirmed` or higher — they're
  deliberately stated, not merely noticed. Only mark something `established` when the
  user explicitly says to treat it as settled, or it's been independently reinforced
  enough times that calling it out as durable in your closing summary (see Output below)
  is clearly justified.

Skip anything covered under "What NOT to remember" in ARCHITECTURE.md — code structure,
git-derivable history, bug-fix mechanics, anything already in `CLAUDE.md`, in-progress
task state.

### 3. Merge / dedupe

For each classified fragment, check `memory/data/<scope>/` for an existing record
covering the same thing:

- If one exists and this fragment updates or corrects it, edit that file in place.
- If one exists and still holds, leave it — don't create a near-duplicate.
- If none exists, create a new file using the matching template in
  [`memory/schema/`](../../../memory/schema/).

Use `memory/data/project/` and `memory/data/team/` for scopes meant to be committed to
this repo. For `user` scope, write outside the repo (e.g. `~/.ai-memory/user/`) rather
than into the gitignored in-repo path — an in-repo path that's gitignored still isn't a
sensible permanent home for genuinely personal memory.

### 4. Flag conflicts

If a fragment contradicts an existing record rather than just updating it, check that
record's confidence tier before touching it:

- `observed` — update it in place, no confirmation needed.
- `confirmed` — update it, but say so plainly in your closing summary (see Output below)
  rather than changing it silently.
- `established` — do not change or remove it. Surface the conflict to the user and let
  them decide; at most, add a note referencing the new, contradicting fragment without
  altering the established record itself.

A wrong memory that's confidently stated is worse than a gap.

### 5. Promote (human-gated)

If something classified as `project` scope looks like it actually generalizes — a
working-style fact about the user, a convention the whole team follows, not just this
repo — propose promoting it to `user` or `team` scope and say why. Do not write to a
broader scope than where the fragment was classified without the user confirming the
promotion first (see ARCHITECTURE.md § Partition & cross-talk rules).

### 6. Prune

If harvesting surfaced information that invalidates an existing record (a decision was
reversed, a preference changed), update or remove that record rather than leaving both
the old and new version to contradict each other later.

### 7. Reindex

After writing or updating records, update `memory/data/<scope>/MEMORY.md` for every
scope touched: one line per record, `- [Title](file.md) — one-line hook`, under ~150
characters. The index is a lookup table, not a memory itself — don't write memory
content directly into it.

## Output

When the pass is done, tell the user concisely what was written, updated, or flagged for
their decision (conflicts, promotion candidates) — don't just say "done." They should be
able to tell what changed without re-reading every file themselves.
