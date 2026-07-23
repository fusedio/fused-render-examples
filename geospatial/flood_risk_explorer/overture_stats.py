"""runPython target: Overture -> what's underwater, per water level.

DuckDB queries Overture GeoParquet on S3 for the AOI:
  buildings -> footprint polygons + heights (for the 3D scene) + flood curves
  places    -> hospitals / schools / fire stations with their flood level
  segments  -> road-km flood curve (sampled every 40 m)

Each feature gets "the water level at which it floods" from flood_mask's
priority-flood grid, so the page updates everything client-side as the
slider moves. Time-budgeted + resumable: raw SQL results are disk-cached
per step; when the budget runs out main() returns {"ready": False, ...} and
the page polls.
"""

# /// script
# dependencies = ["numpy", "pillow", "scipy", "shapely", "duckdb"]
# ///
import hashlib
import json
import math
import os
import sys
import time

_HERE = (os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals()
         else os.path.abspath(sys.path[0]))
_CACHE = os.path.join(_HERE, ".cache")
sys.path.insert(0, _HERE)

from flood_mask import (  # noqa: E402
    LAND_MIN, OVERTURE_RELEASE, compute_grids, duck_connect, stac_files)

LEVELS = [round(i * 0.25, 2) for i in range(41)]   # 0 .. 10 m
TIME_BUDGET_S = 18.0
ROAD_SAMPLE_M = 40.0
MAX_BUILDINGS = 40000
POI_KINDS = {"hospital": "%hospital%", "school": "%school%",
             "fire_station": "%fire_station%"}


# ---------------------------------------------------------------- cache

def _ckey(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


def _cached(name, key, fn):
    path = os.path.join(_CACHE, f"{name}_{key}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    result = fn()
    os.makedirs(_CACHE, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result, fh)
    return result


def _bbox_where(xmin, ymin, xmax, ymax):
    return (f"bbox.xmin >= {xmin} AND bbox.xmax <= {xmax} "
            f"AND bbox.ymin >= {ymin} AND bbox.ymax <= {ymax}")


def sql_preview(xmin, ymin, xmax, ymax):
    """The buildings query, for display in the page."""
    return (
        "SELECT ST_AsGeoJSON(geometry) AS footprint,\n"
        "       COALESCE(height, num_floors * 3.2, 5) AS height_m\n"
        f"FROM read_parquet('s3://overturemaps-us-west-2/release/{OVERTURE_RELEASE}"
        "/theme=buildings/type=building/*')\n"
        f"WHERE {_bbox_where(round(xmin, 4), round(ymin, 4), round(xmax, 4), round(ymax, 4))}"
    )


# ---------------------------------------------------------------- flood lookup

def _flood_levels_at(lons, lats, bbox, flood_grid):
    """Vectorized lookup: point -> min flooding water level (meters)."""
    import numpy as np

    xmin, ymin, xmax, ymax = bbox
    gh, gw = flood_grid.shape
    my0 = math.log(math.tan(math.pi / 4 + math.radians(min(ymax, 85.05)) / 2))
    my1 = math.log(math.tan(math.pi / 4 + math.radians(max(ymin, -85.05)) / 2))
    lons = np.asarray(lons, dtype=np.float64)
    lats = np.asarray(lats, dtype=np.float64)
    cols = np.clip(((lons - xmin) / (xmax - xmin) * (gw - 1)).round().astype(int), 0, gw - 1)
    merc = np.log(np.tan(np.pi / 4 + np.radians(np.clip(lats, -85.05, 85.05)) / 2))
    rows = np.clip(((my0 - merc) / (my0 - my1) * (gh - 1)).round().astype(int), 0, gh - 1)
    return flood_grid[rows, cols]


def _cum_curve(flood_levels):
    import numpy as np

    fl = np.asarray(flood_levels, dtype=np.float32)
    return [int((fl <= lv).sum()) for lv in LEVELS]


# ---------------------------------------------------------------- steps (raw, cached)

def _bld_chunk(con, bbox, min_area_m2=0.0):
    """Buildings (footprint + height) for ONE sub-bbox. Geometry reads off S3
    are the slow part, so the AOI is split 4x4 and each chunk cached — the
    step survives the runner's 60 s kill by resuming chunk-by-chunk.
    `min_area_m2` drops small footprints (used by very large AOIs)."""
    files = stac_files(con, "building", *bbox)
    if not files:
        return {"bld": []}
    area_sql = ""
    if min_area_m2 > 0:
        m_lon = 111_320.0 * math.cos(math.radians((bbox[1] + bbox[3]) / 2))
        min_deg2 = min_area_m2 / (m_lon * 111_320.0)
        area_sql = f" AND (bbox.xmax-bbox.xmin)*(bbox.ymax-bbox.ymin) >= {min_deg2:.3e}"
    flist = ", ".join(f"'{f}'" for f in files)
    rows = con.execute(
        f"SELECT ST_AsGeoJSON(geometry), COALESCE(height, num_floors * 3.2, 5), "
        f"(bbox.xmin+bbox.xmax)/2, (bbox.ymin+bbox.ymax)/2 "
        f"FROM read_parquet([{flist}]) WHERE {_bbox_where(*bbox)}{area_sql} LIMIT {MAX_BUILDINGS}"
    ).fetchall()
    bld = []
    for gj, h, cx, cy in rows:
        try:
            g = json.loads(gj)
        except (TypeError, ValueError):
            continue
        outer = (g["coordinates"][0] if g["type"] == "Polygon"
                 else g["coordinates"][0][0] if g["type"] == "MultiPolygon" else None)
        if not outer or len(outer) < 4:
            continue
        bld.append({"p": [[round(x, 5), round(y, 5)] for x, y in outer],
                    "h": round(float(h or 5.0), 1),
                    "c": [round(float(cx), 6), round(float(cy), 6)]})
    return {"bld": bld}


def _sub_bboxes(bbox, n=3):
    xmin, ymin, xmax, ymax = bbox
    dx, dy = (xmax - xmin) / n, (ymax - ymin) / n
    return [[round(xmin + i * dx, 6), round(ymin + j * dy, 6),
             round(xmin + (i + 1) * dx, 6), round(ymin + (j + 1) * dy, 6)]
            for j in range(n) for i in range(n)]


def _step_pois(con, bbox):
    files = stac_files(con, "place", *bbox)
    if not files:
        return {"pois": []}
    flist = ", ".join(f"'{f}'" for f in files)
    likes = " OR ".join(
        f"lower(CAST(categories AS VARCHAR)) LIKE '{pat}'" for pat in POI_KINDS.values())
    df = con.execute(
        f"SELECT names.primary AS name, categories.primary AS category, "
        f"(bbox.xmin+bbox.xmax)/2 AS lon, (bbox.ymin+bbox.ymax)/2 AS lat "
        f"FROM read_parquet([{flist}]) "
        f"WHERE {_bbox_where(*bbox)} AND ({likes}) LIMIT 1000"
    ).df()
    kind_of = lambda cat: next((k for k, p in POI_KINDS.items()
                                if p.strip("%") in (cat or "")), "other")
    return {"pois": [
        {"name": str(r.name or "?"), "kind": kind_of(str(r.category or "")),
         "lon": round(float(r.lon), 6), "lat": round(float(r.lat), 6)}
        for r in df.itertuples()]}


def _step_roads(con, bbox):
    from shapely.geometry import shape

    files = stac_files(con, "segment", *bbox)
    if not files:
        return {"total_km": 0, "lons": [], "lats": []}
    flist = ", ".join(f"'{f}'" for f in files)
    df = con.execute(
        f"SELECT ST_AsGeoJSON(geometry) AS gj FROM read_parquet([{flist}]) "
        f"WHERE {_bbox_where(*bbox)} AND subtype = 'road' LIMIT 60000"
    ).df()
    m_per_deg = 111_320.0 * math.cos(math.radians((bbox[1] + bbox[3]) / 2))
    step_deg = ROAD_SAMPLE_M / m_per_deg
    lons, lats = [], []
    for gj in df["gj"]:
        line = shape(json.loads(gj))
        n = max(int(line.length / step_deg), 1)
        for k in range(n + 1):
            pt = line.interpolate(k / n, normalized=True)
            lons.append(round(pt.x, 5))
            lats.append(round(pt.y, 5))
    return {"total_km": round(len(lons) * ROAD_SAMPLE_M / 1000.0, 1),
            "lons": lons, "lats": lats}


# ---------------------------------------------------------------- entrypoint

def main(xmin: float, ymin: float, xmax: float, ymax: float,
         barriers: str = "", min_area_m2: float = 0.0, seed_side: str = "",
         bank_min: float = 0.0):
    import numpy as np

    t0 = time.time()
    bbox = [round(xmin, 4), round(ymin, 4), round(xmax, 4), round(ymax, 4)]
    key = _ckey(bbox, OVERTURE_RELEASE, min_area_m2)
    elev_grid, flood_grid, _ = compute_grids(
        *bbox, barriers=barriers, seed_side=seed_side,
        bank_min=bank_min)                               # cached by flood_mask

    con = None
    out = {"ready": True, "bbox": bbox, "levels": LEVELS, "timings": {}}
    work = ([(f"bldq{i}", "buildings", lambda c, sb=sb: _bld_chunk(c, sb, min_area_m2))
             for i, sb in enumerate(_sub_bboxes(bbox, n=4))]
            + [("pois", "pois", lambda c: _step_pois(c, bbox)),
               ("roads", "roads", lambda c: _step_roads(c, bbox))])
    results = {}
    for i, (name, label, fn) in enumerate(work):
        path = os.path.join(_CACHE, f"{name}_{key}.json")
        if not os.path.exists(path) and time.time() - t0 > TIME_BUDGET_S:
            out.update(ready=False, done=i, total=len(work), next_step=label)
            if con:
                con.close()
            return out
        if not os.path.exists(path) and con is None:
            con = duck_connect()
        ts = time.time()
        results[name] = _cached(name, key, lambda: fn(con))
        ms = round((time.time() - ts) * 1000)
        out["timings"][label] = out["timings"].get(label, 0) + ms
    if con:
        con.close()

    # per-feature flood levels + curves against the CURRENT grid (cheap)
    allb = [x for i in range(16) for x in results[f"bldq{i}"]["bld"]]
    b = {"total": len(allb), "bld": allb}
    p, rd = results["pois"], results["roads"]
    if b["bld"]:
        lons = [x["c"][0] for x in b["bld"]]
        lats = [x["c"][1] for x in b["bld"]]
        # buildings are on land by definition: even when the nearest grid
        # cell is water (Venice canals are wider than a cell), a building
        # only counts as flooded at a positive rise
        fl = np.maximum(_flood_levels_at(lons, lats, bbox, flood_grid), LAND_MIN)
        gr = _flood_levels_at(lons, lats, bbox, elev_grid)   # same lookup, elev grid
        for i, x in enumerate(b["bld"]):
            x["fl"] = round(float(fl[i]), 2)
            x["g"] = round(max(float(gr[i]), 0.0), 1)
        out["buildings"] = {"total": b["total"], "curve": _cum_curve(fl),
                            "bld": [{k: x[k] for k in ("p", "h", "fl", "g")} for x in b["bld"]]}
    else:
        out["buildings"] = {"total": 0, "curve": [0] * len(LEVELS), "bld": []}

    pl = np.maximum(_flood_levels_at(
        [q["lon"] for q in p["pois"]], [q["lat"] for q in p["pois"]],
        bbox, flood_grid), LAND_MIN) if p["pois"] else []
    out["pois"] = {"pois": [
        {**q, "flood_level": round(float(pl[i]), 2)} for i, q in enumerate(p["pois"])]}

    rl = np.maximum(np.asarray(
        _flood_levels_at(rd["lons"], rd["lats"], bbox, flood_grid)
        if rd["lons"] else [], dtype=np.float32), LAND_MIN)
    km = ROAD_SAMPLE_M / 1000.0
    out["roads"] = {"total_km": rd["total_km"],
                    "curve_km": [round(float((rl <= lv).sum()) * km, 1) for lv in LEVELS]}

    out["sql"] = sql_preview(*bbox)
    out["ms_total"] = round((time.time() - t0) * 1000)
    return out
