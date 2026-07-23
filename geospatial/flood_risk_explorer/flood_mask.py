"""runPython target: terrain + flood grids for the 3D flood risk explorer.

One call per AOI returns everything the 3D scene needs:
  - elev/flood int16-dm grids (priority-flood: min water level per cell,
    reachable-from-the-edge) -> the slider thresholds these client-side
  - a Terrarium-encoded PNG + satellite JPEG of the SAME AOI, mosaicked here
    from AWS open-data tiles, for deck.gl's TerrainLayer (the page makes
    zero tile requests itself)
  - Overture water polygons (DuckDB over S3, disk-cached) rasterized onto
    the grid: Terrarium DEM over coastal water is garbage (+6 m in Biscayne
    Bay), real water polygons clamp it to 0
"""
# /// script
# dependencies = ["numpy", "pillow", "scipy", "shapely", "duckdb"]
# ///


import base64
import concurrent.futures
import heapq
import io
import json
import math
import os
import sys
import time
import urllib.request

_HERE = (os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals()
         else os.path.abspath(sys.path[0]))
_CACHE = os.path.join(_HERE, ".cache")

TILE_SOURCES = {
    "terrarium": "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png",
    "esri": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/"
            "MapServer/tile/{z}/{y}/{x}",
}
TILE_PX = 256
MAX_GRID = 480          # long side of the flood compute grid, px
MAX_SPAN_DEG = 0.5      # AOIs are curated city boxes, not continents
LAND_MIN = 0.1          # m — dry land today can't flood below +0.1 m rise;
                        # Terrarium reads sub-zero on real land near water

OVERTURE_RELEASE = "2026-06-17.0"
STAC_COLLECTIONS = f"https://stac.overturemaps.org/{OVERTURE_RELEASE}/collections.parquet"
WATER_SUBTYPES = ("ocean", "sea", "lake", "river", "lagoon", "bay", "reservoir", "canal")
# managed water is pumped/locked at its own level — it clamps the DEM and
# renders as water, but it is NOT a flood seed: a canal only rises if the
# sea actually reaches it (the Dutch don't let the sea in through canals)
MANAGED_SUBTYPES = frozenset({"canal", "reservoir"})


# ---------------------------------------------------------------- mercator

def _merc_y(lat):
    lat = max(min(lat, 85.05), -85.05)
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def _tile_of(lon, lat, z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1 - _merc_y(lat) / math.pi) / 2 * n
    return x, y


# ---------------------------------------------------------------- duckdb / overture

def duck_connect():
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("INSTALL spatial; LOAD spatial;")
    try:
        # persistent on-disk cache of S3 range reads, shared across processes.
        # Overture row groups overlap our 4x4 sub-bboxes heavily, so without
        # this every resumed chunk re-downloads the same parquet pages.
        con.execute("INSTALL cache_httpfs FROM community; LOAD cache_httpfs;")
        cache_dir = os.path.join(_CACHE, "duck_httpfs")
        os.makedirs(cache_dir, exist_ok=True)
        con.execute(f"SET cache_httpfs_cache_directory='{cache_dir}';")
    except Exception:
        pass   # hosted runtime may not allow community extensions
    con.execute("SET s3_region='us-west-2';")
    con.execute("SET http_timeout=60000;")
    return con


def stac_files(con, collection, xmin, ymin, xmax, ymax):
    rows = con.execute(
        f"SELECT assets.aws.alternate.s3.href h FROM '{STAC_COLLECTIONS}' "
        f"WHERE collection='{collection}' "
        f"AND bbox.xmax >= {xmin} AND bbox.xmin <= {xmax} "
        f"AND bbox.ymax >= {ymin} AND bbox.ymin <= {ymax}"
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def _overture_water(xmin, ymin, xmax, ymax):
    """Water polygons intersecting the AOI, clipped + simplified. Cached.

    Returns [{"p": [ring, hole, ...], "m": 0|1}, ...] in lon/lat, where
    "m"=1 marks managed water (canal/reservoir). Slow cold (~10-20 s: reads
    Overture GeoParquet geometry off S3) — hence its own disk cache entry.
    """
    key = f"{xmin:.4f}_{ymin:.4f}_{xmax:.4f}_{ymax:.4f}"
    path = os.path.join(_CACHE, f"water2_{key}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    con = duck_connect()
    files = stac_files(con, "water", xmin, ymin, xmax, ymax)
    polys = []
    if files:
        tol = (xmax - xmin) / MAX_GRID
        flist = ", ".join(f"'{f}'" for f in files)
        subs = ", ".join(f"'{s}'" for s in WATER_SUBTYPES)
        rows = con.execute(
            f"SELECT ST_AsGeoJSON(ST_SimplifyPreserveTopology(ST_Intersection(geometry, "
            f"ST_MakeEnvelope({xmin}, {ymin}, {xmax}, {ymax})), {tol})), subtype "
            f"FROM read_parquet([{flist}]) "
            f"WHERE bbox.xmax >= {xmin} AND bbox.xmin <= {xmax} "
            f"AND bbox.ymax >= {ymin} AND bbox.ymin <= {ymax} "
            f"AND subtype IN ({subs}) LIMIT 20000").fetchall()
        for gj, subtype in rows:
            if not gj:
                continue
            g = json.loads(gj)
            m = 1 if subtype in MANAGED_SUBTYPES else 0
            if g["type"] == "Polygon":
                polys.append({"p": g["coordinates"], "m": m})
            elif g["type"] == "MultiPolygon":
                polys.extend({"p": c, "m": m} for c in g["coordinates"])
    con.close()

    for w in polys:
        w["p"] = [[[round(x, 5), round(y, 5)] for x, y in ring] for ring in w["p"]]
    os.makedirs(_CACHE, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(polys, fh)
    return polys


def _rasterize_water(polys, xmin, ymin, xmax, ymax, gh, gw, managed=None):
    """Rasterize water polygons; `managed` filters on the "m" flag
    (None = all water, False = seedable only, True = managed only)."""
    import numpy as np
    from PIL import Image, ImageDraw

    my0, my1 = _merc_y(ymax), _merc_y(ymin)

    def to_px(coords):
        return [(((lon - xmin) / (xmax - xmin)) * (gw - 1),
                 ((my0 - _merc_y(lat)) / (my0 - my1)) * (gh - 1)) for lon, lat in coords]

    img = Image.new("1", (gw, gh), 0)
    draw = ImageDraw.Draw(img)
    for w in polys:
        if managed is not None and bool(w["m"]) != managed:
            continue
        for i, ring in enumerate(w["p"]):
            if len(ring) >= 3:
                draw.polygon(to_px(ring), fill=0 if i else 1)
    return np.asarray(img, dtype=bool)


# ---------------------------------------------------------------- tiles

def _fetch_tile(source, z, x, y):
    ext = "png" if source == "terrarium" else "jpg"
    path = os.path.join(_CACHE, source, f"{z}_{x}_{y}.{ext}")
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read(), 0
    url = TILE_SOURCES[source].format(z=z, x=x, y=y)
    data = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = resp.read()
            break
        except OSError:
            if attempt == 2:
                raise
            time.sleep(0.4 * (attempt + 1))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return data, len(data)


def _decode_terrarium(png_bytes):
    """Terrarium RGB -> elevation meters: (R*256 + G + B/256) - 32768."""
    import numpy as np
    from PIL import Image

    rgb = np.asarray(Image.open(io.BytesIO(png_bytes)).convert("RGB"), dtype=np.float32)
    return rgb[..., 0] * 256.0 + rgb[..., 1] + rgb[..., 2] / 256.0 - 32768.0


def _mosaic_crop(source, z, xmin, ymin, xmax, ymax):
    """Fetch + mosaic + crop tiles for a bbox. Returns (array, n_tiles, bytes).

    terrarium -> float32 meters; esri -> uint8 RGB.
    """
    import numpy as np
    from PIL import Image

    x0f, y1f = _tile_of(xmin, ymin, z)
    x1f, y0f = _tile_of(xmax, ymax, z)
    tx0, tx1, ty0, ty1 = int(x0f), int(x1f), int(y0f), int(y1f)
    n_tiles = (tx1 - tx0 + 1) * (ty1 - ty0 + 1)
    if n_tiles > 200:
        raise ValueError(f"AOI needs {n_tiles} {source} tiles — too large")

    fetched = 0
    tiles = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        futs = {pool.submit(_fetch_tile, source, z, x, y): (x, y)
                for x in range(tx0, tx1 + 1) for y in range(ty0, ty1 + 1)}
        for fut in concurrent.futures.as_completed(futs):
            data, nbytes = fut.result()
            fetched += nbytes
            if source == "terrarium":
                tiles[futs[fut]] = _decode_terrarium(data)
            else:
                tiles[futs[fut]] = np.asarray(
                    Image.open(io.BytesIO(data)).convert("RGB"), dtype=np.uint8)

    shape = ((ty1 - ty0 + 1) * TILE_PX, (tx1 - tx0 + 1) * TILE_PX)
    if source == "terrarium":
        mosaic = np.zeros(shape, dtype=np.float32)
    else:
        mosaic = np.zeros(shape + (3,), dtype=np.uint8)
    for (x, y), arr in tiles.items():
        mosaic[(y - ty0) * TILE_PX:(y - ty0 + 1) * TILE_PX,
               (x - tx0) * TILE_PX:(x - tx0 + 1) * TILE_PX] = arr

    px0, px1 = int((x0f - tx0) * TILE_PX), int((x1f - tx0) * TILE_PX)
    py0, py1 = int((y0f - ty0) * TILE_PX), int((y1f - ty0) * TILE_PX)
    crop = mosaic[max(py0, 0):max(py1, py0 + 2), max(px0, 0):max(px1, px0 + 2)]
    return crop, n_tiles, fetched


def _pick_zoom(xmin, xmax, target_px=700, zmax=14):
    span = max(xmax - xmin, 1e-6)
    z = round(math.log2(target_px * 360 / (TILE_PX * span)))
    return max(6, min(zmax, z))


def _encode_terrarium_png(elev):
    """float32 meters -> Terrarium RGB PNG bytes (lossless)."""
    import numpy as np
    from PIL import Image

    v = np.clip(elev + 32768.0, 0, 65535.9)
    r = np.floor(v / 256.0)
    g = np.floor(v - r * 256.0)
    b = np.floor((v - np.floor(v)) * 256.0)
    rgb = np.stack([r, g, b], axis=-1).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, "PNG", optimize=False)
    return buf.getvalue()


# ---------------------------------------------------------------- grids

def _burn_barriers(crop, barriers, xmin, ymin, xmax, ymax):
    """Burn storm-surge barriers into the DEM as +20 m walls.

    `barriers` = JSON [[[x,y],[x,y]], ...] line segments in lon/lat. This is
    the whole trick behind the "close the gates" toggle: edit the elevation
    grid, re-run the priority flood, and connectivity does the rest.
    """
    import numpy as np
    from PIL import Image, ImageDraw

    gh, gw = crop.shape
    my0, my1 = _merc_y(ymax), _merc_y(ymin)
    img = Image.new("1", (gw, gh), 0)
    draw = ImageDraw.Draw(img)
    for seg in json.loads(barriers):
        pts = [(((lon - xmin) / (xmax - xmin)) * (gw - 1),
                ((my0 - _merc_y(lat)) / (my0 - my1)) * (gh - 1)) for lon, lat in seg]
        draw.line(pts, fill=1, width=4)
    mask = np.asarray(img, dtype=bool)
    crop[mask] = 20.0
    return int(mask.sum())


def _seaward_water(wmask, seed_side, min_cells=50):
    """Keep only water connected to the sea edge(s) of the grid.

    `seed_side` is a string of edges the ocean touches ("w", "sw", ...).
    Water components with no path to those edges (an upstream river cut off
    by a closed barrier, an inland lake) do NOT rise with sea level. Tiny
    edge-touching fragments (a pond clipped by the AOI boundary) are ignored
    too — only bodies of at least `min_cells` count as "the sea".
    """
    import numpy as np
    from scipy import ndimage

    labels, _ = ndimage.label(wmask)
    edge = []
    if "w" in seed_side:
        edge.append(labels[:, 0])
    if "e" in seed_side:
        edge.append(labels[:, -1])
    if "n" in seed_side:
        edge.append(labels[0, :])
    if "s" in seed_side:
        edge.append(labels[-1, :])
    keep = np.unique(np.concatenate(edge)) if edge else np.array([0])
    keep = keep[keep > 0]
    sizes = np.bincount(labels.ravel())
    keep = keep[sizes[keep] >= min_cells]
    return np.isin(labels, keep)


def compute_grids(xmin, ymin, xmax, ymax, barriers="", seed_side=""):
    """Elevation + connected-flood-level grids for an AOI. Disk-cached.

    float32 meters, mercator-linear rows (top row = ymax), water-clamped.
    `barriers` optionally walls off waterways before the flood fill.
    `seed_side` restricts flood seeds to water reachable from those grid
    edges (the open sea) — this is what makes a closed barrier protective.
    """
    import hashlib

    import numpy as np

    bkey = hashlib.sha256(barriers.encode()).hexdigest()[:8] if barriers else "none"
    key = f"{xmin:.4f}_{ymin:.4f}_{xmax:.4f}_{ymax:.4f}_{bkey}_{seed_side or 'any'}"
    npz = os.path.join(_CACHE, f"grids_v7_{key}.npz")
    if os.path.exists(npz):
        d = np.load(npz)
        return d["elev"], d["flood"], json.loads(str(d["meta"]))

    t0 = time.time()
    z = _pick_zoom(xmin, xmax)
    crop, n_tiles, tile_bytes = _mosaic_crop("terrarium", z, xmin, ymin, xmax, ymax)
    ms_tiles = round((time.time() - t0) * 1000)

    h, w = crop.shape
    scale = MAX_GRID / max(h, w)
    if scale < 1:
        from PIL import Image
        gh, gw = max(int(h * scale), 8), max(int(w * scale), 8)
        crop = np.array(
            Image.fromarray(crop).resize((gw, gh), Image.BILINEAR), dtype=np.float32)

    t1 = time.time()
    wmask = seedable = None
    wpolys = _overture_water(xmin, ymin, xmax, ymax)
    if wpolys:
        wmask = _rasterize_water(wpolys, xmin, ymin, xmax, ymax, *crop.shape)
        # canals/reservoirs are held at a managed level — they clamp the DEM
        # and render as water, but the sea doesn't come IN through them
        seedable = _rasterize_water(wpolys, xmin, ymin, xmax, ymax,
                                    *crop.shape, managed=False)
        crop[wmask & (crop > 0)] = 0.0
    ms_water = round((time.time() - t1) * 1000)

    n_barrier_cells = 0
    if barriers:
        n_barrier_cells = _burn_barriers(crop, barriers, xmin, ymin, xmax, ymax)
        if seedable is not None:
            seedable = seedable & (crop < 20.0)   # walled cells are no longer seeds

    # water can only rise out of mapped waterbodies, not out of thin air at
    # the AOI edge (Max: "makes no sense to flood NOLA from the dry side").
    # With seed_side set, only SEA-CONNECTED water rises: a river behind a
    # closed storm-surge barrier is cut off and stays at its own level.
    seedmask = seedable
    if seedmask is not None and seed_side:
        seedmask = _seaward_water(seedmask, seed_side)
    t1 = time.time()
    flood = _priority_flood(crop, seeds=seedmask)
    ms_flood = round((time.time() - t1) * 1000)

    # 0 m = today's baseline: land that is dry today (not mapped water) can
    # only flood at a POSITIVE rise, however low the noisy DEM reads it.
    # Connectivity above was computed on the real values; only the reported
    # thresholds are clamped. Water cells keep fl <= 0 so existing water
    # still renders at the 0 m slider position.
    land = ~wmask if wmask is not None else np.ones(crop.shape, dtype=bool)
    flood[land] = np.maximum(flood[land], LAND_MIN)
    # NOTE: unseeded water (managed canals, a river cut off by a barrier)
    # keeps flood=inf — "the sea never gets here". The page still renders it
    # as water via the elev grid (water cells are the only ones with
    # elev <= 0 after this clamp).
    crop = crop.copy()
    crop[land] = np.maximum(crop[land], LAND_MIN)

    meta = {
        "zoom": z, "n_tiles": n_tiles, "tile_bytes": tile_bytes,
        "ms_tiles": ms_tiles, "ms_water": ms_water, "ms_flood": ms_flood,
        "n_water_polys": len(wpolys), "n_barrier_cells": n_barrier_cells,
        "grid_w": crop.shape[1], "grid_h": crop.shape[0],
    }
    os.makedirs(_CACHE, exist_ok=True)
    np.savez_compressed(npz, elev=crop, flood=flood, meta=json.dumps(meta))
    return crop, flood, meta


def _priority_flood(elev, seeds=None):
    """Min water level per cell, water entering ONLY from seed cells
    (mapped waterbodies). Falls back to grid edges when no seeds exist.

    Classic priority-flood: pop the lowest frontier cell, its neighbors flood
    at max(frontier level, their own elevation). Land with no connected path
    to a waterbody never floods (inf).
    """
    import numpy as np

    h, w = elev.shape
    flood = np.full((h, w), np.float32(np.inf))
    seen = np.zeros((h, w), dtype=bool)
    heap = []
    if seeds is not None and seeds.any():
        si, sj = np.nonzero(seeds)
        heap = [(float(elev[i, j]), int(i), int(j)) for i, j in zip(si, sj)]
    else:
        for i in range(h):
            heap.append((float(elev[i, 0]), i, 0))
            heap.append((float(elev[i, w - 1]), i, w - 1))
        for j in range(1, w - 1):
            heap.append((float(elev[0, j]), 0, j))
            heap.append((float(elev[h - 1, j]), h - 1, j))
    heapq.heapify(heap)

    while heap:
        f, i, j = heapq.heappop(heap)
        if seen[i, j]:
            continue
        seen[i, j] = True
        flood[i, j] = f
        for ni, nj in ((i - 1, j), (i + 1, j), (i, j - 1), (i, j + 1)):
            if 0 <= ni < h and 0 <= nj < w and not seen[ni, nj]:
                heapq.heappush(heap, (max(f, float(elev[ni, nj])), ni, nj))
    return flood


# ---------------------------------------------------------------- 3D assets

def _terrain_assets(xmin, ymin, xmax, ymax):
    """Terrarium PNG + satellite JPEG covering the AOI, for TerrainLayer.

    Water-clamped at native resolution so the 3D mesh is flat over bays.
    Cached as files.
    """
    import numpy as np
    from PIL import Image

    key = f"{xmin:.4f}_{ymin:.4f}_{xmax:.4f}_{ymax:.4f}"
    ppath = os.path.join(_CACHE, f"terrain2_{key}.png")
    jpath = os.path.join(_CACHE, f"texture2_{key}.jpg")
    mpath = os.path.join(_CACHE, f"assets2_{key}.json")
    if all(os.path.exists(p) for p in (ppath, jpath, mpath)):
        with open(ppath, "rb") as f1, open(jpath, "rb") as f2, open(mpath) as f3:
            return f1.read(), f2.read(), json.load(f3)

    t0 = time.time()
    z = _pick_zoom(xmin, xmax)
    dem, _, dem_bytes = _mosaic_crop("terrarium", z, xmin, ymin, xmax, ymax)
    wpolys = _overture_water(xmin, ymin, xmax, ymax)
    dem = dem.copy()
    if wpolys:
        wmask = _rasterize_water(wpolys, xmin, ymin, xmax, ymax, *dem.shape)
        # sink mapped water WELL below the 0 m water surface: coplanar mesh
        # and water columns z-fight into dark triangle artifacts at slider 0
        dem[wmask] = -0.6
    dem[dem < -0.6] = -0.6   # flatten bathymetry: the mesh is scenery, not data
    png = _encode_terrarium_png(dem)

    zs = _pick_zoom(xmin, xmax, target_px=2400, zmax=18)
    sat, n_sat, sat_bytes = _mosaic_crop("esri", zs, xmin, ymin, xmax, ymax)
    buf = io.BytesIO()
    Image.fromarray(sat).save(buf, "JPEG", quality=80)
    jpg = buf.getvalue()

    meta = {"ms_assets": round((time.time() - t0) * 1000),
            "sat_zoom": zs, "n_sat_tiles": n_sat,
            "sat_bytes": sat_bytes, "dem_bytes": dem_bytes,
            "dem_px": [dem.shape[1], dem.shape[0]],
            "sat_px": [sat.shape[1], sat.shape[0]]}
    os.makedirs(_CACHE, exist_ok=True)
    with open(ppath, "wb") as fh:
        fh.write(png)
    with open(jpath, "wb") as fh:
        fh.write(jpg)
    with open(mpath, "w") as fh:
        json.dump(meta, fh)
    return png, jpg, meta


def _b64_int16_dm(arr):
    """float32 meters -> int16 decimeters, little-endian, base64."""
    import numpy as np

    dm = np.clip(np.nan_to_num(arr, posinf=3000.0) * 10.0, -32000, 32000)
    return base64.b64encode(dm.astype("<i2").tobytes()).decode("ascii")


# ---------------------------------------------------------------- entrypoint

def main(xmin: float, ymin: float, xmax: float, ymax: float, barriers: str = "",
         seed_side: str = ""):
    if xmax - xmin > MAX_SPAN_DEG or ymax - ymin > MAX_SPAN_DEG:
        return {"error": "AOI too large"}

    t0 = time.time()
    elev, flood, meta = compute_grids(xmin, ymin, xmax, ymax, barriers, seed_side)
    png, jpg, ameta = _terrain_assets(xmin, ymin, xmax, ymax)
    return {
        "bbox": [xmin, ymin, xmax, ymax],
        "grid_w": meta["grid_w"], "grid_h": meta["grid_h"],
        "elev_b64": _b64_int16_dm(elev),
        "flood_b64": _b64_int16_dm(flood),
        "terrain_png": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
        "texture_jpg": "data:image/jpeg;base64," + base64.b64encode(jpg).decode("ascii"),
        "meta": {**meta, **ameta},
        "ms_total": round((time.time() - t0) * 1000),
    }
