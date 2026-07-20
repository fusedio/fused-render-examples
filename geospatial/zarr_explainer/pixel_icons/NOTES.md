# Pixel icon library — Zarr explainer

Pixelated / dithered icon set matching the explainer's paper-editorial design
system. Standalone — nothing here touches `explainer.html`.

## Files

| File | What |
|---|---|
| `icons.js` | Dependency-free library. Attaches `window.PixelIcons`. |
| `preview.html` | Gallery ("Pixel icon lab — Zarr explainer"): every icon at 48/96/192 px, hover-dim cards, paper/thermal palette toggle. |

## How to embed

```html
<script src="pixel_icons/icons.js"></script>
<canvas id="ic"></canvas>
<script>
  PixelIcons.draw('zarr', document.getElementById('ic'), { size: 96 });
  // palette: 'paper' (default) | 'thermal' | {gold:'#...', ...} overrides
</script>
```

- `PixelIcons.names` — list of all 21 icon names.
- `PixelIcons.palettes` — the two built-in palettes (same tokens as the
  explainer: ink `#28231a`, gold `#b8860b`, land `#e8dcc0`, ocean `#d7e4ea`,
  fetch `#1f8fb8`, cache `#2f9e57`, …). Pass a partial object to override
  any role.
- dpr-aware: the canvas backing store is `size × devicePixelRatio`; CSS size
  is set for you. Safe to re-`draw()` on the same canvas (palette swaps,
  hover states).

## How it's built (design decisions)

- **Small logical grids, hard scale-up.** Each icon is painted on a 16, 24 or
  32 px offscreen grid, then blitted with `imageSmoothingEnabled = false`.
  That's what keeps the big-pixel look identical at every size — same
  technique as the explainer's world map.
- **Ordered dither for tone.** All shading is a 4×4 Bayer matrix (thresholded
  checker), never alpha or gradients. Density levels 2–9 out of 16 give the
  paper-print texture.
- **Dither stays inside silhouettes.** Face shading on the iso cube is applied
  per column of the face, not as a bounding rect — bounding-rect dither
  sprays speckles outside the shape (learned the hard way).
- **One accent per icon.** Gold marks "the thing you touch" (fetched chunk,
  shard index, receipt total); fetch-blue marks bytes that travel; cache-green
  marks the cached tile. Matches the explainer's state colors.
- **Globe uses the real landmask.** `LAND32` in icons.js is the
  `mock_store/land_mask.json` 180×90 mask downsampled to 32×16 at build time
  (lat 58°S–86°N, ≥35% majority filter) — same continents as the explainer map.
- **Act badges are aliases.** `act_*` reuse base glyphs through a palette
  proxy (land→gold tones), so they stay in sync with the base drawing.

## Icon → explainer step map

| Icon | Use in the explainer |
|---|---|
| `globe` / `act_world` | Step 0 hero, step 2 world grid, act-IV badge |
| `cube` / `act_data` | Step 0/7 datacube ("what you asked" vs "what traveled"), act-I badge |
| `download`, `clock_wait` | Step 1 brute-force download |
| `chunk`, `chunks_grid` | Steps 3–4 chunking / chunk positions |
| `folder`, `file_json` | Step 5 store-is-a-folder file tree (`zarr.json` nodes) |
| `bolt_fast` | Step 6 range-request fetch |
| `shard` | Step 8 sharding |
| `receipt` | Step 9 data receipt |
| `zarr`, `cog`, `netcdf`, `parquet` | Format comparison cards |
| `link_out` | "Learn more" affordances |
| `bulb` / `act_idea` | The core idea, act-II badge |
| `act_files` | Act-III badge |

## Preview page routing note

`preview.html` loads `icons.js` relatively (works via `file://` and
`/view/<abs path>`), and when served through `/render?path=…` — which does
not resolve relative siblings — it falls back to
`/api/fs/raw?path=<dir>/icons.js` derived from the query. `/view/…/icons.js`
returns the app's HTML viewer wrapper, not raw JS; `/api/fs/raw` is the raw
route.
