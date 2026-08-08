"""Shared helpers for the parcel-locker impact simulator.

Not a runPython entry point (no main). Imported by tour_data.py / simulate.py /
suggest.py via a sys.path insert. Everything network-touching is disk-cached
under ./.cache so a fresh subprocess per call stays fast:

- Overture address pool + shop candidates  -> cached once per bbox
- OSRM /table duration+distance matrices   -> cached per coordinate chunk, so
  the depot+parcels base matrix is fetched once per seed and a new/moved
  locker only fetches its own rows/columns
- OSRM /route geometries                   -> cached per ordered point list
"""

import functools
import hashlib
import json
import math
import os
import random
import sys
import tempfile

import requests

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
# serve budget; see warm_via_daemon.
_CACHE_DIR = (
    os.path.join(tempfile.gettempdir(), "fr-locker-network-simulator-cache")
    if _HOSTED
    else os.path.join(_HERE, ".cache")
)

UA = {"User-Agent": "fused-render-locker-network-simulator/1.0"}
OSRM = "https://router.project-osrm.org"
OSRM_MAX_TABLE = 100  # verified empirically: 100 coords ok, 130 -> TooBig

# --- Scenario constants -----------------------------------------------------
DEPOT = {"lat": 52.3937, "lon": 4.8402, "name": "Northwind Depot Amsterdam-Westpoort"}
# Delivery area: Amsterdam Oud-West / De Baarsjes / Bos en Lommer
BBOX = (4.83, 52.355, 4.885, 52.385)  # xmin, ymin, xmax, ymax

OVERTURE_RELEASE = "2026-06-17.0"
_COLLECTIONS = f"https://stac.overturemaps.org/{OVERTURE_RELEASE}/collections.parquet"

# Service-time model (seconds)
STOP_SERVICE_S = 120          # one home stop (delivered or failed attempt)
LOCKER_BASE_S = 180           # pulling up + opening the locker
LOCKER_PER_PARCEL_S = 15      # dropping one parcel into a compartment

N_PARCELS = 120
FAIL_RATE = 0.08
EMAIL_RATE = 0.72


def disk_cache(fn):
    """Memoize a JSON-returning function to disk, keyed by its args."""

    def cache_path(*args, **kwargs):
        key_src = json.dumps([fn.__name__, args, kwargs], sort_keys=True, default=str)
        key = hashlib.sha256(key_src.encode()).hexdigest()[:20]
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
        os.replace(tmp, path)  # atomic
        return result

    wrapper.cache_path = cache_path
    return wrapper


def warm_via_daemon(tag: str, target_paths, code: str):
    """Poll-friendly warm-up for work that can outlive the 30 s bridge budget.

    Spawns a DETACHED process running `code` (which fills the disk cache) and
    returns {"ready": False} until every path in target_paths exists. The page
    polls this every couple of seconds.

    Hosted there is no daemon: a detached warmer can't outlive the call and its
    cache wouldn't survive per-call isolation, so this returns ready immediately
    and the caller's data step computes the same @disk_cache work inline.
    """
    import subprocess

    if all(os.path.exists(p) for p in target_paths):
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


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _parse_lockers(lockers: str):
    """Parse a "lat,lon;lat,lon" string into [{"lat","lon"}]. Lives here (not in
    simulate.py) so suggest.py can import it without importing a sibling entrypoint
    — a hosted bundle exposes _common (a bundled asset) but not the other run
    entrypoints as importable modules."""
    out = []
    for part in (lockers or "").split(";"):
        part = part.strip()
        if not part:
            continue
        lat, lon = part.split(",")
        out.append({"lat": round(float(lat), 5), "lon": round(float(lon), 5)})
    return out


# --- Overture data ----------------------------------------------------------

def _duck():
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("SET s3_region='us-west-2';")
    con.execute("SET http_timeout=30000;")
    return con


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
def address_pool(bbox=BBOX):
    """~3k real Amsterdam addresses in the delivery area (thinned by id hash)."""
    xmin, ymin, xmax, ymax = bbox
    con = _duck()
    files = (
        con.execute(
            f"SELECT assets.aws.alternate.s3.href h FROM '{_COLLECTIONS}' "
            f"WHERE collection='address' "
            f"AND bbox.xmax >= {xmin} AND bbox.xmin <= {xmax} "
            f"AND bbox.ymax >= {ymin} AND bbox.ymin <= {ymax}"
        )
        .fetchdf()["h"].dropna().tolist()
    )
    if not files:
        return []
    flist = ", ".join(f"'{_s3_to_https(f)}'" for f in files)
    df = con.execute(f"""
        SELECT ST_Y(geometry) lat, ST_X(geometry) lon, number, street
        FROM read_parquet([{flist}])
        WHERE bbox.xmin >= {xmin} AND bbox.xmax <= {xmax}
          AND bbox.ymin >= {ymin} AND bbox.ymax <= {ymax}
          AND hash(id) % 37 = 0
    """).df()
    con.close()
    out = []
    for r in df.itertuples():
        street = str(r.street or "").strip()
        num = str(r.number or "").strip()
        out.append({
            "lat": round(float(r.lat), 6),
            "lon": round(float(r.lon), 6),
            "addr": (street + " " + num).strip(),
        })
    out.sort(key=lambda a: (a["lat"], a["lon"], a["addr"]))  # deterministic pool
    return out


@disk_cache
def shop_candidates(bbox=BBOX):
    """Real shops/supermarkets in the area — candidate locker host sites."""
    xmin, ymin, xmax, ymax = bbox
    cats = ("supermarket", "convenience_store", "grocery_store", "grocery",
            "drugstore", "pharmacy", "gas_station")
    cat_sql = ", ".join(f"'{c}'" for c in cats)
    con = _duck()
    files = (
        con.execute(
            f"SELECT assets.aws.alternate.s3.href h FROM '{_COLLECTIONS}' "
            f"WHERE collection='place' "
            f"AND bbox.xmax >= {xmin} AND bbox.xmin <= {xmax} "
            f"AND bbox.ymax >= {ymin} AND bbox.ymin <= {ymax}"
        )
        .fetchdf()["h"].dropna().tolist()
    )
    if not files:
        return []
    flist = ", ".join(f"'{_s3_to_https(f)}'" for f in files)
    df = con.execute(f"""
        SELECT names.primary AS name, categories.primary AS category,
               ST_Y(geometry) lat, ST_X(geometry) lon
        FROM read_parquet([{flist}])
        WHERE bbox.xmin >= {xmin} AND bbox.xmax <= {xmax}
          AND bbox.ymin >= {ymin} AND bbox.ymax <= {ymax}
          AND categories.primary IN ({cat_sql})
    """).df()
    con.close()
    out = [
        {"lat": round(float(r.lat), 6), "lon": round(float(r.lon), 6),
         "name": str(r.name or "Shop"), "category": str(r.category or "")}
        for r in df.itertuples()
    ]
    out.sort(key=lambda s: (s["lat"], s["lon"], s["name"]))
    return out


# --- The day of parcels (seeded, reproducible) -------------------------------

def make_parcels(seed):
    pool = address_pool()
    if len(pool) < N_PARCELS:
        raise RuntimeError(f"Address pool too small ({len(pool)}) — Overture fetch failed?")
    rng = random.Random(int(seed))
    picked, seen = [], set()
    idxs = list(range(len(pool)))
    rng.shuffle(idxs)
    for i in idxs:
        a = pool[i]
        key = (round(a["lat"], 4), round(a["lon"], 4))  # ~11 m grid, avoids stacked markers
        if key in seen:
            continue
        seen.add(key)
        picked.append(a)
        if len(picked) == N_PARCELS:
            break
    parcels = []
    for i, a in enumerate(picked):
        w = round(min(20.0, max(0.2, rng.lognormvariate(0.6, 0.9))), 1)
        parcels.append({
            "id": f"P-{int(seed):02d}{i + 1:03d}",
            "lat": a["lat"], "lon": a["lon"], "addr": a["addr"],
            "status": "failed" if rng.random() < FAIL_RATE else "delivered",
            "has_email": rng.random() < EMAIL_RATE,
            "weight_kg": w,
        })
    return parcels


# --- OSRM -------------------------------------------------------------------

def _coord_str(pts):
    return ";".join(f"{p[1]:.6f},{p[0]:.6f}" for p in pts)  # OSRM wants lon,lat


@disk_cache
def _osrm_table_chunk(coords, sources, destinations):
    """One /table request. coords: [[lat,lon],...] (<=100). Returns dur+dist blocks."""
    url = (
        f"{OSRM}/table/v1/driving/{_coord_str(coords)}"
        f"?annotations=duration,distance"
        f"&sources={';'.join(map(str, sources))}"
        f"&destinations={';'.join(map(str, destinations))}"
    )
    resp = requests.get(url, headers=UA, timeout=25)
    if resp.status_code != 200:
        raise RuntimeError(f"OSRM table {resp.status_code}: {resp.text[:200]}")
    d = resp.json()
    if d.get("code") != "Ok":
        raise RuntimeError(f"OSRM table: {d.get('code')} {d.get('message', '')}")
    return {"durations": d["durations"], "distances": d["distances"]}


def full_matrix(points):
    """Full asymmetric duration+distance matrix for [[lat,lon],...] via chunked
    /table calls (public server caps at 100 coords/request). Each chunk is
    disk-cached, so a stable base point set is fetched once and reused."""
    n = len(points)
    dur = [[0.0] * n for _ in range(n)]
    dist = [[0.0] * n for _ in range(n)]
    block = OSRM_MAX_TABLE // 2  # source block + dest block unioned <= 100
    blocks = [list(range(i, min(i + block, n))) for i in range(0, n, block)]
    for bi in blocks:
        for bj in blocks:
            union = sorted(set(bi) | set(bj))
            coords = [[points[k][0], points[k][1]] for k in union]
            pos = {k: p for p, k in enumerate(union)}
            res = _osrm_table_chunk(coords, [pos[k] for k in bi], [pos[k] for k in bj])
            for a, i in enumerate(bi):
                for b, j in enumerate(bj):
                    d = res["durations"][a][b]
                    m = res["distances"][a][b]
                    dur[i][j] = float(d) if d is not None else 1e9
                    dist[i][j] = float(m) if m is not None else 1e9
    return dur, dist


@disk_cache
def route_geometry(points):
    """OSRM /route polyline + totals for an ordered [[lat,lon],...] list.
    Verified: ~350 waypoints ok on the public server (URL-length bound)."""
    url = (
        f"{OSRM}/route/v1/driving/{_coord_str(points)}"
        f"?overview=full&geometries=polyline&steps=false"
    )
    resp = requests.get(url, headers=UA, timeout=25)
    if resp.status_code != 200:
        raise RuntimeError(f"OSRM route {resp.status_code}: {resp.text[:200]}")
    d = resp.json()
    if d.get("code") != "Ok":
        raise RuntimeError(f"OSRM route: {d.get('code')}")
    r = d["routes"][0]
    return {"polyline": r["geometry"],
            "distance_m": float(r["distance"]), "duration_s": float(r["duration"])}


# --- TSP heuristics ----------------------------------------------------------

def tour_cost(order, mat):
    """Roundtrip cost depot(0) -> order... -> depot(0)."""
    c, prev = 0.0, 0
    for k in order:
        c += mat[prev][k]
        prev = k
    return c + mat[prev][0]


def nearest_neighbor(nodes, dur):
    order, left, cur = [], set(nodes), 0
    while left:
        nxt = min(left, key=lambda k: dur[cur][k])
        order.append(nxt)
        left.discard(nxt)
        cur = nxt
    return order


def two_opt(order, dur):
    """2-opt on a symmetrized copy of the (mildly asymmetric) city matrix."""
    n = len(order)
    if n < 4:
        return order
    sym = [[(dur[i][j] + dur[j][i]) / 2 for j in range(len(dur))] for i in range(len(dur))]
    seq = [0] + order + [0]
    improved = True
    while improved:
        improved = False
        for i in range(1, n):
            for j in range(i + 1, n + 1):
                a, b = seq[i - 1], seq[i]
                c, d = seq[j], seq[j + 1]
                if sym[a][b] + sym[c][d] - (sym[a][c] + sym[b][d]) > 1e-9:
                    seq[i:j + 1] = reversed(seq[i:j + 1])
                    improved = True
    return seq[1:-1]


def or_opt(order, dur):
    """Relocate segments of length 1-3 — exact delta on the true matrix."""
    seq = [0] + order + [0]
    improved = True
    while improved:
        improved = False
        for seg in (1, 2, 3):
            i = 1
            while i + seg - 1 < len(seq) - 1:
                a, b = seq[i - 1], seq[i]
                c, d = seq[i + seg - 1], seq[i + seg]
                removal = dur[a][b] + dur[c][d] - dur[a][d]
                chunk = seq[i:i + seg]
                rest = seq[:i] + seq[i + seg:]
                best_gain, best_pos = 1e-9, None
                for p in range(1, len(rest)):
                    x, y = rest[p - 1], rest[p]
                    add = dur[x][b] + dur[c][y] - dur[x][y]
                    if removal - add > best_gain:
                        best_gain, best_pos = removal - add, p
                if best_pos is not None:
                    seq = rest[:best_pos] + chunk + rest[best_pos:]
                    improved = True
                else:
                    i += 1
    return seq[1:-1]


def solve_tour(nodes, dur, init=None):
    """NN (or a given initial order) + 2-opt + or-opt. Deterministic."""
    if not nodes:
        return []
    order = list(init) if init else nearest_neighbor(nodes, dur)
    order = two_opt(order, dur)
    order = or_opt(order, dur)
    return order


def cheapest_insertion(order, node, dur):
    best, pos = None, 0
    seq = [0] + order + [0]
    for p in range(1, len(seq)):
        a, b = seq[p - 1], seq[p]
        add = dur[a][node] + dur[node][b] - dur[a][b]
        if best is None or add < best:
            best, pos = add, p - 1
    out = list(order)
    out.insert(pos, node)
    return out
