# Memory

> **Status — SHIPPED (v3).** This file owns the **memory surface**: discovering and listing
> Claude Code's persistent memory under `~/.claude/projects/*/memory/`, and the **per-folder
> lifecycle controls** — change status (§7), commit (§8), clear (§9), and reveal-in-explorer
> (§6). Memory *file contents* are still authored by Claude Code, never created/edited here;
> what the app added in v3 is management of each folder's **git lifecycle** (is it dirty,
> commit it, wipe it). Implementing modules: `memory.py`
> (`main(action="list"|"open"|"commit"|"clear", …)`), `lib.py` (`safe_subdir`, `reveal`,
> `commit`, `status`), and the Memory section of `index.html`. It does NOT own the git
> mechanics (`version-control.md §3`–§6) nor the whitelist (`version-control.md §2`).

## 1. What memory is

Claude Code keeps curated persistent facts as markdown under
`~/.claude/projects/<project-slug>/memory/`: one `*.md` file per fact, plus a `MEMORY.md`
index. `<project-slug>` is the project path flattened (e.g. `-Users-iamsdas-Work`). There is
**one memory dir per project** you've worked in; there is no separate per-subagent store. These
files are git-tracked (`version-control.md §2`); this surface makes them *viewable in the app*
without opening the files by hand.

## 2. Content read-only; folder lifecycle writable

Memory **file contents** are read-only from the app — it never creates, edits, or deletes an
*individual* memory file, nor shows file contents; Claude Code authors memory, the app only
displays the listing. What v3 adds is **folder-level git lifecycle** actions: commit a folder's
drift (§8) and clear a whole folder (§9, delete every `.md` + commit the deletion). These
operate on the folder as a unit, never on a single file's text. Memory holds curated facts and
preferences, not secrets or transcripts — transcripts are the sibling `.jsonl` files, never
touched here; only markdown under `memory/` is read, committed, or cleared.

## 3. Discovery — `main(action="list")`

`memory.py` walks `CLAUDE_DIR/projects/*/memory/` and returns, per project that has a non-empty
memory dir:

```
{ projects: [{ project: str, files: [str], changes: [{path, status}] }] }
```

- **project**: the slug (directory name under `projects/`).
- **files**: the `*.md` **filenames** in that project's `memory/`, `MEMORY.md` first (the
  index), then the rest alphabetical. A listing only — **file contents are never read**.
- **changes**: the folder's per-folder change status (§7), empty when clean.
- Projects with no `memory/` dir, or an empty one, are omitted. Only `*.md` is listed. Reads one
  `git status` per call (cheap); no caching. Reads only under `CLAUDE_DIR` (`config-store.md §1`),
  so a scratch `CLAUDE_DIR` isolates tests.

The write actions (§6, §8, §9) are separate `action` values on the same module.

## 4. (folded into §3)

The source's separate listing endpoint is folded into `main(action="list")` above — there is no
HTTP surface (`architecture.md §2`), so the enriched listing is one action's return value.

## 5. UI

The Memory section (`index.html`) calls `main(action="list")` and renders one section per
project. The **folder is the unit of interaction** (commit/clear/reveal act on the whole
folder, never a single file). Each section shows the **slug as heading** with a **file-count
badge** (`N file(s)`, from `files.length` — conveys how many memories the folder holds), the
`*.md` filenames as a sub-line, and a **per-folder change indicator** (§7): when the folder has
uncommitted changes, an "N uncommitted change(s)" affordance; when clean, a muted "committed".
Clicking the indicator opens the change **preview-and-commit** dialog (`version-control.md §6`):
the scrollable change list with **Close · Commit** in the footer — review the drift, then either
dismiss or commit this folder (§8). On commit, a toast and the listing refreshes. Commit lives
only here, tied to the preview. Each section also carries:

- **Open folder** — reveal the folder in the OS file explorer (§6).
- **Clear** — delete every memory in this folder (§9). Destructive, so it goes through the
  dialog's **Cancel + confirm** (`version-control.md §6`) naming what will be deleted and that
  it's recoverable from the History tab; on confirm, a toast and the listing refreshes.

Empty state: a muted "No memory recorded yet."

## 6. Reveal folder in file explorer — `main(action="open", project)`

Because the app runs locally, Python can open the real folder in the OS file explorer (Finder
etc.) — a browser cannot. This is a **read-only side effect**: it opens the folder for browsing,
never edits anything.

- Param `project`. `memory.py` resolves the folder via `lib.safe_subdir(<projects dir>, project,
  "memory")` — the **trust boundary**: `project` is charset/`..`-validated, and the **lexical**
  path `base/project/memory` must stay inside `<CLAUDE_DIR>/projects/` (else error). This prevents
  a crafted request from opening arbitrary folders.
- **The check is lexical (`normpath`), not `realpath`-based** — symlinks are *not* resolved, so a
  folder that is itself a symlink pointing outside the base is permitted. Memory folders aren't
  symlinks, but `skills.md §5` reuses this boundary and linked skills are symlinks into `.agents`
  (`skills.md §3`); a realpath check rejected every one of them (the reveal-in-explorer bug). The
  lexical check still rejects `..`/absolute traversal, and resolving the leaf would add no
  protection — planting a symlink under the base already requires filesystem write access.
- `lib.reveal(dir)` runs the platform open command with **array args (no shell)**: macOS `open
  <dir>`, Linux `xdg-open <dir>`. It targets a **directory only** (the validated memory folder) —
  a directory cannot launch an app/script, so opening it is safe; individual files are never
  passed to `open`.
- Returns `{ ok: true }` on success, `{ ok:false, error }` on validation failure.

## 7. Per-folder change status

Whether a folder has drifted from its last commit. `memory.py` runs one `git status --porcelain
-uall` (the same source as the badge count, `version-control.md §5`), parses it, and **groups
the changed paths by project** — a line under `projects/<slug>/memory/` is attributed to
`<slug>`. Zipped into each project's `changes: [{path, status}]` in the §3 listing (empty when
clean).

- Attribution is by path prefix, so untracked new memory files (`??` → `A`), edits (`M`), and
  deletions (`D`) all surface per folder.
- Edge case: a folder whose memories were all deleted **on disk** has no files, so §3 omits it;
  its pending deletions won't show until committed. Acceptable — in-app clear (§9) commits the
  deletion immediately.

## 8. Commit a folder — `main(action="commit", project)`

Commit **only** this folder's drift, leaving unrelated working-tree drift untouched — true
per-folder control, distinct from the badge's whole-tree fold-in (`version-control.md §3`).

- Resolve+validate the dir via `lib.safe_subdir` (§6, the trust boundary), then
  `lib.commit("Update memory for <project>", pathspec=<projects/slug/memory>)` — a
  **path-limited commit** (`version-control.md §3`) that stages untracked/edits/deletions under
  the folder and commits scoped to that pathspec, so the commit contains that folder only. No-op
  returning `null` if the folder has no changes.
- Returns `{ ok: true, committed: sha | null }`. Error on validation failure. All under
  `config_lock()`.

## 9. Clear a folder — `main(action="clear", project)`

Wipe a project's memory and record the wipe in git. **Destructive but recoverable** — the
deletion is committed, so previously-committed memories are restorable from the History tab
(`version-control.md §4`); memories never committed (still untracked) are gone.

- Resolve+validate via `lib.safe_subdir` (§6), delete every `*.md` in the folder, then
  `lib.commit("Clear memory for <project>", pathspec=<folder>)` (`version-control.md §3`). Only
  `*.md` is removed — any non-markdown file is left as-is. No-op returning `null` if empty.
- After clearing, the folder still exists but is empty, so §3 drops it from the listing.
- Returns `{ ok: true, committed: sha | null }`. The destructive confirm lives in the UI (§5),
  not in `memory.py`.

## Non-goals

- **Editing memory *content*** — the app never creates or edits an individual memory file's text
  (§2); memory is authored by Claude Code. Folder-level commit/clear (§8–§9) act on the folder as
  a unit.
- **The git mechanics themselves** — commit, status, diff-preview, restore are owned by
  `version-control.md §3`–§6; this spec scopes them to a memory folder and points at them.
- **Whether memory is version-controlled** — owned by `version-control.md §2` (the `.gitignore`
  whitelist that tracks `projects/*/memory/`).
- **Session transcripts** (`projects/*/*.jsonl`) and per-session state — never read here; they
  are deliberately git-ignored (`version-control.md §2`).
- **The general file model / write mechanics** — `config-store.md`.

## Open questions

- **Reversal (v2 → v3) — "read-only surface".** v2 declared the tab strictly read-only. v3
  reverses that for **folder-level git lifecycle only** (commit §8, clear §9): editing memory
  *file contents* remains a non-goal.
- **Viewing file contents.** The tab lists filenames only. Showing a file's contents on demand
  is deferred until there's a clear need.
- **Per-file git history.** Linking a memory file to its commits is a possible enhancement; v3
  commits/clears at folder granularity only.

## See also

- `version-control.md §2` — the whitelist that git-tracks `projects/*/memory/`.
- `version-control.md §3` — the commit layer; §8–§9 perform path-limited commits on it.
- `version-control.md §5`–§6 — status + change-preview primitives §7 and the clear confirm reuse.
- `config-store.md §1` — `CLAUDE_DIR` resolution that scopes both reads and tests.
