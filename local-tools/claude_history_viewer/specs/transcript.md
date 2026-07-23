# Transcript

> **Status — target (v1).** This file owns the **conversation surface**: the
> `messages` action of `history.py` (pagination, normalization, truncation) and the
> rendering rules for every message and content-block kind. It does NOT own line
> classification or block schemas (`history-data.md §3/§7`) nor the sidebar
> (`browsing.md`). Implementing modules: `history.py` (`main(action="messages")`),
> the transcript portion of `index.html`/`script.js` (renderers incl. `renderMarkdown`).

## 1. UI shape

The main pane. Header: session title + message count + token total. Body: the
message list for the current page. Footer: pager. A `sidechains` toggle in the
header flips the param (`architecture.md §3`).

## 2. `main(action="messages", project, session, offset, limit)`

Parses the session file, classifies lines (`history-data.md §3`), filters out
sidechains unless `sidechains` param is on (the *filtered* list is what `offset`/
`limit`/`total` index over), and returns one page:

```
{ "messages": [Message], "total": int, "offset": int,
  "title": str, "tokens": int }
```

`Message` (normalized wire shape):

```
{ "uuid": str, "kind": "user"|"assistant"|"system", "timestamp": str|null,
  "model": str|null, "is_sidechain": bool, "subtype": str|null,
  "blocks": [Block] }
```

`Block` is one of (from `history-data.md §7`):

```
{ "type": "text",       "text": str, "truncated": bool }
{ "type": "thinking",   "text": str, "truncated": bool }   # redacted → text: "[redacted]"
{ "type": "tool_use",   "id": str, "name": str, "input": str, "truncated": bool }  # input JSON-pretty-printed server-side
{ "type": "tool_result","tool_use_id": str, "text": str, "is_error": bool, "truncated": bool }
{ "type": "image" }                                        # placeholder only
```

Blocks cut by §3 additionally carry `"full_len": int` — the original character
count, which the UI's truncation note displays.

System messages become a single text block from the line's `content`, with `subtype`
set so the UI can badge it.

## 3. Truncation

Server-side, per block: text longer than **10,000 chars** is cut there with
`truncated: true`; the UI appends a "… truncated (N chars total)" note — no
load-more per block in v1. This plus `limit` (default 200) bounds the payload
(`architecture.md §6`).

## 4. Rendering rules

| Kind / block | Rendering |
|---|---|
| user message | right-accented bubble, plain text with preserved newlines; markdown NOT rendered (user text is usually literal) |
| assistant `text` | markdown via the minimal renderer (§5) |
| assistant `thinking` | collapsed `<details>` ("Thinking…"), muted italic text |
| `tool_use` | collapsed card: ⚙ tool name header, pretty-printed input JSON in a `<pre>` |
| `tool_result` | collapsed card paired under its tool_use (matched by `tool_use_id` within the same page; unmatched results render standalone); red accent + "error" badge when `is_error` |
| `image` | placeholder chip ("image — not shown") |
| system message | full-width muted divider row, badged with `subtype` (`compact_boundary` → "Context compacted", `api_error` → red "API error", `local_command` → "Command") |
| sidechain message | when shown, indented with a "subagent" badge |

Every message shows a timestamp (HH:MM) in the gutter; assistant messages also show
the short model name.

## 5. Minimal markdown renderer

Hand-rolled in `script.js` — no external library (self-contained rule,
`architecture.md §1`). Supported subset: fenced code blocks, inline code, headings,
bold/italic, links (rendered as text + href, `rel="noopener"`), unordered/ordered
lists, paragraphs. Everything is HTML-escaped **before** markup substitution —
transcript content is untrusted input and must never inject HTML. Unsupported
markdown degrades to plain text, never breaks layout.

## 6. Paging

Pager shows `offset+1–min(offset+limit, total) of total` with prev/next buttons that
set the `offset` param (stringified). Default page is the **first** page (oldest
messages first, file order). Page size fixed at 200 in v1.

## Non-goals

- Line classification, block schema, sidechain semantics — `history-data.md`.
- Virtualized scrolling, streaming/incremental load, per-block expand-on-demand,
  syntax highlighting inside code fences — dropped for v1.
- Diff rendering for Edit/Write tool calls (upstream has per-tool cards) — all tools
  render as the generic `tool_use` card in v1.

## See also

- `history-data.md` — the schema this surface normalizes.
- `browsing.md` — how a session gets selected.
- `architecture.md` — pagination budget and param wiring.
