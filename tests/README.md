# Testing the examples

`check.py` verifies that every example actually works when someone downloads it
and opens it in a fresh Fused Render install. It runs three layers per project:

| Layer | Needs | What it proves |
|---|---|---|
| **Structure** | nothing | The folder ships clean: one top-level view `.html`, a `.py` with a module-level `main()`, valid PEP 723 header, no import-time `__file__` without a guard, no committed `.env` / `.cache` / `__pycache__`. |
| **Entrypoints** | the running app | Every `runPython(...)` target the view calls is invoked through the app's `/api/run` bridge and must load and run. |
| **Visual** | the app + Chrome | The view is loaded in headless Chrome exactly as a user would open it, given time to render, then checked for real painted content and no Python error. A screenshot lands in `tests/artifacts/`. |

The key idea: each project is copied to a **fresh temp dir containing only its
git-tracked files** (the exact set a user downloads) with **no warm `.cache`** —
so anything that only works on the author's machine (absolute paths, a warm
cache hiding a cold-start timeout, a missing dependency, a leaked secret) fails
here the same way it would for a user.

## Run it

```bash
# all projects, cold (the download-and-open path), in parallel
uv run tests/check.py

# one project
uv run tests/check.py zonal_stats_hex

# reuse the local warm .cache (faster iteration)
uv run tests/check.py --warm

# skip the browser layer (structure + entrypoints only)
uv run tests/check.py --no-visual
```

Prerequisites: the **FusedRender app running** (its bridge port is auto-detected)
and **Google Chrome** installed. The harness itself runs under `uv` and pulls its
own dependencies.

## What is and isn't a failure

A project **fails** only on a structural problem or a genuine load error
(`ImportError`, `SyntaxError`, `NameError`, …) or a blank / errored page in the
visual layer.

An entrypoint line marked `–` is **not** a failure — it's an entrypoint the
smoke layer can't drive on its own because it's input-dependent, so it's deferred
to the visual layer (which drives it with real inputs):

- **needs params the view supplies** — e.g. a data-loader that takes a
  `data_dir` / `action` the page passes in.
- **cold synchronous path > 30 s** — a warm-daemon project whose synchronous
  path intentionally exceeds the runtime's 30 s limit cold; the view polls the
  project's warm step instead.
- **requires an API key** — a project with a `.env.example`; provide a local
  (gitignored) `.env` and the harness copies it in to exercise the real data
  path, otherwise this entrypoint is skipped.

## Note on the template zip-import API

Fused Render also has a zip **template** import API
(`POST /api/templates/import`), but that path is for *file-type viewers* (which
must contain a `template.html` and bind to a file pattern). These examples are
**workspace projects** — a freely-named view + UDFs you open directly — so the
faithful "import as a new file" test is the fresh-copy-and-open flow above, not
the template importer.
