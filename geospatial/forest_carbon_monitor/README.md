# Protected forest monitor

Track deforestation inside protected areas: pick a national park, see its annual
tree-cover loss, cumulative loss, and the share of its year-2000 canopy that's
gone.

**▶ Live demo:** https://open.fused.io/qnstptjoep3pv4akfzdtsk63hi — runs in the
browser, no install. (First open cold-starts in ~40s while the compute spins up.)

![Protected forest monitor](../../assets/forest_carbon_monitor.png)

## What it demonstrates

On-the-fly zonal statistics against a live raster API: park boundaries
(OpenStreetMap protected-area relations, shipped as GeoJSON) are sent to the
Global Forest Watch Data API to compute Hansen/UMD tree-cover loss per year.
Loss and tree-cover rasters are also streamed as map tiles, recolored
client-side by year.

## Run it

Copy this folder into your Fused Render install and open `index.html`. No
keys required (GFW's public frontend key is embedded). First load fetches zonal
stats for six parks via a detached warmer; then cached.

## Files

| File | Role |
|---|---|
| `forest.py` | GFW zonal queries per boundary + warm-up daemon |
| `index.html` | MapLibre loss/cover tiles + KPIs + annual-loss chart |
| `boundaries/` | Six park boundaries (OSM, simplified GeoJSON) |

## Deploying (hosted)

This page can be deployed. Hosted there is no local filesystem and per-call
subprocess isolation, so the background warm-up daemon can't work — this is what
made the page hang at "0/6 parks" when served. `forest.py` detects the hosted
runtime (the `OPENFUSED_DEPLOYED` env var the backend injects on the compute) and
**skips the daemon**, running the per-park GFW zonal queries inline when the
catalog/detail is requested. The park-boundary polygons in `boundaries/` are read
server-side beside the script (`boundaries/<park>.json`) — bundle v2 lands every
bundled file at its real page-relative path under the project root, so the same
path works locally and hosted. Local behaviour is unchanged.

Requirements:
- **The `boundaries/` files bundle automatically** via the bundle manifest at the
  top of `index.html`:
  `<script type="application/fused-bundle">{ "include": ["boundaries/*.json"] }</script>`.
  They're read server-side by a computed path the HTML scan can't see, so the glob
  is what ships them. Add a park to `PARKS`, drop its polygon in `boundaries/`, and
  it ships — no per-file list to maintain.
- **Allow outbound HTTPS** to `data-api.globalforestwatch.org` (the GFW API key
  is a public literal baked into the source — no secret to provision). The map's
  basemap/loss tiles are fetched client-side from GFW/Carto. Confirm the per-call
  timeout accommodates the cold catalog (~12 GFW queries).
