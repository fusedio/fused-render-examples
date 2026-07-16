# Architecture

> **Status — SHIPPED (v2).** This file owns the **system shape** in the fused-render
> runtime: the single-page HTML shell, the `runPython` bridge and its `main()` contract,
> params-as-state (URL sync), the client code layout + static validation (§7), and the
> config-applies-on-next-session contract. Implementing modules: `index.html` (markup +
> the script loader), `script.js` (all client logic), `types/fused.d.ts` + `jsconfig.json`
> (JS type-checking), `validate.py` (the check runner), `lib.py` (shared mechanics), and
> the feature `*.py` files (`preferences.py`, `plugins.py`, `marketplaces.py`, `memory.py`,
> `skills.py`, `git_ops.py`). It does NOT own any feature's `main()` action contract —
> those live in the feature specs.

## 1. Runtime model — one HTML page + local Python

There is **no server, no bundler, no build step**. The app is a folder of files rendered
by the **fused-render desktop app**, which loads `index.html` live and injects a
`window.fused` runtime into it.

| Piece | What it is | Role |
| --- | --- | --- |
| `index.html` | vanilla-ES2020 page (no framework); markup + a small script loader (§7) | Renders every control; one section per feature |
| `script.js` | the client logic, a sibling ES module (§7) | All rendering, `runPython` calls, params wiring, the modal; JSDoc-typed, `tsc`-checked |
| `lib.py` | shared stdlib-Python module | `config-store.md`, `version-control.md` mechanics; imported by every feature module |
| feature `*.py` | one module per feature | Each exposes `main(action=…, …)` dispatching that feature's read/write actions |

The page reaches Python **only** through the injected `fused.runPython`; it never spawns
processes or fetches a backend itself. Direct file IO helpers exist
(`fused.readFile/writeFile/stat/rawUrl`) but this project routes **all config mutation
through Python** so writes are atomic and auto-committed (`config-store.md §4`,
`version-control.md §3`) — the browser never writes `~/.claude` directly.

## 2. The `runPython` bridge — `main()` contract

`await fused.runPython("./feature.py", {action: "get", …})` runs the `main(**params)`
function of `feature.py` in a **fresh Python subprocess** and resolves with its JSON return
value. The rules this project depends on:

- **Fresh subprocess per call.** No module state survives between calls; each call re-imports
  `lib`. There is **no long-lived process to bootstrap once** — the consequence for git is §5.
- **30 s timeout.** Every call is capped at 30 s; a call that would exceed it is killed and
  surfaced as an error. This bounds the delegated `claude` CLI actions (`plugins.md §5`,
  `marketplaces.md §3`), which run best-effort under a sub-30 s internal timeout.
- **Params are strings only.** URL/param values are always strings. A feature that needs
  structured input (e.g. a PATCH body of `{key: value}`) JSON-stringifies it into **one**
  param and `json.loads`-es it in `main()`; scalar action selectors (`action`, `id`, `slug`,
  `sha`) pass as plain strings.
- **JSON-native return only.** `main()` returns dict/list/str/int/float/bool/None. Every
  feature `main()` returns a JSON object (e.g. `{ok, changed}`); errors are returned as
  `{ok: false, error}` or raised (the raised traceback surfaces to the page).

Each feature module is therefore a **dispatcher**: `main(action="get"|"patch"|…, payload="")`
branches on `action` and returns the matching JSON. This replaces what a REST API would call
routes — there is no HTTP surface.

## 3. Params-as-state — the UI shell

`index.html` is a single page with **in-page tab navigation** driven by a `?tab=` param
(`fused.params`). All view state lives in URL params (strings), never only in JS variables:

- `fused.params.get("tab")` selects the visible feature section; a control writes
  `fused.params.set("tab", name)` and a single `onChange` handler re-renders.
- Refresh, bookmark, and back/forward reproduce the exact view — the canonical fused-render
  wiring pattern (params are the state; controls write params; `onChange` re-renders).
- Feature sections fetch their data by calling their module's `main(action="get")` on entry
  and after every mutating action.

## 4. Scope of a "setting"

A setting is anything a feature module can express as a file mutation under `~/.claude`. v1
covers the JSON that holds nearly all preferences: `settings.json` (preferences,
`enabledPlugins`, `extraKnownMarketplaces`) — see `config-store.md §2` — plus read-only
viewers over `projects/*/memory/` and `skills/`. File-based asset *editors* (skills, agents,
hooks) are TARGET (`overview.md` → Planned).

## 5. No persistent server — `ensure_repo()` runs per action

Because every `runPython` call is a fresh subprocess (§2), there is no server-startup hook to
bootstrap the git repo once (contrast the source project, which called `ensureRepo()` at
server boot). Instead, `ensure_repo()` (`version-control.md §1`) is **idempotent** and runs
at the **top of every git action** and on **app load** (the `preferences.py get` the shell
issues first). This is a genuine adaptation of the source's one-time-at-startup model — the
work is the same, but it is re-checked cheaply on each call rather than once per process
lifetime.

## 6. Config-applies-on-next-session contract

A feature module mutates files under `~/.claude`; it does **not** signal a running Claude Code
process. Claude Code reads config at **session start**. Therefore:

- Every write takes effect on the **next** Claude Code session, not the current one.
- The UI states this expectation in its header; no live-reload is attempted. (Unchanged from
  the source — the runtime differs, the contract does not.)

## 7. Client code layout & static validation

The client logic lives in **`script.js`**, a sibling module — not inline in `index.html`.
`index.html` is markup plus a small **script loader**. This split exists to make the JS
checkable by standard tooling (and editors, live); inline `<script>` is invisible to all of
it.

- **Loading (the fused-render catch).** A relative `<script src="./script.js">` 404s — it
  resolves against the server's `/render` route, not the file's dir. And `fused.rawUrl` /
  `fused.readFile` (unlike `fused.runPython`) do **not** resolve a relative path against the
  page's directory either — they pass it verbatim to `/api/fs/raw`, resolved against the
  server cwd. So the inline loader derives the page's own absolute directory from the render
  URL's `path` query param (`new URLSearchParams(location.search).get("path")`) and injects
  `<script src="${fused.rawUrl(dir + '/script.js')}">` — an absolute raw-bytes URL served
  with the correct JS MIME (`mimetypes.guess_type`). Its `onerror` writes a failure message
  into `#main` rather than leaving a silent blank page. The module loads async and runs after
  the DOM is ready, which the code assumes (it reads `#main` at call time).
- **Typing.** `script.js` opens with `// @ts-check`. `types/fused.d.ts` declares the injected
  `window.fused` bridge (§2) as a global — the highest-value type in the project: it turns
  `fused.*` misuse (e.g. `params.set` with a non-string, a typo'd method) into a compile
  error. `jsconfig.json` wires `checkJs` + `lib: [ES2020, DOM]` and runs **lenient**
  (`noImplicitAny`/`strictNullChecks`/`useUnknownInCatchVariables` off): the code is untyped
  vanilla DOM JS, so lenient mode keeps the bug-finding checks (undefined names, typos,
  wrong-arg-count, `fused` misuse) while silencing implicit-any/null noise that would
  otherwise drown the signal. Two typed DOM helpers (`qa`, `gid`) narrow querySelector
  results so element-property access (`.value`/`.checked`/`.dataset`) checks cleanly.
- **The runner — `validate.py`.** One stdlib command runs, for anything present on PATH:
  Python `py_compile` + `pyright`, and JS `node --check` + `tsc -p jsconfig.json`. Absent
  tools are **skipped**, not failed, so it stays portable. It exits non-zero iff a tool that
  ran found a real problem.
- **Limits.** `tsc` sees only `script.js` — never behavior, and never markup in `index.html`.
  Static validation catches defects; it does not replace running the app in fused-render
  (the §3 wiring loop is the only behavioral check).

## Non-goals

- File model and atomic-write mechanics — owned by `config-store.md`.
- Git/versioning mechanics and secret safety — owned by `version-control.md`.
- Per-feature `main()` action contracts — owned by `preferences.md`, `plugins.md`,
  `marketplaces.md`, `memory.md`, `skills.md`.

## Open questions

- **Reversal (v1 → v2) — JS location.** v1 kept all client JS inline in `index.html`. v2
  moves it to a sibling `script.js` loaded via the rawUrl bootstrap (§7) so it can be
  JSDoc-typed and `tsc`-checked. The page stays framework-free; only the code's *location*
  and *typing* changed, not its behavior. Cost recorded: the app now depends on `script.js`
  loading (guarded by the loader's `onerror`).
- **Live apply** — should the tool detect running sessions and warn, or trigger a reload?
  Deferred; §6 assumes next-session semantics.
- **Long CLI actions vs the 30 s cap** — the delegated `claude` actions (`plugins.md §5`)
  must finish inside one `runPython` call. A genuinely slow update can time out; §2 and
  `plugins.md §5` note this as a best-effort caveat, not a guarantee.

## See also

- `config-store.md` — the files this system reads and writes.
- `version-control.md` — how every mutation is committed, and why `ensure_repo()` runs per action (§5).
