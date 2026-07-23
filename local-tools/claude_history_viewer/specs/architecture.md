# Architecture

> **Status — target (v1).** This file owns the **runtime shape** of
> claude-history-viewer: file layout, the `main(action=…)` contract, params-as-state,
> the read-only guarantee, error/empty states, and the size/timeout budget. It does
> NOT own the JSONL parsing rules (`history-data.md`) nor either UI surface
> (`browsing.md`, `transcript.md`). Implementing modules: `index.html`, `script.js`,
> `history.py` (`main`).

## 1. File layout

| File | Role |
|---|---|
| `index.html` | Static shell: layout, styles, empty containers. No inline app logic beyond loading `script.js`. |
| `script.js` | All UI logic, vanilla ES2020. Reads state from `fused.params`, calls `history.py`, renders. |
| `history.py` | The single data module. Stdlib only (`json`, `os`, `datetime`, `pathlib`). One `main(action=…)` dispatching to the three actions. |
| `specs/` | This registry. |
| `README.md` | Example-gallery readme (what it demonstrates, how to run). |

No build step, no external network calls, no API keys, no dependencies beyond the
Python stdlib. The `window.fused` runtime is injected by the explorer — never
`<script src>`'d.

## 2. The `main(action=…)` contract

`history.py` exposes exactly one function:

```python
def main(action: str = "projects", project: str = "", session: str = "",
         offset: int = 0, limit: int = 200, claude_dir: str = ""):
```

- **action**: `"projects"` (`browsing.md §2`) | `"sessions"` (`browsing.md §3`) |
  `"messages"` (`transcript.md §2`). Unknown action → `{"error": "unknown action"}`.
- **project**: the project directory *name* (slug) under `projects/`, never a full
  path. `history.py` joins it against the base dir and must reject values containing
  path separators or `..` (return an error dict) — the slug is the only accepted key.
- **session**: the session filename (`<uuid>.jsonl`), same containment rule.
- **claude_dir**: test seam — overrides the base directory (default
  `~/.claude`). Lets tests point at a fixture tree; the UI never sets it.
- Every action returns a JSON-native dict. Failures return `{"error": str}` rather
  than raising, so the page can render a friendly state instead of the red overlay
  (parse-level per-line failures are skipped silently, see `history-data.md §4`).

## 3. Params-as-state

URL params are the entire view state (fused-render canon: controls write params,
`onChange` re-renders, refresh reproduces the view).

| Param | Meaning | Default when absent |
|---|---|---|
| `project` | selected project slug | none → no session list, no transcript |
| `session` | selected session filename | none → no transcript |
| `offset` | transcript page start (message index) | `"0"` |
| `sidechains` | `"1"` to include sidechain (subagent) messages | `"0"` (hidden) |

Selecting a project clears `session` and `offset`; selecting a session resets
`offset` to `"0"`. All values are strings (`String(n)` before `set`).

## 4. Read-only guarantee

`history.py` opens files with mode `"r"`/`"rb"` only. No `fused.writeFile`, no
mutation of anything under `~/.claude`, ever. This is the project's core safety
property; any future feature that would write is out of scope by definition
(`overview.md` Non-goals).

## 5. Empty and error states (no hard-error on open)

Repo policy: no example may hard-error on open (`tests/check.py` gate). Concretely:

- **Missing `~/.claude/projects`** → `action="projects"` returns
  `{"projects": []}`; the UI shows a hint ("No Claude Code history found at
  ~/.claude/projects").
- **Empty project / vanished file** (deleted between listing and click) →
  `{"error": …}` rendered inline in the affected pane, other panes untouched.
- **Malformed JSONL lines** → skipped, never fatal (`history-data.md §4`).
- `script.js` wraps every `runPython` in try/catch and renders `err.message`
  inline — the red overlay is never the intended failure mode.

## 6. Size & timeout budget

Every `runPython` call is a fresh subprocess killed at 60 s; session files can be
tens of MB. Budget rules:

- **Metadata scans are bounded** — session listing reads each file in one pass but
  parses only what metadata needs (`history-data.md §5`); it never builds full
  message lists.
- **Transcripts are paginated** — `action="messages"` returns at most `limit`
  (default 200) normalized messages per call, with `total` so the UI can page
  (`transcript.md §2`).
- **Blocks are truncated** — any single content block's text is capped server-side
  (`transcript.md §3`) so the JSON payload stays small enough to serialize and render.
- Stdlib-only imports keep subprocess startup ~instant (no pandas tax).

## Non-goals

- JSONL schema and parsing semantics — `history-data.md`.
- What each surface shows and how it's rendered — `browsing.md`, `transcript.md`.
- Writing anything to disk — out of scope for the whole project (`overview.md`).

## See also

- `history-data.md` — the on-disk format all three actions parse.
- `browsing.md` — projects/sessions actions + sidebar UI.
- `transcript.md` — messages action + conversation rendering.
