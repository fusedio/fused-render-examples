# Version Control

> **Status — SHIPPED (v1).** This file owns the **git layer over `~/.claude`**: repo
> bootstrap, the whitelist `.gitignore` secret-safety model, commit-per-save, log, restore,
> status, and the change-preview `diff`. Implementing module: `lib.py` (`GITIGNORE`, `git`,
> `ensure_repo`, `commit`, `log`, `status`, `diff`, `drift_diff`, `restore`) and the git
> actions in `git_ops.py`. Git runs as a **Python subprocess** (`subprocess.run(["git", …],
> cwd=CLAUDE_DIR)`) with an argv array (never a shell string). It does NOT own the file model
> (`config-store.md`) or feature action shapes.

## 1. Repo bootstrap — `ensure_repo()`

Because there is **no persistent server** (`architecture.md §5`), `ensure_repo()` is
idempotent and runs at the **top of every git action** and on **app load** (the first
`preferences.py get`) — not once at startup. Each run:

1. If `CLAUDE_DIR/.git` is absent → `git init`, and set a local `user.email`/`user.name` if
   none is configured (so commits work without global git identity).
2. Write/refresh `.gitignore` to exactly `GITIGNORE` (§2) if missing or drifted.
3. If the repo has no `HEAD` yet → seed commit `"Initial snapshot of Claude config"`.

**Side effect:** pointing the app at a real `~/.claude` creates `~/.claude/.git` on first
run. Non-destructive, but real. Testing uses a scratch `CLAUDE_DIR` (`config-store.md §1`).

## 2. Whitelist `.gitignore` — secret safety (SHIPPED)

The `GITIGNORE` constant uses an **ignore-everything-then-opt-in** model:

```
/*
!.gitignore
!settings.json
!settings.local.json
!CLAUDE.md
!keybindings.json
!statusline-command.sh
!hooks/
!agents/
!skills/
!commands/
!projects/
projects/*
!projects/*/
projects/*/*
!projects/*/memory/
**/.DS_Store
```

**Invariant:** anything not explicitly whitelisted is untracked — including any *future*
secret or cache file dropped into `~/.claude`. A planted `.credentials.json`, `sessions/`, or
`history.jsonl` is excluded; only whitelisted config is tracked. `~/.claude.json` (holds the
OAuth account) lives at `~/`, **outside** the repo, so it is structurally unreachable.

**Persistent memory (SHIPPED).** The last five lines whitelist `projects/*/memory/**` —
Claude Code's curated persistent memory (`memory/*.md` + `MEMORY.md` per project) — while
keeping the rest of each `projects/<slug>/` directory ignored. This is a **surgical
re-include under an ignored parent**: git cannot re-include a path whose ancestor is
ignored, so each level is un-ignored (`!projects/`, `!projects/*/`, `!projects/*/memory/`)
and each level's *other* contents re-ignored (`projects/*`, `projects/*/*`) so only the
`memory/` subtree survives. With sibling `sess.jsonl` transcripts and `<uuid>/` session state
present, `git add -A` tracks **only** the `memory/` dirs and nothing else under `projects/`.
Session transcripts (`*.jsonl`, mode `600`), per-session state dirs, and all other project
scaffolding remain ignored.

- **Scope: all projects.** `projects/*/memory/` matches every project slug, so memory from
  every repo you've worked in is tracked — no per-machine path list, and new projects are
  covered automatically.
- **Sharing caveat.** Because a profile export (`profiles.md §6`) is `git archive` of tracked
  files, exported/pushed profiles **include** memory notes from all projects. Memory is
  portable *intent* (facts, preferences), never secrets or transcripts, so this is safe by the
  same whitelist invariant — but a shared profile carries the author's memory. Flagged, not
  blocked.
- **Agent definitions** (`agents/*.md`) were already whitelisted; there is no separate
  per-subagent memory store on disk — "agent memory" and "memory" are the same
  `projects/*/memory/` files.

## 3. Commit-per-save — `commit(message, pathspec=None)`

After each feature write (`config-store.md §4`), the feature calls `commit(msg)` (inside the
same `config_lock()`, `config-store.md §4`):

1. `git add -A`.
2. If `git status --porcelain` is empty → return `None` (no-op, no empty commits).
3. Else `git commit -m msg`; return the new HEAD sha.

Messages are descriptive and feature-authored, e.g. `Update preferences: model, theme`,
`Enable plugin github@claude-plugins-official`, `Add marketplace test-mp`.

**Path-limited commits.** Passing `pathspec` stages and commits only that subset
(`git add -A -- <pathspec>`, `git commit -m msg -- <pathspec>`), so unrelated working-tree
drift stays uncommitted. Same git layer, narrower scope. Current user: the Memory tab's
per-folder commit and clear (`memory.md §8`–§9).

## 4. History & restore

- `log(50)` → array of `{sha, date (ISO), message}`, newest first (`lib.py` `log`, parses
  `%H%x1f%cI%x1f%s`). Surfaced via `git_ops.py main(action="log")`.
- `restore(sha)` → `git checkout <sha> -- .` (restores whitelisted files to that commit) then
  `commit("Restore config to <sha8>")`. A restore is itself a **forward** commit — history is
  never rewritten, so the commit restored *from* remains in the log and the restore can be
  undone by restoring again. Named "Restore" (not git's narrower "revert") everywhere. `sha`
  is validated with `git rev-parse --verify` → error on unknown ref.

## 5. Working-tree status — `status()`

Because every feature write is committed (§3), the tree is normally **clean** right after a
save. Uncommitted changes only appear when `~/.claude` is edited outside the app (a manual
edit, another tool, or `/config`). The UI surfaces this so the user knows the on-disk state
may have drifted from the last commit.

- `lib.py` `status()` → parses `git status --porcelain -uall` (whitelisted files only, since
  ignored files never appear) into `{ dirty: bool, files: [str] }`. `-uall` expands untracked
  directories to individual files, so a new memory file counts as itself rather than a
  collapsed `projects/` entry.
- `git_ops.py main(action="status")` → `{ dirty, files }`. Read-only; never mutates.
- `git_ops.py main(action="commit")` → `{ ok, committed: sha | null }`: commit all current
  working-tree drift on demand via `commit("Commit working-tree changes")` (§3, `git add -A`);
  `null` if the tree was already clean. This is the badge dialog's **Commit** action.
- UI: a small clean/dirty badge in the app header (`index.html`). Dirty is informational, not
  an error. When dirty, **clicking the badge previews the drift** (§6) in the **in-app
  scrollable dialog** (§6, never a native `alert()` — a drifted tree can hold dozens of files
  and a native dialog can't scroll, pushing its dismiss control off-screen). The dialog offers
  **Commit · Close**. When clean, clicking just re-checks status.

## 6. Change preview — `diff(target)` / `drift_diff()`

Before a **restore** (§4) or a profile **switch** (`profiles.md`) rewrites the working tree,
the UI shows *what would change*. Both operations move the tree to a target ref, so one
primitive serves both: preview the diff between the current `HEAD` and the target. A companion
primitive, `drift_diff()`, previews the *current uncommitted drift* (working tree vs `HEAD`)
behind the status badge (§5). Both return the **same shape**
`{ files: [{path, status}], settings: [SettingsDelta] }` so the UI renders them identically.

- `lib.py` `diff(target)` computes two views:
  - **files** — `git diff --name-status HEAD <target>` parsed into `{path, status}`
    (Added / Modified / Deleted / Renamed). Only whitelisted files ever appear (§2).
  - **settings** — a key-level delta of `settings.json`: read the blob at each ref
    (`git show HEAD:settings.json`, `git show <target>:settings.json`; absent blob → `{}`),
    `flatten` both to dotted leaf paths (e.g. `permissions.defaultMode`), and emit
    `SettingsDelta = {key, from, to}` for every key added (`from: null`), removed (`to: null`),
    or changed. This is the human-meaningful half — it names the preferences that flip.
- `target` is any ref: a commit sha (restore preview) or a branch name (switch preview),
  validated with `git rev-parse --verify` → error on unknown ref. **Read-only**; never mutates.
- `git diff HEAD <target>` is **direction-correct**: it describes the change *applied* by
  moving to `target`, not the inverse.

**Preview dialog — in-app, scrollable (not native).** All three change previews (drift on the
badge, restore, and switch) render through one in-app modal in `index.html`, never
`window.alert`/`window.confirm`: native dialogs can't scroll, and a large change set (a work
session can leave 40+ uncommitted memory files) overflows the viewport and pushes the
OK/Cancel controls off-screen. The in-app dialog gives the change list a **bounded, scrollable
body** with title and buttons pinned outside the scroll area, dismissable via a × close button,
footer buttons, overlay click, and Escape. The informational drift preview exposes **Close**;
the two-button variant exposes **cancel + confirm** so the caller proceeds only on confirm.

**Working-tree drift — `drift_diff()`.** Same two views, comparing `HEAD` to the *current
on-disk state*:
- **files** — parsed from `git status --porcelain` (the **same source as the badge count**,
  §5), so newly-created untracked whitelisted files (e.g. a new memory file) are included.
  Untracked (`??`) maps to status `A`.
- **settings** — `_settings_delta(HEAD:settings.json, <on-disk settings.json>)`, direction
  `HEAD → working tree`. Returns empty `files`/`settings` when the tree is clean.

## Non-goals

- Reading/writing the config files themselves — owned by `config-store.md`.
- **Branches / profiles** — switching between named branches is owned by `profiles.md`
  (TARGET), built on this layer's primitives; this file still owns the primitives.
- Remote/push, multi-device sync — not in v1 (see Open questions, deferred to `profiles.md`).

## Open questions

- **Remote sync** — pushing a branch to a remote is scoped to `profiles.md` (Phase 2, TARGET)
  as the profile *sharing* mechanism; v1 remains local history only.
- **Per-commit diff browsing** — diffing an arbitrary log entry against its parent in the
  History tab remains TARGET; §6 covers the restore/switch/drift preview only.

## See also

- `config-store.md` — the atomic writes this layer commits, and the `config_lock` it shares.
- `architecture.md §5` — why `ensure_repo()` runs per action, not once at startup.
- `memory.md` — the read-only viewer for the `projects/*/memory/` files this whitelist tracks.
