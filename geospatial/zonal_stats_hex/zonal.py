"""Data backend for the "Who lives at what elevation?" dashboard.

Joins the 2020 US Census H3-res8 population hexes (source.coop, public) with the
Copernicus DEM 90 m pre-hexified parquet (s3://fused-asset, public anonymous)
via DuckDB (httpfs + h3 community extensions).

Both remote scans are region-scoped so a cold call fits the 30 s budget:
we cover the region bbox with H3 res-5 cells, turn those into contiguous
child-index ranges (pure bit math on the H3 index layout) so DuckDB can prune
parquet row groups with a plain `hex BETWEEN lo AND hi` predicate, then filter
exactly with `h3_cell_to_parent(hex, 5) IN (...)`.

The two fetches are disk-cached per region; resolution aggregation (H8 -> H7/H6)
is pure-python bit math on the cached base table, so switching resolution or
color mode is instant. The HTML calls step="pop" and step="elev" first (each
fits the timeout on its own), then step="view".
"""

import functools
import hashlib
import json
import os
import sys

_HERE = (os.path.dirname(os.path.abspath(__file__))
         if "__file__" in globals() else os.path.abspath(sys.path[0]))
_CACHE_DIR = os.path.join(_HERE, ".cache")

CENSUS = (
    "s3://us-west-2.opendata.source.coop/fused/hex/"
    "release_2025_04_beta/census/2020_partitioned_h8.parquet"
)
COPDEM = "s3://fused-asset/hex/copernicus-dem-90m"  # one file per res-0 cell, rows at res 10

REGIONS = {
    "nyc": {"name": "New York metro", "bbox": [-74.556, 40.400, -73.374, 41.029]},
    "sf_bay": {"name": "San Francisco Bay Area", "bbox": [-122.75, 37.15, -121.60, 38.20]},
    "miami": {"name": "Miami / South Florida", "bbox": [-80.60, 25.35, -80.05, 26.40]},
    "new_orleans": {"name": "New Orleans", "bbox": [-90.55, 29.55, -89.60, 30.25]},
    "houston": {"name": "Houston / Galveston", "bbox": [-95.85, 29.00, -94.90, 30.20]},
    "seattle": {"name": "Seattle / Puget Sound", "bbox": [-122.65, 47.05, -121.90, 47.95]},
    "denver": {"name": "Denver Front Range", "bbox": [-105.35, 39.40, -104.50, 40.10]},
    "los_angeles": {"name": "Los Angeles", "bbox": [-118.75, 33.60, -117.70, 34.35]},
}

# Elevation bands (meters). Lower edge inclusive; first band swallows negatives.
BANDS = [
    (float("-inf"), 10, "Below 10 m"),
    (10, 50, "10 – 50 m"),
    (50, 100, "50 – 100 m"),
    (100, 200, "100 – 200 m"),
    (200, 500, "200 – 500 m"),
    (500, 1000, "500 – 1,000 m"),
    (1000, 2000, "1,000 – 2,000 m"),
    (2000, float("inf"), "Above 2,000 m"),
]

FLOOD_THRESHOLD_M = 10.0


def disk_cache(fn):
    """Memoize a JSON-returning function to disk, keyed by its args."""

    def cache_path(*args, **kwargs):
        key_src = json.dumps([fn.__name__, args, kwargs], sort_keys=True, default=str)
        key = hashlib.sha256(key_src.encode()).hexdigest()[:16]
        return os.path.join(_CACHE_DIR, f"{fn.__name__}_{key}.json")

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        path = cache_path(*args, **kwargs)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        result = fn(*args, **kwargs)
        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(result, fh)
        os.replace(tmp, path)
        return result

    wrapper.cache_path = cache_path
    return wrapper


# ---------------------------------------------------------------- H3 bit math
# H3 index layout: bits 52-55 = resolution, digits for res r live at bits
# (15-r)*3 .. (15-r)*3+2; unused digits are 7.

def _h3_res(c: int) -> int:
    return (c >> 52) & 0xF


def _h3_parent(c: int, parent_res: int) -> int:
    res = _h3_res(c)
    for r in range(parent_res + 1, res + 1):
        c |= 0x7 << ((15 - r) * 3)
    return (c & ~(0xF << 52)) | (parent_res << 52)


def _child_range(c: int, child_res: int):
    """Contiguous [lo, hi] of all child_res descendants of cell c."""
    res = _h3_res(c)
    lo, hi = c, c
    for r in range(res + 1, child_res + 1):
        off = (15 - r) * 3
        lo &= ~(0x7 << off)
        hi = (hi & ~(0x7 << off)) | (6 << off)
    lo = (lo & ~(0xF << 52)) | (child_res << 52)
    hi = (hi & ~(0xF << 52)) | (child_res << 52)
    return lo, hi


# ---------------------------------------------------------------- DuckDB side

def _connect():
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL h3 FROM community; LOAD h3;")
    # path-style URLs: the source.coop bucket name contains dots, which breaks
    # TLS for virtual-host-style addressing.
    con.execute("CREATE SECRET (TYPE S3, PROVIDER config, REGION 'us-west-2', URL_STYLE 'path');")
    return con


def _region_cells5(con, bbox):
    w, s, e, n = bbox
    wkt = f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))"
    cells = [r[0] for r in con.execute(f"SELECT UNNEST(h3_polygon_wkt_to_cells('{wkt}', 5))").fetchall()]
    if not cells:
        raise ValueError(f"bbox produced no res-5 cells: {bbox}")
    return cells


@disk_cache
def fetch_pop(region: str):
    """Census population per res-8 hex within the region bbox."""
    con = _connect()
    cells5 = _region_cells5(con, REGIONS[region]["bbox"])
    ranges = [_child_range(c, 8) for c in cells5]
    lo, hi = min(r[0] for r in ranges), max(r[1] for r in ranges)
    cl = ",".join(map(str, cells5))
    rows = con.execute(
        f"""
        SELECT hex, SUM(POP20)::BIGINT AS pop
        FROM read_parquet('{CENSUS}')
        WHERE hex BETWEEN {lo} AND {hi}
          AND h3_cell_to_parent(hex, 5) IN ({cl})
        GROUP BY 1
        """
    ).fetchall()
    print(f"fetch_pop({region}): {len(rows)} hexes")
    return {str(h): int(p) for h, p in rows}


@disk_cache
def fetch_elev(region: str):
    """Mean Copernicus DEM elevation per res-8 hex within the region bbox."""
    con = _connect()
    cells5 = _region_cells5(con, REGIONS[region]["bbox"])
    parents0 = sorted({_h3_parent(c, 0) for c in cells5})
    files = [f"'{COPDEM}/{p}.parquet'" for p in parents0]
    ranges = [_child_range(c, 10) for c in cells5]
    lo, hi = min(r[0] for r in ranges), max(r[1] for r in ranges)
    cl = ",".join(map(str, cells5))
    rows = con.execute(
        f"""
        SELECT h3_cell_to_parent(hex, 8) AS h8, AVG(data_avg) AS elev
        FROM read_parquet([{",".join(files)}])
        WHERE hex BETWEEN {lo} AND {hi}
          AND h3_cell_to_parent(hex, 5) IN ({cl})
        GROUP BY 1
        """
    ).fetchall()
    print(f"fetch_elev({region}): {len(rows)} hexes from {len(files)} file(s)")
    return {str(h): round(float(e), 1) for h, e in rows}


# ------------------------------------------------------------ warm-up daemon
# A cold region fetch takes ~60-90 s over the network — well past the 30 s
# bridge budget. warm() therefore spawns a DETACHED warmer process (immune to
# the bridge timeout) that fills the disk cache, and each poll just reports
# whether the cached results exist yet. The page polls until ready.

def _warmer_paths(region: str):
    return (os.path.join(_CACHE_DIR, f"warm_{region}.pid"),
            os.path.join(_CACHE_DIR, f"warm_{region}.err"))


def _spawn_warmer(region: str):
    import subprocess

    lock, err = _warmer_paths(region)
    os.makedirs(_CACHE_DIR, exist_ok=True)
    code = (
        f"import sys; sys.path.insert(0, {_HERE!r}); import zonal; "
        f"zonal.fetch_pop({region!r}); zonal.fetch_elev({region!r})"
    )
    with open(err, "w", encoding="utf-8") as errfh:
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=errfh,
        )
    with open(lock, "w", encoding="utf-8") as fh:
        fh.write(str(proc.pid))


def warm(region: str, kind: str):
    fn = fetch_pop if kind == "pop" else fetch_elev
    if os.path.exists(fn.cache_path(region)):
        return {"ready": True}

    lock, err = _warmer_paths(region)
    if os.path.exists(lock):
        try:
            os.kill(int(open(lock, encoding="utf-8").read().strip()), 0)
            return {"ready": False}  # warmer alive, keep polling
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # stale lock: warmer died
        os.remove(lock)  # so the next call retries
        tail = ""
        if os.path.exists(err):
            tail = open(err, encoding="utf-8").read().strip()[-400:]
        if tail:
            raise RuntimeError(f"background data fetch failed: {tail}")

    _spawn_warmer(region)
    return {"ready": False}


# ---------------------------------------------------------------- aggregation

def _base_table(region: str):
    """Joined res-8 rows: [(hex_int, pop, elev)] — elevation coverage is the base."""
    pop = fetch_pop(region)
    elev = fetch_elev(region)
    rows = []
    for h, e in elev.items():
        rows.append((int(h), pop.get(h, 0), e))
    return rows


def _weighted_median(pairs):
    """Median of `value` weighted by `weight`; pairs = [(value, weight)]."""
    pairs = sorted((p for p in pairs if p[1] > 0), key=lambda p: p[0])
    total = sum(w for _, w in pairs)
    if total <= 0:
        return None
    acc = 0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return v
    return pairs[-1][0]


def main(region: str = "nyc", resolution: int = 7, step: str = "view"):
    if region not in REGIONS:
        raise ValueError(f"region must be one of {sorted(REGIONS)}, got {region!r}")

    # Warm-up steps: resumable within the 30 s bridge budget; the page polls
    # until {"ready": True}. Results are disk-cached per res-5 cell.
    if step == "pop":
        return warm(region, "pop")
    if step == "elev":
        return warm(region, "elev")
    if step != "view":
        raise ValueError(f"step must be pop | elev | view, got {step!r}")

    resolution = int(resolution)
    if resolution not in (6, 7, 8):
        raise ValueError(f"resolution must be 6, 7 or 8, got {resolution}")

    base = _base_table(region)  # res-8, cache hits after warm-up

    # ---- KPIs & bands: always computed at res 8 (full fidelity) ----
    total_pop = sum(p for _, p, _ in base)
    med_elev = _weighted_median([(e, p) for _, p, e in base])
    below = sum(p for _, p, e in base if e < FLOOD_THRESHOLD_M)
    band_pop = [0] * len(BANDS)
    for _, p, e in base:
        for i, (lo, hi, _label) in enumerate(BANDS):
            if lo <= e < hi:
                band_pop[i] += p
                break
    bands = [
        {"label": label, "lo": (None if lo == float("-inf") else lo),
         "hi": (None if hi == float("inf") else hi), "pop": band_pop[i]}
        for i, (lo, hi, label) in enumerate(BANDS)
    ]

    # ---- map hexes at requested resolution (pop-weighted elevation) ----
    if resolution == 8:
        hexes = [[format(h, "x"), p, e] for h, p, e in base]
    else:
        agg = {}
        for h, p, e in base:
            k = _h3_parent(h, resolution)
            a = agg.setdefault(k, [0, 0.0, 0.0, 0])  # pop, sum(e*p), sum(e), n
            a[0] += p
            a[1] += e * p
            a[2] += e
            a[3] += 1
        hexes = []
        for k, (p, ep, es, n) in agg.items():
            e = ep / p if p > 0 else es / n
            hexes.append([format(k, "x"), p, round(e, 1)])

    meta = REGIONS[region]
    w, s, e_, n = meta["bbox"]
    return {
        "region": region,
        "region_name": meta["name"],
        "bbox": meta["bbox"],
        "center": [(s + n) / 2, (w + e_) / 2],
        "resolution": resolution,
        "flood_threshold_m": FLOOD_THRESHOLD_M,
        "kpis": {
            "total_pop": int(total_pop),
            "median_elev_m": (None if med_elev is None else round(float(med_elev), 1)),
            "pop_below_10m": int(below),
            "pct_below_10m": (round(100.0 * below / total_pop, 2) if total_pop else 0.0),
            "hex_count": len(hexes),
        },
        "bands": bands,
        "hexes": hexes,  # [hex_id_hex_string, population, elevation_m]
        "regions": [{"id": k, "name": v["name"]} for k, v in REGIONS.items()],
    }
