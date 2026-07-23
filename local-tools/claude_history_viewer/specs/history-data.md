# History data

> **Status — target (v1).** This file owns the **on-disk data model**: where Claude
> Code history lives, the JSONL line schema, which line types are messages vs. noise,
> session title resolution, and token accounting. Every action in `history.py`
> implements these rules; the surfaces that expose them are `browsing.md` and
> `transcript.md`. Schema knowledge derives from the upstream
> claude-code-history-viewer's Rust parser; only the subset this tool consumes is
> specified here.

## 1. Disk layout

- **Base**: `~/.claude` (overridable via the `claude_dir` test seam,
  `architecture.md §2`).
- **Projects**: `~/.claude/projects/<slug>/` — one directory per project. `<slug>` is
  the project's cwd with `/` and `_` both flattened to `-` (lossy), e.g.
  `-Users-jane-my-repo`.
- **Sessions**: top-level `<uuid>.jsonl` files directly inside a project dir — one
  JSON object per line, append-only. Files in *subdirectories* are subagent/sidechain
  transcripts and are **never listed as sessions**.

## 2. Line schema (consumed subset)

Every line is a JSON object with a `type` field. Fields this tool reads
(camelCase, as on disk):

| Field | On lines | Meaning |
|---|---|---|
| `type` | all | dispatcher — see §3 |
| `uuid` | messages | stable line id |
| `timestamp` | messages | RFC3339 string |
| `sessionId` | messages | the session's real id |
| `isMeta` | messages | `true` → internal, always skipped |
| `isSidechain` | messages | `true` → subagent message (§6) |
| `cwd` | messages | working directory — used for project display name |
| `message.role` | user/assistant | `"user"` \| `"assistant"` |
| `message.content` | user/assistant | `string` or array of content blocks (§7) |
| `message.model` | assistant | model id |
| `message.usage` | assistant | token counts (§8) |
| `summary` | `type:"summary"` | conversation summary text |
| `customTitle` | `type:"custom-title"` | rename via `/branch` |
| `subtype`, `content` | `type:"system"` | system event kind + text |
| `toolUseResult` | user | result payload for a prior tool call |

Unknown fields and unknown block types are ignored, never fatal.

## 3. Line-type classification

- **Rendered as messages** (`transcript.md`): `user`, `assistant`, and `system` lines
  whose `subtype` is one of `local_command`, `compact_boundary`, `api_error`.
- **Metadata-only, never rendered**: `summary` (title source, §5), `custom-title`
  (title source, §5).
- **Always skipped**: `progress`, `queue-operation`, `file-history-snapshot`,
  `last-prompt`, `pr-link`, `agent-name`; `system` lines with any other/no subtype
  (e.g. `stop_hook_summary`, `turn_duration`); any line with `isMeta: true`; any
  `user`/`assistant` line lacking both `sessionId` and `timestamp`.
- **Message count** for a session = lines classified "rendered as messages" (with
  sidechains counted per §6). Sessions with 0 messages are dropped from listings.

## 4. Malformed input

Any line that fails `json.loads`, or is empty, is skipped silently. A file that
cannot be opened is reported as an error entry, not a crash
(`architecture.md §5`). Decode file bytes as UTF-8 with `errors="replace"`.

## 5. Session title resolution

First non-empty wins, in priority order:

1. **Rename** — `customTitle` from a `custom-title` line, or a `system`/
   `local_command` line whose `content` matches
   `<local-command-stdout>Session renamed to: {name}</local-command-stdout>`.
2. **Summary** — the `summary` field of the first `type:"summary"` line.
3. **First genuine user text** — the first user message whose text content is not a
   slash-command display (text starting with `<command-name>`,
   `<local-command-stdout>`, or `<command-message>` is not genuine; nor is text
   starting with `<system-reminder>` or a `Caveat:` wrapper).
4. **First assistant text**.
5. Fallback: the session filename stem.

Titles are truncated to 120 chars for display. Whether the title came from a rename
is surfaced as `is_renamed` (`browsing.md §3`).

## 6. Sidechains

`isSidechain: true` messages are subagent traffic inside the main session file. They
count toward `message_count`, but the transcript hides them by default; the
`sidechains=1` param includes them (`architecture.md §3`,
`transcript.md §2`). Sidechain-only *files* live in subdirectories and are excluded
entirely (§1).

## 7. Content blocks (consumed subset)

`message.content` is either a plain string (treated as one `text` block) or an array
of blocks dispatched by `type`:

| Block type | Fields kept |
|---|---|
| `text` | `text` |
| `thinking` | `thinking` |
| `redacted_thinking` | (none — rendered as a placeholder) |
| `tool_use` | `id`, `name`, `input` |
| `tool_result` | `tool_use_id`, `content` (string or nested text blocks, flattened to text), `is_error` |
| `image` | (no bytes kept — placeholder only) |
| anything else | dropped |

Normalization to the wire shape the UI receives is owned by `transcript.md §3`.

## 8. Token accounting

Per-session totals are summed over every assistant line's `message.usage`:
`input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
`cache_read_input_tokens` (each defaulting 0 when absent). One combined mode only —
the original's billing/conversation split is dropped. No cost computation: v1
surfaces token counts only (upstream reads a `costUSD` field; dropped here).

## 9. Metadata scan (single bounded pass)

Session listing derives everything in **one line-by-line pass per file** without
retaining message bodies: title candidates (§5), first/last message timestamp,
message count, sidechain count, token totals (§8), and `has_tool_use` (any assistant
block of type `tool_use`). Memory stays O(1) per file; no full-file
`json.loads`-and-keep. This is what keeps `action="sessions"` inside the timeout
budget (`architecture.md §6`).

## Non-goals

- Which fields each UI surface displays — `browsing.md`, `transcript.md`.
- Cost estimation, per-model pricing tables — dropped (upstream `costUSD` ignored).
- `sessions-index.json`, symlink dedup, incremental caching — upstream refinements
  not replicated.

## See also

- `browsing.md` — exposes §1/§5/§9 as the project and session lists.
- `transcript.md` — exposes §3/§6/§7 as the conversation view.
