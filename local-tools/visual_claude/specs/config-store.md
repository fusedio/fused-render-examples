# Config Store

> **Status — SHIPPED (v1).** This file owns the **`~/.claude` file model** and the
> **read/write mechanics** every feature uses. Implementing module: `lib.py`
> (`CLAUDE_DIR`, `SETTINGS_PATH`, `INSTALLED_PLUGINS_PATH`, `KNOWN_MARKETPLACES_PATH`,
> `read_json`, `read_settings`, `write_json`, `read_text`, `write_text`,
> `get_path`/`set_path`/`delete_path`/`flatten`, `config_lock`). It does NOT own git
> (`version-control.md`) or which specific keys each feature exposes (feature specs).

## 1. Base directory

`CLAUDE_DIR` resolves to `$CLAUDE_DIR` if set, else `~/.claude` (`lib.py` `CLAUDE_DIR`).
Setting the env var to a throwaway copy is the supported way to test without touching real
config — each `runPython` subprocess inherits the environment fused-render was launched with,
so `CLAUDE_DIR=/tmp/fake-claude` scopes both reads and writes.

## 2. Files (SHIPPED)

| Path | Role | Written by app? |
| --- | --- | --- |
| `settings.json` | Source of truth for preferences, `enabledPlugins`, `extraKnownMarketplaces` | **Yes** (`write_json`) |
| `plugins/installed_plugins.json` | Catalog of installed plugins (`{version, plugins: {id: [...]}}`) | No — read-only, produced by Claude Code |
| `plugins/known_marketplaces.json` | Resolved marketplaces incl. official ones | No — read-only |

The app writes **only** `settings.json`. The two `plugins/*.json` files are read to enrich
the UI (what's installed / resolved) but are never mutated — they're derived state Claude
Code owns.

**Provenance in the UI:** each tab shows a one-line caption naming the file(s) that back it
and whether they're writable, sourced from this table (and `memory.md §1` for the memory
tab). This is a display of the file model, not a second owner of it — `index.html` renders
it; this section remains the source of truth.

## 3. Read contract

- `read_json(path, fallback)` — returns `fallback` if the file is absent; **raises** on
  malformed JSON (never silently returns fallback on parse error, so corruption surfaces).
- `read_settings()` — `read_json(SETTINGS_PATH, {})`.
- `read_text(path)` — returns the file text, or `None` when absent (used for `.gitignore` and
  script bodies).

## 4. Write contract — atomic

`write_json(path, value)` (`lib.py`):

1. `os.makedirs` the parent dir.
2. Write to a sibling temp file `${path}.tmp-${pid}`.
3. `os.replace` temp over the target (atomic on same filesystem).

`write_text(path, content)` follows the same temp-then-replace pattern for non-JSON files.
This guarantees a crash mid-write can never leave a half-written `settings.json`. Output is
pretty-printed (2-space, `ensure_ascii=False`) with a trailing newline to keep git diffs clean
(`version-control.md §3`).

**Mutation lock.** Because each `runPython` call is a **fresh subprocess** (`architecture.md
§2`), two concurrent calls (e.g. a bulk toggle) could otherwise interleave a read-modify-write
(§5). `config_lock()` (`lib.py`) is an `fcntl.flock` context manager over a lock file in
`CLAUDE_DIR`; every write action wraps its read-modify-write **and** the git commit that
follows in it, so parallel subprocesses serialize instead of clobbering. This is a
fused-render-specific addition — a single long-lived server would not need it.

## 5. Merge semantics

Writes are **read-modify-write**: load current `settings.json`, mutate the targeted
key(s), write back the whole object. Unknown/unmanaged keys are preserved untouched —
the app never rewrites keys it doesn't own.

## 6. `~/.claude.json` is intentionally NOT managed (invariant)

`/config` in Claude Code writes to **two** files, and this store owns only the first:

| File | Holds | Managed here? |
| --- | --- | --- |
| `~/.claude/settings.json` | Portable preferences + `enabledPlugins`, `extraKnownMarketplaces`, `hooks`, `statusLine`, `permissions` | **Yes** |
| `~/.claude.json` (home root, **outside `CLAUDE_DIR`**) | Machine-local app state: onboarding flags, telemetry counters (`numStartups`, `pluginUsage`, `skillUsage`), feature-flag caches, `projects`, **and identity/secrets `oauthAccount` / `userID` / `machineID` / `mcpServers`** | **No — by design** |

The two files never share a key (no leakage). `~/.claude.json` is excluded from the managed
git repo because it is per-machine and holds identity/secrets — the same reason it is
structurally outside the repo (`version-control.md §2`).

**No feature reads or writes `~/.claude.json` directly.** The one feature that acts on its
contents — MCP server management (`mcp.md`) — does so **only by delegating to the `claude mcp`
CLI**, which owns the file; this app never opens it. So the no-direct-access rule stands
unbroken (the same pattern `plugins.md §5` uses for `plugins/`). If a *direct* read view is ever
reintroduced it must copy out **only** an explicit allowlist of non-sensitive keys (never
`oauthAccount`/`userID`/`machineID`/`mcpServers`, and never OAuth tokens). Editing the
genuine prefs that live there (`autoUpdates`, `showSpinnerTree`, `defaultToAgentsView`,
`deepLinkTerminal`) remains a deferred decision (`preferences.md` Open questions). Revisit this
section before changing either rule.

## Non-goals

- Committing writes to git — owned by `version-control.md` (features call `commit()` after `write_json`).
- Which `settings.json` keys are surfaced — owned by `preferences.md §1`, `plugins.md §1`, `marketplaces.md §1`.
- Writing `~/.claude.json` — forbidden (§6). MCP server management acts on it only via the
  `claude mcp` CLI, never a direct read/write (`mcp.md §1`).

## Open questions

- **Machine-state viewer.** A read-only `~/.claude.json` viewer (telemetry/onboarding counters)
  is not surfaced; it was judged low-value and none of it is version-controlled. Reintroducing a
  read view would mean the allowlist discipline §6 describes.

## See also

- `version-control.md` — every `write_json` is followed by a commit.
- `preferences.md`, `plugins.md`, `marketplaces.md` — the consumers of this store.
