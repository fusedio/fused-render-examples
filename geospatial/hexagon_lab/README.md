# Hexagon lab

An interactive sandbox for understanding H3 hexagons — three tabs, one idea:
any geodata becomes the same tiny table.

![Hexagon lab](../../assets/hexagon_lab.png)

## What it demonstrates

- **🏠 buildings** — real Overture footprints for five cities. Click a hexagon
  and watch its buildings pour in and become one 20-byte row; ⇧-sweep to
  paint, ⚡ count them all. A live byte meter compares real WKB outline bytes
  against hex rows (×554 smaller for Amsterdam).
- **⛰ terrain** — the same pipeline on a raster: a real Grand Canyon
  elevation grid pours *pixels* into cells. A metric dropdown (avg / min /
  max) shows one cell answering different questions — and at res 10 the meter
  flips red: more rows than pixels, resolution too fine.
- **Δ two releases** — change detection as ONE JOIN: the same Japanese
  neighborhood in two Overture releases (4,196 → 7,356 buildings), matched
  row-by-row on the hex id. The SQL is shown on screen.

Also a study in **first-load UX**: the exact H3 grid (baked ids+boundaries,
default inlined into the page) mounts instantly with pan/zoom/hover, plays a
3 s radial reveal, then the buildings and table fade in over it.

## Run it

Copy this folder into your Fused Render install and open `index.html`.
Everything is self-contained: `data/` ships the real extracts and baked grids,
`h3_ingest.py` is the live backend (DuckDB h3 locally, python-h3 when hosted).

Deep links: `?tab=bld|ras|cmp` · `?place=` · `?cmpplace=japan|ams|assam` ·
`?metric=avg|min|max` · `?res=8..11`. Keys: `A` count all · `B`/`H`/`N` layer
toggles · `R` replay · hold `⇧` to paint.

## Deployable

This page follows the hosted contract (literal `readFile`/`runPython` paths,
files beside the page, hosted detection via `window.__FUSED_RENDER__`, and
`h3_ingest.py` reading `data/` beside the script — bundle v2 lands every bundled
file at its real page-relative path under the project root) — the Deploy button
publishes it as-is. Live copy: https://open.fused.io/mx4yiqseryjfdzl6oc3mwlnx7e

## Files

| File | Role |
|---|---|
| `index.html` | The three-tab lab (canvas rendering, no external deps) |
| `h3_ingest.py` | Live H3 backend: hexify / scene / raster_hexify / diff (+ `action=env` diagnostic) |
| `data/*.json` | Real Overture extracts (2025-04/05 releases), Grand Canyon elevation, baked per-res grids |
