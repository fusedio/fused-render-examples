# Skills

> **Status — SHIPPED (v1).** This file owns the **non-plugin (local) skills viewer**:
> discovering and listing the skills a user authors directly under `~/.claude/skills/`, and
> revealing a skill's folder in the OS file explorer. It is the skills counterpart to the
> Memory viewer (`memory.md`). Read-only — skill *contents* are authored in files, never edited
> here. Implementing modules: `skills.py` (`main(action="list"|"open", …)`), `lib.py`
> (`safe_subdir`, `reveal`), and the Skills section of `index.html`. It does NOT own
> plugin-bundled skills (`plugins.md`), the reveal primitive (`memory.md §6`), or the git layer
> (`version-control.md`).

## 1. What a non-plugin skill is

A **skill** is a folder containing a `SKILL.md` whose YAML frontmatter carries a `name` and a
`description` (the description is how the agent decides when to invoke it). Skills reach Claude
Code from two places:

- **Plugin-bundled** — shipped inside a plugin under `~/.claude/plugins/cache/.../skills/`.
  Owned by the plugin; managed at whole-plugin granularity only (`plugins.md`). **Not shown here.**
- **Non-plugin (local)** — authored directly under `~/.claude/skills/<slug>/SKILL.md`. These are
  the user's own skills (or symlinks to skills kept elsewhere). **This tab lists exactly these.**

The distinction is purely the on-disk location: this surface reads `CLAUDE_DIR/skills/*` and
nothing under `plugins/`.

## 2. Content read-only

The tab **lists** skills and reveals their folders; it never creates, edits, or deletes a
`SKILL.md` or its supporting files. Folder-level lifecycle actions (commit/clear) are **not**
offered — unlike Memory (`memory.md §2`), local skills are hand-authored config the user manages
directly, not app-accumulated state; v1 stays a pure viewer + reveal.

## 3. Discovery — `main(action="list")`

`skills.py` walks `CLAUDE_DIR/skills/*/` and returns, per immediate subdirectory that contains a
`SKILL.md`:

```
{ skills: [{ slug, name, description, linked: bool, source: str|null,
             shareCommand: str|null }] }
```

- **slug**: the directory name under `skills/`.
- **name**: the `name:` value from `SKILL.md` frontmatter; falls back to `slug` if absent/blank.
- **description**: the `description:` value from frontmatter; empty string if absent.
- **linked**: `true` when the `skills/<slug>` entry is a **symlink** (the skill is authored
  elsewhere and linked in) — surfaced so the user knows the folder isn't a plain local dir.
- **source** / **shareCommand**: the skill's origin repo and its paste-able install command,
  derived from the `skills`-CLI lockfile. Both **owned by `sharing.md §4`** — this spec only
  carries the fields through; `null` when the skill has no recorded source.
- **Frontmatter parse** — read only the first `---`…`---` block and extract the `name` and
  `description` scalar lines. A minimal line parser (stdlib only — no YAML dependency). File
  *body* is never read.
- Symlinked entries are **followed** to read their `SKILL.md`; a dangling symlink or a dir with
  no `SKILL.md` is skipped. Only immediate children of `skills/` are scanned (not nested).
- Sorted by `name` (case-insensitive). Reads only under `CLAUDE_DIR` (`config-store.md §1`), so a
  scratch `CLAUDE_DIR` isolates tests.

## 4. (folded into §3)

The source's separate listing endpoint is folded into `main(action="list")` — there is no HTTP
surface (`architecture.md §2`). One directory walk per call, no caching. Empty `skills` when
none exist.

## 5. Reveal folder — `main(action="open", slug)`

Open a skill's folder in the OS file explorer — the same mechanism Memory uses (`memory.md §6`),
scoped to `skills/` instead of `projects/*/memory/`.

- Param `slug`. `skills.py` resolves the folder via `lib.safe_subdir(<CLAUDE_DIR>/skills, slug)`,
  the **trust boundary** (`memory.md §6`): `slug` is charset/`..`-validated and the **lexical**
  path must stay inside `<CLAUDE_DIR>/skills/`. A **linked** skill's entry is a symlink pointing
  outside the base (§3); the boundary is lexical precisely so it permits this — a `realpath`-based
  check resolved the leaf symlink and rejected every linked skill, breaking reveal-in-explorer for
  exactly the skills installed via the `skills` CLI.
- Calls `lib.reveal(dir)` (`memory.md §6`) — array-arg platform open command on the **validated
  directory only** (never a file), so no shell/app-launch risk.
- Returns `{ ok: true }`, or `{ ok:false, error }` on validation failure.

## 6. UI

The Skills section (`index.html`) calls `main(action="list")` and renders one row per skill:
**name** as heading, the **description** as body text (the whole point — it's how the skill
self-describes), a muted **"linked"** tag when `linked`, a **Copy setup command** control
(`sharing.md §6`), and an **Open folder** action (§5). No per-skill toggle — a skill's
availability is not app-managed state (§7). Empty state: a muted "No local skills." Tab
provenance line: backed by `skills/` (read-only).

## 7. Enable/disable is not a concept here

Local skills have no enabled/disabled flag — a `SKILL.md` present under `skills/` is available to
Claude Code, full stop. So this tab has **no toggle** (contrast plugins, whose `enabledPlugins`
state `plugins.md §1` owns). Removing a skill means deleting its folder on disk, out of scope for
the read-only viewer (§2).

## Non-goals

- **Plugin-bundled skills** — owned by `plugins.md`; enabling individual skills within a plugin
  is not a Claude Code concept (whole-plugin granularity only).
- **Editing / creating skill content** — the app never writes a `SKILL.md` (§2). A local skills
  *editor* is TARGET (`overview.md` → Planned).
- **Agents editor** (`agents/*.md`) — a sibling local-asset surface, still Planned; not owned here.
- **The reveal primitive** — `lib.reveal` + the platform open commands are owned by `memory.md §6`;
  this spec reuses them with a `skills/`-scoped trust boundary (§5).
- **Whether skills are version-controlled** — `version-control.md §2` (the whitelist tracks
  `skills/`; note symlinked skills point outside the repo and so aren't tracked content).
- **The git mechanics / file model** — `version-control.md`, `config-store.md`.

## Open questions

- **Read-only (v1).** v1 is a pure viewer + reveal (§2). Folder-level actions (delete a skill, or
  commit `skills/` drift like `memory.md §8`–§9) are deferred until there's a clear need; if
  added, record the reversal here.
- **Viewing SKILL.md contents.** The tab shows frontmatter `name`/`description` only; rendering
  the skill body on demand is deferred (same posture as `memory.md` file contents).
- **Nested skills.** Only immediate children of `skills/` are scanned (§3).

## See also

- `plugins.md` — plugin-bundled skills (the *other* source of skills); this tab is the non-plugin
  complement.
- `memory.md §6` — the reveal-in-explorer primitive and path trust-boundary pattern this reuses.
- `version-control.md §2` — the whitelist that git-tracks `skills/`.
- `config-store.md §1` — `CLAUDE_DIR` resolution that scopes both reads and tests.
- `sharing.md §4` — owns the `source`/`shareCommand` fields this listing carries.
