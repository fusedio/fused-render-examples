# COG range-request viewer

Stream a multi-gigabyte Cloud-Optimized GeoTIFF into the browser without
downloading it — using nothing but HTTP range requests.

![COG range-request viewer](../../assets/cog_range_viewer.png)

## What it demonstrates

The core trick behind cloud-native raster: a COG's internal tiling + overviews
let a client fetch only the bytes it needs for the current view. The page decodes
tiles client-side with geotiff.js and shows exactly which byte ranges were
requested as you pan and zoom.

Every source of bytes here is a plain range-capable HTTP URL — no local daemon:

- **A public COG on S3** (the default) — a 316 MB Sentinel-2 scene, ranged
  straight from the bucket.
- **A bundled COG** (`sample_cog.tif`, shipped with this page) — read via
  `fused.rawUrl()`. Locally that resolves to `/api/fs/raw`; on a **hosted**
  (exported) page it resolves to the `_asset` route. Both honour HTTP `Range`,
  so geotiff.js streams the bundled file byte-range by byte-range in either
  place. This is what makes the viewer work hosted with no `127.0.0.1` daemon.
- **Any local file** — open with `?file=<abs path>` in the FusedRender app; it
  is ranged over `/api/fs/raw`.

## Run it

Copy this folder into your Fused Render install and open `index.html`. Deploy it
(the shell's **Deploy** button) to serve it hosted — the bundled COG rides along
and streams over `Range` from the `_asset` route.

## URL params

| Param | Effect |
|---|---|
| _(none)_ | Public Sentinel-2 COG locally; the bundled `sample_cog.tif` when hosted |
| `?url=<https URL>` | Range a COG at an arbitrary remote URL |
| `?file=<abs path>` | Range a local file over `/api/fs/raw` (FusedRender app) |
| `?block=<bytes>` | Block size the client rounds reads to (default 64 KB) |

## Files

| File | Role |
|---|---|
| `index.html` | Tile viewer that decodes COG tiles and visualizes the range reads |
| `sample_cog.tif` | A small real tiled COG, bundled so the hosted page has one to stream |
