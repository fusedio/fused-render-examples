# Statusline

> **Status — SHIPPED (v1).** This file owns the **statusline viewer**: reading `settings.json` →
> `statusLine`, introspecting the local command-script it points at (leading-comment description +
> which payload fields it consumes), and rendering a **live preview** by executing the command
> against a synthetic sample payload. Read-only — the command and script are never edited here.
> Implementing modules: `statusline.py` (`main(action="get"|"preview")`, plus the pure helpers
> `resolve_script`, `describe_script`, `statusline_fields`, and the `SAMPLE_PAYLOAD` /
> `FIELD_LABELS` constants) and the Statusline section of `index.html` (`renderStatusline`,
> `renderAnsi`). It does NOT own the `settings.json` file model (`config-store.md`), the whitelist
> that tracks the script (`version-control.md §2`), or scalar prefs (`preferences.md`).

## 1. What the statusline is

Claude Code renders a custom status line by running a shell command on every prompt render. The
config lives in `settings.json` → `statusLine`:

- **`statusLine.type`** — `"command"` (the only type this viewer handles).
- **`statusLine.command`** — a shell command string, e.g. `sh ~/.claude/statusline-command.sh`.
  Claude Code pipes a JSON **payload** to stdin each render and takes stdout as the status line.

## 2. Resolving the local script — `resolve_script(command)`

Find the local script a command delegates to, or `None`. Tokenize the command on whitespace;
for each token, expand a leading `~`, `~/`, `$HOME/`, or `$CLAUDE_DIR/`, then accept the **first**
token that resolves to an existing **file**. **Trust boundary:** the resolved path must stay
inside `CLAUDE_DIR` — absolute-only, `..` rejected, must be under `CLAUDE_DIR` (mirrors
`lib.safe_subdir`, `memory.md §6`). The script is only ever **read as text**, never executed as a
resolved path — execution goes through the configured command (§5), not this path.

## 3. Describing the script — `describe_script(text)`

A human description = the script's **leading comment block**. Skip an optional shebang, skip
leading blank lines, then join the contiguous run of `#` comment lines (stripping the `#`).
Empty when the script opens directly with code.

## 4. Which payload fields it shows — `statusline_fields(text)` + `FIELD_LABELS`

Best-effort introspection of what the status line displays. Drop full-line `#` comments first (so
`.g.` in "e.g." isn't read as a path), extract dotted JSON paths (`\.[A-Za-z_][\w.]*` — the usual
source is `jq` accessors), then map each recognized path to a human label via `FIELD_LABELS`
(matched by exact path **or** dotted-prefix, so `.rate_limits.five_hour.used_percentage` maps via
`.rate_limits.five_hour`). Returns `{fields, otherFields}`: recognized labels (deduped) and
unrecognized dotted paths surfaced raw so the label map can lag the payload schema.

## 5. `get` — `main(action="get")`

Returns the configured statusline and, when its command points at a readable local script (§2),
that script's introspection:

```
{ configured: bool, type: str|null, command: str|null,
  script: { path, tracked, size, modified, description, fields[], otherFields[] } | null }
```

- `configured:false` when `statusLine` is absent/not an object. `script:null` when the command
  points at no readable local script (still returns the command).
- `tracked` = whether the script is git-tracked (`git ls-files --error-unmatch`, `version-control.md
  §2` — `statusline-command.sh` is on the whitelist). `modified` is ISO. Reads under `CLAUDE_DIR`.

## 6. `preview` — `main(action="preview")`

Render a live preview by **executing the configured command** against `SAMPLE_PAYLOAD` — a fixed,
non-secret sample of Claude Code's payload (illustrative model/cwd/context/cost values; never real
session data). This is the **one place the app runs an arbitrary user command**; guarded:

- Runs `["sh", "-c", command]` with the JSON sample on **stdin**, `cwd=CLAUDE_DIR`, output capped
  (4096 chars), stdout returned **with ANSI intact**, mutates nothing.
- **Timeout.** A short internal timeout (well under fused-render's 30 s `runPython` cap,
  `architecture.md §2`) — a slow/hanging script returns `{ok:false, error:"… timed out"}` rather
  than blocking. Best-effort, not guaranteed. (This bounded timeout is a change from the source's
  long-lived-server model, which could block indefinitely.)
- Returns `{ok, output, error?}`. The UI renders `output` through `renderAnsi` (a minimal SGR
  parser: reset/bold/dim + basic foreground colors → styled spans) so the preview matches the
  terminal.

## Non-goals

- Editing the command or the script — read-only viewer only.
- The `settings.json` file model — `config-store.md`.
- Sandboxing the executed command beyond the timeout + sample-stdin bound (see Open questions).

## Open questions

- **Sandboxing the preview.** Executing a user's statusline command inside a `runPython`
  subprocess is bounded by a short timeout and fed only a synthetic payload, but still runs
  arbitrary local code with the user's privileges. Acceptable for a local single-user tool;
  a stricter sandbox is TARGET.

## See also

- `config-store.md §6` — `statusLine` lives in `settings.json`.
- `version-control.md §2` — the whitelist that tracks `statusline-command.sh` (drives `tracked`).
- `preferences.md` — scalar prefs; `statusLine` is deliberately not a scalar control there.
