# Marketplaces

> **Status — SHIPPED (v1).** This file owns the **marketplace management feature**: the
> source model, the editable-vs-read-only rule, and the marketplace actions. Implementing
> modules: `marketplaces.py` (`main(action="list"|"add"|"remove", …)`) and the Marketplaces
> section of `index.html`.

## 1. Source model (SHIPPED)

A user marketplace lives in `settings.json` → `extraKnownMarketplaces[name]`:

```
{ source: { source: "github", repo: "owner/repo" }   // kind "github"
        |  { source: "git",    url: "git@host:repo" } // kind "git"
  , autoUpdate?: true }
```

`plugins/known_marketplaces.json` (`config-store.md §2`) is the **resolved** superset — it
includes official marketplaces Claude Code installs automatically. Read for display only.

## 2. Editable vs read-only rule (invariant)

- **editable** = the marketplace exists in `extraKnownMarketplaces` (user-added). Only these
  can be edited or removed.
- Marketplaces present only in `known_marketplaces.json` (e.g. `claude-plugins-official`) are
  **read-only**; the UI shows them without a Remove button and `remove` rejects them.

## 3. Actions — `marketplaces.py main(action, …)`

- **`list`** → `{ marketplaces: [{ name, source, editable, autoUpdate, shareCommand }] }`.
  Union of `extraKnownMarketplaces` + `known_marketplaces.json` names, sorted; `editable` true
  only for user-added. `shareCommand` is computed here but **owned by** `sharing.md §2`.
- **`add`** — params `name`, `kind` (`"github"`|`"git"`), `value`, optional `autoUpdate`.
  `value` is `owner/repo` for github or a git url for git. Missing `name`/`value` → error. Adds
  to `extraKnownMarketplaces`, `write_json`, `commit("Add marketplace <name>")` — under
  `config_lock()`.
- **`remove`** — param `name`; removes from `extraKnownMarketplaces`; if the name isn't
  user-added → `{ok:false, error:"not a user-added marketplace"}` (enforces §2).
  `commit("Remove marketplace <name>")`.
- **CLI add (best-effort, optional).** Where the app also triggers Claude Code's own
  marketplace registration, it delegates via `claude_cli("plugin", "marketplace", "add",
  <source>)` (`lib.py`, argv array). Like plugin update (`plugins.md §5`), this runs under a
  sub-30 s internal timeout inside the one `runPython` call (`architecture.md §2`) and is
  **best-effort** — it may time out on a slow clone; the settings write above is the durable
  record either way.

## 4. Add ≠ sync

Adding a marketplace records the source in settings; it does not clone or fetch it into
`plugins/marketplaces/`. Claude Code performs the clone/sync on its next run (or the best-effort
CLI add above attempts it now, §3).

## Non-goals

- Enabling plugins from a marketplace — `plugins.md`.
- Cloning/updating marketplace contents on disk — Claude Code owns `plugins/marketplaces/`.

## Open questions

- **Sync action** — a first-class button to trigger Claude Code's marketplace clone/update is
  bounded by the 30 s `runPython` cap (§3); the settings write is what v1 guarantees.

## See also

- `plugins.md` — plugins carry an `@<marketplace>` suffix matching these names.
- `version-control.md §3` — add/remove each commit.
- `sharing.md §2` — the `shareCommand` on each marketplace is built from this source model.
