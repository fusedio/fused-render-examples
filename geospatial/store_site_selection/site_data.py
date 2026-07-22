"""Data backend for the "Where should I open a cafe?" dashboard.

One call per city returns EVERYTHING the page needs, raw and unweighted:
  - Census tracts (TIGERweb, keyless) with polygon geometry, centroid, land area
  - Demand per tract (Census ACS 5-year, keyless): population + median income
  - Competitors (Overture Maps Places on public S3 via DuckDB): cafes/coffee shops
  - Per-tract competition metrics: cafes within a trade radius, nearest-3 list

Scoring/weighting is deliberately NOT done here — the page does the weighted
score in JS so the weight sliders re-rank instantly with zero Python calls.

Every expensive step is disk-cached under ./.cache (fresh subprocess per call,
so the cache must live on disk). Warm loads are near-instant.
"""

import functools
import hashlib
import json
import math
import os
import sys
import tempfile

_HERE = (os.path.dirname(os.path.abspath(__file__))
         if "__file__" in globals() else os.path.abspath(sys.path[0]))


# True in a deployed executor. The backend injects OPENFUSED_DEPLOYED
# ("aws"/"fused") on the compute; locally it is unset. A runtime fact, not an
# import probe — `import openfused` also succeeds on the local built-in executor,
# so it can't tell local from hosted.
_HOSTED = bool(os.environ.get("OPENFUSED_DEPLOYED"))

# Hosted the bundle is read-only, so ./.cache next to the script isn't writable —
# cache into a per-run temp dir instead. Cross-call it won't persist (per-call
# subprocess isolation), but each hosted call recomputes inline within the larger
# serve budget; see _warm.
_CACHE_DIR = (
    os.path.join(tempfile.gettempdir(), "fr-store-site-selection-cache")
    if _HOSTED
    else os.path.join(_HERE, ".cache")
)

# Kept in step with the other Overture examples (locker/overture) on a release the
# public STAC catalog still serves — _cafes resolves place files through STAC, and
# only recent releases stay on that endpoint. The Places schema is stable across
# these releases, so the query below is unaffected.
OVERTURE_RELEASE = "2026-06-17.0"
ACS_URL = "https://api.census.gov/data/2022/acs/acs5"
TIGERWEB = ("https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
            "Tracts_Blocks/MapServer/0/query")

# city key -> (label, state FIPS, county FIPS)
CITIES = {
    "austin":      ("Austin, TX (Travis Co.)",       "48", "453"),
    "seattle":     ("Seattle, WA (King Co.)",         "53", "033"),
    "denver":      ("Denver, CO (Denver Co.)",        "08", "031"),
    "portland":    ("Portland, OR (Multnomah Co.)",   "41", "051"),
    "nashville":   ("Nashville, TN (Davidson Co.)",   "47", "037"),
    "minneapolis": ("Minneapolis, MN (Hennepin Co.)", "27", "053"),
}

RADIUS_KM = 1.6  # ~1 mile trade-area radius, same as the source canvas

_BRANDS = ["starbucks", "dutch bros", "summer moon", "coffee bean", "caffe medici",
           "caffé medici", "black rock", "houndstooth", "jo's", "epoch", "ruta maya",
           "dunkin", "scooter", "peet", "panera", "mcdonald", "tim horton", "allegro",
           "caribou", "biggby", "stumptown", "blue bottle", "philz", "la colombe"]


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


def _normalize_brand(name):
    import re
    if not name:
        return "(unknown)"
    s = re.sub(r"[#].*$", "", str(name)).strip()
    low = s.lower()
    for b in _BRANDS:
        if b in low:
            return b.title().replace("Mcdonald", "McDonald")
    return s


@disk_cache
def _tracts_geojson(state: str, county: str):
    """Census tracts w/ generalized polygons from TIGERweb (keyless REST)."""
    import requests
    features, offset = [], 0
    while True:
        js = requests.get(TIGERWEB, params={
            "where": f"STATE='{state}' AND COUNTY='{county}'",
            "outFields": "GEOID,CENTLAT,CENTLON,AREALAND",
            "returnGeometry": "true", "geometryPrecision": "4",
            "outSR": "4326", "f": "geojson", "resultOffset": str(offset),
        }, timeout=90, headers={"User-Agent": "fused-render-demo"}).json()
        batch = js.get("features", [])
        features.extend(batch)
        if not js.get("exceededTransferLimit") or not batch:
            break
        offset += len(batch)
    out = []
    for f in features:
        a = f["properties"]
        area = float(a["AREALAND"]) / 1e6
        if area <= 0:
            continue
        out.append({
            "geoid": a["GEOID"],
            "lat": float(a["CENTLAT"]), "lon": float(a["CENTLON"]),
            "area_km2": round(area, 3),
            "geometry": f["geometry"],
        })
    return out


def _census_key():
    """Census API key from CENSUS_API_KEY env var or a sibling .env file, or
    None if neither is set. Returning None (rather than raising) lets the page
    fall back to the bundled demo city instead of hard-erroring on open — see
    _bundled() and main()."""
    key = os.environ.get("CENSUS_API_KEY")
    if key:
        return key
    env_path = os.path.join(_HERE, ".env")
    if os.path.exists(env_path):
        for line in open(env_path, encoding="utf-8"):
            if line.strip().startswith("CENSUS_API_KEY="):
                return line.strip().split("=", 1)[1]
    return None


# Bundled, key-free demo data. A first-time user opening the example with no
# Census key still gets a live-looking dashboard for the default city, because
# its full _fetch_city payload (tracts + demand + cafes) ships in data/<city>.json.
# Regenerate with tests/../gen_austin.py-style scripts; see README.
_BUNDLE_DIR = os.path.join(_HERE, "data")
KEY_SIGNUP = "https://api.census.gov/data/key_signup.html"


def _bundled(city: str):
    """The committed _fetch_city payload for `city`, or None if not bundled."""
    path = os.path.join(_BUNDLE_DIR, f"{city}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


@disk_cache
def _acs_demand(state: str, county: str):
    """population + median household income per tract (ACS 5-year)."""
    import requests
    resp = requests.get(ACS_URL, params={
        "get": "B01001_001E,B19013_001E",
        "for": "tract:*", "in": f"state:{state} county:{county}",
        "key": _census_key(),
    }, timeout=90, headers={"User-Agent": "fused-render-demo"})
    resp.raise_for_status()
    rows = resp.json()
    hdr = rows[0]
    ip, ii = hdr.index("B01001_001E"), hdr.index("B19013_001E")
    istate, icounty, itract = hdr.index("state"), hdr.index("county"), hdr.index("tract")
    out = {}
    for r in rows[1:]:
        geoid = r[istate] + r[icounty] + r[itract]
        pop = float(r[ip]) if r[ip] is not None else 0.0
        inc = float(r[ii]) if r[ii] is not None else None
        if inc is not None and inc < 0:  # ACS suppression sentinels
            inc = None
        out[geoid] = [pop, inc]
    return out


def _s3_to_https(href: str) -> str:
    """Rewrite an ``s3://`` Overture href to its public HTTPS URL so DuckDB's HTTP
    reader fetches it UNSIGNED (anonymous). Reading ``s3://`` makes httpfs sign the
    request with whatever ambient AWS credentials exist — on a hosted Lambda those
    are the execution role, which the public Overture bucket rejects with HTTP 403.
    The bucket is us-west-2; the region-qualified host avoids a redirect."""
    if not href.startswith("s3://"):
        return href
    bucket, _, key = href[len("s3://"):].partition("/")
    # `=` in the key (theme=…/type=…) is percent-encoded, matching S3's own URL form.
    return f"https://{bucket}.s3.us-west-2.amazonaws.com/{key.replace('=', '%3D')}"


@disk_cache
def _cafes(west: float, south: float, east: float, north: float):
    """Coffee-shop POIs from the Overture Maps public S3 release via DuckDB."""
    import duckdb
    con = duckdb.connect()
    con.sql("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    # Resolve the place parquet files intersecting the bbox from the STAC catalog,
    # then read them over public HTTPS (unsigned). A direct s3:// glob makes DuckDB
    # sign the request with the Lambda's execution role, which the public bucket
    # 403s — and it also scanned every global place file; the STAC bbox prefilter
    # reads only the tiles this city needs.
    stac = f"https://stac.overturemaps.org/{OVERTURE_RELEASE}/collections.parquet"
    files = con.execute(
        f"SELECT assets.aws.alternate.s3.href h FROM '{stac}' "
        f"WHERE collection='place' "
        f"AND bbox.xmax >= {west} AND bbox.xmin <= {east} "
        f"AND bbox.ymax >= {south} AND bbox.ymin <= {north}"
    ).fetchdf()["h"].dropna().tolist()
    if not files:
        return []
    flist = ", ".join(f"'{_s3_to_https(f)}'" for f in files)
    df = con.sql(f"""
        SELECT names.primary AS name,
               bbox.xmin AS lon, bbox.ymin AS lat
        FROM read_parquet([{flist}])
        WHERE bbox.xmin BETWEEN {west} AND {east}
          AND bbox.ymin BETWEEN {south} AND {north}
          AND categories.primary IN ('coffee_shop', 'cafe')
    """).df()
    return [{"name": str(n) if n else "(unknown)",
             "brand": _normalize_brand(n),
             "lon": round(float(lo), 5), "lat": round(float(la), 5)}
            for n, lo, la in zip(df["name"], df["lon"], df["lat"])]


def _competition(tracts, cafes, radius_km):
    """Per-tract: cafes within radius of centroid + nearest 3 (vectorized)."""
    import numpy as np
    if not cafes:
        for t in tracts:
            t.update(comp_count=0, nearest_km=None, nearest=[])
        return
    clat = np.radians(np.array([c["lat"] for c in cafes]))
    clon = np.radians(np.array([c["lon"] for c in cafes]))
    for t in tracts:
        plat, plon = math.radians(t["lat"]), math.radians(t["lon"])
        dlat, dlon = clat - plat, clon - plon
        a = np.sin(dlat / 2) ** 2 + math.cos(plat) * np.cos(clat) * np.sin(dlon / 2) ** 2
        d = 6371.0 * 2 * np.arcsin(np.sqrt(a))
        t["comp_count"] = int((d <= radius_km).sum())
        idx = np.argsort(d)[:3]
        t["nearest_km"] = round(float(d[idx[0]]), 2)
        t["nearest"] = [{"name": cafes[i]["name"], "brand": cafes[i]["brand"],
                         "km": round(float(d[i]), 2)} for i in idx]


@disk_cache
def _fetch_city(city: str):
    """All network-bound data for a city: tracts + demand + competitor POIs."""
    label, state, county = CITIES[city]

    tracts = _tracts_geojson(state, county)
    demand = _acs_demand(state, county)

    lats = [t["lat"] for t in tracts]
    lons = [t["lon"] for t in tracts]
    pad = 0.03
    west, east = min(lons) - pad, max(lons) + pad
    south, north = min(lats) - pad, max(lats) + pad
    cafes = _cafes(round(west, 3), round(south, 3), round(east, 3), round(north, 3))

    for t in tracts:
        pop, inc = demand.get(t["geoid"], [0.0, None])
        t["population"] = pop
        t["median_income"] = inc
    print(f"{label}: {len(tracts)} tracts, {len(cafes)} cafes")
    return {"label": label, "tracts": tracts, "cafes": cafes}


# ------------------------------------------------------------ warm-up daemon
# A cold city fetch (TIGERweb pages + ACS + an Overture Places S3 scan) can
# blow the 30 s bridge budget, so step="warm" spawns a DETACHED process that
# fills the disk cache and the page polls until {"ready": True}.

def _warmer_paths(city: str):
    return (os.path.join(_CACHE_DIR, f"warm_{city}.pid"),
            os.path.join(_CACHE_DIR, f"warm_{city}.err"))


def _spawn_warmer(city: str):
    import subprocess

    lock, err = _warmer_paths(city)
    os.makedirs(_CACHE_DIR, exist_ok=True)
    code = (f"import sys; sys.path.insert(0, {_HERE!r}); "
            f"import site_data; site_data._fetch_city({city!r})")
    with open(err, "w", encoding="utf-8") as errfh:
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=errfh,
        )
    with open(lock, "w", encoding="utf-8") as fh:
        fh.write(str(proc.pid))


def _warm(city: str):
    if os.path.exists(_fetch_city.cache_path(city)):
        return {"ready": True}

    if _HOSTED:
        # No detached daemon or cross-call cache hosted (per-call subprocess
        # isolation, read-only bundle). Skip the background warmer and report
        # ready — the step="view" call runs _fetch_city inline within the larger
        # hosted budget (the local ~30s bridge is the only reason the daemon
        # exists).
        return {"ready": True}

    lock, err = _warmer_paths(city)
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

    _spawn_warmer(city)
    return {"ready": False}


def _needs_key_payload(city: str) -> dict:
    """Structured 'add a key' response — the page renders it as an inline setup
    prompt instead of a full-page traceback. Never raised."""
    return {
        "needsKey": True,
        "city": city, "label": CITIES[city][0],
        "cities": {k: v[0] for k, v in CITIES.items()},
        "keySignup": KEY_SIGNUP,
        "message": (
            f"{CITIES[city][0]} needs a free Census API key to load live. "
            f"Get one at {KEY_SIGNUP}, then set CENSUS_API_KEY — locally in a "
            f".env file next to this example, or as a hosted secret — and reopen."
        ),
    }


def main(city: str = "austin", radius_km: float = RADIUS_KM, step: str = "view") -> dict:
    if city not in CITIES:
        raise ValueError(f"city must be one of {sorted(CITIES)}, got {city!r}")

    key = _census_key()
    bundle = _bundled(city)

    if step == "warm":
        # Already warm, or keyless → nothing to warm (keyless, the view serves the
        # bundle or the add-a-key prompt rather than fetching). But with a key the
        # view does a live _fetch_city even for a bundled city, so the warmer must
        # still run on a cold cache — otherwise that fetch runs synchronously in
        # the view call and can blow the bridge timeout.
        if os.path.exists(_fetch_city.cache_path(city)) or not key:
            return {"ready": True}
        return _warm(city)

    # step == "view": live fetch when a key is available; otherwise fall back to
    # the bundled demo city, and only if neither exists ask the user for a key.
    if key:
        data = _fetch_city(city)
    elif bundle is not None:
        data = bundle
    else:
        return _needs_key_payload(city)

    tracts, cafes = data["tracts"], data["cafes"]
    _competition(tracts, cafes, float(radius_km))

    return {
        "city": city, "label": data["label"], "radius_km": float(radius_km),
        "cities": {k: v[0] for k, v in CITIES.items()},
        "tracts": tracts, "cafes": cafes,
        "cached": key is None,  # True => bundled demo (no live key); UI hints at it
    }
