---
name: verify-examples
description: >-
  Verify that a Fused Render example works the way a user experiences it before
  considering it done. Use whenever you add, edit, or review an example folder
  under geospatial/ or local-tools/ in this repo — especially before opening a PR
  to main. Enforces the repo policy that no example may hard-error on open.
---

# Verifying Fused Render examples

Each example is cloned into a user's install straight from **`main`** (the Learn
showcase cards open `fused-render://open?git=…/tree/main/<category>/<name>`). So
"it works on my branch" is **not** the bar. The bar is: **a first-time user who
downloads this from `main` and opens it sees a working view — never an error.**

There is a harness for exactly this: `tests/check.py` (read `tests/README.md`).
It runs three layers per example — **structure**, **entrypoints** (via the app's
`/api/run` bridge), **visual** (headless Chrome render). Use it; don't eyeball.

## The policy this skill enforces

1. **Every example passes `check.py` cold.** No structural problems, no load
   errors, no blank/errored page.
2. **No example hard-errors on open.** A Python exception that reaches the view
   paints the app's full-page red traceback / error toast — the single worst
   first-run experience. If an example can fail to fetch (missing API key, dead
   endpoint, empty result), it must **degrade gracefully**: render something real
   and show an inline, actionable message. Never re-throw into the app overlay.
3. **Key-gated examples still render with no key.** If an example needs an API
   key, it must ship a **bundled demo** (a committed `data/<city>.json`-style
   snapshot) for its default state so it opens instantly with zero setup, and
   show an inline "add a free key" prompt only for the paths that truly need live
   data. See `geospatial/store_site_selection/` as the reference implementation.

## Definition of done for an example change

The example renders real content **both with and without an API key**, and
`check.py` is green for it. Concretely:

```bash
# 1. Iterate: cold-test just the example you touched (working tree).
uv run tests/check.py <name>

# 2. Prove the keyless first-open path — the investor scenario. The harness
#    copies a local gitignored .env if present, which HIDES this path. Either:
#      a) commit your change to a branch and test a clean worktree of it
#         (a worktree has no gitignored .env):
uv run tests/check.py --ref HEAD <name>
#      b) or temporarily move the example's .env aside and re-run step 1.

# 3. Pre-ship gate: once merged/pushed, confirm main is clean end-to-end.
uv run tests/check.py --ref origin/main
```

Prerequisites: the **FusedRender app must be running** (the harness auto-detects
its bridge port) and **Google Chrome** installed for the visual layer.

## Reading the result

- `structure/entrypoint/visual: ✓` and `→ PASS` — good.
- `entrypoint …: –` — deferred, **not** a failure (input-dependent; the visual
  layer drives it). See `tests/README.md`.
- `visual: ✗ error text on page: ['Traceback …']` — **the exact bug this skill
  exists to prevent.** The view rendered a Python error. Fix the example to
  degrade gracefully (policy #2/#3); do not just add the API key locally and
  call it fixed — a user won't have your key.
- Screenshots land in `tests/artifacts/<name>.png`. **Open the screenshot** and
  confirm the view actually painted (a map/chart/table), not just that no error
  string was found.

## When adding a NEW example

Run the full cold suite (`uv run tests/check.py`) so the new folder is picked up,
then open its screenshot in `tests/artifacts/` (gitignored, generated per run) and
confirm the view actually painted. If it needs a key, implement the bundled-demo +
inline-prompt pattern from `store_site_selection` before you ship it.
