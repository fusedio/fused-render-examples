# COG overview pyramid

See every resolution level inside a Cloud-Optimized GeoTIFF as real data —
dimensions, ground resolution, bytes on disk, and a rendered preview of each
level — in an interactive 3D pyramid.

![COG overview pyramid](../../assets/cog_overview_pyramid.png)

## What it demonstrates

The overview pyramid is what makes COGs fast: each level is ¼ the pixels of the
one below, so a viewer can grab a coarse level for a first paint and only fetch
detail where you zoom. This tool opens any `.tif`/`.tiff`, shows the whole
pyramid stacked in 3D with per-level byte cost and ground resolution, and a
"same ground patch" comparison so you can *see* the detail each level throws
away. If a plain GeoTIFF has no overviews, it can build them (and cogify) in
place.

Ships with a 1.1 MB sample COG (a Maxar Open Data scene of Manila, 6 levels) so
it works the moment you open it; point it at your own `.tif` via the path box.

## Run it

Copy this folder into your Fused Render install and open `index.html`. It
builds a small raster environment (rasterio / rio-cogeo / tifffile) on first run
via [`uv`](https://astral.sh/uv), so make sure `uv` is installed.

## Files

| File | Role |
|---|---|
| `overview_pyramid.py` | Reads a GeoTIFF's levels; analyze / build-overviews / cogify |
| `index.html` | 3D pyramid view, per-level stats, and a map tab |
| `tile_server.py` + `_tiff_core.py` / `tiff_reader.py` / `_raster_common.py` | Bundled tile daemon powering the "On the map" tab |
| `sample_satellite_cog.tif` | 1.1 MB demo COG (Maxar Open Data, Manila) |

## Deploying (hosted)

This page can be deployed, but a hosted artifact has **no local filesystem, no
runtime venv build, and no reachable `127.0.0.1`** — so it runs in a reduced,
read-only mode. The page detects where it runs via `fused.env` (`"local"` vs
`"hosted"`) and adapts:

- **Reads the bundled demo COG only.** Hosted, the reader resolves the shipped
  `.tif` beside the script (bundle v2 lands it at its page-relative path under the
  project root; it ships via the `fused-bundle` manifest in `index.html`), so the
  3D pyramid, per-level stats, and the same-ground comparison all work on
  `sample_satellite_cog.tif`.
- **Hidden when hosted:** the "paste your own path" box, the "On the map" tab
  (its tile daemon binds `127.0.0.1`, unreachable from a served page), and the
  build-overviews / cogify panel (those rewrite the file in place — impossible on
  a read-only artifact). The reader also refuses those actions server-side.

Two requirements to deploy it:

1. **Include the demo COG in the bundle.** It is passed to the reader as data (not
   a `runPython`/`rawUrl` literal), so the exporter won't auto-detect it — add
   `sample_satellite_cog.tif` in the Deploy modal's "Will publish" list.
2. **Bake the raster deps into the serve image.** Hosted execution ignores the
   local `uv` venv and runs on the serve container's interpreter, so that image
   must already have `rasterio` / `numpy` / `pillow` / `tifffile`. A missing dep
   surfaces as a clear `worker failed: ModuleNotFoundError: …`.
