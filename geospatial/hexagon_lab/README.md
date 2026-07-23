# Hexagon lab

Six steps from the whole world to one join — an interactive explainer of
H3 hexagons where every step is a live sandbox, not a slide.

![Hexagon lab](../../assets/hexagon_lab.png)

## The six steps

1. **the world** — the whole Earth cut into H3 cells on a draggable globe,
   with a resolution slider that *animates* between res 0 → 1 → 2 (a
   stochastic dissolve, one level at a time). Toggle the 12 pentagons or
   color every cell by how far its area drifts from the resolution mean.
2. **neighbors** — a hexagon has 6 neighbors all at the same distance; a
   square's 8 come at two. Rings, distance and why that matters for
   convolution-style analysis.
3. **hierarchy** — every cell splits into 7 twisted children. Descend from
   res 0 down to real Amsterdam footprints, watch the ±19° rotation
   alternate (even resolutions align with even, odd with odd), or draw your
   own shape and polyfill it (overlap mode, not centroid).
4. **count buildings** — real Overture footprints for five cities. Click a
   hexagon and it becomes one 20-byte row; ⇧-sweep to paint, ⚡ count them
   all. A live byte meter compares WKB outline bytes against hex rows.
5. **raster statistics** — the same pipeline on a raster: Grand Canyon
   elevation pixels summarized per cell (avg / min / max dropdown). At res
   10 the meter flips red: more rows than pixels, resolution too fine.
6. **spot the change** — change detection as ONE JOIN: the same neighborhood
   in two Overture releases, matched row-by-row on the hex id, SQL on screen.

## Run it

Copy this folder into your Fused Render install and open `index.html`.
Everything is self-contained: `data/` ships the real extracts and baked
grids, `basics/data/` the pre-baked globe levels, `h3_ingest.py` and
`basics/hierarchy_h3.py` are the live backends (DuckDB h3 locally,
python-h3 when hosted).

Deep links: `?tab=world|nbr|hier|bld|ras|cmp` · `?place=` ·
`?cmpplace=japan|ams|assam` · `?metric=avg|min|max` · `?res=8..11`.

## Deployable

This page follows the hosted contract (literal `readFile`/`runPython`
paths, files beside the page, hosted detection via
`window.__FUSED_RENDER__`) — the Deploy button publishes it as-is.
The two biggest globe levels also ship as `.json.gz` twins: the hosted
asset endpoint serves raw bytes with no HTTP compression, so the page
fetches the gzip and inflates it with the browser's native
`DecompressionStream` (res 2: 1.15 MB → 241 KB), which is what keeps the
globe's resolution slider snappy when hosted.

**Live copy: https://open.fused.io/mx4yiqseryjfdzl6oc3mwlnx7e**

## Files

| File | Role |
|---|---|
| `index.html` | The six-step lab (canvas rendering, no external deps) |
| `h3_ingest.py` | Live H3 backend for steps 4–6: hexify / scene / raster_hexify / diff (+ `action=env` diagnostic) |
| `basics/hierarchy_h3.py` | Backend for step 3: descent scenes + overlap-mode polyfill of drawn shapes |
| `data/*.json` | Real Overture extracts (2025-04/05 releases), Grand Canyon elevation, baked per-res grids |
| `basics/data/*.json(.gz)` | Baked globe cell layers (res 0–2, gz twins for hosted), hierarchy + places data |
