# fused-render Claude config editor — Spec Registry

> **Status — partial (v1 shipped).** This file is the capability index: every spec
> earns one bullet here ending in its owning filename. To find the owner of a
> concept, start here. Do not describe behavior in this file — point to the owner.

Spec-driven development: no feature ships without its owning spec updated first.
Specs marked **SHIPPED** reflect code already in the repo; **TARGET** marks planned
work not yet built.

## Capabilities

- **Architecture** — the fused-render runtime shape: a single `index.html` shell, the `fused.runPython` bridge (`main()` contract, fresh subprocess per call, 30 s timeout, string-only params, JSON return), params-as-state URL sync, and the config-applies-on-next-session contract (`architecture.md`).
- **Config store** — the `~/.claude` file model, which files are the source of truth, atomic read/write, and the mutation lock (`config-store.md`).
- **Version control** — git repo over `~/.claude` run via Python subprocess, whitelist `.gitignore` secret safety (including `projects/*/memory/` persistent memory), commit-per-save, log, restore, and the change-preview diff shown before restore/switch (`version-control.md`).
- **Preferences** — a catalog of scalar `settings.json` keys surfaced as form controls, each showing its documented default when unset; the catalog is a checked-in merged JSON (curated UI overlay + docs snapshot), with an optional manual refresh; reset falls back to the default (`preferences.md`).
- **Plugins** — enable/disable toggles over `enabledPlugins`, grouped by marketplace, plus a best-effort delegated update (`plugins.md`).
- **Marketplaces** — add/remove user marketplaces in `extraKnownMarketplaces`; official/resolved ones are read-only (`marketplaces.md`).
- **Memory** — viewer of Claude Code's persistent memory files under `~/.claude/projects/*/memory/`, grouped by project, with per-folder git lifecycle controls (change status, commit, clear); memory *contents* are authored by Claude Code, not edited here (`memory.md`).
- **Skills** — viewer of the user's non-plugin (local) skills under `~/.claude/skills/*/SKILL.md`, listing each skill's name + description with reveal-in-explorer; read-only, plugin-bundled skills excluded (`skills.md`).
- **Sharing** — a copy-able terminal command per marketplace/plugin/skill card that installs it on another machine (`claude plugin marketplace add`, `claude plugin install`, `bunx skills add`); has no module of its own — the three feature modules compute the strings (`sharing.md`).
- **Statusline** — a read-only viewer of `settings.json` → `statusLine`: the command, the local script it points at (description + payload fields), and a live preview against a synthetic payload (`statusline.md`).
- **Profiles** — named git branches over `~/.claude` you switch between; create/switch/delete/export-to-`.zip`/import-from-`.zip`-into-a-new-branch locally (shipped), share via a git remote later (TARGET) (`profiles.md`).
- **MCP servers** — list the global MCP servers Claude Code knows about (with health + auth status), authenticate them (OAuth login/logout), and add/remove — all by delegating to the `claude mcp` CLI, never editing `~/.claude.json` directly (`mcp.md`).

## Planned (TARGET — not yet owned by a shipped spec)

- **Local skills editor** — listing non-plugin skills is owned by `skills.md` (read-only v1). An *editor* for skill contents, and an **agents editor** for `agents/*.md`, remain TARGET (candidate: `local-assets.md`).
- **Hooks editor** — structured editing of `settings.json` → `hooks`. Candidate: `hooks.md`.

## Reading order for a newcomer

1. `architecture.md` — how the pieces fit in the fused-render runtime.
2. `config-store.md` + `version-control.md` — the shared mechanics every feature uses.
3. Any one feature spec (`preferences.md`, `plugins.md`, `marketplaces.md`).
