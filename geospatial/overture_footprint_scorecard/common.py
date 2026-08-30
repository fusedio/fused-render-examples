"""Shared constants and helpers for the Overture vs Philadelphia example."""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".cache")

# Philadelphia city extent (covers every LI_BUILDING_FOOTPRINTS feature).
PHILLY_BOUNDS = (-75.280098, 39.867004, -74.955831, 40.137992)

# Overture releases scored against the city layer, oldest -> newest: roughly
# every other monthly drop, which spans a year and a half at half the download.
# All of these live on Fused's mirror of the Overture releases; the official
# Overture bucket only retains the two most recent monthly drops.
RELEASES = [
    "2024-12-18-0",
    "2025-03-19-1",
    "2025-05-21-0",
    "2025-09-24-0",
    "2025-11-19-0",
    "2026-01-21-0",
    "2026-03-18-0",
    "2026-04-15-0",
]

MIRROR = "https://data.source.coop/fused"
MIRROR_LIST = "https://data.source.coop/fused/?list-type=2"

PHILLY_GEOJSON_URL = (
    "https://hub.arcgis.com/api/v3/datasets/"
    "ab9e89e1273f445bb265846c90b38a96_0/downloads/data"
    "?format=geojson&spatialRefId=4326&where=1%3D1"
)

# IoU bands shared by the pipeline, stats and the UI.
BANDS = [
    ("excellent", 0.75),
    ("good", 0.50),
    ("fair", 0.25),
    ("weak", 1e-9),
]


def cache_path(*parts):
    os.makedirs(CACHE, exist_ok=True)
    return os.path.join(CACHE, *parts)


def release_key(release):
    return release.replace("-", "_")


def write_json_atomic(path, payload, best_effort=False):
    import time

    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    # On Windows os.replace fails while another process holds the target
    # open (the UI polls this file), so retry briefly.
    for attempt in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    try:
        os.replace(tmp, path)
    except PermissionError:
        if not best_effort:
            raise
        try:
            os.remove(tmp)
        except OSError:
            pass


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def connect_plain():
    """No extensions - enough to read the cached parquet and aggregate it."""
    import duckdb

    return duckdb.connect()


def _ensure(con, extension):
    # INSTALL unpacks into a shared extension dir; concurrent runs racing there
    # fail with "Could not move file". Loading first makes the install one-time.
    try:
        con.execute(f"LOAD {extension};")
    except Exception:
        con.execute(f"INSTALL {extension}; LOAD {extension};")


def connect_duckdb():
    con = connect_plain()
    _ensure(con, "spatial")
    return con


def connect_duckdb_remote():
    con = connect_duckdb()
    _ensure(con, "httpfs")
    return con
