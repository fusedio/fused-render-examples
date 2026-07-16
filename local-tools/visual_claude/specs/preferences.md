# Preferences

> **Status — SHIPPED (v1).** This file owns the **preferences feature**: the **settings
> catalog** (which scalar `settings.json` keys are surfaced, their control type, allowed
> options, documented default, and doc) and the `preferences.py` actions. The catalog is a
> **checked-in JSON** (`settings_catalog.json`) — a curated UI overlay merged with a docs
> snapshot, committed as one list — with an optional **manual** refresh (§5). Implementing
> modules: `settings_catalog.json` (the descriptor list), `preferences.py`
> (`main(action="get"|"patch", payload)`), `refresh_catalog.py` (the doc/default
> refresh, §5 — `main()` fused-callable + `__main__` CLI), `lib.py` (`read_settings`, `get_path`/`set_path`/`delete_path`, `write_json`,
> `commit`, `config_lock`), and the Preferences section of `index.html`. Shared mechanics are
> pointed to, not restated.

## 1. Settings catalog (pinned schema)

The surfaced keys are a **catalog of descriptors** — a **JSON list** in `settings_catalog.json`,
the single source of truth for what the Preferences form renders; the `get` action and the UI
both derive from it. Each element is one descriptor with **exactly these fields**:

| Field | Required | Source | Meaning |
| --- | --- | --- | --- |
| `key` | yes | overlay | `settings.json` key; **dotted** for nested (e.g. `permissions.defaultMode`). Matched to a docs row via `docKey ?? key`. |
| `label` | yes | curated overlay | Human label shown in the form. |
| `group` | yes | curated overlay | Section heading the control renders under. |
| `control` | yes | curated overlay | `"select"` \| `"toggle"` \| `"number"` \| `"text"`. |
| `options` | select only | curated overlay | Allowed values for `select` (presentational — the `patch` action does not validate against it). |
| `docKey` | optional | curated overlay | Alias when the docs row name differs from the `settings.json` key (e.g. `permissions.defaultMode` is documented as `defaultMode`). |
| `unsetLabel` | optional | curated overlay | Override for how the unset state reads (e.g. `enableArtifact` → "managed / org default"); defaults to "Claude default" (§4). |
| `doc` | yes | snapshot, else overlay fallback | One-line description, from the setting's docs row (§5). |
| `default` | yes | snapshot, else overlay fallback | Value Claude Code uses **when the key is unset**, or `null` ("Claude default") — see the honesty rule. |
| `minVersion` | yes | docs snapshot | Minimum Claude Code version when the docs annotate one; else `null`. |

**Why one merged JSON.** The docs settings reference (§5) is the authoritative, Anthropic-
maintained list of keys, descriptions, and defaults — but it does **not** encode UI
presentation (control type, grouping, label, options) or *which* keys are worth surfacing. So
the catalog is a **merge**: a curated **overlay** picks the scalar, user-writable keys and
assigns each its `label`/`group`/`control`/`options`; a **docs snapshot** supplies
`doc`/`default`/`minVersion`. In the source project these were two files merged at build time
(a TS overlay + a generated JSON snapshot joined by `scripts/build-catalog.ts`). Here the merge
result is **pre-computed and checked in as the single `settings_catalog.json`** — no build step
runs at load, so the page is deterministic and offline. `refresh_catalog.py` (§5) re-derives the
`doc`/`default` half on demand.

**`default` honesty rule.** A `default` is taken **only** from the setting's documented default
in prose (the docs write it `**Default**: X`). The docs table's rightmost column is an *example
value*, not the default (e.g. `enableArtifact` and `agentPushNotifEnabled` both show `true`
there while their real defaults differ), so it is **never** used for `default`. Where no prose
default is documented, `default` is `null` and the UI shows "Claude default" (§4).

**Catalog contents (v1 curated overlay).** Grouped scalar settings only (defaults come from the
snapshot, §5):

| Group | Keys |
| --- | --- |
| Model & reasoning | `model`, `effortLevel`, `alwaysThinkingEnabled`, `showThinkingSummaries` |
| Appearance & editor | `theme`, `editorMode`, `outputStyle`, `defaultView`, `prefersReducedMotion` |
| Display | `spinnerTipsEnabled`, `showTurnDuration`, `terminalProgressBarEnabled` |
| Permissions | `permissions.defaultMode`, `enableAllProjectMcpServers` |
| Notifications | `preferredNotifChannel`, `inputNeededNotifEnabled`, `agentPushNotifEnabled` |
| Session behavior | `autoCompactEnabled`, `autoMemoryEnabled`, `fileCheckpointingEnabled`, `awaySummaryEnabled`, `todoFeatureEnabled` |
| Features | `enableArtifact` (org-managed tri-state — see Open questions) |
| Maintenance | `cleanupPeriodDays`, `skipWorkflowUsageWarning` |

Non-scalar settings (`hooks`, `permissions.allow/ask/deny`, `mcpServers`, `env`,
`enabledPlugins`, `extraKnownMarketplaces`) are **not** in the catalog — see Non-goals.

## 2. Actions — `preferences.py main(action, payload)`

- **`get`** → `{ schema, prefs }`. `schema` is the parsed `settings_catalog.json` list
  **verbatim** (§1). `prefs` maps **each catalog key** to its **current dotted-path value**
  (§3) in `settings.json`, or **`null`** when the key is absent — `null` signals "using the
  default" to the UI. `get` also runs `ensure_repo()` (`version-control.md §1`) so the repo
  exists before the first `patch` (app load, `architecture.md §5`).
- **`patch`** → `{ ok, changed }`. `payload` is a **JSON-stringified** object `{ key: value }`
  (params are strings, `architecture.md §2`); `preferences.py` `json.loads`-es it. A key is
  applied only if present in the catalog; **unknown keys are rejected** (`{ok:false, error}`),
  not silently ignored. **A value of `null` resets the key** (deletes the path, §3) rather than
  writing a literal null. Under `config_lock()` (`config-store.md §4`): `read_settings` →
  mutate → `write_json` (`config-store.md §4`) → `commit("Update preferences: <keys>")`
  (`version-control.md §3`).

The `patch` action does **not** validate values against `options` (any string is accepted and
written); `options` is presentational. Tightening is an open question below.

## 3. Dotted-path handling

Any catalog `key` may be dotted (`a.b.c`). `get_path`/`set_path`/`delete_path` (`lib.py`) walk
the path generically:

- **read** (`get_path`) returns the value at the path, or `null` if any segment is missing.
- **write** (`set_path`) creates intermediate objects as needed and merges — sibling keys under
  a parent (e.g. other `permissions.*` fields) are preserved.
- **reset** (`delete_path`, PATCH value `null`) deletes the leaf key; it does not prune
  now-empty parents.

Any future nested pref works with no code change beyond a `settings_catalog.json` entry.

## 4. UI behavior

The Preferences section (`index.html`) renders the catalog **grouped by `group`**, one control
per descriptor derived from `control`/`options`. Each control:

- Shows the **set value** when present, otherwise renders an "unset" state surfacing the
  descriptor's `default` (placeholder "Claude default" / the `unsetLabel` override / "default:
  `<value>`").
- **Unset must never masquerade as a set value.** A `select`/`number`/`text` control shows this
  naturally (empty field + placeholder, or a "— Claude default —" option). A **`toggle` cannot**:
  a plain checkbox is binary, so an unset toggle drawn in its off position is indistinguishable
  from an explicit `false` (the original bug — every Claude default read as `false`). So an unset
  toggle renders a distinct **third state** (`indeterminate` checkbox → muted/dashed slider, knob
  centered) rather than on or off, alongside the same unset label. It does **not** pre-fill from
  the default's truthiness; the label carries the default. The first click on it commits an
  explicit value (`patch`), leaving the indeterminate state.
- Offers a **Reset** affordance on any set key that PATCHes `{ key: null }` to fall back to the
  default (§2).
- Saves **per-control on change** (no global save button); each change is one `patch` = one
  commit. A toast confirms `Saved <key>` / `Reset <key>` or shows the error.

The section header carries a **"Refresh catalog" button** (§5) — the only non-per-key control —
that re-derives the `doc`/`default`/`minVersion` half of the catalog from the docs and re-renders.

## 5. Catalog derivation — manual refresh (`refresh_catalog.py`)

The `doc`/`default`/`minVersion` half of the catalog (§1) is derived from Anthropic's settings
reference so the page stays correct as Claude Code adds settings and changes defaults, without
hand-editing.

- **Source of truth.** `https://code.claude.com/docs/en/settings.md` — the settings reference
  served as raw markdown (a `key | description | example` table under *Available settings*,
  version-annotated with `{/* min-version: X */}`).
- **Manual refresh, not a build step.** There is no bundler to hook, and each `runPython` call
  is a fresh subprocess with a 30 s cap (`architecture.md §2`) — so refresh is an **explicit,
  user-triggered action** (`refresh_catalog.py`), never an automatic runtime fetch. It fetches
  the `.md`, parses the *Available settings* table into `{key, doc, default, minVersion}` per
  documented key, re-merges with the curated overlay, and rewrites `settings_catalog.json`.
  `default` is parsed from the `**Default**: X` prose only (§1 honesty rule); `minVersion` from
  the `{/* min-version: X */}` comment. This replaces the source's build-time
  `scripts/build-catalog.ts` + separate generated-snapshot file.
- **Two ways to trigger, one code path.** `refresh_catalog.py` exposes `main()` as a
  fused-callable action returning a JSON result `{ok, updated, total, undocumented[], error?}`
  (never `sys.exit`/`print` for control flow — those crash or get lost under `runPython`); a
  `__main__` wrapper reuses the same result for CLI use (`python3 refresh_catalog.py`). The
  Preferences section (§4) offers a **"Refresh catalog" button** that calls this action, toasts
  the outcome (`updated/total`, or the error), and re-renders on success. It is still a
  user-triggered action, not page-load work. The network fetch uses a **sub-30 s timeout** to
  stay inside the `runPython` cap (`architecture.md §2`); a timeout returns `{ok:false, error}`,
  not a hang.
- **Committed catalog.** `settings_catalog.json` is checked into the repo: the page is
  deterministic and offline-safe, and a refresh surfaces as a reviewable git diff ("Anthropic
  added setting X" / "default changed"). (This is a project source file; it is **not** part of
  the `~/.claude` repo that `version-control.md` owns.)
- **No silent truncation.** A fetch/parse failure leaves the existing `settings_catalog.json`
  untouched and reports loudly; keys the parser cannot interpret are listed, not dropped. Merge
  mismatches — an overlay key with no docs row and no fallback (error), a documented key absent
  from the overlay (info) — are reported so new upstream settings get noticed.

## Non-goals

- Writing/atomicity — `config-store.md §4`. Committing — `version-control.md §3`.
- `enabledPlugins` / `extraKnownMarketplaces` (also in `settings.json`) — owned by
  `plugins.md`, `marketplaces.md`.
- **Array/object settings** — `hooks`, `permissions.allow/ask/deny`, `mcpServers`, `env` need
  structured editors, not scalar form controls. TARGET (`overview.md` → Planned).
- **`statusLine`** — a nested `{type, command}` object, surfaced by its own read-only viewer
  (`statusline.md`), not as a scalar form control here.

## Open questions

- **Value validation** — should `patch` reject values outside `options`, or stay permissive for
  forward-compat with new Claude Code values? Currently permissive.
- **Artifacts tri-state (RESOLVED).** `enableArtifact`'s effective value follows account/org
  availability and a managed `disableArtifact` can override it — there is **no static default
  derivable from disk**. `default` is therefore `null` and the UI renders the unset state as
  **"managed / org default"** (`unsetLabel`, §4) rather than asserting on/off.
- **Thinking mode.** The `/config` "Thinking mode" toggle writes session state
  (`thinkingEnabled`), distinct from the persistent `alwaysThinkingEnabled` key we catalog. We
  surface only the persistent key.
- **`~/.claude.json` preferences.** `/config` also stores a few genuine prefs in
  `~/.claude.json` (`autoUpdates`, `showSpinnerTree`, `defaultToAgentsView`, `deepLinkTerminal`).
  That file is intentionally unmanaged (`config-store.md §6`) because it also holds
  identity/secrets — surfacing them is deferred.
- **Superseded — build-time TS catalog.** The source generated the catalog from a TS overlay +
  a fetched JSON snapshot merged by `scripts/build-catalog.ts` at build time. With no JS build
  here, the merge result is checked in as one `settings_catalog.json` and refreshed manually via
  `refresh_catalog.py` (§5).

## See also

- `config-store.md §5` — read-modify-write preserves unmanaged keys.
- `plugins.md`, `marketplaces.md` — the other two consumers of `settings.json`.
