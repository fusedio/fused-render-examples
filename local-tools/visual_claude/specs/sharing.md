# Sharing

> **Status — SHIPPED (v1).** This file owns **share-command generation**: the paste-able
> terminal command that installs/sets up a marketplace, plugin, or skill on another machine,
> and where each command's inputs come from. **Sharing has NO dedicated module.** The
> `shareCommand` strings are computed *inside* the feature modules that already own each entity
> — `plugins.py`, `marketplaces.py`, and `skills.py` — and returned as a field on their `list`
> actions. This spec **owns the behavior** (the command format and the rules below); those
> three modules **implement it**. The UI's copy control lives in `index.html` (`§6`).

## 1. What a share command is

A **share command** is a string a user copies from a card and pastes into a **recipient's
terminal** to reproduce that config entity. Contract:

- **Targets the recipient's real installation.** Commands use the official CLIs
  (`claude plugin …`, `skills …`), which read the *invoking user's* `~/.claude` — never this
  app's `CLAUDE_DIR`. We only *build the string*; we never execute it (`Non-goals`).
- **User scope by default.** `claude plugin install` and `claude plugin marketplace add` both
  default to `--scope user`, so the flag is omitted. `skills add` uses `-g` (global/user).
- **Non-interactive.** The skill command passes `-y` to skip prompts.
- **No secrets.** Commands carry only a source ref (`owner/repo`, a git URL) and an id/slug —
  never file contents, tokens, or local paths.
- **Best-effort, not validated.** We emit the command from recorded metadata; we do not verify
  the source still resolves on the recipient's machine.

## 2. Marketplace share command (built in `marketplaces.py`)

`claude plugin marketplace add <source>`, where `<source>` is resolved from the marketplace's
source model (`marketplaces.md §1`):

- `source.repo` present → `<owner/repo>` (github kind).
- else `source.url` present → `<url>` (git kind).
- else → **null** (unshareable; `§5`).

Applies to both user-added and read-only official marketplaces — sharing only needs a resolvable
source, not edit rights.

## 3. Plugin share command (built in `plugins.py`)

Two lines:

```
claude plugin marketplace add <source>   ← only if the plugin's marketplace resolves (§2)
claude plugin install <id>               ← always; <id> is name@marketplace (plugins.md §1)
```

The marketplace-add line makes the command **standalone** so the recipient need not already have
the marketplace. If the plugin's marketplace source can't be resolved, only the install line is
emitted (the recipient must add the marketplace themselves). The install line is always present,
so a plugin's `shareCommand` is **never null**. `plugins.py list` loads the marketplace list once
to resolve each plugin's marketplace source (`§2`).

## 4. Skill share command (built in `skills.py`)

Non-plugin skills (`skills.md §1`) installed via the `skills` CLI record their origin in a
lockfile; unmanaged local skills do not.

- **Lockfile:** `<CLAUDE_DIR>/../.agents/.skill-lock.json` (the `.agents` sibling that
  `~/.claude/skills/<slug>` symlinks target). Shape: `{ skills: { <slug>: { source, sourceType,
  sourceUrl, skillPath, … } } }`. `skills.py list` reads it and attaches
  `source = skills[slug].source ?? skills[slug].sourceUrl ?? null` per skill.
- **Managed skill** (source present) → **`bunx skills add <source> -s <slug> -g -y`**.
- **Unmanaged skill** (no lockfile entry — e.g. a hand-authored local dir) → `shareCommand` is
  **null**; the UI disables the button with an explanatory tooltip (`§6`). We deliberately do
  **not** fall back to embedding file contents (`Non-goals`).

**`bunx`, never `npx`** — per the project's package-manager convention (the only place a
JS-tooling command appears in this project; the recipient needs Bun).

## 5. Surfacing — `shareCommand` field on the `list` actions

Rather than a dedicated module or action, each entity's existing `list` return carries a computed
`shareCommand: str | null` (null ⇒ unshareable, render disabled):

- `marketplaces.py list` → each item gains `shareCommand` (`§2`).
- `plugins.py list` → each item gains `shareCommand` (`§3`).
- `skills.py list` → each item gains `shareCommand` (`§4`), plus the raw `source` it derives from.

The three modules are the implementers; this spec is the single owner of the command *format*.

## 6. UI — copy-command control

A compact clipboard **icon** button on each marketplace/plugin/skill card in `index.html`:

- **Enabled** when `shareCommand` is non-null: on click, `navigator.clipboard.writeText` the
  command and toast "Copied setup command". `title` = the command itself (hover preview),
  `aria-label` = "Copy setup command".
- **Disabled** when null (skills only): greyed out, `title` = "No shareable source — this skill
  wasn't installed via the skills CLI".

## Non-goals

- **Executing** the command — the recipient runs it; we never spawn it (contrast the delegated
  *update* in `plugins.md §5`).
- **A dedicated sharing module** — deliberately none; the format lives in this spec, the strings
  are built inside `plugins.py` / `marketplaces.py` / `skills.py` (`§5`).
- **Embedding skill file contents** in a self-contained recreate script — rejected for v1; we
  rely on the `skills` CLI source instead. See Open questions.
- The entity models themselves — `marketplaces.md`, `plugins.md`, `skills.md`.

## Open questions

- **Unmanaged-skill fallback** — v1 disables sharing for skills with no recorded source. A
  self-contained bash script that recreates the skill's file tree inline (base64-embedded) is a
  possible TARGET if hand-authored skills need sharing.
- **`npx` alternative** — the skill command hardcodes `bunx`; a recipient without Bun must
  substitute `npx`. A runner toggle is TARGET.

## See also

- `marketplaces.md §1` — the marketplace source model the marketplace/plugin commands read.
- `plugins.md §1` — the `name@marketplace` id embedded in the plugin command.
- `skills.md §3` — the listing, extended here to read the lockfile and attach `source`.
- `config-store.md §2` — Claude-Code-owned dirs; the lockfile lives outside `CLAUDE_DIR`.
