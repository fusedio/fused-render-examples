# Flood Risk Explorer (3D)

What floods when the water rises? A 3D diorama of six coastal cities — real
terrain, real buildings — with a sea-level slider. Drag it and watch water
rise out of the mapped rivers and bays, flooding only the land it can
actually reach.

![Flood Risk Explorer](../../assets/flood_risk_explorer.png)

## What it demonstrates

The "attached Python kernel" pattern: everything heavy happens in Python,
once per city, and the browser gets *answers*, not raw data.

- **Grids, not results** — Python returns "the water level at which each
  cell / building floods" (a priority-flood fill seeded from Overture water
  polygons). The slider then re-thresholds those grids entirely client-side:
  zero Python calls per drag.
- **Terrain baked in Python** — Terrarium DEM tiles + Esri imagery are
  mosaicked server-side into one Terrarium PNG + JPEG for deck.gl's
  TerrainLayer. The page makes no tile requests.
- **DuckDB over Overture S3** — building footprints with heights, hospitals
  & schools, road samples, queried straight off GeoParquet on S3. The
  buildings query is chunked 4×4 and disk-cached so it survives the runner's
  60 s budget; the page polls until ready.
- **A real scenario** — the Rotterdam Delta preset has toggleable
  storm-surge barriers (the Maeslantkering): close the gates and the same
  +3 m surge drops from ~37,000 buildings underwater to ~100.

## Run it

Copy this folder into your Fused Render install and open `index.html`.
First load of a city takes ~30–60 s (Overture queries warm a local cache);
after that everything is instant.

## Files

- `index.html` — deck.gl 3D scene + explainer column (live pipeline card,
  flood curves, naive-vs-connected toggle)
- `flood_mask.py` — DEM mosaic, water rasterization, priority-flood grids,
  terrain assets
- `overture_stats.py` — chunked DuckDB queries over Overture (buildings /
  places / roads) + per-feature flood levels
