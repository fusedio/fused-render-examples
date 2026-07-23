# Fused Render examples

A gallery of example projects for **[Fused Render](https://fused.io)** — browse
them, see what's possible, and copy any one into your own install to make it
your own.

Each example is a self-contained folder: a Python UDF or two plus an HTML view.
No build step. To run one, copy its folder into your Fused Render install and
open the `.html`. Every example works from public data (a few want a free API
key, noted in their README).

---

## Highlights

<table>
  <tr>
    <td width="50%"><a href="geospatial/hexagon_lab/"><img src="assets/hexagon_lab.png" alt="Hexagon lab"></a><br><b>Hexagon lab</b><br>Six steps from the whole world to one join — an interactive H3 explainer — <a href="https://open.fused.io/mx4yiqseryjfdzl6oc3mwlnx7e">live</a>.</td>
    <td width="50%"><a href="local-tools/visual_claude/"><img src="assets/visual_claude.png" alt="visual-claude"></a><br><b>visual-claude</b><br>A visual settings page for Claude Code, writing to <code>~/.claude</code>.</td>
  </tr>
  <tr>
    <td><a href="geospatial/locker_network_simulator/"><img src="assets/locker_network_simulator.png" alt="Locker network simulator"></a><br><b>Parcel locker simulator</b><br>Live route re-optimization on real roads.</td>
    <td><a href="local-tools/disk_usage/"><img src="assets/disk_usage.png" alt="disk_usage"></a><br><b>disk_usage</b><br>Treemap disk-space explorer for your local filesystem.</td>
  </tr>
</table>

---

## Geospatial

| Example | What it does |
|---|---|
| [buildings_to_hexagons](geospatial/buildings_to_hexagons/) | Interactive explainer: every building on Earth as H3 hexes |
| [hexagon_lab](geospatial/hexagon_lab/) | Six-step H3 explainer: globe → neighbors → hierarchy → buildings → rasters → release diff ([live](https://open.fused.io/mx4yiqseryjfdzl6oc3mwlnx7e)) |
| [cog_range_viewer](geospatial/cog_range_viewer/) | Stream a huge COG with HTTP range requests, no download ([live](https://open.fused.io/s2ndrn6shzzbawm36gkjhwqrhi)) |
| [zarr_explainer](geospatial/zarr_explainer/) | An interactive 11-step guide to the Zarr format, on real NASA ocean data ([live](https://open.fused.io/mjyttte444gnh4sye2bzjgd5f4)) |
| [cog_overview_pyramid](geospatial/cog_overview_pyramid/) | Every resolution level of a COG as an interactive 3D pyramid ([live](https://open.fused.io/5p5yp52aolmwqks63j7l4e7x3u)) |
| [overture_census_isochrone](geospatial/overture_census_isochrone/) | Isochrone + Overture POIs + Census income on H3 hexes |
| [zonal_stats_hex](geospatial/zonal_stats_hex/) | Who lives at what elevation — Census × DEM, joined in DuckDB |
| [store_site_selection](geospatial/store_site_selection/) | "Where should I open a cafe?" weighted tract scoring |
| [forest_carbon_monitor](geospatial/forest_carbon_monitor/) | Deforestation inside protected areas via Global Forest Watch ([live](https://open.fused.io/qnstptjoep3pv4akfzdtsk63hi)) |
| [locker_network_simulator](geospatial/locker_network_simulator/) | Parcel-locker route optimization on real road networks |
| [disaster_response_dashboard](geospatial/disaster_response_dashboard/) | Imagery + storm track + buildings fused for a disaster event |
| [gers_pixel_ref](geospatial/gers_pixel_ref/) | eopix: "a URL for building pixels" — mint/resolve GERS-anchored byte-range references ([live](https://open.fused.io/kmubo2djxutwzc3tuyf3kfvs2y)) |

## Local tools

Fused Render pointed at your **own machine** instead of the cloud.

| Example | What it does |
|---|---|
| [visual-claude](local-tools/visual_claude/) | A visual settings page for Claude Code (writes to `~/.claude`, git-versioned) |
| [pytop](local-tools/pytop/) | A live `top`-style system + process monitor |
| [disk_usage](local-tools/disk_usage/) | Treemap disk-space explorer and cleaner |
| [notion_db](local-tools/notion_db/) | Notion-style task tracker on a local Parquet lake |

---

## How an example is structured

```
example_name/
  README.md            what it is + how to run
  <udf>.py             Python UDF(s) — the data backend
  <view>.html          the browser view (calls the UDFs via fused.runPython)
  .env.example         only if it needs an API key
```

Each Python file just defines a module-level `main(...)` function — that's the
entry point Fused Render calls, with the view's parameters passed as keyword
arguments. Imports live inside the function body, and any pip dependencies are
declared in a [PEP 723](https://peps.python.org/pep-0723/) header. Data that
takes a while to fetch is cached to disk on first run.

## Notes

- These examples are for exploration and learning. Data sources are public
  (OpenStreetMap, Overture, US Census, Copernicus, Global Forest Watch, Maxar
  Open Data, NOAA); scenarios in the logistics/response demos are illustrative.
