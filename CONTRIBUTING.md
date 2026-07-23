# Contributing an example

Every example is a self-contained folder that someone can copy into their own
Fused Render install and open cold — no build step, no repo-relative
assumptions. The test harness (`tests/check.py`) enforces the contract below;
run it before opening a PR.

## The folder contract

Place the example under `geospatial/` or `local-tools/`:

```
example_name/
  README.md            what it is + how to run (template below)
  <view>.html          exactly ONE top-level view — the page a user opens
  <udf>.py             one or more Python files, each with a module-level main()
  .env.example         only if it needs an API key (never commit a real .env)
```

Rules the harness checks:

- **Exactly one top-level `.html`** view per example (helpers can live in
  subfolders).
- **Every `.py` the view calls defines a module-level `main(...)`** — that is
  the entry point Fused Render invokes, with the view's parameters passed as
  keyword arguments.
- **Imports live inside the function body**, and pip dependencies are declared
  in a [PEP 723](https://peps.python.org/pep-0723/) `# /// script` header.
- **No import-time `__file__`** without a guard — hosted deploys run
  entrypoints differently than local dev, so resolve paths inside `main()`.
- **No committed secrets or caches**: `.env`, `.cache/`, and `__pycache__/`
  must not be checked in. Ship an `.env.example` if a key is needed.
- **Cache slow data to disk on first run** (write under `.cache/` next to the
  script) so repeat opens are fast.

## README template

Keep the structure the other examples use:

```markdown
# Example name

One-sentence what it is.

![Example name](../../assets/example_name.png)

## What it demonstrates
## Run it
## Files            (a table mapping each file to its role)
## Deploying (hosted)   (optional — only if the page deploys cleanly)
```

Add a screenshot as `assets/<example_name>.png` and a row to the category
table in the root `README.md`.

## Testing

With the FusedRender app running (and Google Chrome installed for the visual
layer):

```
uv run tests/check.py example_name              # one project, cold
uv run tests/check.py example_name --no-visual  # skip the browser layer
uv run tests/check.py                           # everything
```

The harness copies only git-tracked files to a temp dir and runs every
`runPython` target cold — reproducing the "someone just downloaded this"
path. If it passes cold, it will work when copied into a fresh install.
