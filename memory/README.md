# memory/

This directory has two parts:

- **`schema/`** — canonical templates, one per memory type. Committed, versioned, and
  the thing to update if the record format changes. Read
  [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) first — these templates implement
  that spec, they don't define it.
- **`data/`** — where actual memory records live once a dream cycle starts producing
  them (not present until then; created on first use). Partitioned by scope:

  ```text
  memory/data/
    project/   committed  — facts and decisions specific to this repo
    team/      committed  — conventions this team carries across projects (opt-in)
    user/      gitignored — one person's preferences; belongs on their machine, not in git
    session/   gitignored — ephemeral, never meant to outlive the dream cycle that reads it
  ```

  `user/` and `session/` are listed in `.gitignore` on purpose — see
  [docs/ARCHITECTURE.md § Partition & cross-talk rules](../docs/ARCHITECTURE.md#partition--cross-talk-rules)
  for why. If you're working with this repo cloned onto your own machine and want
  genuine user-scope memory, point it outside the repo entirely (e.g. `~/.ai-memory/`)
  rather than relying on the gitignored in-repo path.

Each record is Markdown with YAML frontmatter — see any file under `schema/` for the
exact shape. A short `MEMORY.md` index at the root of each scope directory (mirroring
the pattern) keeps a one-line pointer per record so a session can see what exists
without reading every file.
