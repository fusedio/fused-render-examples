# Plugins

> **Status — SHIPPED (v1).** This file owns the **plugin enable/disable feature**: the
> enabled-state model, the merge with the installed catalog, the plugin actions, and the
> **delegated update** action (§5). Implementing modules: `plugins.py`
> (`main(action="list"|"toggle"|"update", …)`), `lib.py` (`claude_cli`), and the Plugins
> section of `index.html`.

## 1. Enabled-state model (SHIPPED)

- The source of truth for enablement is `settings.json` → `enabledPlugins`, a map of
  `"<name>@<marketplace>"` → boolean.
- A plugin's **enabled** state = `enabledPlugins[id] ?? false`. Absent means disabled.
- **installed** ≠ **enabled**: a plugin can be installed (present in
  `plugins/installed_plugins.json`, see `config-store.md §2`) but disabled, or enabled in
  settings but not yet installed.

## 2. Listing — `main(action="list")`

Union of ids from `installed_plugins.json` and `enabledPlugins`, sorted. Each entry:

```
{ id, name, marketplace, enabled: bool, installed: bool, version?: str, gitSourced?: bool,
  shareCommand: str }
```

`id` splits on `@` into `name` and `marketplace` (default `"unknown"` if no `@`). The UI
groups the list by `marketplace` and tags entries where `installed === false`.

**Version enrichment (read-only).** For installed plugins, `plugins.py` reads the first install
record from `installed_plugins.json` (`plugins[id][0]`, `config-store.md §2`) and surfaces
`version` (a semver like `1.0.0`, or a short git hash for git-sourced plugins) and `gitSourced`
(`true` when the record has a `gitCommitSha`). The UI shows `version` as a read-only label. This
is a **reflection of current on-disk state, not managed config**: `installed_plugins.json` is
git-ignored (`version-control.md §2`), so plugin versions are never committed and a **Restore
never downgrades a plugin** (§4) — it only rolls back the enabled/disabled intent in
`settings.json`.

`shareCommand` is computed here but **owned by** `sharing.md §3` — this spec only carries it
through the listing.

## 3. Toggle — `main(action="toggle", id, enabled)`

`enabled` arrives as a string param (`"true"`/`"false"`, coerced in `main`). Sets
`enabledPlugins[id] = enabled`, `write_json` (`config-store.md §4`),
`commit("Enable|Disable plugin <id>")` (`version-control.md §3`) — all under `config_lock()`.
Returns `{ok, id, enabled, sha}`.

## 4. Toggle ≠ install (contract boundary)

Flipping `enabledPlugins` records **intent** only. It does not download, build, or install the
plugin — that is Claude Code's own plugin-install flow. Enabling a not-yet-installed plugin is
allowed (the `installed:false` tag warns the user). Fresh **install/uninstall** from
`plugins/cache/` remains out of scope; **update** of an already-installed plugin is supported by
delegation (§5).

## 5. Update — `main(action="update", id)` (delegated, best-effort)

Updates an installed plugin to its latest version by **delegating to the Claude Code CLI**,
never by reimplementing git/install logic:

- Runs `claude_cli("plugin", "update", <id>, "--scope", "user")` (`lib.py` `claude_cli` —
  `subprocess.run` with an **argv array**, so `<id>` cannot inject a shell command).
- **Binary resolution (not bare `PATH`).** A GUI-launched fused-render process inherits a minimal
  `PATH` (`/usr/bin:/bin:…`) that omits where `claude` actually lives (`~/.local/bin`,
  `/opt/homebrew/bin`, a bun/npm global bin, `~/.claude/local`, …), so a bare
  `subprocess.run(["claude", …])` `FileNotFound`s even when `claude` runs fine in the user's
  shell. `claude_cli` therefore **resolves the absolute path** — `shutil.which` against a `PATH`
  augmented with the common install dirs, then a direct probe of those dirs — and invokes the
  resolved path with that augmented `PATH` in `env` (so `claude` can in turn find its own `node`).
  Only when no binary is found anywhere does it return `{ok:false, stderr:"claude CLI not
  found …"}`.
- **Guard:** rejected unless `<id>` is present in the current plugin list (installed catalog ∪
  `enabledPlugins`) — arbitrary ids are never passed to the CLI.
- Returns `{ ok, id, stdout, stderr }`; `ok:false` with `stderr` on non-zero exit.
- **No git commit.** The update only mutates `plugins/` (catalog + cache), which is git-ignored
  (`version-control.md §2`). `settings.json` is untouched, so there is nothing to version.
- **Ownership preserved:** the Claude-Code-owned `plugins/` dir is mutated *by Claude Code
  itself*, not by us reaching into it — so `config-store.md §2`'s read-only-on-`plugins/` rule
  still holds for this app's own writes.
- **Real-install caveat:** the CLI reads its own `~/.claude`, **not** this app's `CLAUDE_DIR`.
  So the update always targets the real installation; a scratch `CLAUDE_DIR` does not sandbox it.
- **30 s-timeout caveat (fused-render).** The whole update must finish inside one `runPython`
  call (30 s cap, `architecture.md §2`). `claude_cli` runs with a sub-30 s internal timeout and
  returns `{ok:false, stderr:"… timed out"}` rather than blocking; it is **best-effort**, not
  guaranteed — a genuinely slow or interactive `claude plugin update` may not complete. This
  bounded-timeout behavior is a genuine change from the source, where a single long-lived server
  process could block indefinitely on `Bun.spawnSync`.
- **Restart required:** the new version applies on the next Claude Code session, same as every
  other change.
- **UI label = "Upgrade", not "Update".** `claude plugin marketplace update` refreshes a
  *marketplace's catalog* — a different operation. To avoid confusion the button reads
  **Upgrade**; the action name and CLI verb stay `update` to mirror `claude plugin update`.

## Non-goals

- Fresh install / uninstall of plugin code — still Claude Code's own flow; not exposed here.
- Editing marketplaces plugins come from — `marketplaces.md`.
- Enabling individual **skills** within a plugin — not a Claude Code concept; whole-plugin
  granularity only.

## Open questions

- **Reversal (§4/§5)** — v1's "installing/updating plugin code is out of scope" is partially
  reversed: **update** is supported via CLI delegation (§5). Fresh install/uninstall remain out
  of scope.
- **Interactivity/timeout** — `claude plugin update` is assumed non-interactive; the 30 s
  `runPython` cap (§5) makes a prompting or slow update fail rather than hang. A `--yes`-style
  flag is TARGET.
- **Update-all** — a single "update every plugin" action is TARGET; v1 is per-plugin (and would
  strain the 30 s cap).

## See also

- `marketplaces.md` — plugins are sourced from marketplaces; the `@<marketplace>` suffix ties them together.
- `config-store.md §2` — `installed_plugins.json` is read-only enrichment.
- `sharing.md §3` — the `shareCommand` on each plugin (marketplace-add + install).
