# Profiles

> **Status — SHIPPED (Phase 1 + export/import); remote sharing TARGET.** This file owns the
> **profile model**: a profile is a named **git branch** over `~/.claude`, and this spec owns
> switching, creating, deleting, exporting, and importing profiles. It builds entirely on the git
> layer `version-control.md` owns — it adds *branch* operations, it does not re-own commit/log/
> restore/status. Implementing modules: `lib.py` (`current_profile`, `branches`, `create_branch`,
> `switch_branch`, `delete_branch`, `archive_zip`, `import_archive`), `profiles.py`
> (`main(action="list"|"create"|"switch"|"delete"|"export"|"inspect"|"import")`), and the Profiles
> section of `index.html` (`renderProfiles`).

## 1. What a profile is

- A **profile** is a git branch in the `~/.claude` repo. The **current profile** is the
  checked-out branch (`git rev-parse --abbrev-ref HEAD`).
- The branch created by `ensure_repo` (`version-control.md §1`) — `main`/`master` — is the
  **default profile**. It always exists and can never be deleted.
- A profile carries exactly the **whitelisted, tracked files** (`version-control.md §2`):
  `settings.json`, `CLAUDE.md`, `hooks/`, `agents/`, `skills/`, `commands/`, and
  `projects/*/memory/`. It does **not** carry `~/.claude.json` (outside the repo) or `plugins/`
  (git-ignored). So a profile is a portable snapshot of *intent* and *portable assets* — never
  machine state, never secrets.
- **Delivery is phased.** Phase 1 (SHIPPED): local branch profiles — create, list, switch,
  delete, export (§6), import (§7), all offline. Remaining TARGET: sharing profiles via a
  *remote* (push/pull).

## 2. `list` — `main(action="list")`

`{ profiles: [{ name, current, isDefault }], current }`. `branches()` runs `git branch
--format=%(refname:short)`, marking `current` (== checked-out) and `isDefault` (`main`/`master`).

## 3. `create` — `main(action="create", name, from?)`

Create a branch, **without switching** (`create_branch` never touches the working tree, so it
always succeeds regardless of drift). The UI then drives a separate `switch` (§4) so the branch
exists even if the user cancels the switch.

- **Guard:** `name` must match `^[A-Za-z0-9._/-]+$` and not start with `-`, must not already
  exist, and `from` (if given) must exist. Returns `{ok:false, error}` on any violation.
- A few strings the regex admits are still invalid git ref names (`a..b`, trailing `.lock`,
  `HEAD`); `create_branch` surfaces git's rejection as `{ok:false, error}`, not a crash.
- Returns `{ok:true, name}`.

## 4. `switch` — `main(action="switch", name, message?)` (dirty-guarded)

Switch the checked-out profile, rewriting the tracked files in place. Because switching would
discard uncommitted drift, it is **dirty-guarded**:

- If the tree is **dirty** (`lib.status()`) and **no `message`** was given, do nothing and return
  `{ok:false, dirty:true, files}` — the UI's cue to offer committing first (the source's HTTP 409
  analog).
- If `message` is given, `lib.commit(message)` **first** (saves drift to the *current* profile),
  then `switch_branch(name)` (`git checkout`).
- If the tree is **clean**, switch directly.
- Returns `{ok:true, current}`. Guard: `name` must exist.
- Switching changes **intent**, not machine state — it moves the tracked files, nothing else.
  The UI previews the change first (`version-control.md §6` `diff(name)`) and reloads on success
  so every other tab re-reads the swapped-in config. All mutation under `config_lock()`.

## 5. `delete` — `main(action="delete", name)`

`delete_branch(name)` = `git branch -d` (**safe delete**). Refused cases return `{ok:false,
error}`, never a crash:

- the **current** profile (switch away first),
- the **default** profile (`main`/`master`),
- a profile with **commits not merged elsewhere** (git's "not fully merged" → a user-actionable
  message). Safe-delete deliberately never force-deletes unmerged work.

## 6. `export` — `main(action="export", name)`

Download a profile as a `.zip` of exactly its tracked, whitelisted files — the portable
snapshot of §1, suitable for handing to another machine or archiving offline.

- `archive_zip(name)` (in `lib.py`) runs `git archive --format=zip <name>` and returns the raw
  zip **bytes**. Because a branch's tree is only the whitelisted, tracked files
  (`version-control.md §2`), the archive carries `settings.json`, `CLAUDE.md`, `hooks/`,
  `agents/`, `skills/`, `commands/`, and `projects/*/memory/` — never `~/.claude.json`,
  `plugins/`, or secrets. **Memory-in-export caveat:** `projects/*/memory/` *is* tracked, so it
  travels in the archive — the same surgical re-include `version-control.md §2` owns.
- **Binary over a JSON return.** `runPython` returns JSON, not a byte stream, so `profiles.py`
  base64-encodes the (tiny) archive: `{ok:true, filename, b64}`. `index.html` decodes `b64` to a
  `Blob` and triggers a client-side download via a transient object-URL anchor — no temp file on
  disk, no cleanup, staying inside the runtime's JSON contract.
- **Filename:** `claude-<name-with-/→->-<YYYY-MM-DD>.zip`. The date is stamped **client-side**
  (`main()` has no wall clock — `runPython` runs stdlib-only and the runtime forbids `datetime.now`
  patterns in views; the page owns "now"). Python returns only the sanitized stem; the page
  appends the date and extension.
- Guard: `name` must exist → else `{ok:false, error}`. Read-only: exports mutate nothing, take no
  lock, and make no commit.

## 7. `import` — inspect then apply a `.zip` into a new profile

The round-trip of §6: take an exported (or hand-made) `.zip`, let the user pick which files/folders
to pull in, and land them **on a new branch** so the current profile is never mutated in place.
Two actions, because the user selects between them:

- **`inspect` — `main(action="inspect", b64)`** → `{ok, entries:[{path, isDir, size}]}`. Reads the
  base64'd zip with stdlib `zipfile` and returns its listing so the page can render a
  file/folder picker. Rejects a non-zip as `{ok:false, error}`. Read-only — touches nothing.
- **`import` — `main(action="import", b64, paths, branch, message?)`** (dirty-guarded). `paths` is
  a JSON-stringified list of selected entries (a file, or a folder prefix that pulls every entry
  under it); `branch` is the new profile to create.

**Always a new branch (never in place).** Import creates `branch` off the current HEAD, switches
into it, overlays the selected files, and commits — so the pre-import profile is untouched and the
user can switch back. `branch` must pass the §3 name guard and must **not** already exist. The
overlay is *additive over a copy of the current profile*: unselected files carry over from the
branch point; selected files overwrite. Because switching into the new branch rewrites the working
tree, import is **dirty-guarded exactly like §4** — dirty tree + no `message` → `{ok:false,
dirty:true, files}`; with `message`, commit drift to the current profile first. All of it under
`config_lock()`, then one commit `"Import into <branch>"`. Returns `{ok:true, branch, imported}`.

**Trust boundary (extraction).** `import_archive(zip_bytes, paths)` in `lib.py` is the write-side
counterpart to the export archiver, and the one place foreign bytes become files under
`CLAUDE_DIR`. Every extracted entry is confined: the entry's `realpath` under `CLAUDE_DIR` must
stay within `CLAUDE_DIR` (the multi-segment analog of `safe_subdir`, `config-store.md`), so zip
path-traversal (`../`, absolute members) is refused, not written. Only entries matching a selected
path are extracted. Whether an extracted file is then *tracked* on the branch is governed by the
whitelist `version-control.md §2` owns — a non-whitelisted member lands on disk but is git-ignored,
so it won't appear in the branch's tree (and thus won't survive a later switch away and back).

## Non-goals

- The git primitives themselves — owned by `version-control.md`; this spec composes them into
  branch operations.
- The whitelist that decides which extracted files are tracked — owned by `version-control.md §2`;
  §7 points at it, it does not re-own it.
- **Remote** push/pull mechanics — TARGET (see Open questions). Export (§6) + import (§7) are the
  offline, file-based sharing path and are SHIPPED.

## Open questions

- **Remote sharing (TARGET).** Pushing/pulling a profile branch to a git remote so two machines
  can converge, rather than the one-way `.zip` handoff §6/§7 ship. Note the memory-in-export caveat
  (§6, `version-control.md §2`) applies to any remote too.
- **Import whitelist policy (RESOLVED — confine, don't restrict).** §7 refuses path-traversal
  unconditionally but does *not* hard-restrict selectable entries to the whitelist — the user picks
  what to pull, and non-whitelisted members simply land git-ignored. Revisit if a foreign zip
  planting stray on-disk files proves confusing.

## See also

- `version-control.md §1`–§6 — the repo, commit, restore, status, and diff-preview primitives
  this spec builds branch operations on.
