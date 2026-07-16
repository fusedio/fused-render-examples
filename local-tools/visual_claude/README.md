# visual-claude

A visual settings page for **Claude Code**, rendered by Fused Render.

Toggle preferences, enable/disable plugins, manage marketplaces and MCP servers,
browse your memory and skills, edit your statusline and profiles — every change
writes to `~/.claude` and is committed to git, so you can review history and roll
back. Standard library only, no keys.

![visual-claude](../../assets/visual_claude.png)

## What it demonstrates

Fused Render as a real local **control panel**: one `index.html` + `script.js`
front end over a set of small Python modules (`main(action=…)` each), all reading
and writing local config on disk with a git safety net. A good template for any
"GUI over files on my machine" tool. It's also **spec-driven** — every feature
has an owning doc in [`specs/`](./specs).

## Run it

Copy this folder into your Fused Render install and open `index.html`. It reads
and writes `~/.claude` and uses `git` there for history/rollback, so make sure
`git` is installed. Nothing else to configure.

## Files

| File | Role |
|---|---|
| `index.html` + `script.js` | The settings UI (vanilla ES2020, URL-param state) |
| `preferences.py` | `settings.json` scalar keys, with documented defaults |
| `plugins.py` / `marketplaces.py` / `mcp.py` | Enable/disable, add/remove, configure |
| `memory.py` / `skills.py` / `profiles.py` / `statusline.py` | Per-feature read/edit |
| `git_ops.py` | Commit log, restore, working-tree drift badge |
| `lib.py` | Shared config-store mechanics |
| `settings_catalog.json` | The preferences catalog (`refresh_catalog.py` regenerates it) |
| `specs/` | The owning spec for every feature |
