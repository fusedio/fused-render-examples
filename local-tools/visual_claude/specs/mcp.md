# MCP Servers

> **Status — SHIPPED (v1).** This file owns the **MCP-server management feature**: listing the
> global MCP servers Claude Code knows about (with per-server health + auth status),
> **authenticating** them (OAuth login/logout), and **add/remove**. Every operation is
> **delegated to the `claude mcp` CLI** — this app never reads or writes `~/.claude.json`
> directly (`config-store.md §6`); the CLI owns that file, exactly as `plugins.md §5` reaches
> `plugins/` only through `claude plugin`. Implementing modules: `mcp.py`
> (`main(action="list"|"login"|"logout"|"remove"|"add")`), `lib.py` (`claude_cli`,
> `claude_cli_detached`, `parse_mcp_list`), and the MCP section of `index.html` (`renderMcp`).

## 1. Why delegate instead of edit the file (invariant)

Global MCP servers live in `~/.claude.json` → `mcpServers`, which `config-store.md §6` declares
**intentionally unmanaged** — it sits outside the git repo alongside identity/secrets. Rather than
reverse that, this feature **never touches the file**: it shells out to the `claude mcp` subcommands
(`list`, `login`, `logout`, `remove`, `add-json`), so:

- the file's writer stays the CLI (no atomic-write/lock/commit concerns here — `config_lock()`
  and git commits are for `CLAUDE_DIR` files, `config-store.md §4`, and MCP state is neither);
- OAuth **tokens** are written/cleared by the CLI into wherever it keeps them — never surfaced,
  copied, or version-controlled by this app;
- the auth **status** of each server (which the JSON file alone can't tell you) comes from the
  CLI's health check.

This is the same CLI-delegation contract `plugins.md §5` established, including its **binary
resolution** (`lib.py` `claude_cli` resolves `claude`'s absolute path against an augmented `PATH`,
`plugins.md §5`) and its **real-install caveat**: `claude mcp` reads the real `~/.claude.json`, not
this app's `CLAUDE_DIR`, so a scratch `CLAUDE_DIR` does **not** sandbox MCP operations.

## 2. `list` — `main(action="list")`

Runs `claude mcp list` (health-checks every server; ~6 s for ~20 servers, comfortably under the
30 s `runPython` cap, `architecture.md §2`) and parses its human-readable output. Returns
`{ok:true, servers:[…]}`, or `{ok:false, error}` when the CLI is absent or exits non-zero.

Each server entry:

```
{ name, endpoint, transport, status, kind, connected, needsAuth, canAuth, removable }
```

**Parsing (`lib.py` `parse_mcp_list`).** Each server is one line of the form
`<name>: <endpoint> [(<TRANSPORT>)] - <glyph> <status>`. The parse is deliberately tolerant of the
fact that **names contain both spaces and colons**:

- **status** — split on the **last** `" - "`; map the trailing glyph/text to a `status` enum:
  `connected` (`✔ Connected`), `needs-auth` (`! Needs authentication`), `failed`
  (`✘ Failed to connect`), `pending` (`⏸ Pending approval`), else `unknown`.
- **name / endpoint** — split the remainder on the **first** `": "` (colon-space). Names use bare
  colons without a space (`plugin:context-mode:context-mode`), so the first colon-**space** is
  always the name/endpoint boundary (`claude.ai Slack: https://…`, `mermaid: claude-mermaid`).
- **transport** — a trailing `(HTTP)`/`(SSE)` marker if present; else inferred (`http`/`sse` when
  `endpoint` is a URL, `stdio` otherwise).
- Lines before the first server (e.g. the `Checking MCP server health…` banner) and blank lines
  are skipped.

**Derived fields** (computed, not parsed):

- **kind** — `plugin` if `name` starts with `plugin:` (owned by the Plugins feature, shown
  read-only here); `connector` if it starts with `claude.ai ` (an account connector); else `user`
  (a user-scoped server like `mermaid`).
- **connected** = `status == "connected"`; **needsAuth** = `status == "needs-auth"`.
- **canAuth** = the server is OAuth-capable (endpoint is a URL / not a `stdio` command) — gates the
  Authenticate / Log out buttons.
- **removable** = `kind == "user"` — only user-scoped servers are removed from here; `plugin:`
  servers are managed by the Plugins flow and `claude.ai` connectors by the account.

**Fragility (open question).** There is no JSON/structured output from `claude mcp list`; the parse
is against a human-facing format and can drift if the CLI changes it. Documented in Open questions.

## 3. `login` — `main(action="login", name)` (detached, fire-and-forget)

Authenticating an OAuth server (`claude mcp login <name>`) **opens a browser and blocks until the
user finishes** — it cannot complete inside a 30 s `runPython` subprocess. So login is a
**detached spawn**: `lib.py` `claude_cli_detached` starts `claude mcp login <name>` in its **own
session** (`subprocess.Popen(..., start_new_session=True)`, stdio to `DEVNULL`) and returns
immediately. The detached process outlives the `runPython` subprocess, opens the browser, runs its
local OAuth callback, and writes the tokens itself.

- Returns `{ok:true, launched:true, name}` as soon as the process is spawned (not when auth
  finishes). `{ok:false, error}` only if the `claude` binary can't be resolved.
- **The UI does not wait.** After launching, the page tells the user to complete auth in the
  browser, then **Refresh** (`list`, §2) to see the status flip to `connected`.
- **Guard:** `name` must be non-empty and contain no control characters; it is passed as a single
  argv element (no shell), so it cannot inject. An unknown name simply makes the detached CLI exit
  with no visible status change on the next refresh.

## 4. `logout` / `remove` / `add` — bounded delegations

All three are non-interactive and fast, so they run through the bounded `claude_cli` (§1) and
return `{ok, name, stdout, stderr}` (`ok:false` + `stderr` on non-zero exit):

- **`logout` — `main(action="logout", name)`** → `claude mcp logout <name>`. Clears stored OAuth
  credentials. This is the "disable" of a connected OAuth connector (there is **no** native
  enable/disable flag for global servers — §6 Open questions).
- **`remove` — `main(action="remove", name)`** → `claude mcp remove <name> --scope user`. Deletes a
  user-scoped server definition. Guarded to `removable` servers (kind `user`) in the UI; the
  `--scope user` flag keeps it from touching project/plugin scopes.
- **`add` — `main(action="add", name, json)`** → `claude mcp add-json <name> <json> --scope user`,
  where `json` is a JSON-stringified server definition (`{type, command, args, env}` for stdio, or
  `{type:"http"|"sse", url, headers?}`). `main` validates that `json` parses and `name` is
  non-empty before delegating; the definition is passed as one argv element.

**No git commit, no lock** for any MCP action — the mutated state lives in `~/.claude.json`
(outside `CLAUDE_DIR`, so git-untracked, `version-control.md §2`) and the CLI serializes its own
writes. Nothing to version, nothing to lock.

## 5. UI shape (`index.html` `renderMcp`)

A dedicated **MCP** tab (nav via `?tab=mcp`, the params-as-state pattern, `architecture.md`). It
lists §2's servers grouped by `kind` (User · Connectors · Plugin), each row showing name, endpoint,
transport, and a status pill. Per-row actions follow the derived flags:

- `needsAuth && canAuth` → **Authenticate** (§3), then a hint to Refresh.
- `connected && canAuth` → **Log out** (§4).
- `removable` → **Remove** (§4).
- An **Add server** control posts `add` (§4). A **Refresh** control re-runs `list`.

`plugin:` rows are read-only (no actions) — they're surfaced for completeness and point the user at
the Plugins tab.

## Non-goals

- **Editing `~/.claude.json` directly** — forbidden (`config-store.md §6`); all writes go through
  `claude mcp`. This spec does not re-own the file model.
- **Project-scoped / `.mcp.json` servers and their approval** (`enabledMcpjsonServers` /
  `disabledMcpjsonServers`, per-project) — v1 is **global** servers only, as the user asked. TARGET.
- **Plugin-provided MCP servers** (`plugin:*`) — owned by the plugin that ships them (`plugins.md`);
  shown read-only here, never added/removed/authed from this tab.
- **The `claude` binary resolution + 30 s-cap semantics** — owned by `plugins.md §5`; this spec
  reuses `claude_cli` and points at it, it does not re-own it.

## Open questions

- **No native enable/disable for global servers (RESOLVED — map to auth + add/remove).** Claude Code
  offers only add/remove and login/logout for user-scoped servers; there is no on/off flag. The tab
  therefore expresses "enable/disable" as **login/logout** for OAuth connectors and **add/remove**
  for custom servers, rather than faking a toggle. A soft-disable-by-stashing model was considered
  and rejected as app-invented state with no CLI counterpart.
- **List-output parsing fragility (§2).** `claude mcp list` has no structured/JSON mode, so the
  parse is against a human-facing string format. If a future CLI adds `--json`, switch to it and
  drop the tolerant parser.
- **Detached-login liveness (§3).** Login relies on `start_new_session=True` keeping the browser/
  callback process alive after the `runPython` subprocess exits. Assumed to hold; revisit if
  fused-render ever reaps whole process groups. Auth genuinely completing is best-effort and
  observed only via a later `list` refresh.
- **Project-scoped MCP + `.mcp.json` approval (TARGET).** Surfacing per-project servers and their
  approve/deny state (the `enabled/disabledMcpjsonServers` arrays) is deferred.

## See also

- `plugins.md §5` — the CLI-delegation contract this feature reuses (binary resolution, argv-array
  safety, 30 s-cap best-effort, real-install caveat).
- `config-store.md §6` — why `~/.claude.json` is unmanaged and why this feature reaches it only
  through the CLI.
- `sharing.md` — the copy-able-command pattern (an alternative auth surface that was not chosen; §3
  uses detached spawn instead).
