# Browsing

> **Status — target (v1).** This file owns the **project-list and session-list
> surfaces**: the `projects` and `sessions` actions of `history.py` and the sidebar
> UI that renders them. It does NOT own the disk format or parsing rules
> (`history-data.md`) nor the transcript pane (`transcript.md`). Implementing
> modules: `history.py` (`main(action="projects"|"sessions")`), the sidebar portion
> of `index.html`/`script.js`.

## 1. UI shape

A two-level sidebar next to the transcript pane (`transcript.md §4`):

- **Projects pane** — always visible. Clicking a project sets the `project` param
  (clearing `session`/`offset`, `architecture.md §3`) and loads its sessions.
- **Sessions pane** — visible once a project is selected; lists that project's
  sessions. Clicking one sets `session` (resetting `offset`).
- Selected items are visually highlighted, derived from params (not click state), so
  a refreshed URL restores the exact selection.
- Dark theme matching the explorer.

## 2. `main(action="projects")`

Scans `<claude_dir>/projects/*` (depth 1, dirs only). A directory qualifies if it
contains ≥1 top-level `.jsonl` file. Returns, sorted by `last_modified` desc:

```
{ "projects": [{ "slug": str, "name": str, "path": str,
                 "session_count": int, "last_modified": float }] }
```

- **slug**: the directory name — the key later calls pass back as `project`.
- **name**: display name — leaf of the resolved project path.
- **path**: resolved project path for the tooltip/subtitle. Resolution priority:
  1. first non-empty `cwd` within the first 100 lines of the most recently modified
     session file (`history-data.md §2`);
  2. lossy decode of the slug (`-` → `/`, best effort).
- **session_count**: number of top-level `.jsonl` files (no parsing needed).
- **last_modified**: max mtime (epoch seconds) across those files. The UI renders it
  as a relative time ("2h ago").

No per-session parsing beyond the one bounded `cwd` probe — the projects call must
stay fast on dozens of projects (`architecture.md §6`).

## 3. `main(action="sessions", project=slug)`

Runs the single-pass metadata scan (`history-data.md §9`) over each top-level
`.jsonl` in the project. Returns, sorted by `last_time` desc, dropping zero-message
sessions:

```
{ "sessions": [{ "file": str, "title": str, "is_renamed": bool,
                 "message_count": int, "sidechain_count": int,
                 "first_time": str|null, "last_time": str|null,
                 "has_tool_use": bool, "tokens": int }] }
```

- **file**: the `.jsonl` filename — the key `transcript.md §2` passes back.
- **title** / **is_renamed**: `history-data.md §5`.
- **tokens**: combined token total (`history-data.md §8`).
- Timestamps are the raw RFC3339 strings; the UI formats them.

## 4. Session row rendering

Each row shows the title (bold when `is_renamed`), a meta line with message count,
relative last-activity time, and compact token total (`12.4k tok`), and a small
tool-use indicator when `has_tool_use`. Rows are plain buttons — no context menus,
no rename (read-only, `architecture.md §4`).

## Non-goals

- Parsing/classification rules the scan applies — `history-data.md`.
- Message loading and rendering — `transcript.md`.
- Search/filter boxes over projects or sessions — dropped for v1 (candidate future
  spec: `filtering.md`).

## See also

- `history-data.md` — the scan these two actions implement.
- `transcript.md` — what a click on a session row leads to.
- `architecture.md` — param wiring and error states these panes obey.
