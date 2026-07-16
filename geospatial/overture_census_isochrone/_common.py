"""Shared helpers for the Overture + Census isochrone dashboard port.

Ported from the Fused canvas "Dashboard_Overture_Census_Isochrone_ORS".
This module replaces the canvas plumbing that does not exist locally:

  fused.load("...")      -> plain imports from this module
  @fused.cache           -> disk_cache (JSON memo under ./.cache)
  fused.secrets["..."]   -> sibling .env file (ORS_API_KEY=...)
  common.gdf_to_hex      -> DuckDB `h3` community extension
  common.duckdb_connect  -> h3_connect()

Every function that hits the network is disk-cached, so only the first call
for a given parameter combination is slow; fused-render runs each bridge call
in a fresh subprocess, which is why the cache must live on disk.
"""

import functools
import hashlib
import json
import os
import sys
import tempfile
import time

_HERE = (os.path.dirname(os.path.abspath(__file__))
         if "__file__" in globals() else os.path.abspath(sys.path[0]))


def _is_hosted() -> bool:
    """True on the hosted serve runtime (which injects the `openfused` shim);
    locally the example runs in its own uv script-venv where it's absent. Same
    probe cog_overview_pyramid/overview_pyramid.py uses."""
    try:
        import openfused  # noqa: F401

        return True
    except ImportError:
        return False


_HOSTED = _is_hosted()

# Hosted the bundle is read-only, so ./.cache next to the script isn't writable —
# cache into a per-run temp dir instead. Cross-call it won't persist (per-call
# subprocess isolation), but each hosted call recomputes inline within the larger
# serve budget; see warm_via_daemon.
_CACHE_DIR = (
    os.path.join(tempfile.gettempdir(), "fr-overture-census-isochrone-cache")
    if _HOSTED
    else os.path.join(_HERE, ".cache")
)

# Canvas `costing` values mapped to ORS profiles (same mapping the canvas
# UDF latlng_isochrone_simplified used).
COSTING_TO_ORS = {
    "auto": "driving-car",
    "pedestrian": "foot-walking",
    "bicycle": "cycling-regular",
}
PROFILES = set(COSTING_TO_ORS.values())

# The canvas offered several Overture releases; only recent releases exist on
# the public STAC endpoint, so the port pins the newest one (see PORT_NOTES).
OVERTURE_RELEASE = "2026-06-17.0"
STAC_COLLECTIONS = f"https://stac.overturemaps.org/{OVERTURE_RELEASE}/collections.parquet"

# POI categories offered by the canvas widget (overture_poi_selector.json).
# The canvas filtered with a substring match over the whole `categories`
# struct; we reproduce that with LIKE on the struct cast to VARCHAR.
POI_CATEGORIES = {
    "restaurant": "%restaurant%",
    "cafe": "%cafe%",
    "retail": "%retail%",
    "grocery": "%grocery%",
    "pharmacy": "%pharmacy%",
    "school": "%school%",
    "bank": "%bank%",
    "hospital": "%hospital%",
    "park": "%park%",  # NB: also matches 'parking' — see main() special-case
    "gym": "%gym%",
}

ACS_YEAR = 2022  # canvas default (Median Household Income, 2022 ACS 5-yr)
_B19013_URL = (
    "https://www2.census.gov/programs-surveys/acs/summary_file/"
    f"{ACS_YEAR}/table-based-SF/data/5YRData/acsdt5y{ACS_YEAR}-b19013.dat"
)
_TIGERWEB_BG = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/"
    "TIGERweb/Tracts_Blocks/MapServer/1/query"
)
_ACS_SENTINEL = -666666666  # suppressed-data sentinel the canvas zeroed out


def disk_cache(fn):
    """Memoize a JSON-returning function to disk, keyed by its args.

    Drop-in stand-in for @fused.cache (same as the site_isochrone example).
    """

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
        os.replace(tmp, path)  # atomic — never a half-written cache file
        return result

    wrapper.cache_path = cache_path
    return wrapper


def warm_via_daemon(tag: str, target_path: str, code: str):
    """Poll-friendly warm-up for work that can outlive the 30 s bridge budget.

    Spawns a DETACHED process running `code` (which must end up writing the
    disk-cache file at target_path) and returns {"ready": False} until that
    file exists. Callers poll from the page every couple of seconds.

    Hosted there is no daemon: a detached warmer can't outlive the call and its
    cache wouldn't survive per-call isolation, so this returns ready immediately
    and the caller's data step computes the same @disk_cache work inline.
    """
    import subprocess

    if os.path.exists(target_path):
        return {"ready": True}

    if _HOSTED:
        # Skip the background warmer entirely (see docstring); the local ~30s
        # bridge budget is the only reason it exists, and the hosted budget fits
        # the cold fetch inline.
        return {"ready": True}

    os.makedirs(_CACHE_DIR, exist_ok=True)
    lock = os.path.join(_CACHE_DIR, f"warm_{tag}.pid")
    err = os.path.join(_CACHE_DIR, f"warm_{tag}.err")
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

    with open(err, "w", encoding="utf-8") as errfh:
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=errfh,
        )
    with open(lock, "w", encoding="utf-8") as fh:
        fh.write(str(proc.pid))
    return {"ready": False}


def api_key() -> str:
    """ORS key: process env first, else the sibling .env file."""
    key = os.environ.get("ORS_API_KEY")
    if key:
        return key.strip()
    env_path = os.path.join(_HERE, ".env")
    try:
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("ORS_API_KEY="):
                    return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    raise RuntimeError("ORS_API_KEY not found in env or sibling .env file")


@disk_cache
def geocode(address: str):
    """[lat, lon, display_name] via Nominatim (canvas used geopy/Nominatim)."""
    import requests

    resp = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": address, "format": "json", "limit": 1},
        headers={"User-Agent": "fused-render-overture-census-dashboard"},
        timeout=15,
    )
    time.sleep(1)  # Nominatim fair use: ~1 req/sec
    results = resp.json()
    if not results:
        raise ValueError(f"Address not found: {address}")
    r = results[0]
    return [float(r["lat"]), float(r["lon"]), str(r.get("display_name", address))]


@disk_cache
def isochrone(lat: float, lon: float, minutes: int, profile: str):
    """[geojson_geometry, area_m2] — public Valhalla, ORS fallback.

    Primary is the keyless OSM Valhalla server (the canvas's `costing` values
    are Valhalla terms anyway). ORS is kept as fallback but its free key
    started returning 403 "Access to this API has been disallowed"
    (2026-07-07), and its WAF tar-pits Python TLS fingerprints (curl gets an
    instant 403, requests/urllib hang until read-timeout).
    """
    import requests

    ors_to_costing = {v: k for k, v in COSTING_TO_ORS.items()}
    try:
        resp = requests.post(
            "https://valhalla1.openstreetmap.de/isochrone",
            json={
                "locations": [{"lat": lat, "lon": lon}],
                "costing": ors_to_costing[profile],
                "contours": [{"time": int(minutes)}],
                "polygons": True,
            },
            timeout=20,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Valhalla {resp.status_code}: {resp.text[:300]}")
        geometry = resp.json()["features"][0]["geometry"]
        return [geometry, _geodesic_area_m2(geometry)]
    except Exception as valhalla_err:
        resp = requests.post(
            f"https://api.openrouteservice.org/v2/isochrones/{profile}",
            headers={"Authorization": api_key(), "Content-Type": "application/json"},
            json={
                "locations": [[lon, lat]],  # ORS is [lon, lat]
                "range": [int(minutes) * 60],  # seconds
                "range_type": "time",
                "attributes": ["area"],
            },
            timeout=25,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Valhalla failed ({valhalla_err}); "
                f"ORS fallback {resp.status_code}: {resp.text[:300]}"
            )
        feature = resp.json()["features"][0]
        return [feature["geometry"], float(feature["properties"]["area"])]


def _geodesic_area_m2(geometry: dict) -> float:
    """Valhalla returns no area attribute; compute it from the polygon."""
    from pyproj import Geod
    from shapely.geometry import shape

    area, _ = Geod(ellps="WGS84").geometry_area_perimeter(shape(geometry))
    return abs(area)


def hex_res_for_area(area_km2: float) -> int:
    """Adaptive H3 resolution.

    The canvas hard-coded res 9 everywhere; a 30-min driving isochrone at
    res 9 is tens of thousands of cells (too heavy for one JSON bridge
    response), so the port steps down for large areas. Documented in
    PORT_NOTES.md.
    """
    if area_km2 <= 120:
        return 9
    if area_km2 <= 900:
        return 8
    return 7


def h3_connect():
    """DuckDB connection with the community h3 extension loaded.

    Stand-in for fused common.duckdb_connect(); the extension download is a
    one-time cost (DuckDB caches it in ~/.duckdb).
    """
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL h3 FROM community; LOAD h3;")
    return con


def polygon_to_cells(con, geometry: dict, res: int):
    """H3 cells (string ids) covering a GeoJSON (Multi)Polygon.

    Stand-in for fused common.gdf_to_hex (same centroid-containment
    semantics as h3's polygon_to_cells).
    """
    from shapely.geometry import shape

    geom = shape(geometry)
    polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
    cells = set()
    for p in polys:
        rows = con.execute(
            "SELECT unnest(h3_polygon_wkt_to_cells_string(?, ?))", [p.wkt, res]
        ).fetchall()
        cells.update(r[0] for r in rows)
    return sorted(cells)


@disk_cache
def overture_pois_bbox(xmin, ymin, xmax, ymax, category: str):
    """Overture Places in a bbox matching `category` (substring over the
    categories struct — faithful to the canvas's query_overture_pois).

    The slow step (S3 parquet scan via DuckDB) — disk-cached by bbox+category.
    """
    import duckdb

    if category not in POI_CATEGORIES:
        raise ValueError(f"category must be one of {sorted(POI_CATEGORIES)}")
    pattern = POI_CATEGORIES[category]
    extra = "AND lower(CAST(categories AS VARCHAR)) NOT LIKE '%parking%'" if category == "park" else ""

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("SET s3_region='us-west-2';")
    con.execute("SET http_timeout=30000;")

    files = (
        con.execute(
            f"SELECT assets.aws.alternate.s3.href h FROM '{STAC_COLLECTIONS}' "
            f"WHERE collection='place' "
            f"AND bbox.xmax >= {xmin} AND bbox.xmin <= {xmax} "
            f"AND bbox.ymax >= {ymin} AND bbox.ymin <= {ymax}"
        )
        .fetchdf()["h"]
        .dropna()
        .tolist()
    )
    if not files:
        con.close()
        return []

    flist = ", ".join(f"'{f}'" for f in files)
    df = con.execute(f"""
        SELECT names.primary AS name,
               categories.primary AS category,
               ST_Y(geometry) AS lat, ST_X(geometry) AS lon
        FROM read_parquet([{flist}])
        WHERE bbox.xmin >= {xmin} AND bbox.xmax <= {xmax}
          AND bbox.ymin >= {ymin} AND bbox.ymax <= {ymax}
          AND lower(CAST(categories AS VARCHAR)) LIKE '{pattern}' {extra}
    """).df()
    con.close()

    if df.empty:
        return []
    return [
        {
            "lat": float(r.lat),
            "lon": float(r.lon),
            "name": str(r.name or ""),
            "category": str(r.category or ""),
        }
        for r in df.itertuples()
    ]


@disk_cache
def blockgroups_bbox(xmin, ymin, xmax, ymax):
    """Census block-group geometries intersecting a bbox, via TIGERweb.

    Substitute for the canvas's s3://fused-asset/infra/census_bg_us table
    (which needs Fused auth). Returns GeoJSON features with GEOID.
    """
    import requests

    resp = requests.get(
        _TIGERWEB_BG,
        params={
            "geometry": f"{xmin},{ymin},{xmax},{ymax}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "GEOID",
            "returnGeometry": "true",
            "outSR": "4326",
            "geometryPrecision": "5",
            "f": "geojson",
        },
        timeout=28,
        headers={"User-Agent": "fused-render-overture-census-dashboard"},
    )
    resp.raise_for_status()
    data = resp.json()
    if "features" not in data:
        raise RuntimeError(f"TIGERweb error: {json.dumps(data)[:300]}")
    return [
        {"geoid": f["properties"]["GEOID"], "geometry": f["geometry"]}
        for f in data["features"]
    ]


def _b19013_raw_path() -> str:
    """Download the national B19013 (median household income) table once.

    Public census.gov file (~18 MB) — the same file the canvas's
    Census_ACS_5yr UDF read (that UDF used the s3://fused-asset mirror).
    """
    import requests

    os.makedirs(_CACHE_DIR, exist_ok=True)
    path = os.path.join(_CACHE_DIR, f"acsdt5y{ACS_YEAR}-b19013.dat")
    if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
        return path
    tmp = f"{path}.{os.getpid()}.tmp"
    with requests.get(_B19013_URL, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(1 << 20):
                fh.write(chunk)
    os.replace(tmp, path)
    return path


@disk_cache
def income_by_state(state_fips: str):
    """{12-digit block-group GEOID: median_household_income} for one state."""
    import pandas as pd

    path = _b19013_raw_path()
    df = pd.read_csv(path, delimiter="|", dtype={"GEO_ID": str})
    # Block-group rows look like 1500000US360610106021
    prefix = f"1500000US{state_fips}"
    df = df[df["GEO_ID"].str.startswith(prefix)]
    df = df[df["B19013_E001"].notna()]
    out = {}
    for r in df.itertuples():
        val = float(r.B19013_E001)
        if val == _ACS_SENTINEL or val < 0:
            continue  # canvas replaced sentinels; we drop them
        out[r.GEO_ID.split("US")[-1]] = val
    return out


def resolve_iso(address: str, travel_time_min: int, transport_mode: str):
    """Common front half of every panel: geocode + isochrone + hex res."""
    if transport_mode not in PROFILES:
        raise ValueError(f"transport_mode must be one of {sorted(PROFILES)}")
    lat, lon, label = geocode(address)
    geometry, area_m2 = isochrone(lat, lon, int(travel_time_min), transport_mode)
    area_km2 = area_m2 / 1e6
    return lat, lon, label, geometry, area_km2, hex_res_for_area(area_km2)
