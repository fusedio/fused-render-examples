# claude-history-viewer — Spec Registry

> **Status — target (v1 not yet built).** This file is the capability index: every
> spec earns one bullet here ending in its owning filename. To find the owner of a
> concept, start here. Do not describe behavior in this file — point to the owner.

Spec-driven development: no feature ships without its owning spec updated first.
The tool is a **read-only browser for Claude Code conversation history** — a
simplified, Claude-only clone of
[claude-code-history-viewer](https://github.com/jhlee0409/claude-code-history-viewer)
built as a fused-render view. It never writes to `~/.claude`.

## Capabilities

- **Architecture** — the fused-render runtime shape: one `index.html` + `script.js`
  over a single `history.py` (`main(action=…)`), params-as-state URL sync, read-only
  guarantee, graceful empty/error states, and the pagination strategy that keeps every
  call under the 60 s subprocess timeout (`architecture.md`).
- **History data** — where Claude Code stores history on disk
  (`~/.claude/projects/<slug>/<uuid>.jsonl`), the JSONL line schema, which line types
  are shown vs. skipped, session title resolution, and token accounting
  (`history-data.md`).
- **Browsing** — the project-list and session-list surfaces:
  `main(action="projects")` / `main(action="sessions")`, sort order, display-name
  resolution, and their sidebar UI (`browsing.md`).
- **Transcript** — the conversation surface: `main(action="messages")` with
  pagination, content-block normalization and truncation, and the rendering rules for
  text/markdown, thinking, tool_use/tool_result cards, and system events
  (`transcript.md`).

## Non-goals (whole project)

- Other providers (Gemini, Codex, Cursor, …) — Claude Code only.
- Global search, analytics dashboard, session board, export, session rename, file
  watching, incremental-parse caching — all present in the original, all dropped.
- Any write to `~/.claude` — this tool is strictly read-only.

## Reading order for a newcomer

1. `architecture.md` — how the pieces fit in the fused-render runtime.
2. `history-data.md` — the on-disk format everything parses.
3. `browsing.md`, then `transcript.md` — the two UI surfaces.
