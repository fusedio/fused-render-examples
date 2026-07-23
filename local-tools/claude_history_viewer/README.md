# claude-history-viewer

A read-only browser for your **Claude Code** conversation history, rendered by
Fused Render.

Pick a project, pick a session, and read the transcript — user turns, assistant
markdown, thinking blocks, tool calls and their results, token totals — straight
from `~/.claude/projects/*/*.jsonl`. Nothing is ever written back. Standard
library only, no keys. A simplified, Claude-only take on
[claude-code-history-viewer](https://github.com/jhlee0409/claude-code-history-viewer).

## What it demonstrates

Fused Render as a **local data browser** over an on-disk log format: one
`index.html` + `script.js` front end calling a single `history.py`
(`main(action=…)`) that parses JSONL, resolves session titles, and paginates
transcripts — all URL-param state so any view is bookmarkable and refresh-proof.
A good template for "read and render a pile of local files" tools. It's also
**spec-driven** — every behavior has an owning doc in [`specs/`](./specs).

## Run it

Copy this folder into your Fused Render install and open `index.html`. It reads
`~/.claude/projects` (read-only); if you have never run Claude Code it shows an
empty-state hint. Nothing to configure.

## Files

| File | Role |
|---|---|
| `index.html` + `script.js` | Three-pane UI — projects · sessions · transcript (vanilla ES2020, URL-param state, hand-rolled markdown) |
| `history.py` | `main(action="projects"\|"sessions"\|"messages")` — JSONL parsing, title resolution, token accounting, pagination (stdlib only, read-only) |
| `test_history.py` | Spec tests over a synthetic fixture tree — `python3 test_history.py` (no pytest) |
| `specs/` | The owning spec for every behavior |
